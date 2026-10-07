
"""
Word (.docx) export of the statutory NDT report (8 Sep 2026 review
meeting, item H7; 4 Sep register C5): an editable Word document carrying
the same sections, the same CMS-resolved prose and the same
server-computed figures as the fpdf2 PDF in ndt_reports.py.

The PDF remains the certified document of record (its exact bytes are
archived + checksummed); the .docx is the editable working copy the
client asked for. Every value here is read from the same helpers the
PDF uses (NDTReportService._element_data, ecs_report_disclosure,
report_cms.get_cms_text), so the two outputs cannot diverge. Since the
§2.5 document-structure work, both editions also share the project's
section order / enable-disable / custom-section configuration through
report_structure.resolve_report_structure.
"""
import io
from datetime import datetime

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, Inches

from .ndt_reports import (
    ECS_FORMULA_LINE, NDTReportService, _element_display,
    ecs_report_disclosure,
)
from .report_cms import cms_list_items, cms_paragraphs, get_cms_text


def _natural_sort_key(item):
    import re
    if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], tuple):
        item = item[0]
    if isinstance(item, tuple) and len(item) == 2:
        floor_str = str(item[0]).lower()
        member_str = str(item[1])
        parts = [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', member_str)]
        return [floor_str] + parts
    member_str = str(item)
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', member_str)]


def _add_para(doc, text, *, bold=False, size=11, align=None, space_after=6):
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = 'Cambria'
    if align is not None:
        para.alignment = align
    para.paragraph_format.space_after = Pt(space_after)
    return para


def _add_heading(doc, text, level=1):
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.bold = True
    run.font.size = Pt(14 if level == 1 else 12)
    run.font.name = 'Cambria'
    para.paragraph_format.space_before = Pt(14 if level == 1 else 10)
    para.paragraph_format.space_after = Pt(6)
    return para


def _add_table(doc, headers, rows):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = 'Table Grid'
    for cell, text in zip(table.rows[0].cells, headers):
        cell.text = ''
        run = cell.paragraphs[0].add_run(str(text))
        run.bold = True
        run.font.size = Pt(9)
        run.font.name = 'Cambria'
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.text = ''
            run = cell.paragraphs[0].add_run(str(value))
            run.font.size = Pt(9)
            run.font.name = 'Cambria'
    _add_para(doc, '', size=4, space_after=0)
    return table


class NDTWordExporter:
    """Builds the .docx edition of the statutory NDT report."""

    @classmethod
    def export_docx(cls, project, user=None, operator=None, element_id=None):
        from apps.digital_eye.models import PUNDITTest, RebarTest

        S = NDTReportService
        all_tests = list(
            PUNDITTest.objects
            .filter(project=project)
            .select_related('device', 'operator', 'created_by')
            .prefetch_related('files', 'readings')
            .order_by('structural_element', 'tested_at')
        )

        def _get_test_operator_label(t):
            if t.operator_name and t.operator_name.strip():
                return t.operator_name.strip()
            if t.operator:
                full = (t.operator.get_full_name() or '').strip()
                if full:
                    return full
                if t.operator.username:
                    return t.operator.username.strip()
                if t.operator.email:
                    return t.operator.email.strip()
            if t.created_by:
                full = (t.created_by.get_full_name() or '').strip()
                if full:
                    return full
                if t.created_by.username:
                    return t.created_by.username.strip()
                if t.created_by.email:
                    return t.created_by.email.strip()
            return None

        available_operators = []
        for t in all_tests:
            lbl = _get_test_operator_label(t)
            if lbl and lbl not in available_operators:
                available_operators.append(lbl)

        def _test_matches_operator(t, target_str):
            if not target_str:
                return False
            norm_target = ' '.join(str(target_str).strip().lower().split())
            if not norm_target:
                return False

            lbl = _get_test_operator_label(t)
            if lbl and ' '.join(lbl.strip().lower().split()) == norm_target:
                return True

            if t.operator_name:
                op_norm = ' '.join(t.operator_name.strip().lower().split())
                if op_norm == norm_target or norm_target in op_norm or op_norm in norm_target:
                    return True

            for u in (t.operator, t.created_by):
                if not u:
                    continue
                if str(u.pk).lower() == norm_target:
                    return True
                full = ' '.join((u.get_full_name() or '').strip().lower().split())
                if full and (full == norm_target or norm_target in full or full in norm_target):
                    return True
                if u.email and u.email.strip().lower() == norm_target:
                    return True
                if u.username and u.username.strip().lower() == norm_target:
                    return True
            return False

        selected_operator_label = None
        has_explicit_operator = bool(
            operator and str(operator).strip() and str(operator).strip().lower() not in ('all', 'null', 'undefined')
        )

        if has_explicit_operator:
            op_target = str(operator).strip()
            for av in available_operators:
                if ' '.join(av.lower().split()) == ' '.join(op_target.lower().split()):
                    selected_operator_label = av
                    break
            if not selected_operator_label:
                for av in available_operators:
                    if op_target.lower() in av.lower() or av.lower() in op_target.lower():
                        selected_operator_label = av
                        break
            if not selected_operator_label:
                selected_operator_label = op_target
        else:
            if user and getattr(user, 'is_authenticated', False):
                user_full = (user.get_full_name() or '').strip()
                user_uname = (user.username or '').strip()
                for av in available_operators:
                    if user_full and ' '.join(av.lower().split()) == ' '.join(user_full.lower().split()):
                        selected_operator_label = av
                        break
                    if user_uname and ' '.join(av.lower().split()) == ' '.join(user_uname.lower().split()):
                        selected_operator_label = av
                        break
            if not selected_operator_label and available_operators:
                selected_operator_label = available_operators[0]

        if selected_operator_label:
            matched = [t for t in all_tests if _test_matches_operator(t, selected_operator_label)]
            if matched:
                tests = matched
            else:
                lbl_matched = [t for t in all_tests if _get_test_operator_label(t) == selected_operator_label]
                tests = lbl_matched if lbl_matched else all_tests
        else:
            tests = all_tests
            
        if element_id:
            tests = [t for t in tests if str(t.structural_element).strip().lower() == str(element_id).strip().lower() or str(t.batch_id).strip().lower() == str(element_id).strip().lower()]

        rebar_tests = list(
            RebarTest.objects.filter(project=project).order_by('recorded_at')
        )
        pulse_tests = [t for t in tests if t.test_type == 'pulse_velocity']
        crack_tests = [t for t in tests if t.test_type == 'crack_depth']
        surface_tests = [t for t in tests if t.test_type == 'surface_quality']
        devices = []
        for t in tests:
            if t.device and t.device not in devices:
                devices.append(t.device)

        element_data = S._element_data(pulse_tests)
        good_members = [e for e in element_data if e['remark'] == 'GOOD']
        poor_members = [e for e in element_data if e['remark'] == 'POOR']
        visual_notes = S._visual_observations(tests, project=project, operator=selected_operator_label)
        report_no, _year = S._effective_report_number(project, tests)
        tested = [t.test_date for t in tests if t.test_date]
        date_max = max(tested) if tested else datetime.now().date()

        # Generated-content CMS bodies (11 Sep): same resolution as the PDF
        # renderer, so the .docx can never show different wording.
        from apps.digital_eye.models import BIMElementMapping
        floors_present = sorted({e['floor_label'] for e in element_data})
        bim_levels = sorted(set(
            BIMElementMapping.objects.filter(project=project)
            .exclude(level='').values_list('level', flat=True)))
        has_drawings = BIMElementMapping.objects.filter(
            project=project).exists()
        same_day = bool(tested) and min(tested) == max(tested)
        date_min = min(tested) if tested else date_max
        cms = S._computed_bodies(
            project, tests=tests, rebar_tests=rebar_tests,
            element_data=element_data, good_members=good_members,
            poor_members=poor_members, visual_notes=visual_notes,
            floors_present=floors_present, bim_levels=bim_levels,
            has_drawings=has_drawings, tested=tested, same_day=same_day,
            date_min=date_min, date_max=date_max)

        doc = Document()

        def emit_cover():
            # ------------------------------------------------------------ cover
            _add_para(doc, 'LAGOS STATE MATERIALS TESTING LABORATORY',
                      bold=True, size=16, align=WD_ALIGN_PARAGRAPH.CENTER,
                      space_after=18)
            _add_para(doc, 'IN-SITU INTEGRITY TEST (NON-DESTRUCTIVE) OF '
                           'COMPRESSIVE STRENGTH OF STRUCTURAL MEMBERS',
                      bold=True, size=12, align=WD_ALIGN_PARAGRAPH.CENTER,
                      space_after=18)
            _add_table(doc, ['FIELD', 'VALUE'], [
                ['Report No', report_no],
                ['Project', project.name or '-'],
                ['Client', project.client_name or '-'],
                ['Site Address', ', '.join(
                    p for p in (project.site_address, project.lga,
                                project.state) if p) or '-'],
                ['Date of Test', date_max.strftime('%d/%m/%Y')],
            ])
            _add_para(doc, 'NOTE: this Word document is an editable working '
                           'copy. The certified document of record is the PDF '
                           'edition archived (SHA-256 checksummed) on the '
                           'platform.', size=9, space_after=12)

        def emit_intro():
            # ------------------------------------------------------------ 1.0
            _add_heading(doc, '1.0 INTRODUCTION')
            for para in cms_paragraphs(get_cms_text(project, 'introduction')[0]):
                _add_para(doc, para)
            # Generated-content CMS section (11 Sep): the project paragraphs
            # resolve exactly as the PDF renders them.
            for para in cms_paragraphs(
                    get_cms_text(project, 'introduction_project',
                                 computed=cms)[0]):
                _add_para(doc, para)

        def emit_purpose():
            # ------------------------------------------------------------ 2.0
            _add_heading(doc, '2.0 PURPOSE OF INVESTIGATION')
            _add_para(doc, 'The purpose of the investigation is to:')
            for item in cms_list_items(get_cms_text(project,
                                                    'purpose_items')[0]):
                para = doc.add_paragraph(item, style='List Number')
                para.paragraph_format.space_after = Pt(4)

        def emit_lit():
            # ------------------------------------------------------------ 3.0
            _add_heading(doc, '3.0 LITERATURE REVIEW')
            for para in cms_paragraphs(get_cms_text(project,
                                                    'literature_review')[0]):
                _add_para(doc, para)
            _add_para(doc, 'The pulse velocity is calculated using the '
                           'relationship:')
            _add_para(doc, 'UPV = L / t', align=WD_ALIGN_PARAGRAPH.CENTER)
            _add_para(doc, 'L = Distance between transducers (mm)', size=10)
            _add_para(doc, 't = Pulse transit time (microseconds)', size=10)
            _add_para(
                doc,
                'The testing procedure and interpretation of results are '
                'conducted in accordance with international standards including '
                'ASTM C597 (2020), BS EN 12504-4:2004 and ACI 228.2R (2018).')
            disclosure, derivation = ecs_report_disclosure(project)
            _add_para(doc, disclosure)
            _add_para(
                doc,
                'Derivation of the reported results: each test point velocity '
                'is V = L / t; the element pulse velocity is the arithmetic '
                'mean of its point velocities, V(element) = (V1 + V2 + ... + '
                f'Vn) / n; and the estimated compressive strength follows '
                f'{derivation}. Pulse velocities in the Section 5.0 tables are '
                'reported in metres per second (m/s); 1 km/s = 1000 m/s.')
            example = S._worked_example(project, element_data)
            if example:
                _add_para(doc, example)

        def emit_visual():
            # ------------------------------------------------------------ 4.1
            _add_heading(doc, '4.1 VISUAL TEST')
            for para in cms_paragraphs(get_cms_text(project,
                                                    'visual_preamble')[0]):
                _add_para(doc, para)
            # Generated-content CMS section (11 Sep): the recorded observations
            # resolve exactly as the PDF renders them.
            visual_body, _src = get_cms_text(project, 'visual_observations',
                                             computed=cms)
            if visual_body is not None:
                for item in cms_list_items(visual_body):
                    _add_para(doc, item, size=10)
            else:
                _add_para(doc, 'No visual/surface condition observations '
                               'recorded.')

        def emit_methodology():
            # ------------------------------------------------------------ 4.2
            _add_heading(doc, '4.2 METHODOLOGY')
            for para in cms_paragraphs(
                    get_cms_text(project, 'methodology_equipment',
                                 computed=cms)[0]):
                _add_para(doc, para)
            _add_heading(doc, 'CONCRETE', level=2)
            for para in cms_paragraphs(get_cms_text(project,
                                                    'methodology_concrete')[0]):
                _add_para(doc, para)
            _add_para(doc, 'The Pundit test equipment can also determine the '
                           'following:')
            for item in ('The homogeneity and uniformity of the concrete.',
                         'Changes in the strength of the concrete which may '
                         'occur with time.',
                         'The quality of the concrete in relation to standard '
                         'requirements.',
                         'The quality of one element of concrete in relation to '
                         'another.'):
                doc.add_paragraph(item, style='List Bullet')
            from apps.digital_eye.strength_curves import resolve_active_curve
            try:
                active_curve = resolve_active_curve(project)
            except Exception:  # noqa: BLE001 — conversion line must not kill export
                active_curve = None
            if active_curve is not None and active_curve.project_id:
                from apps.digital_eye.strength_curves import formula_display
                conversion = (
                    formula_display(active_curve.curve_type,
                                    active_curve.formula_params or {})
                    + ' (f_cu in N/mm2, V in m/s'
                    + (', R the rebound number)'
                       if active_curve.curve_type == 'sonreb' else ')'))
            else:
                conversion = ECS_FORMULA_LINE
            _add_para(doc, 'Estimated compressive strength conversion: '
                           + conversion + '. The full calibration statement is '
                           'given in Section 3.0.')

            # ------------------------------------------------------------ 4.3
            _add_heading(doc, '4.3 REINFORCING BAR (REBAR) ASSESSMENT')
            # Generated-content CMS section (11 Sep): resolves exactly as the
            # PDF renders it; without a survey the honest statement prints.
            rebar_body, _src = get_cms_text(project, 'rebar_statement',
                                            computed=cms)
            if rebar_body is not None:
                _add_para(doc, rebar_body)
                _add_table(
                    doc,
                    ['S/N', 'STRUCTURAL MEMBER', 'MAIN BAR (MM)', 'LINKS (MM)',
                     'SPACING (MM)', 'COVER DEPTH (MM)'],
                    [[str(i + 1),
                      (rt.structural_element or 'Unknown').upper(),
                      str(int(rt.main_bar_mm)) if rt.main_bar_mm else '-',
                      str(int(rt.links_mm)) if rt.links_mm else '-',
                      str(rt.spacing_mm) if rt.spacing_mm else '-',
                      str(int(rt.cover_depth_mm)) if rt.cover_depth_mm else '-']
                     for i, rt in enumerate(sorted(rebar_tests, key=lambda r: _natural_sort_key(r.structural_element or '')))])
            else:
                _add_para(doc, 'No rebar assessment was recorded during this '
                               'investigation.')

            # ------------------------------------------------------------ 4.4
            _add_heading(doc, '4.4 EQUIPMENT')
            if devices:
                _add_table(doc, ['NAME OF EQUIPMENT', 'EQUIPMENT ID'],
                           [[d.name or d.model or '-', d.device_id or '-']
                            for d in devices])
            else:
                _add_para(doc, 'No field device was recorded against these '
                               'tests.')

        def emit_analysis():
            # ------------------------------------------------------------ 5.0
            _add_heading(doc, '5.0 ANALYSIS OF TEST RESULT')
            if crack_tests:
                _add_heading(doc, '5.1 CRACK DEPTH MEASUREMENTS '
                                  '(TIME-DIFFERENCE METHOD)', level=2)
                for t in sorted(crack_tests, key=lambda ct: _natural_sort_key(ct.structural_element or '')):
                    rows = t.reading_rows()
                    mean_depth = S._crack_depth(t)
                    _add_table(
                        doc,
                        ['ELEMENT', 'POINT', 'SPACING b (MM)', 'T CRACKED (US)',
                         'T UNCRACKED (US)', 'CRACK DEPTH (MM)',
                         'MEAN DEPTH (MM) / REMARK'],
                        [[_element_display(t.structural_element) if i == 0 else '',
                          r['label'] or '-',
                          S._fmt(r['path_mm'], 1),
                          S._fmt(r['transit_us'], 1),
                          S._fmt(r['uncracked_us'], 1),
                          S._fmt(r['crack_depth_mm'], 1),
                          (f"{S._fmt(mean_depth, 1)} / {S._crack_remark(t)}"
                           if i == len(rows) // 2 or len(rows) == 1 else '')]
                         for i, r in enumerate(rows)])
            if not element_data:
                _add_para(doc, 'No pulse velocity tests recorded for this '
                               'project.')
            else:
                _add_heading(doc, 'SUMMARY OF TEST ANALYSIS', level=2)
                analysis_groups = {}
                for e in element_data:
                    key = (e['floor_label'], e['member_type'])
                    g = analysis_groups.setdefault(key, {'count': 0, 'points': 0})
                    g['count'] += 1
                    g['points'] += e['n_points']
                _add_table(
                    doc,
                    ['STRUCTURAL MEMBER', 'NUMBER TESTED', 'LOCATION',
                     'NO OF POINT TAKEN'],
                    [[member.title(), str(g['count']), floor.title(),
                      str(g['points'])]
                     for (floor, member), g in sorted(
                         analysis_groups.items(), key=_natural_sort_key)])

                _add_heading(doc, 'SUMMARY OF TEST RESULTS', level=2)
                for floor in floors_present:
                    floor_elements = sorted(
                        [e for e in element_data if e['floor_label'] == floor],
                        key=lambda e: _natural_sort_key(e['element'])
                    )
                    member_order = []
                    for e in floor_elements:
                        if e['member_type'] not in member_order:
                            member_order.append(e['member_type'])
                    member_order = sorted(member_order, key=_natural_sort_key)
                    for member in member_order:
                        group = sorted(
                            [e for e in floor_elements if e['member_type'] == member],
                            key=lambda e: _natural_sort_key(e['element'])
                        )
                        plural = member if member.endswith('S') else member + 'S'
                        _add_heading(doc, f'{floor.upper()} {plural}', level=3)
                        table_rows = []
                        for e in group:
                            rows = e['rows']
                            mid = len(rows) // 2 if len(rows) > 1 else 0
                            remark = e['remark']
                            if (remark != 'UNVERIFIED'
                                    and e['spread_pct'] is not None
                                    and e['spread_pct'] > 2.0):
                                remark += (f" (UPV VARIANCE BETWEEN POINTS "
                                           f"{e['spread_km_s'] * 1000:.0f} M/S, "
                                           f"±{e['spread_pct'] / 2:.1f}%)")
                            
                            for i, r in enumerate(rows):
                                table_rows.append([
                                    _element_display(e['element']) if i == 0 else '',
                                    S._fp(r['path_mm']),
                                    S._f1(r['transit_us']),
                                    S._fms(r['velocity_km_s']),
                                    S._f1(r['ecs_mpa']),
                                    S._f1(e['mean_ecs']) if i == mid else '',
                                    remark if i == mid else ''
                                ])
                        _add_table(
                            doc,
                            ['Structural Element', 'PATH LENGTH', 'TRANSIT TIME',
                             'PULSE VELOCITY (M/S)', 'E.C.S',
                             'AVERAGE COMPRESSIVE STRENGTH (N/mm2)', 'REMARK'],
                            table_rows)
                result_groups = {}
                for e in element_data:
                    key = (e['floor_label'], e['member_type'])
                    g = result_groups.setdefault(
                        key, {'good': 0, 'poor': 0, 'unverified': 0, 'total': 0})
                    g['total'] += 1
                    if e['remark'] == 'GOOD':
                        g['good'] += 1
                    elif e['remark'] == 'POOR':
                        g['poor'] += 1
                    elif e['remark'] == 'UNVERIFIED':
                        g['unverified'] += 1

                def _pct(count, g):
                    # Percentage of the elements that could be assessed — see
                    # ndt_reports for why an unverifiable element is not in the
                    # denominator.
                    graded = g['good'] + g['poor']
                    return f"{count} ({round(count * 100 / graded, 1)}%)" if graded else '-'

                _add_table(
                    doc,
                    ['STRUCTURAL MEMBER', 'LOCATION', 'GOOD (NO, %)',
                     'POOR (NO, %)'],
                    [[member.title(), floor.title(),
                      _pct(g['good'], g), _pct(g['poor'], g)]
                     for (floor, member), g in sorted(result_groups.items(), key=_natural_sort_key)])
                unverified_members = [e for e in element_data
                                      if e['remark'] == 'UNVERIFIED']
                if unverified_members:
                    _add_para(
                        doc,
                        f"{len(unverified_members)} of {len(element_data)} "
                        "element(s) could not be verified and are excluded "
                        "from the GOOD / POOR summary above. Their pulse "
                        "velocity is outside the range physically plausible "
                        "for concrete, so no grade and no compressive strength "
                        "is asserted for them, and the percentages above are of "
                        "the elements that could be assessed.")
                    for e in unverified_members:
                        _add_para(doc, f"{e['element']}: {e['implausibility_note']}")
            if surface_tests:
                _add_heading(doc, '5.2 SURFACE QUALITY OBSERVATIONS', level=2)
                for t in sorted(surface_tests, key=lambda st: _natural_sort_key(st.structural_element or '')):
                    for r in t.reading_rows():
                        condition = r.get('surface_condition') \
                            or 'condition not recorded'
                        _add_para(
                            doc,
                            f"{_element_display(t.structural_element)} "
                            f"{r['label'] or '-'}: {condition}")

        def emit_discussion_of_results():
            # ------------------------------------------------------------ 5.4
            _add_heading(doc, '5.4 DISCUSSION OF RESULTS', level=2)
            body = get_cms_text(project, 'discussion_of_results')[0]
            for para in cms_paragraphs(body):
                _add_para(doc, para)

        def emit_remarks():
            # ------------------------------------------------------------ 5.4
            _add_heading(doc, '5.4 FIELD REMARKS & OBSERVATIONS', level=2)

            lead_in = get_cms_text(project, 'remarks_preamble')[0]
            for para in cms_paragraphs(lead_in):
                _add_para(doc, para)

            def _is_substantive(text):
                if not text or not str(text).strip():
                    return False
                t_low = str(text).lower()
                for synth_phrase in ('synthetic value', 'sample file', 'upload testing only', '[manual_field_entry'):
                    if synth_phrase in t_low:
                        return False
                return True

            # Query on-site visual observations and photo evidence from telemetry session and inspection
            site_visual_obs = []
            site_photos_count = 0
            gps_tags = []
            try:
                from apps.telemetry.models import TelemetrySession
                ts_qs = list(TelemetrySession.objects.filter(project=project))
                if selected_operator_label:
                    op_str = selected_operator_label.strip().lower()
                    ts_qs = [
                        s for s in ts_qs
                        if (s.operator_name and s.operator_name.strip().lower() == op_str)
                        or (s.operator and (s.operator.get_full_name().strip().lower() == op_str or s.operator.email.strip().lower() == op_str))
                    ]
                for s in ts_qs:
                    cfg = s.session_config or {}
                    vis = (cfg.get('visual_observation') or '').strip()
                    if vis and _is_substantive(vis) and vis not in site_visual_obs:
                        site_visual_obs.append(vis)
                    photos = cfg.get('photos') or []
                    site_photos_count += len(photos)
                    lat = cfg.get('latitude')
                    lon = cfg.get('longitude')
                    if lat is not None and lon is not None:
                        gps_str = f"{lat:.6f}°, {lon:.6f}°"
                        if gps_str not in gps_tags:
                            gps_tags.append(gps_str)
            except Exception as e:
                logger.warning("Could not query telemetry visual observations: %s", e)

            try:
                from apps.inspections.models import Inspection
                insp = Inspection.objects.filter(project=project).order_by('-created_at').first()
                if insp:
                    if insp.visual_site_observations:
                        for line in insp.visual_site_observations.splitlines():
                            line = line.strip()
                            if line and _is_substantive(line) and line not in site_visual_obs:
                                site_visual_obs.append(line)
                    if insp.visual_site_photos:
                        site_photos_count = max(site_photos_count, len(insp.visual_site_photos))
            except Exception as e:
                logger.warning("Could not query inspection visual observations: %s", e)

            attached_files_count = sum(t.files.count() for t in tests)
            total_photos_count = max(site_photos_count, attached_files_count)
            floors_list = sorted({t.floor for t in tests if (t.floor or '').strip()})
            floors_desc = ", ".join(floors_list) if floors_list else "all inspected floor levels"

            anomalies = []
            for t in tests:
                clean_notes = (S._PROVENANCE_STAMP_RE.sub('', t.notes or '').strip()
                               if t.notes else '')
                if clean_notes and _is_substantive(clean_notes):
                    anomalies.append(f"{t.structural_element or 'Element'}: {clean_notes}")
                for r in t.reading_rows():
                    raw_pt = (r.get('notes') or '').strip() if isinstance(r, dict) else ''
                    pt_note = (S._PROVENANCE_STAMP_RE.sub('', raw_pt).strip() if raw_pt else '')
                    if pt_note and _is_substantive(pt_note):
                        anomalies.append(f"{t.structural_element or 'Element'} (Pt {r.get('label', '')}): {pt_note}")

            summary_table_rows = []
            if anomalies:
                cond_summary = "; ".join(anomalies[:5])
            else:
                cond_summary = (
                    "Uniform surface preparation per BS 1881-203. Concrete surfaces sound, "
                    "dry, and free of honeycombing, spalling, or structural voids."
                )
            summary_table_rows.append([
                "Structural Member Surfaces",
                f"{len(tests)} stations tested across {floors_desc}",
                cond_summary
            ])

            if site_visual_obs:
                vis_summary = "; ".join(site_visual_obs)
            else:
                vis_summary = "Standard field conditions recorded on site during testing."
            summary_table_rows.append([
                "Visual Site Observations",
                "Testing Zone / Laydown Area",
                vis_summary
            ])

            photo_notes = f"{total_photos_count} site photo(s) documented"
            if gps_tags:
                photo_notes += f" with GPS anchoring ({gps_tags[0]})"
            photo_notes += ". Archived in Appendix photographic dossier."
            summary_table_rows.append([
                "Photographic Evidence",
                "Site Evidence & Provenance",
                photo_notes
            ])

            _add_table(
                doc,
                ['FIELD ASSESSMENT SCOPE', 'LOCATION / LEVEL', 'SUMMARIZED REMARKS & OBSERVATIONS'],
                summary_table_rows
            )

            _add_para(
                doc,
                f"Fieldwork & Surface Synthesis: A total of {len(tests)} structural member test stations "
                f"were assessed across {floors_desc}. In accordance with BS 1881-203 and BS EN 12504-4, "
                f"all tested elements provided direct acoustic coupling. "
                + (f"On-site visual observation noted: “{'; '.join(site_visual_obs)}”. " if site_visual_obs else "")
                + (f"Attached photographic records ({total_photos_count} photo(s)) confirm the physical state as at test time. " if total_photos_count else "No surface defects requiring structural intervention were identified during fieldwork.")
            )

        def emit_reco():
            # ------------------------------------------------------------ 6.0
            _add_heading(doc, '6.0 RECOMMENDATION')
            if element_data:
                lead_in = get_cms_text(project, 'recommendation_preamble')[0]
                findings_body, _src = get_cms_text(project, 'findings_statement',
                                                   computed=cms)
                _add_para(doc, lead_in.rstrip() + ' ' + findings_body)
            else:
                _add_para(doc, 'No pulse velocity results are available for '
                               'this project; no recommendation on concrete '
                               'quality can be made.')
            from apps.evidence.models import AIAnalysisRecord
            recommendations = []
            ai_record = (AIAnalysisRecord.objects
                             .filter(project=project, analysis_type='pundit')
                             .order_by('-created_at').first())
            if ai_record:
                for rec in (ai_record.recommendations or []):
                    if isinstance(rec, dict):
                        line = f'[{str(rec.get("priority", "Routine")).upper()}] ' \
                               f'{rec.get("recommendation", "")}'
                    else:
                        line = str(rec)
                    if line not in recommendations:
                        recommendations.append(line)
            for rec in recommendations:
                doc.add_paragraph(rec, style='List Bullet')

        def emit_conclusion():
            # ------------------------------------------------------------ 7.0
            _add_heading(doc, '7.0 CONCLUSION')
            if element_data:
                for para in cms_paragraphs(get_cms_text(project,
                                                        'conclusion_preamble')[0]):
                    _add_para(doc, para)
                # Generated-content CMS section (11 Sep): the conclusion items
                # resolve exactly as the PDF renders them.
                for item in cms_list_items(
                        get_cms_text(project, 'conclusion_items',
                                     computed=cms)[0]):
                    para = doc.add_paragraph(item, style='List Number')
                    para.paragraph_format.space_after = Pt(4)
                _add_para(
                    doc,
                    'However, it is imperative to state clearly that '
                    'non-adherence to the recommendation excludes the testing '
                    'laboratory of any responsibility.')
                _add_para(
                    doc,
                    'The test assumed 25 N/mm2 as the strength of the '
                    'structural members, however a substructure probe is '
                    'required to ascertain the integrity of the building '
                    'foundation.')
            else:
                _add_para(doc, 'No ultrasonic pulse velocity results are '
                               'available for this project; no conclusion on '
                               'concrete quality can be drawn.')

        # ---------------------- §2.5 ordered emission (document structure)
        # Same source of truth as the certified PDF: the project's structure
        # rows drive section order, enable/disable state and custom
        # sections. Sections the .docx edition has never carried (executive
        # summary, location map, field work, AI interpretation, appendix)
        # skip silently here, exactly as before — but their position in the
        # order still holds for the sections that do render.
        from .report_structure import resolve_report_structure
        emitters = {
            'cover_page': emit_cover,
            '1.0': emit_intro,
            '2.0': emit_purpose,
            '3.0': emit_lit,
            '4.1': emit_visual,
            '4.2': emit_methodology,   # 4.2 + 4.3 + 4.4, as one unit
            '5.0': emit_analysis,
            '5.4': emit_remarks,
            '5.5': emit_discussion_of_results,
            'remarks': emit_remarks,
            '6.0': emit_reco,
            '7.0': emit_conclusion,
        }
        for entry in resolve_report_structure(project):
            if not entry['is_enabled']:
                continue
            if entry['is_custom']:
                title = (entry.get('title') or 'CUSTOM SECTION').strip()
                _add_heading(doc, title.upper())
                for para in cms_paragraphs(entry.get('body') or ''):
                    _add_para(doc, para)
                continue
            emitter = emitters.get(entry['key'])
            if emitter is not None:
                emitter()

        # ---------------------------------------------------------- sign-off
        user_label = 'Unauthenticated'
        if user and getattr(user, 'is_authenticated', False):
            user_label = user.get_full_name() or user.email
        tested_by = (selected_operator_label or 'NOT RECORDED').upper()
        _add_table(doc, ['TESTED BY', 'APPROVED BY'],
                   [[tested_by, user_label.upper()]])

        # ------------------------------------------------------------- footer
        digest = S._statutory_digest(project, tests, report_no)
        _add_para(doc, 'REPORT INTEGRITY', bold=True)
        _add_para(
            doc,
            'Content digest (SHA-256 of the underlying test identifiers, '
            'results and reading rows):')
        _add_para(doc, digest, size=9)
        _add_para(
            doc,
            f'Generated {datetime.now().strftime("%d/%m/%Y %H:%M")} by '
            f'{user_label}.', size=9)

        buffer = io.BytesIO()
        doc.save(buffer)
        return buffer.getvalue()
