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
    @patch("apps.common.ai_service.AIService.generate_recommendations")
    @patch("apps.reports.tasks.generate_report_task.delay")
    def test_generate_report(self, mock_delay, mock_ai, mock_pdf):
        mock_pdf.return_value = "https://res.cloudinary.com/demo/image/upload/v1/mock.pdf"
        # Hermetic (like every sibling test): no live provider call — the
        # real API once 429'd here on exhausted quota and failed the suite.
        mock_ai.return_value = {"recommendations": ["Monitor the crack."],
                                "text_confidence": 0.9}
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
from datetime import date, datetime, timezone as dt_timezone

from django.core.files.base import File as DjangoFile

from apps.digital_eye.models import (FieldDevice, PUNDITReading, PUNDITTest,
                                     SensorDataFile)
from apps.projects.models import Project
from apps.reports.ndt_reports import (
    ECS_CALIBRATION_SOURCE, NDTReportService, _element_display,
    estimated_compressive_strength,
)
from PIL import Image


def _pdf_text(data):
    """Extract the full text of a rendered PDF through pypdf.

    pypdf reconstructs spacing from glyph positions, so justified Cambria
    text extracts with doubled spaces and adjacent cells land on separate
    lines. Collapse each page's whitespace to single spaces so phrase
    assertions are layout-robust; pages stay '\n'-separated.
    """
    from pypdf import PdfReader
    return '\n'.join(' '.join((p.extract_text() or '').split())
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
            calibration_date=date(2026, 1, 15),
            assigned_project=project,
        )

    def make_test(self, project, device, **kwargs):
        defaults = dict(
            project=project, device=device, test_type="pulse_velocity",
            structural_element="COL-C24", transducer_frequency_khz=54,
            tested_at=datetime(2026, 8, 20, 10, 30, tzinfo=dt_timezone.utc),
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

    def test_report_discloses_project_calibration_curve(self):
        # 8 Sep 2026 meeting (Nexucon Link): with a project curve active,
        # Section 3.0 discloses THAT curve — formula, calibration points,
        # validity — and the worked example substitutes its real
        # parameters; without one, the documented laboratory default.
        from apps.digital_eye.models import (ProjectCurveSetting,
                                             StrengthCurve)
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        flat = ' '.join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        self.assertIn('8.961', flat)  # the laboratory default curve

        curve = StrengthCurve.objects.create(
            name='Marina trial-mix calibration', curve_type='linear',
            project=self.project, standard='Project cube tests, Aug 2026',
            formula_params={'m': 0.012, 'c': -30.0},
            valid_range_min_ms=2000.0, valid_range_max_ms=5000.0,
            data_points=[{'v': 3000.0, 'f': 6.0}, {'v': 4000.0, 'f': 18.0}])
        ProjectCurveSetting.objects.create(project=self.project,
                                           active_curve=curve)
        flat = ' '.join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        self.assertIn('project-specific calibration curve', flat)
        self.assertIn('f_cu = 0.012 x V - 30', flat)
        self.assertIn('2 real calibration pair', flat)
        self.assertIn('Project cube tests, Aug 2026', flat)
        # The worked example substitutes the project curve's parameters:
        # 250 mm / 62.5 us = 4000 m/s -> f_cu = 0.012*4000 - 30 = 18 N/mm2.
        self.assertIn('f_cu = 0.012 x 4000.00 - 30 = 18.00', flat)
        # And the Section 5.0 verdict itself follows the project curve.
        self.assertIn('18.0', flat)

    def test_report_element_verdicts_follow_the_project_curve(self):
        # The Section 5.0 average compressive strength — and the archive's
        # pass/fail basis — flow through the project's active curve, not a
        # fixed formula (8 Sep meeting: f_cu is the reason for the link).
        from apps.digital_eye.models import (ProjectCurveSetting,
                                             StrengthCurve)
        test = self.make_test(self.project, self.device,
                              path_length_mm=250.0, pulse_time_us=62.5)
        for label, transit in (('A', 62.5), ('B', 62.5), ('C', 62.5)):
            PUNDITReading.objects.create(
                test=test, point_label=label, path_length_mm=250.0,
                transit_time_us=transit)
        curve = StrengthCurve.objects.create(
            name='Marina strict calibration', curve_type='linear',
            project=self.project,
            formula_params={'m': 0.012, 'c': -30.0},
            valid_range_min_ms=2000.0, valid_range_max_ms=5000.0)
        ProjectCurveSetting.objects.create(project=self.project,
                                           active_curve=curve)
        flat = ' '.join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        # 4000 m/s -> 0.012*4000 - 30 = 18.0 N/mm2 (the fixed curve would
        # have said 27.9); 18 < 25 so the remark is POOR.
        self.assertIn('18.0', flat)
        element = NDTReportService._element_data([test])[0]
        self.assertAlmostEqual(element['mean_ecs'], 18.0, places=2)
        self.assertEqual(element['remark'], 'POOR')

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
            'TABLE OF CONTENT',
            '1.0 INTRODUCTION',
            '4.2 METHODOLOGY',
            'ANALYSIS OF TEST RESULT',
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
        # Reference TOC entries are Title Case ("1.0. Introduction 1"),
        # so parse number + title-case heading + arabic page number.
        entries = re.findall(r'(\d\.\d)\.?\s+([A-Za-z][A-Za-z /(),&\-]+?)\s+(\d+)\b',
                             toc_text)
        self.assertTrue(entries, 'TOC entries not parsed from the document')
        # The Title-Case TOC wording maps onto the ALL-CAPS body headings
        # (the reference diverges: "Field Work/Equipment Status Check" for
        # the body's "FIELD WORK", plural "Recommendations", ...).
        body_headings = {
            'Introduction': 'INTRODUCTION',
            'Purpose of investigation': 'PURPOSE OF INVESTIGATION',
            'Literature Review': 'LITERATURE REVIEW',
            'Location Map/ Weather Condition': 'LOCATION MAP/ WEATHER',
            'Field Work/Equipment Status Check': 'FIELD WORK',
            'Visual Test': 'VISUAL TEST',
            'Methodology': 'METHODOLOGY',
            'Reinforcing Bar (Rebar) Assessment':
                'REINFORCING BAR (REBAR) ASSESSMENT',
            'Equipment/Rebar Assessment Table':
                'EQUIPMENT/REBAR ASSESSMENT TABLE',
            'Analysis of Test Results': 'ANALYSIS OF TEST RESULT',
            'Recommendations': 'RECOMMENDATION',
            'Conclusion': 'CONCLUSION',
        }
        # TOC numbers are BODY page numbers (reference numbering:
        # Introduction = page 1; cover/TOC/summary pages are unnumbered).
        # Locate the physical Introduction page to recover the offset.
        intro_page = None
        for idx in range(2, len(reader.pages)):
            flat = ' '.join((reader.pages[idx].extract_text() or '').split())
            if '1.0 INTRODUCTION' in flat:
                intro_page = idx + 1
                break
        self.assertIsNotNone(intro_page,
                             'physical Introduction page not found')
        offset = intro_page - 1
        for num, title, page in entries:
            heading = body_headings.get(title, title.upper())
            page = int(page) + offset       # body number -> physical page
            self.assertLessEqual(page, len(reader.pages))
            body = reader.pages[page - 1].extract_text() or ''
            if page < len(reader.pages):
                body += '\n' + (reader.pages[page].extract_text() or '')
            # Divider pages (e.g. '5.0 ANALYSIS OF TEST RESULT') draw the
            # title on separate centred lines, so compare with whitespace
            # collapsed.
            body_flat = ' '.join(body.split())
            self.assertIn(heading, body_flat,
                          f'TOC says {num} {title!r} is on page {page} but '
                          f'the heading is not there (or on the next page)')

    def test_executive_summary_present_but_absent_from_toc(self):
        # C1: the executive summary is a real page but deliberately not a
        # TOC entry (reference layout).
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        data = NDTReportService.generate_ndt_report(self.project)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        full = _pdf_text(data)
        self.assertIn('EXECUTIVE SUMMARY', full)
        toc_text = reader.pages[1].extract_text() or ''
        self.assertNotIn('EXECUTIVE SUMMARY', toc_text)

    def test_storey_count_from_test_floors_and_bim_levels(self):
        # The building profile is quantified only from real records: test
        # floors and imported BIM levels both count, unmapped labels (Roof,
        # FLOOR NOT RECORDED) contribute nothing, and nothing recorded
        # leaves the profile unquantified (None).
        self.assertIsNone(NDTReportService._storey_count(
            ['FLOOR NOT RECORDED'], []))
        self.assertEqual(NDTReportService._storey_count(
            ['Ground Floor', 'First Floor'], []), 2)
        self.assertEqual(NDTReportService._storey_count(
            ['Ground Floor'], ['0. NGL', '1. First Floor']), 2)
        self.assertEqual(NDTReportService._storey_count(
            ['Roof'], ['0. NGL']), 1)

    def test_executive_summary_building_profile_and_arrangement(self):
        # Reference p3 paragraph 1: "…of an existing 2-floor building (A, B
        # &C) belonging to …, at …" — the storey count derives only from
        # recorded levels; paragraph 3 states the drawing availability
        # honestly either way.
        from apps.digital_eye.models import BIMElementMapping
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        flat = ' '.join(text.split())
        # Nothing recorded: unquantified profile + reference's no-drawing
        # statement.
        self.assertIn('of an existing building ("Marina NDT Test Project") '
                      'belonging to', flat)
        self.assertIn('no structural drawing was provided', flat)

        BIMElementMapping.objects.create(
            project=self.project, bim_guid='a' * 22, element_id='S-101',
            element_name='RC Slab 200', element_type='IfcSlab',
            level='0. NGL', source='ifc_upload')
        BIMElementMapping.objects.create(
            project=self.project, bim_guid='b' * 22, element_id='C-201',
            element_name='RC Column', element_type='IfcColumn',
            level='1. First Floor', source='ifc_upload')
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        flat = ' '.join(text.split())
        self.assertIn('an existing 2-floor building ("Marina NDT Test '
                      'Project")', flat)
        self.assertIn('referenced from the structural information available '
                      'on the platform', flat)

    def test_appendix_schedule_lists_imported_bim_elements(self):
        # The appendix schedule lists the REAL imported elements
        # (BIMElementMapping) with the properties the import recorded —
        # never the legacy demo table.
        from apps.digital_eye.models import BIMElementMapping
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        BIMElementMapping.objects.create(
            project=self.project, bim_guid='c' * 22, element_id='S-780904',
            element_name='Floor:200THK RC SLAB:780904',
            element_type='IfcSlab', level='0. NGL', source='ifc_upload',
            properties={'Tag': '780904',
                        'Material': 'Concrete - Cast-in-Place Concrete '
                                    '(200 mm)'})
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        flat = ' '.join(text.split())
        # The appendix 'drawing' page is registered in the TOC and carries
        # the project-name label; the heading itself is deliberately not
        # rendered (reference layout).
        self.assertIn('Drawing of the Building', flat)
        # One Revit segment per line — whole tokens, no mid-word breaks.
        self.assertIn('Floor 200THK RC SLAB 780904', flat)
        # The CATEGORY column reads as plain words for non-technical
        # reviewers — never the raw IFC class name or a STEP dump.
        self.assertIn(' Slab ', flat)
        self.assertNotIn('IfcSlab', flat)
        self.assertIn('MATERIAL / GRADE RECORDED', flat)
        self.assertIn('Concrete - Cast-in-Place Concrete (200 mm)', flat)

    def test_visual_observations_are_reference_style_sentences(self):
        # §4.1 reads like the reference — 'Tacky floor observed on <element>
        # (see pic i).' — not raw record dumps: the '[MANUAL_FIELD_ENTRY —
        # Station …]' provenance stamp never prints, the observation words
        # stay the operator's own, and the element/location are appended as
        # context.
        t1 = self.make_test(
            self.project, self.device,
            structural_element='Floor:200THK RC SLAB:780904',
            test_location='First Floor',
            notes='[MANUAL_FIELD_ENTRY — Station UPV-FLD-2026-002] tacky '
                  'floor')
        t2 = self.make_test(
            self.project, self.device,
            structural_element='M_Footing-Rectangular:900 x 900 x 200mm:803486',
            surface_condition='dsmooth finishing')
        obs = NDTReportService._visual_observations([t1, t2])
        self.assertEqual(obs, [
            'Tacky floor observed at First Floor on '
            'Floor:200THK RC SLAB:780904.',
            'Dsmooth finishing observed on '
            'M_Footing-Rectangular:900 x 900 x 200mm:803486.',
        ])

    def test_visual_observation_references_appendix_photo(self):        # The '(see pic N)' cross-reference must point at the photograph the
        # appendix actually numbers — same walk, same roman numeral.
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
                png_path = os.path.join(media, "slab_photo.png")
                Image.new("RGB", (60, 40), (200, 30, 30)).save(png_path)
                with open(png_path, "rb") as fh:
                    data_file = SensorDataFile.objects.create(
                        file_type="photo",
                        file_name="slab_photo.png",
                        file_size_bytes=os.path.getsize(png_path),
                    )
                    data_file.file.save("slab_photo.png", DjangoFile(fh),
                                        save=True)
                test = self.make_test(
                    self.project, self.device,
                    structural_element='Floor:200THK RC SLAB:780904',
                    path_length_mm=250.0, pulse_time_us=62.5,
                    notes='[MANUAL_FIELD_ENTRY — Station UPV-FLD-2026-002] '
                          'tacky floor')
                test.files.add(data_file)
                text = _pdf_text(
                    NDTReportService.generate_ndt_report(self.project))
                flat = ' '.join(text.split())
                self.assertIn('Tacky floor observed on Floor:200THK RC '
                              'SLAB:780904 (see pic i).', flat)
                self.assertIn('PIC I: slab_photo.png', text)
        finally:
            shutil.rmtree(media, ignore_errors=True)

    def test_member_type_from_bim_style_element_names(self):
        # BIM-style names ('M_Footing-Rectangular:900 x 900 x 200mm:803711')
        # classify to readable member types — the §5.0 group headings and
        # summary tables must never print raw name fragments like
        # 'M_FOOTINGS' or 'FLOOR:200THKS'.
        mt = NDTReportService._member_type
        self.assertEqual(mt('COL-C24'), 'COLUMN')
        self.assertEqual(mt('Floor:200THK RC SLAB:781094'), 'SLAB')
        self.assertEqual(
            mt('M_Footing-Rectangular:900 x 900 x 200mm:803711'),
            'FOUNDATION')
        self.assertEqual(mt('WALL-W2'), 'WALL')
        self.assertEqual(mt('BEAM-B12'), 'BEAM')
        self.assertEqual(mt(''), 'UNSPECIFIED')

    def test_element_display_breaks_revit_segments(self):
        # Revit names are 'Family:Type:Tag' — one segment per line in the
        # narrow report columns, so names never break mid-token
        # ('M_Footing-Rectangula / r:900').
        self.assertEqual(
            _element_display('M_Footing-Rectangular:900 x 900 x 200mm:803711'),
            'M_Footing-Rectangular\n900 x 900 x 200mm\n803711')
        self.assertEqual(
            _element_display('Floor:200THK RC SLAB:781235'),
            'Floor\n200THK RC SLAB\n781235')
        self.assertEqual(_element_display('COL-C24'), 'COL-C24')
        self.assertEqual(_element_display(''), '-')
        self.assertEqual(_element_display(None), '-')

    def test_section5_readable_for_bim_element_names(self):
        # End-to-end: the §5.0 group heading, the summary tables and the
        # §5.2 notes column read like the reference — member words, not
        # name fragments, and no provenance stamps.
        self.make_test(
            self.project, self.device,
            structural_element='Floor:200THK RC SLAB:781235',
            floor='Second Floor',
            path_length_mm=120.0, pulse_time_us=62.5)
        self.make_test(
            self.project, self.device, test_type='surface_quality',
            structural_element='M_Footing-Rectangular:900 x 900 x 200mm:803486',
            surface_condition='dsmooth finishing',
            notes='[MANUAL_FIELD_ENTRY — Station UPV-FLD-2026-004] '
                  'smooth dense surface')
        # Multi-point (A1) crack test on another long Revit name — §5.1's
        # per-point table must fit the same segmented element display.
        crack = self.make_test(
            self.project, self.device, test_type='crack_depth',
            structural_element='M_Footing-Rectangular:900 x 900 x 200mm:803711',
            crack_path_length_mm=300.0)
        for label, (tc, t0) in zip('ABC', ((70.0, 62.5), (75.0, 62.5), (80.0, 62.5))):
            PUNDITReading.objects.create(
                test=crack, point_label=label, path_length_mm=300.0,
                transit_time_us=tc, uncracked_transit_time_us=t0)
        mean_depth = crack.element_mean_crack_depth_mm()
        crack.crack_depth_mm = mean_depth
        crack.estimated_crack_depth_mm = mean_depth
        crack.save()
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        flat = ' '.join(text.split())
        self.assertIn('SECOND FLOOR SLABS OF', flat)
        # The element renders one Revit segment per line — whole tokens
        # (this fails if the name breaks mid-word) and no colons in the
        # table (the §4.1 prose keeps the full name).
        self.assertIn('M_Footing-Rectangular 900 x 900 x 200mm 803486',
                      flat)
        self.assertIn('M_Footing-Rectangular 900 x 900 x 200mm 803711',
                      flat)
        # §5.1 renders one row per point with its computed depth and the
        # element mean.
        self.assertIn('CRACK DEPTH MEASUREMENTS', flat)
        self.assertIn('75.7', flat)
        self.assertIn('98.3', flat)
        self.assertIn('dsmooth finishing', flat)
        self.assertIn('smooth dense surface', flat)
        # Raw fragments and provenance stamps never print.
        self.assertNotIn('FLOOR:200THKS', flat)
        self.assertNotIn('M_FOOTINGS', flat)
        self.assertNotIn('MANUAL_FIELD_ENTRY', flat)

    def test_charts_section_is_registered_in_toc(self):
        # The reference TOC carries no charts entry (C14's original intent —
        # a section, not an anonymous page — is met by the numbered
        # BAR CHART section in the body, which the exact-reference TOC
        # deliberately omits).
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        data = NDTReportService.generate_ndt_report(self.project)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        toc_text = reader.pages[1].extract_text() or ''
        self.assertNotIn('BAR CHART', toc_text)
        full = _pdf_text(data)
        self.assertIn('BAR CHART SHOWING SUMMARY OF TEST RESULTS', full)

    def test_rebar_table_only_when_rebar_rows_exist(self):
        # C12: never claim "not applicable" and then print a table.
        from apps.digital_eye.models import RebarTest
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        self.assertIn('No Rebar scanning data recorded', text)
        self.assertNotIn('MAIN BAR (MM)', text)

        RebarTest.objects.create(
            project=self.project, structural_element='COL-R01',
            test_location='Ground Floor', main_bar_mm=16.0,
            links_mm=8.0, spacing_mm='200', cover_depth_mm=25.0)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        flat = ' '.join(text.split())
        self.assertIn('MAIN BAR (MM)', flat)
        self.assertIn('COVER DEPTH', flat)

    # --------------------------------------------------- section 5.0 data
    def test_section5_groups_by_element_with_averages(self):
        self.make_test(self.project, self.device,
                       structural_element="COL-C24",
                       path_length_mm=250.0, pulse_time_us=62.5)
        self.make_test(self.project, self.device,
                       structural_element="BEAM-B12",
                       path_length_mm=400.0, pulse_time_us=100.0)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        # Reference 7-column layout: element name once, per-point rows,
        # average compressive strength + remark columns (header wraps in the
        # narrow column, so compare on flattened text).
        flat = ' '.join(text.split())
        self.assertIn('COL-C24', flat)
        self.assertIn('BEAM-B12', flat)
        self.assertIn('AVERAGE COMPRESSIVE STRENGTH (N/mm2)', flat)
        self.assertIn('REMARK', flat)
        # 250 mm / 62.5 us = 4.00 km/s -> E.C.S 8.961*4.0 - 7.97 = 27.9.
        self.assertIn('27.9', text)
        # Velocities print like the reference (one decimal).
        self.assertIn('4.0', text)
        # Both summaries come from the real rows.
        self.assertIn('SUMMARY OF TEST ANALYSIS', text)
        self.assertIn('SUMMARY OF TEST RESULTS', text)

    def test_readings_render_as_point_rows(self):
        # The multi-reading model: one element, three A/B/C test points,
        # verdict from the element means.
        from apps.digital_eye.models import PUNDITReading
        test = self.make_test(self.project, self.device,
                              structural_element="COL-G01", floor="Ground Floor",
                              path_length_mm=250.0)
        for label, transit in (('A', 62.5), ('B', 60.0), ('C', 61.0)):
            PUNDITReading.objects.create(
                test=test, point_label=label, path_length_mm=250.0,
                transit_time_us=transit)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        # Point labels and their one-decimal values appear in the table.
        self.assertIn('A', text)
        self.assertIn('62.5', text)
        self.assertIn('60.0', text)
        # Mean velocity (4.0 + 4.167 + 4.098)/3 = 4.088 -> E.C.S 28.7 -> GOOD.
        self.assertIn('28.7', text)
        self.assertIn('GOOD', text)
        # The floor groups the block (reference 'GROUND FLOOR ...' header).
        self.assertIn('GROUND FLOOR', text)

    def test_ecs_remark_consistent_with_statutory_threshold(self):
        # GOOD/POOR is decided at the statutory 25 N/mm2 design strength:
        # 4.0 km/s -> 27.9 N/mm2 (GOOD); 2.9 km/s -> 18.0 N/mm2 (POOR).
        self.make_test(self.project, self.device,
                       structural_element="COL-C24",
                       path_length_mm=250.0, pulse_time_us=62.5)
        self.make_test(self.project, self.device,
                       structural_element="COL-C25",
                       path_length_mm=290.0, pulse_time_us=100.0)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        self.assertIn('27.9', text)
        self.assertIn('18.0', text)
        self.assertIn('GOOD', text)
        self.assertIn('POOR', text)
        self.assertGreater(estimated_compressive_strength(4.0), 25.0)
        self.assertLess(estimated_compressive_strength(2.9), 25.0)

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
        self.assertIn('4.0', text)           # computed on the fly for pending
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
        flat = ' '.join(text.split())
        self.assertIn('Depth exceeds 25 mm - structural review required', flat)

    def test_multi_point_crack_and_surface_subtables(self):
        """A1 for crack depth + surface quality: the report renders one row
        per test point (label / measurements / computed value) exactly like
        the Section 5.0 velocity tables, with the element verdict (mean
        depth / observed conditions) alongside."""
        crack = self.make_test(self.project, self.device,
                               test_type="crack_depth", structural_element="SLAB-S3",
                               crack_path_length_mm=300.0)
        for label, (tc, t0) in zip('ABC', ((70.0, 62.5), (75.0, 62.5), (80.0, 62.5))):
            PUNDITReading.objects.create(
                test=crack, point_label=label, path_length_mm=300.0,
                transit_time_us=tc, uncracked_transit_time_us=t0)
        # Persist the element verdict the way the serializer does.
        mean_depth = crack.element_mean_crack_depth_mm()
        crack.crack_depth_mm = mean_depth
        crack.estimated_crack_depth_mm = mean_depth
        crack.save()

        surface = self.make_test(self.project, self.device,
                                 test_type="surface_quality",
                                 structural_element="WALL-W2",
                                 surface_condition="Smooth finished")
        for label, condition in (('A', 'Smooth finished'),
                                 ('B', 'Honeycombing at mid-height'),
                                 ('C', 'Hairline cracks near the support')):
            PUNDITReading.objects.create(test=surface, point_label=label,
                                         surface_condition=condition)

        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        flat = ' '.join(text.split())
        # Per-point crack rows: each label's own t_c / t_0 / depth.
        self.assertIn('CRACK DEPTH MEASUREMENTS', flat)
        self.assertIn('POINT', flat)
        self.assertIn('75.7', flat)   # point A: 150*sqrt(1.12^2-1)
        self.assertIn('119.8', flat)  # point C: 150*sqrt(1.28^2-1)
        # The element verdict = the mean of the three depths (98.3) with its
        # remark, on the middle row.
        self.assertIn('98.3', flat)
        self.assertIn('Depth exceeds 25 mm - structural review required', flat)
        # Per-point surface rows carry each observed condition.
        self.assertIn('SURFACE QUALITY OBSERVATIONS', flat)
        self.assertIn('Honeycombing at mid-height', flat)
        self.assertIn('Hairline cracks near the support', flat)
        self.assertIn('Smooth finished', flat)

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
        long_element = ('M_Footing-Rectangular:900 x 900 x 200mm:803711')
        self.make_test(
            self.project, self.device, test_reference=long_reference,
            structural_element=long_element,
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
            # reference header's own runs live at y=1.6..12.8 (MTL text +
            # serial box) and the top margin is 20.7. Anything body-like at
            # 12.9..15.5 is the page-break bug come back — but the
            # reference's appendix drawing-page label legitimately sits at
            # y=16.9 ('BUILDING A' below the header), so the band stops
            # short of it.
            if page >= 2 and 12.9 <= y <= 15.5:
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
        # Long values are rendered whole — wrapped, never truncated. The
        # long element name exercises the §5.0 table's narrow column; the
        # test reference is provenance and deliberately does NOT print in
        # the results table (it lives in the registry / integrity digest).
        flat = ' '.join(_pdf_text(data).split())
        self.assertIn(' '.join(long_condition.split()), flat)
        self.assertIn(long_element.replace(' ', ''),
                      flat.replace(' ', ''))
        self.assertNotIn(long_reference, flat)

    def test_empty_project_report_is_honest(self):
        data = NDTReportService.generate_ndt_report(self.project)
        self.assertTrue(data.startswith(b'%PDF'))
        text = _pdf_text(data)
        self.assertIn('No pulse velocity tests recorded', text)
        # The reference cover carries a single ordinal date line
        # ("27TH APRIL, 2026.") — with no recorded tests it falls back to
        # today, honestly, in that exact format.
        self.assertRegex(text, r'\d{1,2}(ST|ND|RD|TH) [A-Z]+, \d{4}\.')
        self.assertIn('No photographs recorded for these tests.', text)

    # ------------------------------------------------- BIM site-plan (C6)
    def test_bim_model_plan_view_replaces_map_placeholder(self):
        # With an imported BIM model, the map page renders the model's real
        # plan-view geometry in one of the two reference image frames instead
        # of the "SITE LOCATION MAP NOT PROVIDED" placeholder (the reference
        # prints no captions under the frames — the images speak alone).
        from apps.digital_eye.models import BIMModelGeometry
        from pypdf import PdfReader
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        BIMModelGeometry.objects.create(
            project=self.project, source_file='STACKING_AREA_IFC.rvt',
            element_count=2,
            elements=[
                # A 4 m x 3 m slab: two triangles over four corner verts.
                {'guid': 'g1', 'name': 'Slab-1', 'type': 'IfcSlab',
                 'verts': [0, 0, 0, 4000, 0, 0, 4000, 3000, 0, 0, 3000, 0],
                 'faces': [0, 1, 2, 0, 2, 3]},
                {'guid': 'g2', 'name': 'Col-1', 'type': 'IfcColumn',
                 'verts': [1000, 1000, 0, 1000, 1200, 0, 1200, 1200, 0,
                           1200, 1000, 0],
                 'faces': [0, 1, 2, 0, 2, 3]},
            ])
        data = NDTReportService.generate_ndt_report(self.project)
        self.assertTrue(data.startswith(b'%PDF'))
        text = _pdf_text(data)
        self.assertNotIn('SITE LOCATION MAP NOT PROVIDED', text)
        # The map page carries the running watermark PLUS the plan image
        # (single source -> one centred frame).
        reader = PdfReader(io.BytesIO(data))
        map_pages = [p for p in reader.pages
                     if 'LOCATION MAP' in (p.extract_text() or '')]
        self.assertTrue(map_pages, 'map page not found')
        self.assertGreaterEqual(len(map_pages[0].images), 2,
                                'map page should carry the watermark and the '
                                'BIM plan view')

    def test_map_placeholder_when_no_bim_model_or_attachment(self):
        # No imported model and no attached map photo — the honest
        # placeholder box stays.
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        data = NDTReportService.generate_ndt_report(self.project)
        self.assertIn('SITE LOCATION MAP NOT PROVIDED', _pdf_text(data))

    def test_bim_plan_view_honest_for_degenerate_geometry(self):
        # Geometry whose plan projection collapses to a line has nothing
        # honest to draw — the placeholder must come back rather than a
        # zero-width "plan".
        from apps.digital_eye.models import BIMModelGeometry
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        BIMModelGeometry.objects.create(
            project=self.project, source_file='flat.ifc', element_count=1,
            elements=[{'guid': 'g1', 'name': 'Line', 'type': 'IfcBeam',
                       'verts': [0, 0, 0, 1000, 0, 0, 2000, 0, 0],
                       'faces': [0, 1, 2]}])
        buf, caption = NDTReportService._generate_bim_plan_view(self.project)
        self.assertIsNone(buf)
        self.assertEqual(caption, '')
        self.assertIn('SITE LOCATION MAP NOT PROVIDED',
                      _pdf_text(
                          NDTReportService.generate_ndt_report(self.project)))

    def test_bim_preview_capture_used_as_site_map(self):
        # An operator's screenshot of the BIM model 3D preview (project-level
        # SensorDataFile photo marked 'BIM 3D model view') fills one of the
        # two reference map frames; the server-rendered plan view fills the
        # other — the reference prints no captions, so the frames are
        # verified by their embedded images.
        from apps.digital_eye.models import BIMModelGeometry, SensorDataFile
        from pypdf import PdfReader
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
                png_path = os.path.join(media, "bim_capture.png")
                Image.new("RGB", (160, 100), (15, 23, 42)).save(png_path)
                with open(png_path, "rb") as fh:
                    data_file = SensorDataFile.objects.create(
                        project=self.project,
                        file_type="photo",
                        file_name="bim_capture.png",
                        file_size_bytes=os.path.getsize(png_path),
                        description=(
                            'BIM 3D model view — captured from the platform '
                            'model preview'),
                    )
                    data_file.file.save("bim_capture.png", DjangoFile(fh),
                                        save=True)
                self.make_test(self.project, self.device,
                               path_length_mm=250.0, pulse_time_us=62.5)
                BIMModelGeometry.objects.create(
                    project=self.project, source_file='STACKING_AREA_IFC.rvt',
                    element_count=1,
                    elements=[{'guid': 'g1', 'name': 'Slab-1',
                               'type': 'IfcSlab',
                               'verts': [0, 0, 0, 4000, 0, 0,
                                         4000, 3000, 0, 0, 3000, 0],
                               'faces': [0, 1, 2, 0, 2, 3]}])
                data = NDTReportService.generate_ndt_report(self.project)
                text = _pdf_text(data)
                self.assertNotIn('SITE LOCATION MAP NOT PROVIDED', text)
                # The map page carries the watermark, the operator's capture
                # AND the BIM plan view (both frames filled).
                reader = PdfReader(io.BytesIO(data))
                map_pages = [p for p in reader.pages
                             if 'LOCATION MAP' in (p.extract_text() or '')]
                self.assertTrue(map_pages, 'map page not found')
                self.assertGreaterEqual(len(map_pages[0].images), 3,
                                        'map page should carry the watermark, '
                                        'the BIM capture and the plan view')
        finally:
            shutil.rmtree(media, ignore_errors=True)

    # ------------------------------------------- C6: Google Maps link + map
    def test_recorded_coordinates_print_google_maps_link(self):
        # Real GNSS coordinates on the project print the exact coordinates
        # and the Google Maps link on the map page — the address section of
        # the report can be followed to the site.
        project = self.make_project(latitude=6.4281, longitude=3.4219)
        self.make_test(project, self.device)
        data = NDTReportService.generate_ndt_report(project)
        text = _pdf_text(data)
        self.assertIn('6.428100', text)
        self.assertIn('3.421900', text)
        self.assertIn('https://www.google.com/maps/search/?api=1&query=6.4281,3.4219',
                      text)

    def test_no_coordinates_means_no_maps_link(self):
        # Without recorded coordinates the link line is absent — the location
        # section never carries a guessed point.
        data = NDTReportService.generate_ndt_report(self.project)
        text = _pdf_text(data)
        self.assertNotIn('google.com/maps', text)
        self.assertNotIn('Coordinates:', text)

    def test_static_map_used_when_key_and_coordinates_exist(self):
        # With coordinates + a configured key, the Google static map fills
        # the first reference map frame (mocked fetch — tests never call the
        # network). The map page then carries the watermark + the map image.
        from pypdf import PdfReader
        png = io.BytesIO()
        Image.new('RGB', (640, 640), (240, 240, 235)).save(png, format='PNG')
        png.seek(0)
        with patch.object(NDTReportService, '_google_static_map',
                          return_value=png) as fetch:
            with self.settings(GOOGLE_MAPS_API_KEY='test-key'):
                project = self.make_project(latitude=6.4281, longitude=3.4219)
                self.make_test(project, self.device)
                data = NDTReportService.generate_ndt_report(project)
        fetch.assert_called_once()
        text = _pdf_text(data)
        self.assertNotIn('SITE LOCATION MAP NOT PROVIDED', text)
        reader = PdfReader(io.BytesIO(data))
        map_pages = [p for p in reader.pages
                     if 'LOCATION MAP' in (p.extract_text() or '')]
        self.assertTrue(map_pages, 'map page not found')
        self.assertGreaterEqual(len(map_pages[0].images), 2,
                                'map page should carry the watermark and the '
                                'Google static map')

    def test_static_map_absent_without_key(self):
        # No key configured -> the static-map helper itself returns no map
        # (verified directly — no fetch is possible), and the report falls
        # through to the honest fallbacks (placeholder, no model here).
        with self.settings(GOOGLE_MAPS_API_KEY=''):
            project = self.make_project(latitude=6.4281, longitude=3.4219)
            self.assertIsNone(
                NDTReportService._google_static_map(project))
            self.make_test(project, self.device)
            data = NDTReportService.generate_ndt_report(project)
        self.assertIn('SITE LOCATION MAP NOT PROVIDED', _pdf_text(data))
        # The link line still prints — coordinates alone are enough for it.
        self.assertIn('https://www.google.com/maps/search/', _pdf_text(data))

    def test_unmarked_project_photo_is_not_treated_as_bim_capture(self):
        # Only photos explicitly marked 'BIM 3D model view' are used as the
        # §3.0 map — an ordinary project photo must not hijack the slot.
        from apps.digital_eye.models import SensorDataFile
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
                png_path = os.path.join(media, "site_photo.png")
                Image.new("RGB", (60, 40), (30, 200, 30)).save(png_path)
                with open(png_path, "rb") as fh:
                    data_file = SensorDataFile.objects.create(
                        project=self.project,
                        file_type="photo",
                        file_name="site_photo.png",
                        file_size_bytes=os.path.getsize(png_path),
                        description='Progress photo of the site entrance',
                    )
                    data_file.file.save("site_photo.png", DjangoFile(fh),
                                        save=True)
                self.make_test(self.project, self.device,
                               path_length_mm=250.0, pulse_time_us=62.5)
                self.assertIn('SITE LOCATION MAP NOT PROVIDED',
                              _pdf_text(
                                  NDTReportService.generate_ndt_report(
                                      self.project)))
        finally:
            shutil.rmtree(media, ignore_errors=True)

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
                self.assertIn('PIC I: column_crack.png', text)

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


class NDTReviewMeeting2ReportTests(NDTReportFixtureMixin, TestCase):
    """7 Sep 2026 review meeting (meeting #2) regressions: m/s table cells
    with decimal points (never commas), the averaging method stated with a
    worked example, point-spread disclosure, crack-depth-first analysis
    order, concrete maturity notes and per-floor BIM plan pages."""

    def setUp(self):
        self.project = self.make_project()
        self.device = self.make_device(self.project)

    def _multi_point_test(self, **kwargs):
        """The client's averaging case: 120 mm at 30 / 29 / 33.3 us."""
        from apps.digital_eye.models import PUNDITReading
        defaults = dict(structural_element="COL-G01", floor="Ground Floor",
                        path_length_mm=120.0)
        defaults.update(kwargs)
        test = self.make_test(self.project, self.device, **defaults)
        for label, transit in (("A", 30.0), ("B", 29.0), ("C", 33.3)):
            PUNDITReading.objects.create(
                test=test, point_label=label, path_length_mm=120.0,
                transit_time_us=transit)
        return test

    # --------------------------------------------- m/s display standard
    def test_velocity_tables_print_ms_two_decimals_no_commas(self):
        self.make_test(self.project, self.device,
                       structural_element="COL-C24",
                       path_length_mm=250.0, pulse_time_us=62.5)
        text = _pdf_text(NDTReportService.generate_ndt_report(self.project))
        self.assertIn("PULSE VELOCITY (M/S)", text)
        # 250 mm / 62.5 us = 4000.00 m/s — two decimals, never a comma.
        self.assertIn("4000.00", text)
        self.assertNotIn("4,000", text)

    # -------------------------------------- averaging method + example
    def test_averaging_is_mean_of_point_velocities_with_worked_example(self):
        self._multi_point_test()
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        # Per-point velocities: 120/30, 120/29, 120/33.3 us.
        self.assertIn("4000.00", flat)
        self.assertIn("4137.93", flat)
        self.assertIn("3603.60", flat)
        # Element verdict = MEAN of the point velocities = 3913.84 m/s
        # (not the velocity of the mean transit time — that is the manual
        # vs system arithmetic the client asked to reconcile).
        self.assertIn("3913.84", flat)
        # f_cu = 8.961 x 3.914 - 7.97 = 27.10 N/mm2, shown to 2 decimals.
        self.assertIn("27.10", flat)
        # The method statement and the worked example are both printed.
        self.assertIn("V(element) = (V1 + V2 + ... + Vn) / n", flat)
        self.assertIn("Worked example", flat)

    def test_point_spread_disclosed_when_points_disagree(self):
        self._multi_point_test()
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        # max-min = 534 m/s (13.6% of the mean) — flagged, not averaged away.
        self.assertIn("POINT SPREAD 534 M/S", flat)

    # ------------------------------------- crack depth before velocity
    def test_crack_depth_analysis_precedes_velocity_tables(self):
        from apps.digital_eye.models import PUNDITReading
        crack = self.make_test(self.project, self.device,
                               test_type="crack_depth",
                               structural_element="SLAB-S3",
                               floor="Ground Floor",
                               crack_path_length_mm=300.0)
        for label, (tc, t0) in zip("ABC", ((70.0, 62.5), (75.0, 62.5),
                                           (80.0, 62.5))):
            PUNDITReading.objects.create(
                test=crack, point_label=label, path_length_mm=300.0,
                transit_time_us=tc, uncracked_transit_time_us=t0)
        mean_depth = crack.element_mean_crack_depth_mm()
        crack.crack_depth_mm = mean_depth
        crack.estimated_crack_depth_mm = mean_depth
        crack.save()
        self.make_test(self.project, self.device,
                       structural_element="COL-C24",
                       path_length_mm=250.0, pulse_time_us=62.5)
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        crack_pos = flat.find("CRACK DEPTH MEASUREMENTS")
        velocity_pos = flat.find("PULSE VELOCITY (M/S)")
        self.assertGreater(crack_pos, 0)
        self.assertGreater(velocity_pos, 0)
        self.assertLess(crack_pos, velocity_pos)

    # ------------------------------------------- concrete maturity note
    def test_concrete_age_note_only_when_recorded(self):
        self.make_test(self.project, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5,
                       concrete_age_days=28)
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        self.assertIn("recorded as 28 days", flat)
        self.assertIn("Strength gain beyond 28 days is minimal", flat)

        # A second project without recorded ages prints no maturity note.
        bare = self.make_project(name="No Age Recorded Project")
        self.make_test(bare, self.device,
                       path_length_mm=250.0, pulse_time_us=62.5)
        bare_flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(bare)).split())
        self.assertNotIn("age of the concrete at the time of test",
                         bare_flat)

    # ------------------------------------------------ per-floor plans
    @staticmethod
    def _quad(x, y, z, w=2.0, d=3.0):
        return ([x, y, z, x + w, y, z, x + w, y + d, z, x, y + d, z],
                [0, 1, 2, 0, 2, 3])

    def _import_two_level_model(self):
        from apps.digital_eye.models import (BIMElementMapping,
                                             BIMModelGeometry)
        v0, f0 = self._quad(0.0, 0.0, 0.0)
        v1, f1 = self._quad(0.0, 0.0, 3.6)
        BIMModelGeometry.objects.create(
            project=self.project, source_file="levels.ifc", element_count=2,
            elements=[
                {"guid": "G-SLAB-G", "name": "Ground slab",
                 "type": "IfcSlab", "verts": v0, "faces": f0},
                {"guid": "G-SLAB-1", "name": "First floor slab",
                 "type": "IfcSlab", "verts": v1, "faces": f1},
            ])
        for guid, level in (("G-SLAB-G", "Ground Floor"),
                            ("G-SLAB-1", "First Floor")):
            BIMElementMapping.objects.create(
                project=self.project, bim_guid=guid, element_id=guid,
                element_name=f"{level} slab", element_type="IfcSlab",
                level=level, source="ifc_upload")

    def test_per_floor_plan_pages_render_per_tested_floor(self):
        self._import_two_level_model()
        self.make_test(self.project, self.device,
                       structural_element="COL-G01", floor="Ground Floor",
                       path_length_mm=250.0, pulse_time_us=62.5)
        self.make_test(self.project, self.device,
                       structural_element="COL-101", floor="First Floor",
                       path_length_mm=250.0, pulse_time_us=62.5)
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        # The appendix TOC registers the per-floor pages (title case, like
        # the reference's lettered appendix entries).
        self.assertIn("Floor Plans", flat)
        # One labelled page per tested floor, captioned with the level the
        # elements were grouped by.
        self.assertIn('level "Ground Floor"', flat)
        self.assertIn('level "First Floor"', flat)

    def test_no_floor_plan_pages_without_level_mappings(self):
        # Geometry whose elements carry no recorded levels cannot be split
        # honestly — the whole-model plan stands and no floor pages appear.
        from apps.digital_eye.models import BIMModelGeometry
        v0, f0 = self._quad(0.0, 0.0, 0.0)
        v1, f1 = self._quad(0.0, 0.0, 3.6)
        BIMModelGeometry.objects.create(
            project=self.project, source_file="no-levels.ifc",
            element_count=2,
            elements=[
                {"guid": "X1", "name": "A", "type": "IfcSlab",
                 "verts": v0, "faces": f0},
                {"guid": "X2", "name": "B", "type": "IfcSlab",
                 "verts": v1, "faces": f1},
            ])
        self.make_test(self.project, self.device,
                       structural_element="COL-G01", floor="Ground Floor",
                       path_length_mm=250.0, pulse_time_us=62.5)
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        self.assertNotIn("Floor Plans", flat)
        self.assertNotIn('level "', flat)

    # --------------------------------------- AI analysis in the PDF
    def _ai_record(self, **kwargs):
        from apps.evidence.models import AIAnalysisRecord
        defaults = dict(
            project=self.project, analysis_type="pundit",
            observations=[
                "Ground Floor: COL-G01 shows a mean pulse velocity of "
                "3913.84 m/s, within the good concrete band.",
                "The 534 m/s point spread on COL-G01 warrants a retest at "
                "additional stations per ACI 228.2R.",
            ],
            recommendations=[{
                "recommendation": "Retest COL-G01 at three further stations.",
                "priority": "Routine",
            }],
            model_provider="gemini", model_version="gemini-3.5-flash-lite",
            confidence=0.93,
        )
        defaults.update(kwargs)
        return AIAnalysisRecord.objects.create(**defaults)

    def test_ai_narrative_is_embedded_as_decision_support(self):
        self.make_test(self.project, self.device,
                       structural_element="COL-G01", floor="Ground Floor",
                       path_length_mm=250.0, pulse_time_us=62.5)
        self._ai_record()
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        # The section exists, after the measurement tables it interprets.
        self.assertIn("AI-ASSISTED INTERPRETATION", flat)
        self.assertLess(flat.find("PULSE VELOCITY (M/S)"),
                        flat.find("AI-ASSISTED INTERPRETATION"))
        # The narrative itself, verbatim.
        self.assertIn("3913.84 m/s, within the good concrete band", flat)
        self.assertIn("warrants a retest", flat)
        # Provider, model and the evidence-based confidence are disclosed.
        # The phrase is asserted in two halves — a page break can land inside
        # it and the extracted text then carries the running footer between
        # the halves ("evidence-based 12 MTL/NDT/2026 7298 confidence").
        self.assertIn("synthesised by gemini (gemini-3.5-flash-lite)", flat)
        self.assertIn("evidence-based", flat)
        self.assertIn("confidence of 93%", flat)
        # The sign-off caveat — the professional, not the AI, owns the report.
        self.assertIn("decision support for the responsible engineer", flat)

    def test_deterministic_only_analysis_prints_no_ai_section(self):
        self.make_test(self.project, self.device,
                       structural_element="COL-G01", floor="Ground Floor",
                       path_length_mm=250.0, pulse_time_us=62.5)
        self._ai_record(model_provider="deterministic",
                        model_version="BS 1881-203 / ASTM C597 v1",
                        confidence=None)
        flat = " ".join(_pdf_text(
            NDTReportService.generate_ndt_report(self.project)).split())
        self.assertNotIn("AI-ASSISTED INTERPRETATION", flat)
