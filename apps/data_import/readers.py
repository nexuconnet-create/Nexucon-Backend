"""
Reading an uploaded file into rows.

This module does one thing: turn bytes into a list of ``SourceRow`` where each
row carries the values the file actually held and the line it came from. It
interprets nothing. Deciding that "42.1" is a transit time is the registry's
job, and keeping the two apart is what makes ``ImportRecord.raw_data``
trustworthy — it is written from this layer, before any interpretation happened.

Three rules the readers enforce:

**The bytes decide the format.** Not the filename, and not the client's claim.
An export saved with the wrong extension still parses; a file whose content
contradicts its name is reported rather than guessed at. This is the same rule
the calibration upload already follows.

**Keys are matched, not spelled.** ``STRUCTURAL ELEMENT``, ``Structural Element``
and ``structural_element`` are one key. A file is produced by a person in a
spreadsheet, and rejecting it over capitalisation would be pedantry with a
support cost.

**A row number is a physical line.** ``csv`` reports the real line it read, so
a quoted field containing a newline does not shift every subsequent row number
by one — the number in the error is the number the inspector can open the file
and look at.

**Renaming a column is declared, never inferred.** A caller may pass a
``header_map`` naming which of its instrument's columns mean which contract
keys. Columns it does not name keep their own names and are refused for being
unknown, which is the point: the platform must be able to say *why* it did not
read a column, and "we guessed" is not an answer that belongs on a record.

**Declining a column is declared too.** ``{"Velocity": null}`` says the
platform was told about this column and told not to read it, and the value is
dropped rather than passed on. Without it there would be no way to express the
difference between a column nobody has accounted for — which must be refused,
because it might be a measurement — and one a person looked at and set aside.
That difference is the whole reason an instrument's own export can be accepted
at all: a PL-200 writes six columns and the contract reads two, so a mapping
that could only rename would leave the other four refusing the file forever.

**A declaration may also carry a unit.** ``{"Distance": {"to":
"path_length_l_mm", "scale": 1000}}`` says the column holds millimetres
expressed in metres, and every value in it is multiplied on the way in. This
is the one place the module does more than move text, and it earns its place
here rather than in the registry: a rename and a conversion are the same kind
of statement, made by the same person about the same column at the same
moment, and splitting them across two modules is how they would come to
disagree. It is also safe to keep here because ``header_map`` has exactly one
caller — the telemetry file import — and the CSV wizard passes ``None``. So
``ImportRecord.raw_data``, which is written from this layer and is the
platform's record of what a file actually said, is never scaled: the wizard
takes the path it always took.

A scale that is absent means 1. A scale that is present and unusable is
refused outright rather than treated as 1 — a multiplier silently dropped is a
column of numbers wrong by three orders of magnitude with nothing on the
record to show it.
"""
import csv
import io
import json
import math
import re
from dataclasses import dataclass

#: Enough of a PDF's header to recognise one. Checked before anything else,
#: because a PDF's first printable bytes are not text a CSV reader survives.
PDF_MAGIC = b'%PDF'

#: Keys are folded to this form on both sides, so a header written as
#: "Path Length L (mm)" and a JSON key written as "path_length_mm" agree.
_KEY_FOLD = re.compile(r'[^a-z0-9]+')


class ImportReadError(Exception):
    """The file could not be read. The message is shown to the uploader."""


@dataclass(frozen=True)
class SourceRow:
    """One row of the source file, exactly as it was written."""

    row_number: int
    data: dict


def normalise_key(key) -> str:
    """``'Path Length L (mm)'`` → ``'path_length_l_mm'``."""
    return _KEY_FOLD.sub('_', str(key).strip().lower()).strip('_')


def _declared_scale(value, source):
    """A mapping's multiplier, refusing one that would quietly corrupt a row.

    Absent is 1 — the overwhelmingly common case, and the only one an older
    mapping can be in. Present-but-unusable is an error rather than a 1: a
    multiplier the platform could not read and then ignored would leave every
    value in that column wrong by a factor nobody wrote down anywhere.
    """
    if value is None or value == '':
        return 1.0
    try:
        scale = float(value)
    except (TypeError, ValueError):
        raise ImportReadError(
            f'The column mapping for "{source}" gives a scale of {value!r}, '
            'which is not a number. A scale the platform cannot read is '
            'refused rather than ignored, because ignoring it would write '
            'every value in that column unconverted.')
    if not math.isfinite(scale) or scale <= 0:
        raise ImportReadError(
            f'The column mapping for "{source}" gives a scale of {scale}, '
            'which cannot convert anything. Use a positive number, or leave '
            'the scale out to record the column exactly as the file writes it.')
    return scale


