"""
Report CMS (8 Sep 2026 review meeting, item H7; 4 Sep register C4/C5):
the boilerplate prose sections of the statutory NDT report become
editable template variables, guarded by a Director-set password.

The registry below is the single source of the DEFAULT wording — the
same strings the report previously hardcoded. Both emitters (the fpdf2
PDF in ndt_reports.py and the Word export in word_export.py) resolve
every section through ``get_cms_text``, so the PDF, the .docx and the
CMS editor can never show different text for the same section.

Computed content is deliberately NOT editable: formulas, the
calibration disclosure, result tables, worked examples and every
figure stay server-computed (the platform's no-fabrication rule).
Only the prose blocks are exposed here — the static boilerplate with
registry defaults, plus (11 Sep 2026) the generated-content sections
whose default body is the wording computed from the project's own
recorded data, so the operator can review and reword everything the
report will print before generating. The recommendation lead-in is the
one split section: its editable default ends mid-sentence because the
computed findings sentence and the professional-advice sentence are
appended after it.
"""

INTRODUCTION_DEFAULT = (
    'Non-Destructive Test (NDT), as the name implies, means that the '
    'material under test is not damaged during test. Direct '
    'measurement of the strength of concrete involves destructive '
    'stresses and cannot be used for determining the quality of '
    'already cast concrete. It is for this reason that direct methods '
    'are not employed in determining the strength of in-situ '
    'concrete. As a result, indirect method was used.'
)

PURPOSE_ITEMS_DEFAULT = (
    'Determine the present status of the structures from the outcome '
    'of the visual and Non-Destructive test of the tested structures '
    'to ascertain their state with respect to BS EN 12504-4:2021, '
    'ASTM C597-09.\n'
    'Determine the ongoing concrete strength of the structure with '
    'respect to BS 8110: Part 1 1997.\n'
    'Provide engineering advice based on the visual and '
    'Non-Destructive test conducted.\n'
    "To comply with government's statutory requirements."
)

LITERATURE_REVIEW_DEFAULT = (
    'Ultrasonic Pulse Velocity (UPV) testing is a non-destructive '
    'testing technique used for assessing the quality, uniformity, and '
    'internal condition of hardened concrete. The method operates by '
    'transmitting high-frequency ultrasonic waves through concrete and '
    'measuring the travel time between transmitting and receiving '
    'transducers.'
)

VISUAL_PREAMBLE_DEFAULT = (
    'From the visual inspection conducted on the structure the '
    'following observations were noted and recorded as at the time of '
    'test;'
)

METHODOLOGY_CONCRETE_DEFAULT = (
    'Pulse velocity measurements made on concrete structures are '
    'used for quality control purposes. In comparison with mechanical '
    'tests on control samples such as cubes or cylinders, pulse '
    'velocity measurements have the advantage that they relate '
    'directly to the concrete in the structure rather than to '
    'samples, which may not be always truly representative of the '
    'concrete in situ.'
    '\n\n'
    'A pulse of longitudinal vibrations is produced by an '
    'electro-acoustical transducer, which is held in contact with one '
    'surface of the concrete under test. When the pulse generated is '
    'transmitted into the concrete from the transducer using a '
    'certified coupling gel material, it undergoes multiple '
    'reflections at the boundaries of the different material phases '
    'within the concrete. A complex system of stress waves develops, '
    'which includes both longitudinal and shear waves, and propagates '
    'through the concrete. The first waves to reach the receiving '
    'transducer are the longitudinal waves, which are converted into '
    'an electrical signal by a second transducer. Electronic timing '
    'circuits enable the transit time (T) of the pulse to be '
    'measured. This test is conducted for assessing the quality and '
    'integrity of concrete by passing ultrasound waves through the '
    'specimen under test.'
)

RECOMMENDATION_LEAD_DEFAULT = (
    'Based on the purpose of investigation, the outcome of the Visual '
    'and Non-Destructive Test carried out on the building which shows '
    'the present state of the building as described in the visual '
    'test and depicted in the bar charts,'
)

CONCLUSION_PREAMBLE_DEFAULT = (
    'The visual and structural integrity test was conducted in '
    'accordance with BS 1881: Part 201: 1986, BS EN 12504-4:2004, '
    'BS EN 12504-4:2021.'
)

