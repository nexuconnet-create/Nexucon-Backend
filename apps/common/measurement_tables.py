"""Rewrite measurement-heavy narrative as markdown tables.

The platform's AI interpretation layer is asked for tables and sometimes
answers in prose instead — one structural element per clause:

    Ground Floor element-by-element: S20 4,294.92 m/s, 46.2 MPa, spread
    3.4%, good; S19 4,335.56 m/s, 47.8 MPa, spread 7.8%, good; ...

Twenty elements read that way are unreadable in a report and impossible to
check against the recorded readings. This module rewrites such a run into a
markdown table **copying every figure from the sentence it replaces** — the
numbers are never recomputed, rounded, re-derived or invented. Text that is
not a measurement is kept as its own paragraph, in its original order, so
nothing the analysis said is lost. A run shorter than
``MIN_ROWS_PER_TABLE`` measurements is left as prose: a two-element list is
not made more readable by a table.

Two callers use it: the certified PDF/§5.3 renderer
(``apps/reports/ndt_reports.py``) and the API that feeds the AI-analysis
screens (``apps/evidence/serializers.py``).
"""
import re
from collections import Counter

# A list shorter than this stays prose. Four is the point at which a
# sentence stops being readable and starts being a table.
MIN_ROWS_PER_TABLE = 4

# Element token then a pulse velocity in m/s. The token is deliberately
# loose (records carry names like 'COL-A1', 'S20', 'BEAM-B2' and IFC-ish
# 'Floor:200THK RC SLAB:780904' — the last matches on its tail segment,
# which still carries the digits); `_is_element_token` is the strict gate.
_MEASUREMENT_RE = re.compile(
    r'(?P<element>[A-Za-z][A-Za-z0-9_./\-]{0,47})'
    r'[ \t]*[\(\[]?[ \t]*'
    r'(?P<velocity>\d[\d,]*(?:\.\d+)?)[ \t]*m[ \t]*/[ \t]*s',
    re.IGNORECASE,
)

_STRENGTH_RE = re.compile(
    r'(\d[\d,]*(?:\.\d+)?)[ \t]*(?:N[ \t]*/[ \t]*mm\s*2|N[ \t]*/[ \t]*mm²|MPa)',
    re.IGNORECASE,
)
_SPREAD_RE = re.compile(
    r'spread[^0-9%]{0,12}(\d+(?:\.\d+)?)[ \t]*%', re.IGNORECASE)
_GRADE_RE = re.compile(
    r'\b(excellent|very good|good|medium|poor|doubtful|unverified|pending)\b',
    re.IGNORECASE,
)

# Sentence boundary: the point where a measurement clause ends and the
# writer's prose (usually the next group's heading) resumes. A decimal point
# never carries a trailing space, so '. ' is unambiguous here.
_SENTENCE_RE = re.compile(r'\.\s+')

# Where one prose sentence ends and the next begins, for laying the
# writer's own words out as separate paragraphs rather than one wall.
_PROSE_SENTENCE_RE = re.compile(r'(?<=\.)\s+(?=[A-Z“"(\[])')

# Writer's connectives a split sentence can begin with.
_LEAD_NOISE_RE = re.compile(
    r'^[\s,;:.—-]*(?:and|with|while|whereas|however|also|plus)\b[\s,;:.—-]*',
    re.IGNORECASE)

# An existing markdown table must pass through untouched.
_MD_TABLE_ROW_RE = re.compile(r'^\s*\|.*\|\s*$')
_MD_TABLE_SEP_RE = re.compile(r'^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$')

# Words that look like an element token but are ordinary English. Only used
# when the caller cannot supply the project's recorded element names.
_NOT_AN_ELEMENT = {
    'velocity', 'velocities', 'speed', 'speeds', 'mean', 'average', 'avg',
    'median', 'value', 'values', 'reading', 'readings', 'point', 'points',
    'element', 'elements', 'member', 'members', 'slab', 'slabs', 'beam',
    'beams', 'column', 'columns', 'wall', 'walls', 'footing', 'footings',
    'spread', 'strength', 'using', 'with', 'recorded', 'measured', 'tested',
    'between', 'range', 'from', 'each', 'total', 'minimum', 'maximum', 'min',
    'max', 'over', 'under', 'above', 'below', 'and', 'the', 'per', 'grade',
    'condition', 'section', 'level', 'floor', 'floors', 'grid',
    'location', 'notes', 'note',
}