def _fold_map(header_map):
    """Fold a caller's column mapping the same way headers are folded.

    A mapping is written by a person naming the columns of *their* instrument,
    so it gets the same tolerance a header gets: ``Distance (mm)`` and
    ``distance_mm`` are one declaration. Targets are folded too, which is
    harmless when they are already contract keys and helpful when someone
    writes the template's own spelling instead.

    A value is one of three things, and all three fold to a ``(key, scale)``
    pair:

    * the target key on its own — ``'transit_time_t_us'``;
    * an object carrying the key with the scale that gets from the instrument's
      unit to the contract's — ``{'to': 'path_length_l_mm', 'scale': 1000}``;
    * ``None``, meaning the column was accounted for and is deliberately not
      read. It folds to ``(None, 1.0)``, and the reader drops the column.

    Returns ``{}`` for ``None``, so a caller with no mapping — the CSV import
    wizard, every existing test — takes exactly the path it took before.
    """
    if not header_map:
        return {}
    folded = {}
    for source, target in header_map.items():
        if not source:
            continue
        if target is None:
            key, scale = None, 1.0
        elif isinstance(target, dict):
            key = normalise_key(target.get('to'))
            scale = _declared_scale(target.get('scale'), source)
        else:
            key = normalise_key(target)
            scale = 1.0
        folded[normalise_key(source)] = (key, scale)
    return folded


def _apply(folded, raw_key):
    """The contract key a source column maps to, and its scale.

    Three answers, and the difference between the last two is the whole reason
    this returns a pair rather than a key:

    * a contract key, with whatever scale was declared for it;
    * ``None`` — declared and deliberately not read, so the caller drops it;
    * the column's own folded name, with no scale, which is what an *unmapped*
      column gets and is still refused later for being something the registry
      does not know. Unmapped is not the same as declined, and collapsing the
      two would let an unaccounted column through.
    """
    key = normalise_key(raw_key)
    return folded.get(key, (key, 1.0))


def _scaled(value, scale):
    """A declared unit conversion, applied only to a value that is a number.

    A scaled column that does not hold numbers is left exactly as it was. The
    alternative — coercing the text, or raising — would either invent a value
    or fail an import over a declaration that had no effect on any row.
    """
    if scale == 1.0 or _blank(value):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value * scale
    try:
        return float(str(value).strip()) * scale
    except (TypeError, ValueError):
        return value


def detect_import_type(content: bytes) -> str:
    """What this file *is*, from its bytes.

    PDF first: its magic number is unambiguous and its content is not text a
    CSV or JSON reader can survive. Then JSON, which announces itself with a
    leading brace or bracket. Everything else is CSV, because there is no
    other format left for it to be — and a CSV reader given garbage produces a
    readable error naming the row, whereas a wrong guess produces a confusing
    one.
    """
    if content[:4] == PDF_MAGIC:
        return 'PDF'
    head = content.lstrip()[:1]
    if head in (b'{', b'['):
        return 'JSON'
    return 'CSV'


def read_rows(content: bytes, import_type: str, header_map=None):
    """Parse a file into ``(rows, skipped_blank_rows)``.

    Raises ``ImportReadError`` when the file cannot be read at all. A *row*
    that cannot be read is not an error here — it becomes a row whose values
    fail validation later, so the inspector gets a line number and a reason
    instead of a rejection with no location.

    ``header_map`` optionally renames the file's own columns to the platform's
    contract keys before they are folded — ``{'Distance (mm)':
    'path_length_l_mm'}``. It is a *declaration* made by whoever knows the
    instrument, not a guess: a column the caller did not name still arrives
    under its own name and is refused by the registry for being unknown. That
    distinction is the whole reason this is a parameter rather than a fuzzy
    header matcher — a mapping that silently accepted anything would put an
    unverified number into a statutory registry.

    A mapping may also convert, by naming the scale from the instrument's unit
    to the contract's — ``{'Distance': {'to': 'path_length_l_mm', 'scale':
    1000}}``. The conversion is applied here, to the values as they are read,
    so everything downstream sees the contract's unit and no caller has to
    remember which columns arrived converted.

    And it may decline, by naming a column with ``None``. The column is dropped
    rather than passed on, which is how an instrument that writes six columns
    gets read by a contract that wants two without the other four refusing the
    file. Declining is a declaration like any other: it says a person saw the
    column and set it aside, which is not the same as a column nobody named.
    """
    folded = _fold_map(header_map)
    if import_type == 'PDF':
        raise ImportReadError(
            'A PDF cannot be validated for import. A PDF has no column contract '
            '— the platform would have to guess which printed number is a '
            'transit time and which is a path length, and a misread column '
            'would put a wrong value into a statutory registry with nothing to '
            'show it happened. Export the same data as CSV or JSON, or have the '
            'PDF\'s column layout agreed with Nexucon first.'
        )
    if import_type == 'JSON':
        return _read_json(content, folded)
    if import_type == 'CSV':
        return _read_csv(content, folded)
    raise ImportReadError(f'"{import_type}" is not an importable file type.')