# key -> section descriptor. Ordered; the CMS editor lists sections in
# this order. ``kind`` tells the editor how to parse the body:
#   'paragraphs' — blank lines separate paragraphs
#   'list'       — each non-empty line is one numbered/bulleted item
# ---------------------------------------------------------------------------
# Generated-content sections (11 Sep 2026 client request): the wording the
# report COMPUTES from the project's recorded data — the executive summary,
# the project paragraphs of the introduction, the visual observations, the
# equipment methodology, the rebar statement, the findings statement and the
# conclusion items. Their default body is NOT a static string: it is produced
# per project by NDTReportService.computed_section_bodies(). A key whose
# body is None has no recorded data behind it (e.g. no test results yet) and
# the CMS refuses edits for it — an override there could only invent
# results, which the platform never does. These sections are per-project
# only (no platform-wide override: the text is data-derived).
# ---------------------------------------------------------------------------
EXECUTIVE_SUMMARY_HELP = (
    'Generated from the project profile and the test outcome. **double '
    'asterisks** render as bold. Blank lines start new paragraphs. The '
    'result tables, charts and every figure stay server-computed.'
)

INTRODUCTION_PROJECT_HELP = (
    'The paragraphs naming the project, its client, its location and the '
    'structural drawing policy — generated from project records. **double '
    'asterisks** render as bold. Blank lines start new paragraphs.'
)

VISUAL_OBSERVATIONS_HELP = (
    'The lettered observations of Section 4.1, generated from the recorded '
    'test notes. One observation per line; each prints lettered a, b, c ... '
    'Only editable when observations have been recorded.'
)

METHODOLOGY_EQUIPMENT_HELP = (
    'The PUNDIT/Profoscope paragraphs opening Section 4.2, generated to '
    'match the tests actually recorded. Blank lines start new paragraphs. '
    'The capability list and the calibration conversion statement stay '
    'server-computed.'
)

REBAR_STATEMENT_HELP = (
    'The Section 4.3 wording, generated from the rebar survey records. '
    'When no rebar survey was recorded the section stays fixed at the '
    'honest "Not Applicable" statement and cannot be edited.'
)

FINDINGS_STATEMENT_HELP = (
    'The computed findings sentence of Section 6.0 — how many members were '
    'good / below the statutory 25 N/mm2 — followed by the advice to engage '
    'a structural engineer. The editable lead-in precedes it. Only editable '
    'when test results exist.'
)

CONCLUSION_ITEMS_HELP = (
    'The numbered conclusion items of Section 7.0, generated with the '
    'computed percentages. One item per line. Only editable when test '
    'results exist.'
)

