"""
Report structure (REFINED EXECUTIVE SUMMARY §2.5 — document flexibility):
the per-project section order, per-section enable/disable state and custom
sections for the statutory NDT report.

DEFAULT_STRUCTURE is the canonical document — exactly the §2.1 preview
sidebar's fourteen sections in the order the reference template prints
them. A project with NO ReportSectionConfig rows resolves to that default,
so every pre-existing project keeps byte-for-byte the document it always
had. The first structure change for a project materialises the complete
default set as rows (see ensure_structure_rows); after that the rows are
the truth — reorder/toggle/custom operations only ever mutate rows, so a
partial row set can only mean "rows for defaults that did not exist when
the set was materialised" (forward-compatibility for newly added
built-ins).

Both emitters — the fpdf2 PDF (ndt_reports.py) and the .docx export
(word_export.py) — resolve through resolve_report_structure(), so the
certified PDF, the editable Word copy and the DOCUMENT EDITOR UI can never
describe three different documents.
"""
from django.db.models import Max

# key -> sidebar label, in the canonical print order (wireframe §2.1/§2.5).
DEFAULT_STRUCTURE = (
    ('cover_page', 'Cover Page'),
    ('executive_summary', 'Executive Summary'),
    ('1.0', 'Introduction'),
    ('2.0', 'Purpose'),
    ('3.0', 'Literature Review'),
    ('3.1', 'Location Map'),
    ('4.0', 'Field Work'),
    ('4.1', 'Visual Test'),
    ('4.2', 'Methodology'),
    ('5.0', 'Analysis'),
    ('5.3', 'AI Interpretation'),
    ('6.0', 'Recommendations'),
    ('7.0', 'Conclusion'),
    ('APPENDIX', 'Appendix'),
)

DEFAULT_STRUCTURE_KEYS = tuple(k for k, _ in DEFAULT_STRUCTURE)
_LABELS = dict(DEFAULT_STRUCTURE)


def _entry_from_row(row):
    if row.is_custom:
        label = (row.title or '').strip() or 'Custom Section'
        return {
            'key': row.section_key,
            'label': label,
            'is_custom': True,
            'title': row.title,
            'body': row.body,
            'is_enabled': row.is_enabled,
        }
    return {
        'key': row.section_key,
        'label': _LABELS.get(row.section_key, row.section_key),
        'is_custom': False,
        'title': None,
        'body': None,
        'is_enabled': row.is_enabled,
    }


def resolve_report_structure(project):
    """
    The ordered section list for the project's report. No rows at all ->
    the canonical default (everything enabled, template order). Otherwise
    the rows in (display_order, created_at) order, with any built-in key
    missing from the rows appended at the end in canonical order.
    """
    from .models import ReportSectionConfig
    rows = list(ReportSectionConfig.objects.filter(project=project))
    if not rows:
        return [{'key': key, 'label': label, 'is_custom': False,
                 'title': None, 'body': None, 'is_enabled': True}
                for key, label in DEFAULT_STRUCTURE]
    out = [_entry_from_row(r)
           for r in sorted(rows, key=lambda r: (r.display_order,
                                                r.created_at))]
    known = {entry['key'] for entry in out}
    for key, label in DEFAULT_STRUCTURE:
        if key not in known:
            # A built-in added after this project's rows were materialised.
            out.append({'key': key, 'label': label, 'is_custom': False,
                        'title': None, 'body': None, 'is_enabled': True})
    return out


def ensure_structure_rows(project, user=None):
    """
    Materialise the complete default section set for the project
    (idempotent). Called before any structure mutation so the row set is
    always complete: reorder can then treat rows as the whole truth.
    Returns the freshly created keys.
    """
    from .models import ReportSectionConfig
    existing = set(ReportSectionConfig.objects
                   .filter(project=project)
                   .values_list('section_key', flat=True))
    missing = [k for k in DEFAULT_STRUCTURE_KEYS if k not in existing]
    if not missing:
        return []
    base = (ReportSectionConfig.objects.filter(project=project)
            .aggregate(m=Max('display_order'))['m'])
    base = -1 if base is None else base
    ReportSectionConfig.objects.bulk_create([
        ReportSectionConfig(project=project, section_key=key, is_custom=False,
                            display_order=base + 1 + i, updated_by=user)
        for i, key in enumerate(missing)
    ])
    return missing


def new_custom_section_key():
    """A fresh key for a custom section ('custom:<12 hex>')."""
    import uuid
    return 'custom:' + uuid.uuid4().hex[:12]