_COLUMNS = (
    ('element', 'STRUCTURAL ELEMENT'),
    ('velocity', 'MEAN PULSE VELOCITY (M/S)'),
    ('strength', 'EST. COMPRESSIVE STRENGTH (N/MM2)'),
    ('spread', 'POINT SPREAD (%)'),
    ('grade', 'GRADE'),
)


def _is_element_token(token, known_elements=None):
    """True when `token` can be trusted to name a structural element."""
    token = (token or '').strip()
    if not token:
        return False
    if known_elements is not None:
        return token.lower() in known_elements
    # Without the project's element list, demand an identifier shape: a
    # digit is present and the word is not ordinary English.
    if not any(ch.isdigit() for ch in token):
        return False
    stripped = token.strip('.,;:()[]').lower()
    if stripped in _NOT_AN_ELEMENT:
        return False
    # A bare number ("2273.53 m/s") is a reading, not an element.
    if re.fullmatch(r'[\d.,]+', stripped):
        return False
    return True


def _display_element(token):
    """S20 → S20, col-a1 → COL-A1; long IFC-ish names keep their case."""
    token = token.strip().strip('.,;:')
    if len(token) <= 12 and ' ' not in token:
        return token.upper()
    return token


def has_markdown_table(text):
    """True when `text` already carries a markdown table."""
    lines = [ln for ln in str(text).splitlines() if ln.strip()]
    for i, line in enumerate(lines[:-1]):
        if _MD_TABLE_ROW_RE.match(line) and _MD_TABLE_SEP_RE.match(lines[i + 1]):
            return True
    return False


def _cells_from_tail(tail):
    """Read the companion figures out of one measurement's tail text."""
    strength = _STRENGTH_RE.search(tail)
    spread = _SPREAD_RE.search(tail)
    grade = _GRADE_RE.search(tail)
    return {
        'strength': strength.group(1).strip() if strength else '',
        'spread': spread.group(1).strip() if spread else '',
        'grade': grade.group(1).strip().lower() if grade else '',
    }


def _table_markdown(rows):
    """rows: list of dicts keyed by _COLUMNS' keys."""
    columns = [key for key, _ in _COLUMNS
               if any(row.get(key) for row in rows)]
    if len(columns) < 3:
        # Element + one figure is not a table worth its own heading.
        return None
    headers = [label for key, label in _COLUMNS if key in columns]
    lines = ['| ' + ' | '.join(headers) + ' |',
             '| ' + ' | '.join('---' for _ in headers) + ' |']
    for row in rows:
        lines.append('| ' + ' | '.join(str(row.get(key, '') or '-')
                                        for key in columns) + ' |')
    return '\n'.join(lines)


def _blocks(text, known_elements=None):
    """Split `text` into ('text' | 'table', body) blocks, in order.

    A run of measurements long enough to read as a table becomes one. A
    shorter run is emitted as the original words the writer used — sliced
    straight out of `text`, never reconstructed, so no figure and no
    qualifier is lost or reworded.
    """
    # A match that does not name a recorded element is prose — a bare
    # reading, or a heading like 'mean V 4,250 m/s'. Drop it up front so
    # the text around it stays whole inside the gaps between real rows.
    matches = [match for match in _MEASUREMENT_RE.finditer(text)
               if _is_element_token(match.group('element'), known_elements)]
    if len(matches) < MIN_ROWS_PER_TABLE:
        return None

    blocks = []
    current = []            # rows accumulated for the table being built

    def flush(pending_text=None):
        if current:
            markdown = (_table_markdown(current)
                        if len(current) >= MIN_ROWS_PER_TABLE else None)
            if markdown:
                blocks.append(('table', markdown))
            else:
                original = text[current[0]['_start']:current[-1]['_end']]
                if original.strip():
                    blocks.append(('text', original.strip()))
            current.clear()
        if pending_text:
            for paragraph in _prose_paragraphs(pending_text):
                blocks.append(('text', paragraph))

    lead = text[:matches[0].start()].strip()
    if lead:
        blocks.append(('text', _strip_noise(lead)))

    for i, match in enumerate(matches):
        gap_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        gap = text[match.end():gap_end]

        # The row's own figures stop where the next sentence starts; the
        # rest of the gap is prose (usually the next group's heading) and
        # is emitted as its own paragraph so it keeps its position.
        parts = _SENTENCE_RE.split(gap, maxsplit=1)
        tail = parts[0]
        overflow = _strip_noise(parts[1]) if len(parts) > 1 else ''

        token = match.group('element')
        row = {
            'element': _display_element(token),
            'velocity': match.group('velocity').strip(),
            '_start': match.start(),
            '_end': match.end() + len(tail),
        }
        row.update(_cells_from_tail(tail))
        current.append(row)

        if overflow:
            flush(pending_text=overflow)

    flush()
    return blocks or None


