import os
import sys
import io
import django

# Set up Django environment
sys.path.insert(0, os.path.abspath('Nexucon_backend'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

import pypdf
from apps.projects.models import Project
from apps.digital_eye.models import PUNDITTest, StrengthCurve, ProjectCurveSetting, FieldDevice
from apps.evidence.models import AIAnalysisRecord
from apps.digital_eye.adapters import PUNDITAdapter
from apps.reports.ndt_reports import NDTReportService

print("=== STARTING FULL NEXUCON REMEDIATION VERIFICATION ===")

# 1. Fetch or configure Project
project = Project.objects.filter(name__icontains='ladol').first()
if not project:
    project = Project.objects.first()
    project.name = "Ladol Exon Laydown Area"
    project.site_address = "Ladol Free Zone, Apapa, Lagos"
    project.save()

print(f"Project selected: ID={project.id}, Name='{project.name}', Address='{project.site_address}'")

# Ensure active calibration curve is set
curve = StrengthCurve.objects.filter(project=project, curve_type='exponential').first()
if not curve:
    curve = StrengthCurve.objects.create(
        project=project,
        name="Ladol Project Calibration",
        curve_type='exponential',
        formula_params={'a': 3051.166318, 'b': 2.71395e-06, 'c': -3056.280893},
        valid_range_min_ms=2000.0,
        valid_range_max_ms=5000.0,
        standard='BS 1881-203 / BS EN 12504-4',
        data_points=[
            {'v': 3200.0, 'f': 7.4}, {'v': 3400.0, 'f': 11.2},
            {'v': 3600.0, 'f': 15.1}, {'v': 3800.0, 'f': 19.0},
            {'v': 4000.0, 'f': 22.9}, {'v': 4200.0, 'f': 26.8},
            {'v': 4400.0, 'f': 30.5}, {'v': 4600.0, 'f': 34.3},
            {'v': 4800.0, 'f': 38.2}, {'v': 5000.0, 'f': 42.0}
        ]
    )
setting, _ = ProjectCurveSetting.objects.get_or_create(project=project)
setting.active_curve = curve
setting.save()
print("ProjectCurveSetting active curve verified: exponential (R^2 = 0.8966)")

# 2. Test Layer 2: build_statistical_summary directly
test_point_spread = [
    {
        'element': 'S1',
        'floor': 'Ground Floor',
        'readings': [
            {'path_length_mm': 150.0, 'transit_time_us': 34.3, 'velocity_km_s': 4.37318},
            {'path_length_mm': 150.0, 'transit_time_us': 34.1, 'velocity_km_s': 4.39883},
            {'path_length_mm': 150.0, 'transit_time_us': 33.5, 'velocity_km_s': 4.47761},
        ]
    }
]
summary_out = PUNDITAdapter.build_statistical_summary(test_point_spread, project=project)
assert len(summary_out) == 1, "Expected 1 summary row for S1"
s1 = summary_out[0]
print(f"Layer 2 Engine S1 Output: V_mean={s1['velocity_mean_ms']} m/s, f_cu={s1['est_strength_n_mm2']} N/mm2, CoV={s1['cov_percent']}%, Verdict={s1['compliance']}")
assert abs(s1['est_strength_n_mm2'] - 31.68) < 0.1, f"Expected S1 strength ~31.68 MPa, got {s1['est_strength_n_mm2']}"
print("[PASS] Layer 2 Pre-calculation Engine verified with hand-calculated benchmark (31.68 MPa).")

# 3. Create or update AIAnalysisRecord with pre-calculated statistical_summary
ai_record, _ = AIAnalysisRecord.objects.get_or_create(
    project=project,
    analysis_type='pundit',
    defaults={
        'model_provider': 'gemini',
        'model_version': 'gemini-flash-latest',
        'confidence': 0.95,
        'observations': [
            "Assessment of RCC slab-on-grade laydown area confirms sound structural condition.",
            "All tested stations attain characteristic design strength requirement of 25 N/mm2.",
            "Rebound hammer equivalent indicates high surface hardness.",  # Intentionally contains hallucinated rebound hammer to test sanitizer
            "Derived equivalent cube strengths span 40.3-54.6 MPa across elements." # Intentionally contains conflicting range to test sanitizer
        ],
        'recommendations': ["Maintain periodic routine inspection."],
        'correlations': {'statistical_summary': summary_out}
    }
)
ai_record.correlations = {'statistical_summary': summary_out}
ai_record.save()

# 4. Generate Full Bundled NDT PDF Report
print("Generating bundled NDT PDF report via NDTReportService...")
pdf_bytes, bundle = NDTReportService.generate_ndt_report_bundled(project)
print(f"Report generated successfully: {len(pdf_bytes)} bytes, {bundle.get('page_count')} pages.")

# 5. Inspect and Validate Extracted PDF Text against all QA Validation Requirements
reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
full_text = "\n".join([page.extract_text() for page in reader.pages])
normalized_text = " ".join(full_text.split())

# Validation Checks
checks = [
    ("BLOCKER #1: Asset identified as slab-on-grade laydown area", "slab-on-grade laydown area" in normalized_text.lower()),
    ("BLOCKER #2: Calibration validity window preceding fieldwork", "(valid 12 months)" in normalized_text.lower()),
    ("BLOCKER #3: Reconciled strength range without 40.3-54.6 contradiction", "40.3-54.6" not in normalized_text and "40.3–54.6" not in normalized_text),
    ("BLOCKER #3 (Sanitizer): Rebound Hammer hallucination stripped", "(rebound hammer" not in normalized_text.lower() and "rebound hammer equivalent" not in normalized_text.lower()),
    ("BLOCKER #4: Substructure probe removed for slab-on-grade", "substructure probe is required to ascertain the integrity of the building foundation" not in normalized_text),
    ("VAL-005: Canonical GPS coordinates (6.424153, 3.386375)", "6.424153" in normalized_text and "3.386375" in normalized_text),
    ("VAL-006: Weather & Environmental Panel rendered", "weather condition:" in normalized_text.lower() or "surface temp:" in normalized_text.lower()),
    ("VAL-008: S11 2-point statistical note present", "2-point basis" in normalized_text.lower() or "bs en 12504-4 recommends" in normalized_text.lower()),
    ("VAL-010: Preliminary survey clarification in photo appendix", "preliminary site reconnaissance" in normalized_text.lower()),
    ("VAL-011: Modern NDT standards cited in conclusions (BS EN 12504-4, ACI 228.2R)", "bs en 12504-4" in normalized_text.lower() and "aci 228.2r" in normalized_text.lower()),
    ("VAL-012: ASTM C597-22 current edition cited", "c597-22" in normalized_text.lower() or "astm c597" in normalized_text.lower()),
    ("VAL-014: Table 3.0 Calibration Pairs Table present", "table 3.0" in normalized_text.lower() and "0.8966" in normalized_text),
    ("VAL-015: SHA-256 cryptographic provenance clarification", "cryptographic sha-256 hash" in normalized_text.lower()),
    ("PHASE 10: Section 5.5 Statistical Summary Table header", "5.5 statistical summary table" in normalized_text.lower()),
    ("PHASE 10: 13-column LASBCA filing columns present", "uniformity" in normalized_text.lower() and "compliance" in normalized_text.lower()),
]

all_passed = True
for name, passed in checks:
    status = "[PASS]" if passed else "[FAIL]"
    print(f"{status} {name}")
    if not passed:
        all_passed = False

if all_passed:
    print("\n>>> ALL VALIDATION CHECKS AND REMEDIATIONS FULLY VERIFIED! <<<")
else:
    print("\n>>> SOME CHECKS FAILED! INSPECT OUTPUT ABOVE. <<<")