def read_headers(content: bytes, import_type: str, sample_limit: int = 5):
    """The file's own column names, verbatim, with a few sample values.

    Returns ``(headers, samples)``: ``headers`` is the header text exactly as
    the file spells it — not folded, not renamed — and ``samples`` maps each
    header to up to ``sample_limit`` of its non-blank values, as written.

    ``read_rows`` cannot answer this. It keys every row by the *contract* key a
    column resolves to, so by the time a caller sees a row the instrument's own
    spelling is gone. A person recording a mapping has to recognise the words
    their instrument wrote, and a mapping keyed by text the file does not
    literally contain is one nobody can check — so those words are what this
    returns.

    The samples exist for the one judgement a header cannot support: whether a
    column of numbers is in the unit its name claims. ``Distance`` holding
    ``0.100`` is not a path length in millimetres, and nothing in the header
    says so.

    Nothing is interpreted and nothing is written. This reads a file in order
    to describe it, and the caller decides what, if anything, to do about it.
    """
    if import_type == 'PDF':
        raise ImportReadError(
            'A PDF cannot be read for its columns — it has no column contract, '
            'so the platform would have to guess which printed number is which '
            'field. Export the same data as CSV or JSON.')
    if import_type == 'CSV':
        return _headers_from_csv(content, sample_limit)
    if import_type == 'JSON':
        return _headers_from_json(content, sample_limit)
    raise ImportReadError(f'"{import_type}" is not an importable file type.')


# ----------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------

def _decode(content: bytes, complaint: str) -> str:
    """The file's text, or the caller's own refusal.

    The complaint differs by format on purpose — "re-save it as CSV UTF-8" is
    the right advice for a spreadsheet export and nonsense for a JSON one — so
    it is passed in rather than composed here.
    """
    try:
        return content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportReadError(complaint)


#: What a spreadsheet saved in the machine's local encoding looks like when it
#: is read as UTF-8, and the one fix that works.
_NOT_UTF8_CSV = (
    'The file is not UTF-8 text. Re-save it as "CSV UTF-8" and upload it '
    'again — a spreadsheet saved as a plain CSV is written in the machine\'s '
    'local encoding, and the values cannot be read reliably from it.'
)

_NOT_UTF8_JSON = 'The file is not UTF-8 text, so it is not JSON.'


def _csv_reader(content: bytes) -> csv.DictReader:
    """A ``DictReader`` over the file, refusing one with no header row.

    Shared by ``_read_csv`` and ``read_headers`` so that "what this file's
    header row is" has one definition. Two definitions would be two answers to
    a question a person is about to write a mapping against.
    """
    reader = csv.DictReader(io.StringIO(_decode(content, _NOT_UTF8_CSV),
                                        newline=''))
    if not reader.fieldnames:
        raise ImportReadError(
            'The file has no header row. The first line must name the columns '
            '— download the template for this record type to see them.')
    return reader