def _strip_noise(text):
    return _LEAD_NOISE_RE.sub('', text).strip()


def _numbers(text):
    """Every figure in `text`, comma-grouping removed, as a multiset."""
    return Counter(re.sub(r',', '', found)
                   for found in re.findall(r'\d[\d,]*(?:\.\d+)?', str(text)))


def _prose_paragraphs(text):
    """The writer's own sentences, one paragraph each."""
    text = _strip_noise(text)
    if not text:
        return []
    return [part.strip() for part in _PROSE_SENTENCE_RE.split(text)
            if part.strip()]


def tabulate_measurements(text, known_elements=None):
    """Return `text` with measurement runs rewritten as markdown tables.

    `known_elements`, when given, is the set of lower-cased element names the
    project's recorded tests actually carry. A run containing any name
    outside that set is left exactly as written — the writer tabulates only
    what it can verify, and never drops a figure it could not place.
    """
    text = '' if text is None else str(text)
    if not text.strip() or has_markdown_table(text):
        return text

    if known_elements is not None:
        known_elements = {str(e).strip().lower() for e in known_elements
                          if str(e).strip()}
        for match in _MEASUREMENT_RE.finditer(text):
            token = match.group('element').strip().lower()
            if token not in known_elements:
                # One unplaceable name and the whole run stays prose: the
                # returned text must carry every figure it arrived with.
                return text

    blocks = _blocks(text, known_elements)
    if not blocks:
        return text
    if not any(kind == 'table' for kind, _ in blocks):
        return text

    result = '\n\n'.join(body for _, body in blocks if body)
    # Every figure the writer gave must still be here. The table renders the
    # measurements as cells, so the comparison skips each table's header line
    # (whose units carry a digit) and counts only what was rewritten.
    rewritten = '\n\n'.join(
        '\n'.join(body.splitlines()[1:]) if kind == 'table' else body
        for kind, body in blocks if body)
    if _numbers(rewritten) != _numbers(text):
        return text
    return result


def normalize_observations(observations, known_elements=None):
    """Apply `tabulate_measurements` to a stored observations list."""
    if not isinstance(observations, (list, tuple)):
        return observations
    return [tabulate_measurements(obs, known_elements=known_elements)
            for obs in observations]


def _split_table_row(line):
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def markdown_blocks(text):
    """Split markdown `text` into ('text', body) and ('table', (headers, rows)).

    Every renderer that shows an observation — the certified PDF today, any
    later one — reads the same parse, so a table in the narrative becomes a
    real table everywhere rather than pipes in a paragraph.
    """
    blocks, text_lines, table_lines = [], [], []

    def flush_text():
        body = '\n'.join(text_lines).strip()
        text_lines.clear()
        if body:
            blocks.append(('text', body))

    def flush_table():
        if not table_lines:
            return
        parsed = [_split_table_row(line) for line in table_lines
                  if not _MD_TABLE_SEP_RE.match(line)]
        table_lines.clear()
        if len(parsed) >= 2:
            headers = parsed[0]
            rows = [(row + [''] * len(headers))[:len(headers)]
                    for row in parsed[1:]]
            blocks.append(('table', (headers, rows)))
        elif parsed:
            blocks.append(('text', ' '.join(parsed[0])))

    for line in str(text).splitlines():
        if line.strip().startswith('|'):
            flush_text()
            table_lines.append(line.strip())
        else:
            flush_table()
            text_lines.append(line)
    flush_table()
    flush_text()
    return blocks