CMS_SECTIONS = {
    'introduction': {
        'label': 'Introduction — opening paragraph (Section 1.0)',
        'kind': 'paragraphs',
        'default': INTRODUCTION_DEFAULT,
        'help': 'Blank lines start a new paragraph. The paragraphs '
                'naming the project, its location and the structural '
                'drawing policy are computed from project records and '
                'always follow this text.',
    },
    'purpose_items': {
        'label': 'Purpose of investigation — numbered items (Section 2.0)',
        'kind': 'list',
        'default': PURPOSE_ITEMS_DEFAULT,
        'help': 'One item per line; each line prints as a numbered '
                'item under "The purpose of the investigation is to:".',
    },
    'literature_review': {
        'label': 'Literature review — opening paragraph (Section 3.0)',
        'kind': 'paragraphs',
        'default': LITERATURE_REVIEW_DEFAULT,
        'help': 'Blank lines start new paragraphs. The UPV formula, '
                'the standards list and the calibration disclosure '
                'that follow are computed and stay fixed.',
    },
    'visual_preamble': {
        'label': 'Visual test — opening sentence (Section 4.1)',
        'kind': 'paragraphs',
        'default': VISUAL_PREAMBLE_DEFAULT,
        'help': 'Precedes the recorded visual observations (lettered '
                'a, b, c ...), which are field data and never '
                'editable here.',
    },
    'methodology_concrete': {
        'label': 'Methodology — concrete paragraphs (Section 4.2)',
        'kind': 'paragraphs',
        'default': METHODOLOGY_CONCRETE_DEFAULT,
        'help': 'Blank lines start new paragraphs. These paragraphs '
                'sit under the CONCRETE heading; the PUNDIT equipment '
                'description, capability list and the calibration '
                'conversion statement are computed.',
    },
    'recommendation_preamble': {
        'label': 'Recommendation — opening lead-in (Section 6.0)',
        'kind': 'paragraphs',
        'default': RECOMMENDATION_LEAD_DEFAULT,
        'help': 'Editable lead-in only. The findings sentence (how '
                'many members were good / below 25 N/mm2) and the '
                'advice to engage a structural engineer are computed '
                'and appended after this text automatically.',
    },
    'conclusion_preamble': {
        'label': 'Conclusion — standards sentence (Section 7.0)',
        'kind': 'paragraphs',
        'default': CONCLUSION_PREAMBLE_DEFAULT,
        'help': 'Precedes the numbered conclusion items, whose '
                'percentages are computed from the test results.',
    },
    # ---- generated-content sections (computed defaults, per-project) ----
    'executive_summary': {
        'label': 'Executive Summary — generated content',
        'kind': 'paragraphs',
        'default': None,
        'computed': True,
        'help': EXECUTIVE_SUMMARY_HELP,
    },
    'introduction_project': {
        'label': 'Introduction — generated project paragraphs (Section 1.0)',
        'kind': 'paragraphs',
        'default': None,
        'computed': True,
        'help': INTRODUCTION_PROJECT_HELP,
    },
    'visual_observations': {
        'label': 'Visual test — generated observations (Section 4.1)',
        'kind': 'list',
        'default': None,
        'computed': True,
        'help': VISUAL_OBSERVATIONS_HELP,
    },
    'methodology_equipment': {
        'label': 'Methodology — generated equipment paragraphs (Section 4.2)',
        'kind': 'paragraphs',
        'default': None,
        'computed': True,
        'help': METHODOLOGY_EQUIPMENT_HELP,
    },
    'rebar_statement': {
        'label': 'Rebar assessment — generated statement (Section 4.3)',
        'kind': 'paragraphs',
        'default': None,
        'computed': True,
        'help': REBAR_STATEMENT_HELP,
    },
    'findings_statement': {
        'label': 'Recommendation — generated findings (Section 6.0)',
        'kind': 'paragraphs',
        'default': None,
        'computed': True,
        'help': FINDINGS_STATEMENT_HELP,
    },
    'conclusion_items': {
        'label': 'Conclusion — generated items (Section 7.0)',
        'kind': 'list',
        'default': None,
        'computed': True,
        'help': CONCLUSION_ITEMS_HELP,
    },
}

CMS_SECTION_KEYS = tuple(CMS_SECTIONS.keys())


def get_cms_text(project, key, computed=None):
    """
    Resolve the effective body for a CMS section.

    Precedence: project override > platform-wide override > the computed
    generated body (generated-content sections, when ``computed`` supplies
    one) > the registry default. Returns ``(text, source)`` with source one
    of ``'project_override' | 'platform_override' | 'computed' | 'default'``.
    A generated-content section with no computed body resolves to
    ``(None, 'unavailable')`` — the caller keeps its honest fixed rendering.
    """
    if key not in CMS_SECTIONS:
        raise KeyError(f'Unknown report CMS section: {key!r}')
    from .models import ReportSectionOverride
    if project is not None:
        row = (ReportSectionOverride.objects
               .filter(project=project, section_key=key).first())
        if row is not None:
            return row.body, 'project_override'
    if not CMS_SECTIONS[key].get('computed'):
        row = (ReportSectionOverride.objects
               .filter(project__isnull=True, section_key=key).first())
        if row is not None:
            return row.body, 'platform_override'
    if CMS_SECTIONS[key].get('computed'):
        body = (computed or {}).get(key)
        if body is not None:
            return body, 'computed'
        return None, 'unavailable'
    return CMS_SECTIONS[key]['default'], 'default'


def cms_paragraphs(text):
    """Split a paragraphs-kind body on blank lines (never returns [])."""
    parts = [p.strip() for p in str(text).split('\n\n')]
    return [p for p in parts if p] or ['']


def cms_list_items(text):
    """Split a list-kind body on lines (never returns [])."""
    items = [line.strip() for line in str(text).splitlines()]
    return [line for line in items if line] or ['']