def _read_csv(content: bytes, folded=None):
    folded = folded or {}
    reader = _csv_reader(content)

    # ``(contract key, the header as the file spells it, scale)`` — the scale
    # is carried alongside rather than looked up again per row, and the header
    # is kept because ``record.get`` needs the file's own spelling, not the key.
    # A column the mapping declined is not collected at all, so nothing about it
    # reaches a row and the unknown-column guard has nothing to complain about.
    columns = []
    headers = {}
    for raw_header in reader.fieldnames:
        if raw_header is None or not str(raw_header).strip():
            continue
        key, scale = _apply(folded, raw_header)
        if key is None:
            continue
        if key in headers:
            raise ImportReadError(
                f'The file has two columns that both mean "{key}" '
                f'({headers[key]!r} and {raw_header!r}). Rename one of them — '
                'the platform cannot tell which value is the real one.'
            )
        headers[key] = raw_header
        columns.append((key, raw_header, scale))

    rows = []
    skipped = 0
    for record in reader:
        # `line_num` is the physical line the reader stopped on, so a quoted
        # field containing a newline does not shift the numbering.
        data = {}
        for key, raw_header, scale in columns:
            value = record.get(raw_header)
            if isinstance(value, str):
                value = value.strip()
            data[key] = _scaled(value, scale)
        if all(_blank(value) for value in data.values()):
            skipped += 1
            continue
        rows.append(SourceRow(row_number=reader.line_num, data=data))
    return rows, skipped


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------

def _json_records(content: bytes) -> list:
    """The file's list of records, unwrapping the one wrapper shape allowed.

    Shared by ``_read_json`` and ``read_headers``, so a file that reads as a
    list of records does so for both — a suggester that saw columns in a file
    the importer then refused to open would be worse than no suggester.
    """
    try:
        parsed = json.loads(_decode(content, _NOT_UTF8_JSON))
    except json.JSONDecodeError as exc:
        raise ImportReadError(
            f'The file is not valid JSON: {exc.msg} (line {exc.lineno}, '
            f'column {exc.colno}).'
        )

    if isinstance(parsed, dict):
        # A wrapper object is accepted because exports of a paginated API come
        # back that way. The first list-valued key among the known names wins;
        # anything else is a file whose shape is not known, and guessing would
        # mean importing the wrong array.
        for name in ('records', 'rows', 'data', 'items'):
            if isinstance(parsed.get(name), list):
                parsed = parsed[name]
                break
        else:
            raise ImportReadError(
                'The JSON file must be a list of records, or an object with a '
                '"records" list in it. This file is an object with keys '
                f'{", ".join(sorted(parsed)[:8]) or "none"}.'
            )

    if not isinstance(parsed, list):
        raise ImportReadError(
            'The JSON file must be a list of records. This file is a '
            f'{type(parsed).__name__}.')
    return parsed


def _read_json(content: bytes, folded=None):
    folded = folded or {}
    parsed = _json_records(content)

    rows = []
    skipped = 0
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            # Numbered by position in the array, which for JSON *is* the row.
            rows.append(SourceRow(row_number=index, data={'_not_an_object': item}))
            continue
        data = {}
        for key, value in item.items():
            mapped, scale = _apply(folded, key)
            if mapped is None:
                continue
            data[mapped] = _scaled(value, scale)
        if all(_blank(value) for value in data.values()):
            skipped += 1
            continue
        rows.append(SourceRow(row_number=index, data=data))
    return rows, skipped


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


# ----------------------------------------------------------------------
# Header description — what a file's columns are called, and what they hold
# ----------------------------------------------------------------------

def _headers_from_csv(content: bytes, sample_limit: int):
    reader = _csv_reader(content)
    headers = [header for header in reader.fieldnames
               if header is not None and str(header).strip()]
    samples = {header: [] for header in headers}
    for record in reader:
        for header in headers:
            if len(samples[header]) >= sample_limit:
                continue
            value = record.get(header)
            if isinstance(value, str):
                value = value.strip()
            if _blank(value):
                continue
            samples[header].append(str(value))
        if all(len(values) >= sample_limit for values in samples.values()):
            break
    return headers, samples


def _headers_from_json(content: bytes, sample_limit: int):
    """Headers in first-seen order, so the list reads like the file does.

    A later record may carry a key an earlier one did not — a sparse export is
    still a file whose columns the operator has to account for, so the union is
    taken rather than only the first record's keys.
    """
    headers = []
    samples = {}
    for item in _json_records(content):
        if not isinstance(item, dict):
            continue
        for key in item:
            if key not in samples:
                headers.append(key)
                samples[key] = []
            if len(samples[key]) >= sample_limit:
                continue
            value = item.get(key)
            if _blank(value):
                continue
            samples[key].append(str(value))
    return headers, samples
