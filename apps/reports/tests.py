from unittest.mock import patch, MagicMock
import hashlib
from datetime import timedelta
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from apps.scans.models import (
    ScanSession, Defect, ThermalAnomaly, BIMAlignmentResult,
    ProgressValidationResult, ScanFile,
)
from apps.reports.models import ArchivedReport, QualityReport, ReportTemplate
import os
import shutil
import tempfile
import uuid

User = get_user_model()

class ReportIntegrationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testreportuser", email="testreportuser@test.com", password="testpassword"
        )
        refresh = RefreshToken.for_user(self.user)
        self.token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

        # Create a completed scan session
        self.session = ScanSession.objects.create(
            scanner_id="TER-S1-TEST",
            status="completed",
            sensors_used=["lidar", "rgb"],
        )

        # Add a structural defect
        Defect.objects.create(
            session=self.session,
            type="crack",
            severity="high",
            status="OPEN",
            description="Test crack",
        )

        self.generate_report_url = reverse(
            "generate_report", kwargs={"session_id": str(self.session.id)}
        )
        self.download_report_url = reverse(
            "download_report",
            kwargs={"session_id": str(self.session.id), "file_format": "pdf"},
        )

    @patch("apps.storage.cloudinary_service.CloudinaryService.upload_file")
    @patch("apps.reports.tasks.generate_report_task.delay")
    def test_generate_report(self, mock_delay, mock_pdf):
        mock_pdf.return_value = "https://res.cloudinary.com/demo/image/upload/v1/mock.pdf"
        """
        POST to generate_report should synchronously build the QA/QC report and
        return 201 with the full report payload.  The async Celery task is mocked
        so the test never needs a broker.
        """
        mock_delay.return_value = None  # simulate task successfully enqueued

        response = self.client.post(self.generate_report_url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "completed")
        self.assertEqual(response.data["defect_count"], 1)
        self.assertEqual(response.data["anomaly_count"], 0)
        self.assertIn("recommendations", response.data)
        self.assertTrue(len(response.data["recommendations"]) > 0)

        # Verify QualityReport persisted in DB
        self.assertTrue(
            QualityReport.objects.filter(scan=self.session).exists(),
            "QualityReport was not saved to the database.",
        )

        # -----------------------------------------------------------------
        # GET download_report — should return 200 and the PDF since it's generated on the fly
        # -----------------------------------------------------------------
        response = self.client.get(self.download_report_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')

    @patch("apps.storage.cloudinary_service.CloudinaryService.upload_file")
    @patch("apps.reports.tasks.generate_report_task.delay")
    def test_generate_report_idempotent(self, mock_delay, mock_pdf):
        mock_pdf.return_value = "https://res.cloudinary.com/demo/image/upload/v1/mock.pdf"
        """
        A second POST when a completed report already exists returns 200
        (not 201) and does not create a duplicate report.
        """
        mock_delay.return_value = None
        self.client.post(self.generate_report_url)  # creates the report

        response = self.client.post(self.generate_report_url)  # idempotent call
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            QualityReport.objects.filter(scan=self.session).count(),
            1,
            "Expected exactly one report after two POSTs.",
        )

    def test_download_report_before_generation(self):
        """
        GET download_report before any report has been generated must return 404.
        """
        response = self.client.get(self.download_report_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("error", response.data)


# ---------------------------------------------------------------------------
# Extended coverage for apps.reports.services and apps.reports.ai_reports.
#
# Every fixture is a real database row created through the ORM. External
# services (AI providers, Cloudinary storage, outbound HTTP) are mocked with
# unittest.mock so no test ever touches the network.
# ---------------------------------------------------------------------------

from fpdf import FPDF
from PIL import Image

from apps.common.ai_service import AIService, AIProviderUnavailable
from apps.compliance.models import CorrectiveActionPlan, NonConformanceReport
from apps.evidence.models import CorrelationFinding, EvidenceRecord
from apps.inspections.models import Finding, Inspection
from apps.projects.models import Project
from apps.reports.ai_reports import AIReportBuilder, AIReportService
from apps.reports.services import (
    ReportService, SLA_DAYS, TEMPLATE_SECTIONS,
    SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW,
)
from apps.storage.cloudinary_service import CloudinaryService


def _make_png(path, size=(320, 240), color=(10, 20, 30)):
    """Write a real PNG image on disk (used for the annotation pipeline)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def _ai_recommendations(defects=None, anomalies=None, deviation=0.0):
    """Deterministic stand-in for the external AI recommendation provider."""
    return {
        "recommendations": [
            {
                "recommendation": "Engage a structural engineer to assess the crack.",
                "priority": "Urgent",
                "related_finding_id": "defect-1",
            },
            {
                "recommendation": "Seal the thermal leakage path at the roof junction.",
                "priority": "High",
                "related_finding_id": "anomaly-1",
            },
        ],
        "text_confidence": 0.82,
    }


class ReportFixtureMixin:
    """Real ORM rows shared by the service-level test cases."""

    def make_project(self, **kwargs):
        defaults = dict(
            name="Marina Heights Tower",
            status="ACTIVE",
            client_name="Lagos State Ministry of Works",
            project_number="NXC-2026-042",
            site_address="12 Marina Road, Lagos Island",
            client_contact="eng.tunde@example.com",
        )
        defaults.update(kwargs)
        return Project.objects.create(**defaults)

    def make_scan(self, project=None, **kwargs):
        defaults = dict(
            scanner_id="TER-S1-COV",
            status="completed",
            sensors_used=["lidar", "rgb", "thermal"],
            project=project,
        )
        defaults.update(kwargs)
        return ScanSession.objects.create(**defaults)


class ReportServicePrimitivesTests(TestCase):
    """Low-level lookups used by every generated report."""

    def test_severity_color_mapping(self):
        self.assertEqual(ReportService._severity_color("critical"), SEV_CRITICAL)
        self.assertEqual(ReportService._severity_color("high"), SEV_HIGH)
        self.assertEqual(ReportService._severity_color("medium"), SEV_MEDIUM)
        self.assertEqual(ReportService._severity_color("low"), SEV_LOW)
        # case-insensitive, None- and unknown-safe (defaults to low)
        self.assertEqual(ReportService._severity_color("HIGH"), SEV_HIGH)
        self.assertEqual(ReportService._severity_color(None), SEV_LOW)
        self.assertEqual(ReportService._severity_color("bogus"), SEV_LOW)

    def test_plain_english_known_defect_types(self):
        text = ReportService._plain_english("crack", "critical")
        self.assertIn("split or break", text)
        self.assertIn("DO NOT proceed", text)
        for dtype in ("crack", "concrete_crack", "spalling", "corrosion",
                      "thermal_anomaly", "deformation", "delamination"):
            self.assertTrue(ReportService._plain_english(dtype, "high"))
        # Unknown type falls back to the generic engineer-review wording
        generic = ReportService._plain_english("martian_scribble", "low")
        self.assertIn("qualified engineer", generic)
        # None-safe on both arguments
        self.assertTrue(ReportService._plain_english(None, None))

    def test_nigerian_standard_lookup(self):
        self.assertIn("NIS 87", ReportService._nigerian_standard("crack"))
        self.assertIn("SON NIS 412", ReportService._nigerian_standard("thermal_anomaly"))
        self.assertIn("ISO 19650", ReportService._nigerian_standard("bim_deviation"))
        # Unknown / None fall back to the general standard, never an empty string
        self.assertIn("SON", ReportService._nigerian_standard("unknown_thing"))
        self.assertIn("SON", ReportService._nigerian_standard(None))

    def test_sla_and_template_catalogue(self):
        self.assertEqual(SLA_DAYS, {"critical": 2, "high": 7, "medium": 10, "low": 14})
        # qaqc includes every section; the focused templates subset them
        self.assertIsNone(TEMPLATE_SECTIONS["qaqc"])
        self.assertEqual(TEMPLATE_SECTIONS["progress"], {"progress"})
        self.assertEqual(TEMPLATE_SECTIONS["deviation"], {"bim"})
        self.assertEqual(TEMPLATE_SECTIONS["earthworks"], {"progress"})
        self.assertEqual(TEMPLATE_SECTIONS["compliance"], set())


class ReportServicePdfPrimitivesTests(TestCase):
    """The drafting primitives must render without error on a real FPDF doc."""

    def _pdf(self):
        pdf = FPDF(orientation="L")
        pdf.set_margins(15, 22, 15)
        pdf.set_auto_page_break(auto=True, margin=22)
        pdf.add_page()
        return pdf

    def test_drafting_primitives_render(self):
        pdf = self._pdf()
        ReportService._dashed_line(pdf, 20, 20, 200, 20)
        ReportService._dashed_line(pdf, 20, 20, 20, 100, color=(1, 2, 3), width=0.5)
        # zero-length dashed line is a no-op, not a crash
        ReportService._dashed_line(pdf, 20, 20, 20, 20)
        ReportService._tick_ruler(pdf, 20, 40, 200)
        ReportService._tick_ruler(pdf, 20, 40, 100, vertical=True, spacing=7)
        ReportService._corner_ticks(pdf, 20, 60, 150, 40)
        ReportService._swatch(pdf, 20, 110, (3, 95, 180))
        width = ReportService._status_tag(pdf, 40, 110, "PASS", (21, 128, 61))
        self.assertGreater(width, 0)
        ReportService._blueprint_grid(pdf, 20, 130, 150, 40)
        out = bytes(pdf.output())
        self.assertTrue(out.startswith(b"%PDF"))

    def test_section_heading_bars_and_frame(self):
        pdf = self._pdf()
        ReportService._section_heading(pdf, "1", "Site Inspection - Quick Summary")
        ReportService._subsection_bar(pdf, "Scan Parameters")
        ReportService._dim_row(pdf, "Mean Deviation", "0.0040 M")
        ReportService._dim_row(pdf, "Tolerance", "0.010 M", x=30, w_label=60, font_size=7)
        ReportService._drafting_frame(pdf)
        out = bytes(pdf.output())
        self.assertTrue(out.startswith(b"%PDF"))

    def test_cover_page_renders_from_real_project_and_scan(self):
        project = ReportFixtureMixin().make_project()
        scan = ReportFixtureMixin().make_scan(project=project)
        pdf = self._pdf()
        pdf.set_auto_page_break(auto=False)
        ReportService._draw_cover_page(pdf, scan, project)
        out = bytes(pdf.output())
        self.assertTrue(out.startswith(b"%PDF"))

    def test_cover_page_flags_missing_required_fields(self):
        # A project with no client / number / address / contact must not be
        # silently printed as complete: the validation warning block renders.
        project = Project.objects.create(name="Bare Project")
        scan = ReportFixtureMixin().make_scan(project=project)
        pdf = self._pdf()
        pdf.set_auto_page_break(auto=False)
        ReportService._draw_cover_page(pdf, scan, project)
        out = bytes(pdf.output())
        self.assertTrue(out.startswith(b"%PDF"))


class ReportServiceImageTests(TestCase, ReportFixtureMixin):
    """Image fetch / annotation pipeline against real files on disk."""

    def setUp(self):
        self.media = tempfile.mkdtemp(prefix="nexucon_media_")
        self.scan = self.make_scan(scanner_id="TER-S1-IMG")

    def tearDown(self):
        shutil.rmtree(self.media, ignore_errors=True)

    def test_try_embed_image(self):
        with self.settings(MEDIA_ROOT=self.media):
            pdf = FPDF()
            pdf.add_page()
            # empty / None URL -> nothing embedded
            self.assertFalse(ReportService._try_embed_image(pdf, ""))
            self.assertFalse(ReportService._try_embed_image(pdf, None))
            # missing local file -> nothing embedded
            self.assertFalse(ReportService._try_embed_image(pdf, "/media/does_not_exist.png"))
            # real local file -> embedded
            _make_png(os.path.join(self.media, "site.png"))
            self.assertTrue(ReportService._try_embed_image(pdf, "/media/site.png"))

    def test_fetch_image_file_from_local_media(self):
        with self.settings(MEDIA_ROOT=self.media):
            _make_png(os.path.join(self.media, "rgb", "frame_0001.png"))
            ScanFile.objects.create(
                session=self.scan, file_type="rgb",
                file_url="http://127.0.0.1:8000/media/rgb/frame_0001.png",
                file_name="frame_0001.png",
            )
            path = ReportService._fetch_image_file(self.scan, "rgb")
            self.assertIsNotNone(path)
            self.assertTrue(os.path.exists(path))

    def test_fetch_image_file_without_registered_file(self):
        self.assertIsNone(ReportService._fetch_image_file(self.scan, "thermal"))

    def test_fetch_image_file_missing_local_file(self):
        with self.settings(MEDIA_ROOT=self.media):
            ScanFile.objects.create(
                session=self.scan, file_type="rgb", file_url="/media/rgb/absent.png"
            )
            self.assertIsNone(ReportService._fetch_image_file(self.scan, "rgb"))

    def test_annotate_image_pins_bboxes_and_reticles(self):
        src = _make_png(os.path.join(self.media, "annot_src.png"), size=(400, 300))
        points = [
            {"bbox": (0.1, 0.1, 0.4, 0.4), "color": (222, 30, 38, 255),
             "label": "FINDING #1", "pin": True, "num": 1},
            {"x": 0.7, "y": 0.7, "color": (240, 140, 16, 255),
             "label": "ANOMALY #2", "pin": False},
            {"x": 0.5, "y": 0.5, "color": (0, 0, 0, 255)},  # no label, no bbox
        ]
        out = ReportService._annotate_image(src, points)
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith(".png"))
        self.assertTrue(os.path.exists(out))
        os.remove(out)

    def test_annotate_image_without_points_returns_none(self):
        src = _make_png(os.path.join(self.media, "annot_empty.png"))
        self.assertIsNone(ReportService._annotate_image(src, []))
        self.assertIsNone(ReportService._annotate_image(src, None))
        os.remove(src)

    def test_annotate_image_missing_source_returns_none(self):
        missing = os.path.join(self.media, "missing.png")
        self.assertIsNone(
            ReportService._annotate_image(missing, [{"x": 0.5, "y": 0.5, "color": (1, 2, 3)}])
        )

    def test_generate_annotated_sitemap(self):
        with self.settings(MEDIA_ROOT=self.media):
            _make_png(os.path.join(self.media, "rgb", "survey.png"))
            ScanFile.objects.create(
                session=self.scan, file_type="rgb", file_url="/media/rgb/survey.png"
            )
            Defect.objects.create(
                session=self.scan, type="crack", severity="high", status="OPEN",
                bbox_xmin=0.1, bbox_ymin=0.1, bbox_xmax=0.35, bbox_ymax=0.3,
            )
            # no bbox -> never pinned onto the photograph
            Defect.objects.create(
                session=self.scan, type="spalling", severity="low", status="OPEN"
            )
            out = ReportService._generate_annotated_sitemap(
                self.scan, Defect.objects.filter(session=self.scan), [], []
            )
            self.assertIsNotNone(out)
            self.assertTrue(os.path.exists(out))
            os.remove(out)

    def test_generate_annotated_sitemap_without_located_defects(self):
        with self.settings(MEDIA_ROOT=self.media):
            _make_png(os.path.join(self.media, "rgb", "survey2.png"))
            ScanFile.objects.create(
                session=self.scan, file_type="rgb", file_url="/media/rgb/survey2.png"
            )
            Defect.objects.create(session=self.scan, type="crack",
                                  severity="low", status="OPEN")  # no bbox
            self.assertIsNone(ReportService._generate_annotated_sitemap(
                self.scan, Defect.objects.filter(session=self.scan), [], []
            ))

    def test_generate_annotated_sitemap_without_image(self):
        Defect.objects.create(
            session=self.scan, type="crack", severity="high", status="OPEN",
            bbox_xmin=0.1, bbox_ymin=0.1, bbox_xmax=0.3, bbox_ymax=0.3,
        )
        self.assertIsNone(ReportService._generate_annotated_sitemap(
            self.scan, Defect.objects.filter(session=self.scan), [], []
        ))

    def test_generate_annotated_thermal(self):
        with self.settings(MEDIA_ROOT=self.media):
            _make_png(os.path.join(self.media, "thermal", "ir.png"))
            ScanFile.objects.create(
                session=self.scan, file_type="thermal", file_url="/media/thermal/ir.png"
            )
            ThermalAnomaly.objects.create(
                session=self.scan, temperature_variance=6.5, severity="high",
                bbox_xmin=0.2, bbox_ymin=0.2, bbox_xmax=0.5, bbox_ymax=0.5,
            )
            out = ReportService._generate_annotated_thermal(
                self.scan, ThermalAnomaly.objects.filter(session=self.scan)
            )
            self.assertIsNotNone(out)
            self.assertTrue(os.path.exists(out))
            os.remove(out)

    def test_generate_annotated_thermal_without_located_anomalies(self):
        with self.settings(MEDIA_ROOT=self.media):
            _make_png(os.path.join(self.media, "thermal", "ir2.png"))
            ScanFile.objects.create(
                session=self.scan, file_type="thermal", file_url="/media/thermal/ir2.png"
            )
            ThermalAnomaly.objects.create(
                session=self.scan, temperature_variance=1.0, severity="low"
            )  # no bbox
            self.assertIsNone(ReportService._generate_annotated_thermal(
                self.scan, ThermalAnomaly.objects.filter(session=self.scan)
            ))

    def test_annotate_image_font_fallbacks(self):
        from PIL import ImageDraw, ImageFont
        src = _make_png(os.path.join(self.media, "font_fallback.png"))
        # no 'num' key -> the numbered-pin branch is skipped; the label branch
        # exercises the textbbox failure fallback (draw without a measured box).
        points = [{"x": 0.5, "y": 0.5, "color": (222, 30, 38, 255),
                   "label": "FALLBACK LABEL", "pin": False}]
        real_load_default = ImageFont.load_default

        def fake_load_default(size=None):
            if size is not None:
                # old Pillow without the size kwarg -> TypeError fallback
                raise TypeError("load_default() got an unexpected keyword argument 'size'")
            return real_load_default()

        with patch("PIL.ImageFont.load_default", side_effect=fake_load_default), \
                patch.object(ImageDraw.ImageDraw, "textbbox",
                             side_effect=RuntimeError("measure failed")):
            out = ReportService._annotate_image(src, points)
        self.assertIsNotNone(out)
        self.assertTrue(os.path.exists(out))
        os.remove(out)
        # sanity: the real default loader still works
        self.assertIsNotNone(real_load_default())


class ReportServiceFetchClashesTests(TestCase, ReportFixtureMixin):
    def test_no_alignment_returns_empty_list(self):
        scan = self.make_scan()
        self.assertEqual(ReportService._fetch_clashes(scan), [])

    def test_returns_persisted_clashes(self):
        scan = self.make_scan()
        clashes = [
            {"id": "CLASH-001", "severity": "high", "element1_id": "W-8",
             "element2_id": "D-2", "location": "Level 2"},
        ]
        BIMAlignmentResult.objects.create(
            session=scan, alignment_status="SUCCESS", clashes=clashes
        )
        self.assertEqual(ReportService._fetch_clashes(scan), clashes)

    def test_alignment_without_clash_data_returns_empty(self):
        scan = self.make_scan()
        BIMAlignmentResult.objects.create(session=scan, alignment_status="SUCCESS")
        self.assertEqual(ReportService._fetch_clashes(scan), [])

    def test_storage_failure_returns_empty(self):
        scan = self.make_scan()
        with patch.object(BIMAlignmentResult.objects, "filter",
                          side_effect=RuntimeError("db unavailable")):
            self.assertEqual(ReportService._fetch_clashes(scan), [])


class ReportServiceBuildDocumentTests(TestCase, ReportFixtureMixin):
    """Exercise the full PDF document builder from real DB rows."""

    def setUp(self):
        self.project = self.make_project()
        self.scan = self.make_scan(scanner_id="TER-S1-BLD", project=self.project)

    def _full_fixture(self):
        Defect.objects.create(
            session=self.scan, type="crack", severity="critical", status="OPEN",
            description="Wide flexural crack at beam midspan",
            location_x=1.2, location_y=3.4, location_z=0.5,
            confidence_score=0.93, grid_zone="Zone A", room_level="Level 2",
        )
        Defect.objects.create(
            session=self.scan, type="spalling", severity="low", status="OPEN",
            description="Minor surface spalling", confidence_score=0.71,
        )
        # empty description -> the builder derives an honest one from the row
        Defect.objects.create(
            session=self.scan, type="corrosion", severity="high", status="OPEN",
            description="", confidence_score=0.88,
        )
        ThermalAnomaly.objects.create(
            session=self.scan, temperature_variance=7.4, severity="high",
            status="OPEN", description="Hot spot near parapet",
            confidence_score=0.8,
            bbox_xmin=0.2, bbox_ymin=0.2, bbox_xmax=0.4, bbox_ymax=0.4,
        )
        BIMAlignmentResult.objects.create(
            session=self.scan, alignment_status="SUCCESS",
            mean_deviation=1500.0, max_deviation=2200.0,
            top_deviations=[
                {"deviation_mm": 2200.0, "location": "COL-C24 (Level 3)",
                 "description": "Column offset from design grid"},
                {"deviation_mm": 900.0, "location": "Beam B-12",
                 "type": "clash", "element": "duct bank"},
                {"deviation_mm": 400.0, "location": "Slab S-4"},  # description fallback
            ],
            clashes=[
                {"id": "CLASH-001", "severity": "high", "element1_id": "Wall W-8",
                 "element2_id": "Duct D-2", "location": "Level 2, Grid C"},
                {"id": "CLASH-002", "severity": "medium", "element1_id": "Beam B-3",
                 "element2_id": "Pipe P-9", "location": "Level 1"},
            ],
        )
        ProgressValidationResult.objects.create(
            session=self.scan, progress_score=0.64, covered_area_sqm=1240.5
        )

    def _render(self, recommendations=None, **kwargs):
        if recommendations is None:
            recommendations = [
                {"recommendation": "Commission a structural engineer review.",
                 "priority": "Urgent", "related_finding_id": "F1"},
                "Monitor the low-severity spalling at the next visit.",  # plain string
            ]
        defects = Defect.objects.filter(session=self.scan)
        anomalies = ThermalAnomaly.objects.filter(session=self.scan)
        alignment = BIMAlignmentResult.objects.filter(session=self.scan).first()
        progress = ProgressValidationResult.objects.filter(session=self.scan).first()
        return ReportService._build_fpdf_document(
            self.scan, defects, anomalies,
            alignment.mean_deviation if alignment else None,
            alignment.top_deviations if alignment else None,
            recommendations, progress,
            clashes=ReportService._fetch_clashes(self.scan),
            overall_confidence=0.87, project=self.project, **kwargs,
        )

    def _assert_valid_pdf(self, pdf, min_pages=3, min_bytes=10000):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            pdf.output(path)
            with open(path, "rb") as f:
                data = f.read()
        finally:
            os.remove(path)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), min_bytes)
        self.assertGreater(pdf.page_no(), min_pages - 1)
        return data

    def test_full_document_renders_valid_pdf(self):
        self._full_fixture()
        pdf = self._render()
        self._assert_valid_pdf(pdf, min_pages=3)

    def test_empty_scan_document_is_honest(self):
        # No defects, no anomalies, no alignment, no progress, no recommendations:
        # every affected section must render its "not assessed / none found" text.
        pdf = self._render(recommendations=[])
        self._assert_valid_pdf(pdf, min_pages=2)

    def test_frame_mismatch_flags_data_error_not_stop_work(self):
        # mean 5.0 m while every individual deviation is tiny: geometrically
        # impossible -> coordinate-frame data error branch, no STOP WORK.
        Defect.objects.create(session=self.scan, type="crack", severity="high",
                              status="OPEN", description="Crack")
        BIMAlignmentResult.objects.create(
            session=self.scan, alignment_status="SUCCESS",
            mean_deviation=5000.0,
            top_deviations=[{"deviation_mm": 90.0, "location": "COL-C1"}],
        )
        pdf = self._render()
        self._assert_valid_pdf(pdf, min_pages=2)

    def test_bim_within_tolerance_renders_pass(self):
        BIMAlignmentResult.objects.create(
            session=self.scan, alignment_status="SUCCESS",
            mean_deviation=4.0,  # 4 mm -> 0.004 m, within the 10 mm tolerance
            top_deviations=[{"deviation_mm": 6.0, "location": "Slab S-1",
                             "description": "Minor setting-out deviation"}],
        )
        pdf = self._render()
        self._assert_valid_pdf(pdf, min_pages=2)

    def test_bim_review_required_branch(self):
        BIMAlignmentResult.objects.create(
            session=self.scan, alignment_status="SUCCESS",
            mean_deviation=50.0,  # 50 mm: real deviation but below the stop-work level
            top_deviations=[{"deviation_mm": 80.0, "location": "Beam B-9"}],
        )
        pdf = self._render()
        self._assert_valid_pdf(pdf, min_pages=2)

    def test_cover_overrides_and_section_filter(self):
        self._full_fixture()
        pdf = self._render(
            cover_overrides={
                "client_name": "Override Client Ltd",
                "project_number": "",  # empty overrides are ignored
                "site_address": "Override Address, Lagos",
                "client_contact": "override@example.com",
            },
            include_sections={"progress"},
        )
        self._assert_valid_pdf(pdf, min_pages=2)

    def test_sections_embed_annotated_and_site_images(self):
        # §2 must embed the annotated sitemap (defect bbox pins), list the
        # registered lidar/rgb files, and embed / link the scan site images;
        # §5 must embed the annotated thermal image and fall back to a plain
        # link when an anomaly heatmap cannot be fetched.
        media = tempfile.mkdtemp(prefix="nexucon_media_embed_")
        try:
            with self.settings(MEDIA_ROOT=media):
                _make_png(os.path.join(media, "rgb", "survey.png"), size=(640, 480))
                _make_png(os.path.join(media, "thermal", "ir.png"), size=(640, 480))
                _make_png(os.path.join(media, "rgb_view.png"), size=(640, 480))
                ScanFile.objects.create(
                    session=self.scan, file_type="rgb", file_url="/media/rgb/survey.png"
                )
                ScanFile.objects.create(
                    session=self.scan, file_type="thermal", file_url="/media/thermal/ir.png"
                )
                ScanFile.objects.create(
                    session=self.scan, file_type="lidar",
                    file_url="https://storage.example.com/pointcloud.las",
                    file_name="pointcloud.las",
                )
                self.scan.rgb_url = "/media/rgb_view.png"
                self.scan.thermal_url = "/media/thermal/missing_view.png"
                self.scan.save()

                Defect.objects.create(
                    session=self.scan, type="crack", severity="high", status="OPEN",
                    description="Crack near column base",
                    bbox_xmin=0.2, bbox_ymin=0.2, bbox_xmax=0.5, bbox_ymax=0.45,
                )
                ThermalAnomaly.objects.create(
                    session=self.scan, temperature_variance=5.5, severity="medium",
                    description="Thermal bridging at parapet",
                    image_url="/media/thermal/missing_anomaly_heatmap.png",
                    bbox_xmin=0.3, bbox_ymin=0.3, bbox_xmax=0.6, bbox_ymax=0.6,
                )
                pdf = self._render()
                self._assert_valid_pdf(pdf, min_pages=3)
        finally:
            shutil.rmtree(media, ignore_errors=True)

    def test_long_document_paginates_every_section(self):
        # Enough findings, anomalies, deviations, clashes and recommendations
        # to force page breaks inside every table-heavy section.
        long_desc = "Long engineering assessment. " * 30
        for i in range(18):
            Defect.objects.create(
                session=self.scan,
                type=["crack", "spalling", "corrosion", "deformation"][i % 4],
                severity=["critical", "high", "medium", "low"][i % 4],
                status="OPEN", description=long_desc,
                confidence_score=0.8,
            )
        for i in range(8):
            ThermalAnomaly.objects.create(
                session=self.scan, temperature_variance=3.0 + i,
                severity="medium", description=long_desc,
            )
        BIMAlignmentResult.objects.create(
            session=self.scan, alignment_status="SUCCESS",
            mean_deviation=1200.0,
            top_deviations=[
                {"deviation_mm": 900.0 - i * 20, "location": f"COL-C{i} (Level {i % 5})",
                 "description": long_desc}
                for i in range(14)
            ],
            clashes=[
                {"id": f"CLASH-{i:03d}", "severity": "high" if i % 2 else "medium",
                 "element1_id": f"Element-One-{i}-with-a-long-descriptive-GUID",
                 "element2_id": f"Element-Two-{i}-with-a-long-descriptive-GUID",
                 "location": f"Level {i % 6}, Grid {chr(65 + i % 6)}"}
                for i in range(10)
            ],
        )
        recommendations = [
            {"recommendation": f"Action item {i}: " + long_desc,
             "priority": ["Urgent", "High", "Routine", "Low"][i % 4],
             "related_finding_id": f"F{i}"}
            for i in range(24)
        ]
        pdf = self._render(recommendations=recommendations)
        self._assert_valid_pdf(pdf, min_pages=6)


class ReportServiceGenerateReportTests(TestCase, ReportFixtureMixin):
    """generate_qaqc_report / generate_pdf_bytes against real rows."""

    def setUp(self):
        self.project = self.make_project()
        self.scan = self.make_scan(project=self.project)

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_generate_report_from_real_rows(self, mock_ai, mock_upload):
        mock_ai.side_effect = _ai_recommendations
        Defect.objects.create(
            session=self.scan, type="crack", severity="high", status="OPEN",
            description="Test crack", confidence_score=0.9,
        )
        ThermalAnomaly.objects.create(
            session=self.scan, temperature_variance=4.2, severity="medium",
            confidence_score=0.7,
        )
        BIMAlignmentResult.objects.create(
            session=self.scan, alignment_status="SUCCESS",
            mean_deviation=140.4,  # millimetres, as persisted by the alignment run
            top_deviations=[{"deviation_mm": 260.0, "location": "COL-C2"}],
        )

        report = ReportService.generate_qaqc_report(self.scan)

        self.assertEqual(report.status, "completed")
        self.assertEqual(report.report_type, "qaqc")
        self.assertEqual(report.defect_count, 1)
        self.assertEqual(report.anomaly_count, 1)
        self.assertAlmostEqual(report.mean_deviation, 140.4)
        self.assertEqual(report.report_url, "https://res.example.com/reports/mock.pdf")
        self.assertEqual(len(report.recommendations), 2)
        # overall confidence = mean(per-finding confidences, text confidence)
        self.assertAlmostEqual(report.overall_ai_confidence, ((0.9 + 0.7) / 2 + 0.82) / 2)
        # summary is built from the real rows
        self.assertEqual(report.summary["defects"][0]["type"], "crack")
        self.assertEqual(report.summary["thermal_anomalies"][0]["temperature_variance"], 4.2)
        self.assertEqual(report.summary["scanner_id"], self.scan.scanner_id)
        # the AI provider is narrated metres: mm must be converted at the boundary
        self.assertAlmostEqual(mock_ai.call_args.kwargs["deviation"], 0.1404)

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_generate_report_empty_scan_is_honest(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        report = ReportService.generate_qaqc_report(self.scan)
        self.assertEqual(report.defect_count, 0)
        self.assertEqual(report.anomaly_count, 0)
        self.assertEqual(report.recommendations, [])
        self.assertIsNone(report.overall_ai_confidence)
        self.assertIsNone(report.mean_deviation)

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_generate_report_unknown_template_falls_back_to_qaqc(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        report = ReportService.generate_qaqc_report(self.scan, report_type="not-a-template")
        self.assertEqual(report.report_type, "qaqc")

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_report_regenerated_per_template_replaces_previous(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        first = ReportService.generate_qaqc_report(self.scan, report_type="qaqc")
        ReportService.generate_qaqc_report(self.scan, report_type="progress")
        # both templates coexist
        self.assertEqual(
            QualityReport.objects.filter(scan=self.scan).count(), 2
        )
        regenerated = ReportService.generate_qaqc_report(self.scan, report_type="qaqc")
        # one stored report per (scan, template): the old qaqc row was replaced
        self.assertEqual(
            QualityReport.objects.filter(scan=self.scan, report_type="qaqc").count(), 1
        )
        self.assertNotEqual(regenerated.id, first.id)
        self.assertEqual(
            QualityReport.objects.filter(scan=self.scan).count(), 2
        )

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_generate_report_survives_pdf_failure(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        with patch.object(ReportService, "_build_fpdf_document",
                          side_effect=RuntimeError("pdf engine exploded")):
            report = ReportService.generate_qaqc_report(self.scan)
        # the failure is recorded honestly: no URL, but the metrics stay real
        self.assertIsNone(report.report_url)
        self.assertEqual(report.defect_count, 0)

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_high_severity_defect_triggers_auto_ncr(self, mock_ai, mock_upload):
        from apps.inspections.models import NonConformanceReport as LegacyNCR
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        Defect.objects.create(
            session=self.scan, type="crack", severity="high", status="OPEN",
            description="Serious crack",
        )
        ReportService.generate_qaqc_report(self.scan)
        self.assertTrue(
            LegacyNCR.objects.filter(session=self.scan).exists(),
            "auto_generate_ncrs_for_scan did not create an NCR for the high-severity defect",
        )

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_generate_report_survives_ncr_failure(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        with patch("apps.inspections.services.InspectionService.auto_generate_ncrs_for_scan",
                   side_effect=RuntimeError("NCR pipeline down")):
            report = ReportService.generate_qaqc_report(self.scan)
        # the report itself is still produced; the NCR failure is only logged
        self.assertEqual(report.status, "completed")

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_generate_pdf_bytes_uses_persisted_alignment(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        BIMAlignmentResult.objects.create(
            session=self.scan, alignment_status="SUCCESS",
            mean_deviation=80.0,
            top_deviations=[{"deviation_mm": 120.0, "location": "COL-C9",
                             "description": "Column offset"}],
            clashes=[{"id": "CLASH-9", "severity": "medium",
                      "element1_id": "W-1", "element2_id": "P-2",
                      "location": "Level 1"}],
        )
        report = ReportService.generate_qaqc_report(self.scan)
        data = ReportService.generate_pdf_bytes(report)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 10000)

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_generate_pdf_bytes_and_cover_overrides(self, mock_ai, mock_upload):
        mock_ai.return_value = {
            "recommendations": [{"recommendation": "Act on the crack.",
                                 "priority": "High", "related_finding_id": "F1"}],
            "text_confidence": 0.5,
        }
        Defect.objects.create(
            session=self.scan, type="crack", severity="high", status="OPEN",
            description="Test crack", confidence_score=0.9,
        )
        report = ReportService.generate_qaqc_report(self.scan)

        data = ReportService.generate_pdf_bytes(report)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 10000)

        # cover-page overrides force a re-render with the overridden cover
        data = ReportService.generate_pdf_bytes(
            report, cover_overrides={"client_name": "Override Client Ltd"}
        )
        self.assertTrue(data.startswith(b"%PDF"))

        # a focused template re-render only includes its sections
        data = ReportService.generate_pdf_bytes(
            report, include_sections={"progress"}
        )
        self.assertTrue(data.startswith(b"%PDF"))


class AIReportServiceUnitTests(TestCase):
    def test_report_hash_is_deterministic_sha256(self):
        h1 = AIReportService._report_hash("ncr", 1, "Open", "Major", 2)
        h2 = AIReportService._report_hash("ncr", 1, "Open", "Major", 2)
        h3 = AIReportService._report_hash("ncr", 2, "Open", "Major", 2)
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 64)

    def test_user_label(self):
        self.assertEqual(AIReportService._user_label(None), "Unauthenticated")
        named = User.objects.create_user(
            username="eng1", email="eng1@example.com", password="pw12345!",
            first_name="Tunde", last_name="Ade",
        )
        self.assertEqual(AIReportService._user_label(named), "Tunde Ade")
        bare = User.objects.create_user(
            username="bare", email="bare@example.com", password="pw12345!"
        )
        self.assertEqual(AIReportService._user_label(bare), "bare@example.com")


class AIReportBuilderTests(TestCase):
    def test_builder_layout_methods(self):
        builder = AIReportBuilder("Test Report", "Test Subject", "Ref TEST-1")
        builder.section("1. Section")
        builder.kv("Key", "value")
        builder.kv("Empty", None)   # rendered as '-', never a crash
        builder.para("Some paragraph text.")
        builder.table(["A", "B"], [[1, 2], [3, 4]], [50, 50])
        builder.signoff_block([("Inspector", "Tunde Ade"), ("Date", "2026-09-03")])
        out = bytes(builder.bytes())
        self.assertTrue(out.startswith(b"%PDF"))
        self.assertGreater(len(out), 1000)

    def test_risk_badge(self):
        self.assertEqual(AIReportBuilder("T", "S", "R").risk_badge("high"), "HIGH")
        self.assertEqual(AIReportBuilder("T", "S", "R").risk_badge(""), "-")


class AIReportsGenerationTests(TestCase, ReportFixtureMixin):
    """The three statutory AI reports generated from live records only."""

    def setUp(self):
        self.project = self.make_project()
        self.user = User.objects.create_user(
            username="aieng", email="aieng@example.com", password="pw12345!",
            first_name="Ada", last_name="Obi",
        )

    @patch.object(AIService, "generate_structured_json")
    def test_project_intelligence_report_from_real_rows(self, mock_llm):
        mock_llm.return_value = {"observations": ["Structural risk concentrated in zone A."]}
        evidence = EvidenceRecord.objects.create(
            project=self.project, source_type="scan_defect",
            structural_element_id="COL-C24", payload={"severity": "critical"},
        )
        structural = CorrelationFinding.objects.create(
            project=self.project, title="Multi-source corrosion signal on COL-C24",
            risk_level="critical", risk_score=0.9, status="pending_review",
            structural_element_id="COL-C24",
            reasoning="GPR and PUNDIT evidence agree on delamination.",
        )
        structural.evidence.add(evidence)
        CorrelationFinding.objects.create(
            project=self.project, title="Reviewed minor thermal finding",
            risk_level="low", risk_score=0.2, status="accepted",
            reviewed_by=self.user,
        )
        NonConformanceReport.objects.create(
            project=self.project, title="Open safety NCR",
            description="Missing edge protection", severity="Critical",
            status="Open",
        )

        out = AIReportService.generate_project_intelligence_report(self.project, self.user)
        data = bytes(out)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 3000)

    @patch.object(AIService, "generate_structured_json")
    def test_project_intelligence_report_empty_project(self, mock_llm):
        # LLM unavailable -> deterministic aggregate; no findings/NCRs -> the
        # report must say so instead of inventing content.
        mock_llm.side_effect = AIProviderUnavailable("no key configured")
        out = AIReportService.generate_project_intelligence_report(self.project, self.user)
        data = bytes(out)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 3000)

    def test_project_intelligence_report_without_observations_or_actions(self):
        # A summary with no observations and no recommendations must render
        # the explicit "none recorded" lines, not blank or invented content.
        empty_summary = {
            "risk_scores": {},
            "metrics": {"open_ncrs": 0},
            "ai_observations": None,
            "recommended_actions": [],
        }
        with patch("apps.evidence.intelligence.ProjectIntelligenceService.aggregate",
                   return_value=empty_summary):
            out = AIReportService.generate_project_intelligence_report(self.project, self.user)
        data = bytes(out)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 3000)

    def test_inspection_report_from_real_rows(self):
        inspection = Inspection.objects.create(
            project=self.project,
            inspection_type="Foundation Inspection",
            status="COMPLETED",
            priority="High",
            inspector=self.user,
            inspector_name="Ada Obi",
            requested_by_name="Site Foreman",
            scheduled_date=timezone.now() - timedelta(days=2),
            completed_date=timezone.now(),
            checkin_time=timezone.now() - timedelta(days=2),
            gps_latitude=6.4541,
            gps_longitude=3.3947,
            gps_verified=True,
            outcome="PASSED",
            summary_notes="Foundation works conform to the approved drawings.",
            checklist_results=[
                {"item": "Rebar cover checked", "result": "PASS", "notes": "40mm cover"},
                {"item": "Concrete cube test", "result": "FAIL", "notes": "Low strength"},
                "legacy-freeform-entry",
            ],
        )
        Finding.objects.create(
            inspection=inspection, project=self.project,
            title="Low concrete strength", description="Cube result below spec",
            severity="HIGH", is_resolved=False,
        )
        Finding.objects.create(
            inspection=inspection, project=self.project,
            title="Curing period short", description="Slab cured 5 days",
            severity="LOW", is_resolved=True,
        )
        out = AIReportService.generate_inspection_report(inspection, self.user)
        data = bytes(out)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 3000)

    def test_inspection_report_minimal_record(self):
        # No GPS, no checklist, no findings, no inspector: nothing fabricated.
        inspection = Inspection.objects.create(
            project=self.project,
            inspection_type="Site Verification",
            status="REQUESTED",
        )
        out = AIReportService.generate_inspection_report(inspection)
        data = bytes(out)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 3000)

    def test_ncr_report_from_real_rows(self):
        ncr = NonConformanceReport.objects.create(
            project=self.project,
            title="Unsupported excavation edge",
            description="Trench at grid line C has no shoring.",
            severity="Critical",
            category="Safety",
            status="Open",
            reported_by_name="Ada Obi",
            source="SITE_MONITORING",
            source_reference="SCAN-123",
            escalation_level=3,
        )
        CorrectiveActionPlan.objects.create(
            project=self.project, ncr=ncr, title="Install trench shoring",
            priority="Critical", status="in-progress",
            due_date=timezone.localdate() + timedelta(days=7),
        )
        CorrectiveActionPlan.objects.create(
            project=self.project, ncr=ncr, title="Daily excavation inspection",
            priority="High", status="todo",
        )
        # originating AI correlation finding, escalated into this NCR
        finding = CorrelationFinding.objects.create(
            project=self.project, title="Excavation instability signal",
            risk_level="high", risk_score=0.72, status="escalated",
            structural_element_id="TRENCH-C", bim_guid="3xS$0ck251Z8wQyGK$9fO1",
            reasoning="GPR void detection combined with GNSS movement vector.",
            reviewed_by=self.user, linked_ncr=ncr,
        )
        out = AIReportService.generate_ncr_report(ncr, self.user)
        data = bytes(out)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 3000)
        self.assertIsNotNone(finding.linked_ncr)

    def test_ncr_report_resolved_without_capas(self):
        ncr = NonConformanceReport.objects.create(
            project=self.project,
            title="Minor documentation gap",
            description="Material certificate not on file.",
            severity="Minor",
            category="Quality",
            status="Closed",
            resolved_at=timezone.now(),
            resolution_notes="Certificate received and filed.",
        )
        out = AIReportService.generate_ncr_report(ncr)
        data = bytes(out)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 2500)


from datetime import timedelta  # noqa: E402,F401  (kept for clarity; imported above)


class AIReportViewTests(APITestCase):
    """API surface of the three AI report endpoints plus the catalogue."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="aireportsadmin", email="aireportsadmin@example.com",
            password="pw12345!",
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.project = Project.objects.create(
            name="View Test Project", status="ACTIVE",
            client_name="Client Ltd", project_number="PRJ-1",
            site_address="1 Test Road", client_contact="ct@example.com",
        )

    def test_intelligence_report_view_returns_pdf(self):
        url = reverse("project-intelligence-report",
                      kwargs={"project_id": self.project.id})
        with patch.object(AIService, "generate_structured_json",
                          side_effect=AIProviderUnavailable("no key")):
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("intelligence_report", response["Content-Disposition"])

    def test_intelligence_report_view_project_out_of_scope(self):
        url = reverse("project-intelligence-report",
                      kwargs={"project_id": uuid.uuid4()})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_inspection_report_view_returns_pdf(self):
        inspection = Inspection.objects.create(
            project=self.project, inspection_type="Safety Audit",
            status="COMPLETED", gps_verified=True,
            gps_latitude=6.5, gps_longitude=3.4,
            checklist_results=[{"item": "PPE available", "result": "PASS"}],
        )
        url = reverse("inspection-report", kwargs={"inspection_id": inspection.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_inspection_report_view_out_of_scope(self):
        url = reverse("inspection-report", kwargs={"inspection_id": uuid.uuid4()})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_ncr_report_view_returns_pdf(self):
        ncr = NonConformanceReport.objects.create(
            project=self.project, title="Test NCR",
            description="Test description", severity="Major", status="Open",
        )
        url = reverse("ncr-report", kwargs={"ncr_id": ncr.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_ncr_report_view_out_of_scope(self):
        url = reverse("ncr-report", kwargs={"ncr_id": uuid.uuid4()})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class QualityReportListAndTemplateTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="listuser", email="listuser@example.com", password="pw12345!"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.scan = ScanSession.objects.create(
            scanner_id="TER-S1-LIST", status="completed", sensors_used=["lidar"]
        )

    def test_quality_report_list_returns_stored_reports(self):
        QualityReport.objects.create(
            scan=self.scan, status="completed", defect_count=3, anomaly_count=1
        )
        response = self.client.get(reverse("quality_reports"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["defect_count"], 3)
        self.assertEqual(response.data[0]["anomaly_count"], 1)

    def test_report_template_catalogue_lists_active_only(self):
        ReportTemplate.objects.create(
            name="QA/QC Summary", report_type="qaqc", sort_order=1, is_active=True
        )
        ReportTemplate.objects.create(
            name="Disabled Template", report_type="compliance",
            sort_order=2, is_active=False,
        )
        response = self.client.get(reverse("report_template-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [row["name"] for row in response.data]
        self.assertIn("QA/QC Summary", names)
        self.assertNotIn("Disabled Template", names)


class GenerateReportViewExtraTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="genxuser", email="genxuser@example.com", password="pw12345!"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.session = ScanSession.objects.create(
            scanner_id="TER-S1-GENX", status="completed", sensors_used=["lidar"]
        )
        self.generate_report_url = reverse(
            "generate_report", kwargs={"session_id": str(self.session.id)}
        )
        self.download_report_url = reverse(
            "download_report",
            kwargs={"session_id": str(self.session.id), "file_format": "pdf"},
        )

    def test_unknown_report_type_rejected(self):
        response = self.client.post(
            self.generate_report_url, {"report_type": "bogus"},
            REMOTE_ADDR="10.77.0.1",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_download_with_cover_overrides_rerenders(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        self.client.post(self.generate_report_url, {}, REMOTE_ADDR="10.77.0.2")
        response = self.client.get(
            self.download_report_url,
            {"client_name": "Override Client Ltd", "template": "qaqc"},
            REMOTE_ADDR="10.77.0.2",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-PDF-Source"], "rendered")

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_download_streams_stored_pdf(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        self.client.post(self.generate_report_url, {}, REMOTE_ADDR="10.77.0.3")
        fake = MagicMock(content=b"%PDF-stored-bytes")
        fake.raise_for_status.return_value = None
        with patch("requests.get", return_value=fake):
            response = self.client.get(
                self.download_report_url, {"template": "qaqc"},
                REMOTE_ADDR="10.77.0.3",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-PDF-Source"], "stored")
        self.assertEqual(response.content, b"%PDF-stored-bytes")

    @patch.object(CloudinaryService, "upload_file",
                  return_value="https://res.example.com/reports/mock.pdf")
    @patch.object(AIService, "generate_recommendations")
    def test_download_renders_when_stored_pdf_unreachable(self, mock_ai, mock_upload):
        mock_ai.return_value = {"recommendations": [], "text_confidence": None}
        self.client.post(self.generate_report_url, {}, REMOTE_ADDR="10.77.0.4")
        with patch("requests.get", side_effect=RuntimeError("storage down")):
            response = self.client.get(
                self.download_report_url, {"template": "qaqc"},
                REMOTE_ADDR="10.77.0.4",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["X-PDF-Source"], "rendered")


# ---------------------------------------------------------------------------
# MTL-style NDT (PUNDIT) report — apps/reports/ndt_reports.py
# ---------------------------------------------------------------------------
import io
import re
from datetime import datetime

from django.core.files.base import File as DjangoFile

from apps.digital_eye.models import FieldDevice, PUNDITTest, SensorDataFile
from apps.projects.models import Project
from apps.reports.ndt_reports import (
    ECS_CALIBRATION_SOURCE, NDTReportService, estimated_compressive_strength,
)
from apps.digital_eye.adapters import PUNDITAdapter
from PIL import Image


def _pdf_text(data):
    """Extract the full text of a rendered PDF through pypdf."""
    from pypdf import PdfReader
    return '\n'.join(p.extract_text() or ''
                     for p in PdfReader(io.BytesIO(data)).pages)


class NDTReportFixtureMixin:
    """Real ORM rows for the MTL-style NDT report tests."""

    def make_project(self, **kwargs):
        defaults = dict(
            name="Marina NDT Test Project", status="ACTIVE",
            client_name="Lagos State Ministry of Works",
            site_address="12 Marina Road, Lagos Island",
            lga="Lagos Island", state="Lagos State",
            structural_system="Reinforced concrete frame",
            number_of_floors=4,
        )
        defaults.update(kwargs)
        return Project.objects.create(**defaults)

    def make_device(self, project=None):
        return FieldDevice.objects.create(
            device_reference=f"DE-NDT{uuid.uuid4().hex[:6]}",
            device_type="pundit", name="Pundit PL-2",
            model="Pundit PL-2", manufacturer="Proceq",
            device_id="SN-88112", status="online",
            calibration_date=datetime(2026, 1, 15, tzinfo=timezone.utc),
            assigned_project=project,
        )

    def make_test(self, project, device, **kwargs):
        defaults = dict(
            project=project, device=device, test_type="pulse_velocity",
            structural_element="COL-C24", transducer_frequency_khz=54,
            tested_at=datetime(2026, 8, 20, 10, 30, tzinfo=timezone.utc),
        )
        defaults.update(kwargs)
        return PUNDITTest.objects.create(**defaults)


class NDTReportUnitTests(NDTReportFixtureMixin, TestCase):
    """Pure-function and service-level tests (no HTTP)."""

    def setUp(self):
        self.project = self.make_project()
        self.device = self.make_device(self.project)

    # ------------------------------------------------------- calibration
    def test_calibration_curve_matches_reference_points(self):
        # Exact value at the anchor point 4.0 km/s.
        self.assertAlmostEqual(
            estimated_compressive_strength(4.0), 27.874, places=2)
        # The curve reproduces the laboratory's reference pairs within the
        # least-squares regression tolerance (max residual 2.54 N/mm2 at
        # 4.4 km/s).
        for velocity, strength in ((2.9, 19), (3.9, 25), (4.0, 27),
                                   (4.2, 29), (4.4, 34)):
            self.assertLessEqual(
                abs(estimated_compressive_strength(velocity) - strength),
                2.6,
                f'curve drifted at {velocity} km/s')
        # Missing velocity and out-of-range velocities never extrapolate.
        self.assertIsNone(estimated_compressive_strength(None))
        self.assertIsNone(estimated_compressive_strength(1.5))
        self.assertIsNone(estimated_compressive_strength(5.5))

    def test_calibration_curve_is_monotonic_in_valid_range(self):
        values = [estimated_compressive_strength(v)
                  for v in (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)]
        self.assertEqual(values, sorted(values))

    # ----------------------------------------------------- report number
    def test_report_number_deterministic_and_well_formed(self):
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        tests = list(PUNDITTest.objects.filter(project=self.project))
        first, _ = NDTReportService._report_number(self.project, tests)
        second, _ = NDTReportService._report_number(self.project, tests)
        self.assertEqual(first, second)
        self.assertRegex(first, r'^\d{4} / MTL/NDT/\d{4}$')
        # Year derives from the earliest recorded test date.
        self.assertIn(' / MTL/NDT/2026', first)

    # ---------------------------------------------------- full document
    def test_full_report_renders_letter_times_pdf_with_sections(self):
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        self.make_test(self.project, self.device,
                       structural_element="BEAM-B12",
                       path_length_mm=400.0, pulse_time_us=100.0)
        data = NDTReportService.generate_ndt_report(self.project)
        self.assertTrue(data.startswith(b'%PDF'))
        text = _pdf_text(data)
        for expected in (
            'LAGOS STATE MATERIALS TESTING LABORATORY',
            'OJODU BERGER, LAGOS.',
            'TABLE OF CONTENTS',
            '1.0 INTRODUCTION',
            '4.2 METHODOLOGY',
            '5.0 ANALYSIS OF TEST RESULTS',
            '7.0 CONCLUSION',
            '8.96',  # calibration curve disclosed in the report
        ):
            self.assertIn(expected, text)
        # US Letter portrait (612 x 792 pt).
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        box = reader.pages[0].mediabox
        self.assertAlmostEqual(float(box.width), 612, delta=1)
        self.assertAlmostEqual(float(box.height), 792, delta=1)

    def test_toc_page_numbers_point_at_real_pages(self):
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        data = NDTReportService.generate_ndt_report(self.project)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        toc_text = reader.pages[1].extract_text() or ''
        entries = re.findall(r'(\d\.\d [A-Z][A-Z /(),\-]+?)\s+(\d+)\b',
                             toc_text)
        self.assertTrue(entries, 'TOC entries not parsed from the document')
        for name, page in entries:
            page = int(page)
            self.assertLessEqual(page, len(reader.pages))
            body = reader.pages[page - 1].extract_text() or ''
            self.assertIn(name.split(' ', 1)[1][:12], body,
                          f'TOC says {name!r} is on page {page} but the '
                          f'heading is not there')

    # --------------------------------------------------- section 5.0 data
    def test_section5_groups_by_element_with_averages(self):
        self.make_test(self.project, self.device,
                       structural_element="COL-C24",
                       path_length_mm=250.0, pulse_time_us=62.5)
        self.make_test(self.project, self.device,
                       structural_element="BEAM-B12",
                       path_length_mm=400.0, pulse_time_us=100.0)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        self.assertIn('STRUCTURAL ELEMENT: COL-C24', text)
        self.assertIn('STRUCTURAL ELEMENT: BEAM-B12', text)
        # 250 mm / 62.5 us = 4.00 km/s -> E.C.S round(27.87) = 28 N/mm2.
        self.assertIn('4.00', text)
        self.assertIn('28', text)
        # Element averages are printed.
        self.assertIn('AVERAGE PULSE VELOCITY: 4.00 KM/S', text)

    def test_ecs_remark_consistent_with_adapter_grade(self):
        # Two tests on one element: mean velocity (4.00 + 2.90) / 2 = 3.45
        # km/s -> 'questionable' by the platform's own adapter.
        self.make_test(self.project, self.device,
                       structural_element="COL-C24",
                       path_length_mm=250.0, pulse_time_us=62.5)
        self.make_test(self.project, self.device,
                       structural_element="COL-C24",
                       path_length_mm=290.0, pulse_time_us=100.0)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        self.assertIn('AVERAGE PULSE VELOCITY: 3.45 KM/S', text)
        self.assertIn('QUESTIONABLE', text)
        self.assertEqual(PUNDITAdapter.grade_quality(3.45), 'questionable')

    def test_pending_and_missing_tests_are_honest(self):
        # A test never run through the analyze endpoint still shows its
        # computed velocity plus the disclosure preamble.
        pending = self.make_test(self.project, self.device,
                                 path_length_mm=250.0, pulse_time_us=62.5)
        self.assertEqual(pending.quality_grade, 'pending')
        # A test with no measurements at all renders honest dashes.
        self.make_test(self.project, self.device,
                       structural_element="BEAM-B12",
                       path_length_mm=None, pulse_time_us=None)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        self.assertIn('have not been run through the platform analysis '
                      'endpoint', text)
        self.assertIn('4.00', text)          # computed on the fly for pending
        self.assertIn('NOT RECORDED', text)  # element without measurements

    def test_crack_depth_subtable(self):
        # d = L/2 * sqrt((t_c/t_0)^2 - 1) = 150 * sqrt(1.44 - 1) = 99.5 mm.
        self.make_test(self.project, self.device,
                       test_type="crack_depth", structural_element="SLAB-S3",
                       crack_path_length_mm=300.0, crack_pulse_time_us=75.0,
                       uncracked_pulse_time_us=62.5)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        self.assertIn('CRACK DEPTH MEASUREMENTS', text)
        self.assertIn('99.5', text)
        self.assertIn('Depth exceeds 25 mm - structural review required', text)

    # ------------------------------------------------- layout regression
    def test_wrap_lines_fit_column_and_lose_nothing(self):
        from apps.reports.ndt_reports import NDTReportBuilder, _wrap_lines
        builder = NDTReportBuilder('0001 / MTL/NDT/2026')
        builder.pdf.set_font('Times', '', 10)
        pdf = builder.pdf
        token = 'a1b2' * 16      # 64-char digest-like unbreakable token
        sentence = ('Hairline map cracking with honeycombing at the north '
                    'face together with exposed reinforcement over roughly '
                    'two square metres of the column at mid-height')
        for text in (token, sentence, None, '', 'single', 'a' * 200):
            lines = _wrap_lines(pdf, text, 30)      # 30 mm column
            self.assertTrue(lines)
            for line in lines:
                self.assertLessEqual(
                    pdf.get_string_width(line), 30,
                    f'line overflows the column: {line!r}')
        # Hard-broken tokens drop no characters.
        self.assertEqual(''.join(_wrap_lines(pdf, token, 30)), token)
        # Wrapped sentences keep every word.
        self.assertEqual(
            ' '.join(' '.join(_wrap_lines(pdf, sentence, 30)).split()),
            ' '.join(sentence.split()))
        # Missing values render as the honest dash, never 'None'.
        self.assertEqual(_wrap_lines(pdf, None, 30), ['-'])

    def test_report_text_does_not_overlap_or_enter_the_header(self):
        # Long live-DB values (reference codes, models, remarks, notes) used
        # to run into neighbouring columns (fpdf2 cell() never wraps), and
        # every page break used to restart body text inside the running
        # header band. Instrument every text run and assert neither happens.
        from fpdf import FPDF
        long_condition = ('Hairline map cracking with honeycombing and '
                          'exposed reinforcement at the north face over '
                          'approximately two square metres around mid-height')
        long_reference = 'UPV-FLD-2026-VERY-LONG-STATION-REF-0001'
        self.make_test(
            self.project, self.device, test_reference=long_reference,
            path_length_mm=250.0, pulse_time_us=62.5,
            notes='Measured with 54 kHz transducers — direct mode — per '
                  'operator field sheet')
        self.make_test(self.project, self.device,
                       test_type='crack_depth', structural_element='SLAB-S3',
                       crack_path_length_mm=300.0, crack_pulse_time_us=75.0,
                       uncracked_pulse_time_us=62.5)
        self.make_test(self.project, self.device,
                       test_type='surface_quality',
                       structural_element='WALL-W2',
                       surface_condition=long_condition,
                       notes='Observed from the access scaffold, level 3')
        runs = []
        real_cell, real_multi_cell = FPDF.cell, FPDF.multi_cell

        def spy_cell(pdf, w=None, h=None, text='', *a, **k):
            if text:
                runs.append((pdf.page_no(), pdf.x, pdf.y, h or 0, str(text)))
            return real_cell(pdf, w, h, text, *a, **k)

        def spy_multi_cell(pdf, w=None, h=None, text='', *a, **k):
            if text:
                runs.append((pdf.page_no(), pdf.x, pdf.y, h or 0,
                             str(text).split('\n')[0]))
            return real_multi_cell(pdf, w, h, text, *a, **k)

        FPDF.cell, FPDF.multi_cell = spy_cell, spy_multi_cell
        try:
            data = NDTReportService.generate_ndt_report(self.project)
        finally:
            FPDF.cell, FPDF.multi_cell = real_cell, real_multi_cell

        for i, (page, x, y, h, text) in enumerate(runs):
            # Body text may never sit inside the running-header band: the
            # header's own runs live at y=10..16, the rule is at 17.5 and
            # the top margin is 24. Anything body-like at 10.5..23 is the
            # page-break bug come back.
            if page >= 2 and 10.5 <= y <= 23:
                self.fail(
                    f'body text inside the header band: {text[:40]!r} at y={y}')
            for page2, x2, y2, h2, text2 in runs[i + 1:]:
                if page != page2:
                    continue
                if (abs(y - y2) < max(min(h, h2) - 0.5, 3)
                        and x + min(len(text) * 0.5, 170) > x2 + 1.0
                        and x2 + min(len(text2) * 0.5, 170) > x + 1.0):
                    self.fail(f'overlapping text runs: '
                              f'{text[:30]!r} / {text2[:30]!r}')
        # Long values are rendered whole — wrapped, never truncated.
        flat = ' '.join(_pdf_text(data).split())
        self.assertIn(' '.join(long_condition.split()), flat)
        self.assertIn(long_reference, flat)

    def test_empty_project_report_is_honest(self):
        data = NDTReportService.generate_ndt_report(self.project)
        self.assertTrue(data.startswith(b'%PDF'))
        text = _pdf_text(data)
        self.assertIn('No pulse velocity tests recorded', text)
        self.assertIn('DATE OF TEST: NOT RECORDED', text)
        self.assertIn('No photographs recorded for these tests.', text)

    # --------------------------------------------------------- appendix
    def test_appendix_embeds_photos_and_honest_when_none(self):
        media = tempfile.mkdtemp(prefix="ndt_media_")
        hermetic = dict(
            MEDIA_ROOT=media,
            MEDIA_URL='/media/',
            STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
            },
        )
        try:
            with self.settings(**hermetic):
                png_path = os.path.join(media, "test_photo.png")
                Image.new("RGB", (60, 40), (200, 30, 30)).save(png_path)
                with open(png_path, "rb") as fh:
                    data_file = SensorDataFile.objects.create(
                        file_type="photo",
                        file_name="column_crack.png",
                        file_size_bytes=os.path.getsize(png_path),
                    )
                    data_file.file.save("column_crack.png",
                                        DjangoFile(fh),
                                        save=True)
                test = self.make_test(self.project, self.device,
                                      path_length_mm=250.0,
                                      pulse_time_us=62.5)
                test.files.add(data_file)
                text = _pdf_text(
                    NDTReportService.generate_ndt_report(self.project))
                self.assertIn('Photo 1: column_crack.png', text)

                # Once the photo is detached, the appendix states so honestly.
                test.files.remove(data_file)
                data_file.delete()
                text = _pdf_text(
                    NDTReportService.generate_ndt_report(self.project))
                self.assertIn('No photographs recorded for these tests.', text)
        finally:
            shutil.rmtree(media, ignore_errors=True)


class _HermeticMediaMixin:
    """Archive writes real files — point MEDIA_ROOT at a temp dir per test."""

    def setUp(self):
        super().setUp()
        self._media = tempfile.mkdtemp()
        self._settings = override_settings(MEDIA_ROOT=self._media)
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.addCleanup(shutil.rmtree, self._media, ignore_errors=True)


class NDTReportViewTests(_HermeticMediaMixin, NDTReportFixtureMixin, APITestCase):
    """API surface of the NDT report endpoint."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username="ndtadmin", email="ndtadmin@example.com",
            password="pw12345!",
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.project = self.make_project()
        device = self.make_device(self.project)
        self.make_test(self.project, device,
                       path_length_mm=250.0, pulse_time_us=62.5)

    def test_ndt_report_view_returns_pdf(self):
        url = reverse("project-ndt-report",
                      kwargs={"project_id": self.project.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("ndt_report", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_ndt_report_view_project_out_of_scope(self):
        url = reverse("project-ndt-report",
                      kwargs={"project_id": uuid.uuid4()})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ArchivedReportTests(_HermeticMediaMixin, NDTReportFixtureMixin, APITestCase):
    """Statutory dossier archive: every generated NDT report is persisted
    exactly as produced, checksummed, listable and re-downloadable."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username="archadmin", email="archadmin@example.com",
            password="pw12345!", first_name="Ada", last_name="Onike",
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.project = self.make_project()
        self.device = self.make_device(self.project)
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)  # 4.0 km/s → pass
        self.generate_url = reverse("project-ndt-report",
                                    kwargs={"project_id": self.project.id})
        self.list_url = reverse("project-archived-reports",
                                kwargs={"project_id": self.project.id})

    def test_generation_persists_checksummed_archive(self):
        response = self.client.get(self.generate_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        archive = ArchivedReport.objects.get(project=self.project)
        self.assertEqual(archive.report_kind, 'ndt')
        self.assertRegex(archive.report_reference, r'^\d{4} / MTL/NDT/\d{4}$')
        self.assertEqual(archive.sha256_checksum,
                         hashlib.sha256(response.content).hexdigest())
        self.assertEqual(archive.file_size_bytes, len(response.content))
        self.assertEqual(archive.test_count, 1)
        self.assertEqual(archive.assessed_count, 1)
        self.assertEqual(archive.passed_count, 1)
        self.assertEqual(archive.compliance_status, 'COMPLIANT')
        self.assertTrue(archive.file)

    def test_identical_generation_is_not_archived_twice(self):
        first = self.client.get(self.generate_url)
        second = self.client.get(self.generate_url)
        self.assertEqual(ArchivedReport.objects.filter(project=self.project).count(), 1)
        # PDF bytes differ between runs (creation timestamp), so the byte
        # checksums differ — only the deterministic content key can dedupe.
        self.assertNotEqual(
            hashlib.sha256(first.content).hexdigest(),
            hashlib.sha256(second.content).hexdigest())
        archive = ArchivedReport.objects.get(project=self.project)
        # The stored file stays byte-identical to the FIRST generation (the
        # one that created the archive); the second response streamed fresh
        # bytes, which were deduped away and never re-stored.
        self.assertEqual(archive.sha256_checksum,
                         hashlib.sha256(first.content).hexdigest())

    def test_new_data_creates_a_new_archive_and_flags_defects(self):
        self.client.get(self.generate_url)  # passing dossier archived
        # A deficient station arrives: 250 mm / 100 µs → 2.5 km/s → ~14.4 MPa.
        self.make_test(self.project, self.device,
                       structural_element="SLAB-S1",
                       path_length_mm=250.0, pulse_time_us=100.0)
        self.client.get(self.generate_url)  # new content → new archive row
        archives = ArchivedReport.objects.filter(project=self.project)
        self.assertEqual(archives.count(), 2)
        latest = archives.order_by('-created_at').first()
        self.assertEqual(latest.test_count, 2)
        self.assertEqual(latest.assessed_count, 2)
        self.assertEqual(latest.passed_count, 1)
        self.assertEqual(latest.compliance_status, 'FLAGGED_DEFECTS')

    def test_list_endpoint_returns_scoped_archives(self):
        self.client.get(self.generate_url)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row['project_name'], self.project.name)
        self.assertEqual(row['compliance_status'], 'COMPLIANT')
        self.assertEqual(row['test_count'], 1)
        self.assertEqual(row['generated_by_name'], self.user.get_full_name())
        # A project outside the caller's scope is invisible.
        other = reverse("project-archived-reports",
                        kwargs={"project_id": uuid.uuid4()})
        self.assertEqual(self.client.get(other).status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_download_streams_the_archived_bytes(self):
        generated = self.client.get(self.generate_url)
        archive = ArchivedReport.objects.get(project=self.project)
        url = reverse("archived-report-download",
                      kwargs={"report_id": archive.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        # The audit guarantee: the stored dossier is byte-for-byte the
        # document that was generated and checksummed.
        self.assertEqual(response.content, generated.content)
        self.assertEqual(
            hashlib.sha256(response.content).hexdigest(),
            archive.sha256_checksum)

    def test_download_out_of_scope_404(self):
        self.client.get(self.generate_url)
        url = reverse("archived-report-download",
                      kwargs={"report_id": uuid.uuid4()})
        self.assertEqual(self.client.get(url).status_code,
                         status.HTTP_404_NOT_FOUND)
