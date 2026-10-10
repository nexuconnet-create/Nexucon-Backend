import re
import sys

def patch():
    with open('apps/reports/ndt_reports.py', 'r', encoding='utf-8') as f:
        code = f.read()

    # 1. Recommendation logic (executive summary)
    exec_old = """                if has_visual_defects or poor_members:
                    if has_visual_defects and poor_members:
                        remedial_clause = (
                            'proffer solution to the defects observed, give technical '
                            'advice on the poor structural members tested and further '
                            'analyse the structural arrangement'
                        )
                    elif has_visual_defects:
                        remedial_clause = (
                            'proffer solution to the defects observed and further '
                            'analyse the structural arrangement'
                        )
                    else:
                        remedial_clause = (
                            'give technical advice on the poor structural members '
                            'tested and further analyse the structural arrangement'
                        )
                    builder.para(
                        '**In view of the above, it is advised that a qualified structural '
                        f'engineer should be engaged to {remedial_clause} to '
                        'guarantee the stability, integrity and the serviceability of the '
                        'structure.**',
                        markdown=True
                    )"""

    exec_new = """                has_flagged_members = any(e.get('flagged_spread') for e in element_data)
                if has_visual_defects or poor_members or has_flagged_members:
                    reco_parts = []
                    if has_visual_defects:
                        reco_parts.append('proffer solution to the defects observed')
                    if poor_members:
                        reco_parts.append('give technical advice on the poor structural members tested')
                    if has_flagged_members and not poor_members:
                        reco_parts.append('investigate the structural members flagged for excessive point spread')
                    reco_parts.append('and further analyse the structural arrangement')
                    
                    if len(reco_parts) > 2:
                        remedial_clause = ', '.join(reco_parts[:-1]) + ' and ' + reco_parts[-1]
                    else:
                        remedial_clause = ' '.join(reco_parts)
                        
                    builder.para(
                        '**In view of the above, it is advised that a qualified structural '
                        f'engineer should be engaged to {remedial_clause} to '
                        'guarantee the stability, integrity and the serviceability of the '
                        'structure.**',
                        markdown=True
                    )"""
    if exec_old in code:
        code = code.replace(exec_old, exec_new)
    else:
        print("ERROR: exec_old not found!")

    # 2. Recommendation logic (report_cms)
    reco_old = """            if has_visual_defects or poor_members:
                eff_client = cls._effective_client_name(project)
                client_target = (eff_client.upper() if eff_client
                                 else 'the client')
                if has_visual_defects and poor_members:
                    reco_clause = (
                        'proffer solution to the defects observed, technical '
                        'advice on the poor structural members tested and further '
                        'analyse the structural arrangement'
                    )
                elif has_visual_defects:
                    reco_clause = (
                        'proffer solution to the defects observed and further '
                        'analyse the structural arrangement'
                    )
                else:
                    reco_clause = (
                        'technical advice on the poor structural members '
                        'tested and further analyse the structural arrangement'
                    )
                advice_sentence = (
                    f'**It is advised that {client_target} engage a qualified '
                    f'structural engineer and other relevant professionals in '
                    f'the built environment to {reco_clause} to guarantee the '
                    f'stability, integrity and the serviceability of the '
                    f'building.**'
                )
                exec_paras.append(advice_sentence)"""

    reco_new = """            has_flagged_members = any(e.get('flagged_spread') for e in element_data)
            
            if has_visual_defects or poor_members or has_flagged_members:
                eff_client = cls._effective_client_name(project)
                client_target = (eff_client.upper() if eff_client else 'the client')
                
                reco_parts = []
                if has_visual_defects:
                    reco_parts.append('proffer solution to the defects observed')
                if poor_members:
                    reco_parts.append('provide technical advice on the poor structural members tested')
                if has_flagged_members and not poor_members:
                    reco_parts.append('investigate the structural members flagged for excessive point spread')
                
                reco_parts.append('and further analyse the structural arrangement')
                
                if len(reco_parts) > 2:
                    reco_clause = ', '.join(reco_parts[:-1]) + ' and ' + reco_parts[-1]
                else:
                    reco_clause = ' '.join(reco_parts)
                    
                advice_sentence = (
                    f'**It is advised that {client_target} engage a qualified '
                    f'structural engineer and other relevant professionals in '
                    f'the built environment to {reco_clause} to guarantee the '
                    f'stability, integrity and the serviceability of the '
                    f'building.**'
                )
                exec_paras.append(advice_sentence)"""
    if reco_old in code:
        code = code.replace(reco_old, reco_new)
    else:
        print("ERROR: reco_old not found!")

    # 3. Inject flagged_spread into element_data
    inject_old = """            e['spread_km_s'] = spread
            e['spread_pct'] = (spread / e['mean_v'] * 100) if e['mean_v'] else None"""
            
    inject_new = """            e['spread_km_s'] = spread
            e['spread_pct'] = (spread / e['mean_v'] * 100) if e['mean_v'] else None
            
            # Compute total spread % and either-side spread % for flagging
            point_vels = [r['velocity_km_s'] for r in e['rows'] if r.get('velocity_km_s') is not None]
            e['flagged_spread'] = False
            if len(point_vels) > 1 and e['mean_v']:
                max_v = max(point_vels)
                min_v = min(point_vels)
                total_spread = (max_v - min_v) / e['mean_v'] * 100
                either_side = max((max_v - e['mean_v']) / e['mean_v'] * 100, (e['mean_v'] - min_v) / e['mean_v'] * 100)
                if total_spread > 15.0 or either_side > 7.5:
                    e['flagged_spread'] = True
                    e['total_spread_pct'] = total_spread"""
    if inject_old in code:
        code = code.replace(inject_old, inject_new)
    else:
        print("ERROR: inject_old not found!")

    # 4. Revert emit_analysis logic back to individual tables + grouped summary analysis
    with open('old_ndt.py', 'r', encoding='utf-8') as fold:
        old_code = fold.read()

    # Extract original emit_analysis from old_ndt.py
    import re
    match = re.search(r'(# ---- Summary of Test Analysis: each structural member listed.*?builder\.ruled_table\(\n.*?\[\'STRUCTURAL ELEMENT\',.*?REMARK\'\],\n.*?all_floor_rows,\n.*?\[25, 25, 25, 34, 50\],\n.*?\[\'C\', \'C\', \'C\', \'C\', \'L\'\]\n.*?\))', old_code, re.DOTALL)
    
    if match:
        original_emit = match.group(1)
        # We need to modify original_emit to:
        # A. Group Summary of Test Analysis
        analysis_old = """                # ---- Summary of Test Analysis: each structural member listed
                builder.heading('SUMMARY OF TEST ANALYSIS', page_break=False)
                analysis_rows = [
                    [_element_display(e['element']), '1', e['floor_label'].title(), str(e['n_points'])]
                    for e in sorted(element_data, key=lambda e: _natural_sort_key(e['element']))
                ]
                builder.ruled_table(
                    ['STRUCTURAL MEMBER', 'NUMBER TESTED', 'LOCATION',
                     'NO OF POINT TAKEN'],
                    analysis_rows,
                    [50, 32, 60, 40],
                    ['C', 'C', 'C', 'C'],
                )"""

        analysis_new = """                # ---- Summary of Test Analysis: grouped by member type
                builder.heading('SUMMARY OF TEST ANALYSIS', page_break=False)
                analysis_rows = []
                for floor in floors_present:
                    floor_elements = sorted(
                        [e for e in element_data if e['floor_label'] == floor],
                        key=lambda e: _natural_sort_key(e['element'])
                    )
                    member_order = []
                    for e in floor_elements:
                        if e['member_type'] not in member_order:
                            member_order.append(e['member_type'])
                    for member in member_order:
                        group = [e for e in floor_elements if e['member_type'] == member]
                        count = len(group)
                        floor_abbr = cls._floor_code(floor)
                        member_abbr = cls._member_code(member)
                        label = f'**{floor_abbr}{member_abbr} — {floor.title()} {member.title()}**'
                        tested_str = f'**{count} Tested Panels**' if member.endswith('SLAB') else f'**{count} Tested Elements**'
                        analysis_rows.append([label, tested_str])
                
                builder.ruled_table(
                    ['STRUCTURAL ELEMENT', 'NUMBER TESTED'],
                    analysis_rows,
                    [120, 62],
                    ['L', 'R'],
                )"""

        original_emit = original_emit.replace(analysis_old, analysis_new)

        # B. Modify Section 5 element table to have POINT SPREAD (%) and new column widths
        table_loop_old = """                    # Collect all rows for this floor (all members) before rendering table
                    all_floor_rows = []
                    for member in member_order:
                        group = sorted(
                            [e for e in floor_elements if e['member_type'] == member],
                            key=lambda e: _natural_sort_key(e['element'])
                        )

                        for e in group:
                            rows = e['rows']
                            mid = len(rows) // 2 if len(rows) > 1 else 0
                            # The element's own name (its BIM identity)
                            element_cell = _element_display(e['element'])
                            remark = e['remark']
                            if (remark != 'UNVERIFIED'
                                    and e['spread_pct'] is not None
                                    and e['spread_pct'] > 2.0):
                                remark += (f"\\nUPV VARIANCE BETWEEN POINTS "
                                           f"{e['spread_km_s'] * 1000:.0f} M/S "
                                           f"(±{e['spread_pct'] / 2:.1f}%)")
                            for i, r in enumerate(rows):
                                all_floor_rows.append([
                                    element_cell if i == 0 else '-',
                                    cls._fp(r['path_mm']),
                                    cls._f1(r['transit_us']),
                                    cls._fms(r['velocity_km_s']),
                                    cls._f1(r['ecs_mpa']),
                                    cls._f1(e['mean_ecs']) if i == mid else '-',
                                    remark if i == mid else '-',
                                ])

                    # Render heading and table once per floor with all members
                    if member_order:
                        member_label = member_order[0] if len(member_order) == 1 else 'STRUCTURAL ELEMENTS'
                    else:
                        member_label = 'STRUCTURAL ELEMENTS'
                    if member_label in ('STRUCTURAL ELEMENT', 'STRUCTURAL ELEMENTS'):
                        member_label = 'STRUCTURAL ELEMENTS'
                    elif not member_label.endswith('S'):
                        member_label = f'{member_label}S'
                    builder.subheading(
                        f'{floor.upper()} {member_label}'
                        + (f' OF {project.name.upper()}'
                           if project.name else ''))
                    builder.ruled_table(
                        ['Structural Element', 'PATH LENGTH',
                         'TRANSIT TIME', 'PULSE VELOCITY (M/S)',
                         'E.C.S',
                         'AVERAGE COMPRESSIVE STRENGTH (N/mm2)',
                         'REMARK'],
                        all_floor_rows,
                        # REMARK now carries 'UNVERIFIED' as well as
                        # 'GOOD' / 'POOR', and at its old width the
                        # renderer broke that word mid-character
                        # ("UNVERIFIE" / "D").
                        #
                        # 21 + 17 + 21 + 25 + 17 + 36 + 45 = 182
                        [21, 17, 21, 25, 17, 36, 45],
                        ['C', 'C', 'C', 'C', 'C', 'C', 'C']
                    )"""

        table_loop_new = """                    all_floor_rows = []
                    for member in member_order:
                        group = sorted(
                            [e for e in floor_elements if e['member_type'] == member],
                            key=lambda e: _natural_sort_key(e['element'])
                        )
                        for e in group:
                            rows = e['rows']
                            mid = len(rows) // 2 if len(rows) > 1 else 0
                            element_cell = _element_display(e['element'])
                            remark = f"**{e['remark']}**" if e['remark'] else "-"
                            
                            flag_text = ""
                            spread_str = "-"
                            if e.get('flagged_spread'):
                                flag_text = "\\n**FLAGGED FOR FURTHER INSPECTION — POINT SPREAD EXCEEDS ACCEPTABLE LIMIT.**"
                            
                            point_vels = [r['velocity_km_s'] for r in rows if r.get('velocity_km_s') is not None]
                            if len(point_vels) > 1 and e['mean_v']:
                                total_spread = (max(point_vels) - min(point_vels)) / e['mean_v'] * 100
                                spread_str = f"{total_spread:.1f}%"
                            
                            if (e['remark'] != 'UNVERIFIED' and e['spread_pct'] is not None and e['spread_pct'] > 2.0):
                                remark += (f"\\nUPV VARIANCE BETWEEN POINTS {e['spread_km_s'] * 1000:.0f} M/S (±{e['spread_pct'] / 2:.1f}%)")
                            remark += flag_text
                            
                            for i, r in enumerate(rows):
                                all_floor_rows.append([
                                    element_cell if i == 0 else '-',
                                    cls._fp(r['path_mm']),
                                    cls._f1(r['transit_us']),
                                    cls._fms(r['velocity_km_s']),
                                    spread_str if i == mid else '-',
                                    cls._f1(e['mean_ecs']) if i == mid else '-',
                                    remark if i == mid else '-',
                                ])

                    if member_order:
                        member_label = member_order[0] if len(member_order) == 1 else 'STRUCTURAL ELEMENTS'
                    else:
                        member_label = 'STRUCTURAL ELEMENTS'
                    if member_label in ('STRUCTURAL ELEMENT', 'STRUCTURAL ELEMENTS'):
                        member_label = 'STRUCTURAL ELEMENTS'
                    elif not member_label.endswith('S'):
                        member_label = f'{member_label}S'
                    builder.subheading(
                        f'{floor.upper()} {member_label}'
                        + (f' OF {project.name.upper()}'
                           if project.name else ''))
                    
                    builder.ruled_table(
                        ['STRUCTURAL\\nELEMENT', 'PATH\\nLENGT\\nH',
                         'TRANSI\\nT\\nTIME', 'PULSE\\nVELOCITY\\n(M/S)',
                         'POINT\\nSPREAD\\n(%)',
                         'AVERAGE\\nCOMPRESSIVE\\nSTRENGTH\\n(N/mm2)',
                         'REMARK'],
                        all_floor_rows,
                        [22, 12, 12, 17, 17, 22, 80],
                        ['L', 'C', 'C', 'C', 'C', 'C', 'L']
                    )"""

        # There is a risk that table_loop_old doesn't match perfectly. Let's just regex replace the for-loop to end of ruled_table
        import re
        original_emit = re.sub(
            r'                    # Collect all rows for this floor.*?\[\'C\', \'C\', \'C\', \'C\', \'C\', \'C\', \'C\'\]\n                    \)',
            table_loop_new.replace('\\', '\\\\'),
            original_emit,
            flags=re.DOTALL
        )

        # We also need to replace the NEW emit logic currently in the file!
        current_emit_match = re.search(r'(# ---- Summary of Test Analysis: each structural member listed.*?builder\.ruled_table\(\n.*?\[\'STRUCTURAL\\nELEMENT\', \'MEAN\\nVELOCITY \(m/s\)\', \'EST\.\\nSTRENGTH \(N/mm2\)\', \'95% CONFIDENCE\\nINTERVAL\', \'DATA QUALITY\\nRATING\'\],\n.*?\[25, 25, 25, 34, 50\],\n.*?\[\'C\', \'C\', \'C\', \'C\', \'L\'\]\n.*?                    \))', code, re.DOTALL)
        
        if current_emit_match:
            code = code.replace(current_emit_match.group(1), original_emit)
        else:
            print("ERROR: current_emit_match not found! Falling back to simpler regex.")
            # Let's just find `# ---- Summary of Test Analysis` until `# ---- Summary of Test Results: GOOD / POOR per member`
            alt_match = re.search(r'(# ---- Summary of Test Analysis.*?)(?=                # ---- Summary of Test Results)', code, re.DOTALL)
            if alt_match:
                # Need to append `                # ---- Summary of Test Results` or preserve it
                code = code.replace(alt_match.group(1), original_emit + '\n\n')
            else:
                print("ERROR: alt_match not found!")

    with open('apps/reports/ndt_reports.py', 'w', encoding='utf-8') as f:
        f.write(code)

patch()
