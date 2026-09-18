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
"""
import csv
import io
import json
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


def _fold_map(header_map):
    """Fold a caller's column mapping the same way headers are folded.

    A mapping is written by a person naming the columns of *their* instrument,
    so it gets the same tolerance a header gets: ``Distance (mm)`` and
    ``distance_mm`` are one declaration. Targets are folded too, which is
    harmless when they are already contract keys and helpful when someone
    writes the template's own spelling instead.

    Returns ``{}`` for ``None``, so a caller with no mapping — the CSV import
    wizard, every existing test — takes exactly the path it took before.
    """
    if not header_map:
        return {}
    return {normalise_key(source): normalise_key(target)
            for source, target in header_map.items() if source}


def _apply(folded, raw_key) -> str:
    """The contract key a source column maps to, or itself when unmapped."""
    key = normalise_key(raw_key)
    return folded.get(key, key)


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


# ----------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------

def _read_csv(content: bytes, folded=None):
    folded = folded or {}
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportReadError(
            'The file is not UTF-8 text. Re-save it as "CSV UTF-8" and upload '
            'it again — a spreadsheet saved as a plain CSV is written in the '
            'machine\'s local encoding, and the values cannot be read reliably '
            'from it.'
        )

    reader = csv.DictReader(io.StringIO(text, newline=''))
    if not reader.fieldnames:
        raise ImportReadError(
            'The file has no header row. The first line must name the columns '
            '— download the template for this record type to see them.')

    headers = {}
    for raw_header in reader.fieldnames:
        if raw_header is None or not str(raw_header).strip():
            continue
        key = _apply(folded, raw_header)
        if key in headers:
            raise ImportReadError(
                f'The file has two columns that both mean "{key}" '
                f'({headers[key]!r} and {raw_header!r}). Rename one of them — '
                'the platform cannot tell which value is the real one.'
            )
        headers[key] = raw_header

    rows = []
    skipped = 0
    for record in reader:
        # `line_num` is the physical line the reader stopped on, so a quoted
        # field containing a newline does not shift the numbering.
        data = {}
        for key, raw_header in headers.items():
            value = record.get(raw_header)
            if isinstance(value, str):
                value = value.strip()
            data[key] = value
        if all(_blank(value) for value in data.values()):
            skipped += 1
            continue
        rows.append(SourceRow(row_number=reader.line_num, data=data))
    return rows, skipped


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------

def _read_json(content: bytes, folded=None):
    folded = folded or {}
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportReadError('The file is not UTF-8 text, so it is not JSON.')

    try:
        parsed = json.loads(text)
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

    rows = []
    skipped = 0
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            # Numbered by position in the array, which for JSON *is* the row.
            rows.append(SourceRow(row_number=index, data={'_not_an_object': item}))
            continue
        data = {_apply(folded, key): value for key, value in item.items()}
        if all(_blank(value) for value in data.values()):
            skipped += 1
            continue
        rows.append(SourceRow(row_number=index, data=data))
    return rows, skipped


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())
