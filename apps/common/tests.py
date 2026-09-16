from django.test import TestCase, override_settings
from django.conf import settings
from unittest import mock
from unittest.mock import patch, MagicMock
from io import BytesIO
from pathlib import Path
import csv
import json
import tempfile

import requests
from PIL import Image

from apps.common.ai_service import (
    AIService, AIServiceError, AIQuotaExceeded, AIProviderUnavailable,
)


def _png_bytes(color=(255, 0, 0), size=(10, 10), mode="RGB"):
    """Real PNG bytes so image-handling paths run against genuine images."""
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _openai_client_with_content(content):
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    client.chat.completions.create.return_value = response
    return client

class AIServiceTests(TestCase):

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="", OPENAI_API_KEY="")
    def test_fallback_mock_responses(self):
        """Without provider keys the AI service must NOT fabricate findings.

        A mock URL points at no real image, so visual and thermal detection
        return an empty list (nothing real to report) instead of invented
        defects, and recommendation generation honestly returns no
        recommendations rather than invented guidance.
        """
        # 1. Defect detection — no real image, no findings
        defects = AIService.detect_visual_defects("mock_url")
        self.assertEqual(defects, [])

        # 2. Thermal anomaly fallback — same rule
        anomalies = AIService.detect_thermal_anomalies("mock_url")
        self.assertEqual(anomalies, [])

        # 3. No provider key configured — no recommendations are invented.
        recs = AIService.generate_recommendations([{"type": "crack", "severity": "high"}], [], 0.01)
        self.assertEqual(recs["recommendations"], [])
        self.assertIsNone(recs["text_confidence"])

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="fake-gemini-key")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_visual_defects(self, mock_generative_model):
        """Test Gemini integration for visual defect detection."""
        mock_model_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '[{"type": "spalling", "severity": "medium", "description": "Concrete spalling on column", "location_x": 1.0, "location_y": 2.0, "location_z": 3.0}]'
        mock_model_instance.generate_content.return_value = mock_response
        mock_generative_model.return_value = mock_model_instance

        defects = AIService.detect_visual_defects("http://example.com/image.jpg")
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["type"], "spalling")
        self.assertEqual(defects[0]["severity"], "medium")

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="fake-openai-key")
    @patch("apps.common.ai_service.OpenAI")
    def test_openai_visual_defects(self, mock_openai_class):
        """Test OpenAI integration for visual defect detection."""
        mock_client = MagicMock()
        mock_chat = MagicMock()
        mock_completions = MagicMock()
        
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.content = '[{"type": "corrosion", "severity": "critical", "description": "Severe rebar corrosion", "location_x": 0.0, "location_y": 0.0, "location_z": 0.0}]'
        
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]
        mock_completions.create.return_value = mock_response
        mock_chat.completions = mock_completions
        mock_client.chat = mock_chat
        mock_openai_class.return_value = mock_client

        defects = AIService.detect_visual_defects("http://example.com/image.jpg")
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["type"], "corrosion")
        self.assertEqual(defects[0]["severity"], "critical")


class AIServiceConfigTests(TestCase):
    """Provider selection and client initialisation."""

    def test_provider_defaults_to_gemini_without_setting(self):
        # AI_PROVIDER is not a Django setting; it comes from the environment.
        with mock.patch.dict("os.environ"):
            import os
            os.environ.pop("AI_PROVIDER", None)
            self.assertEqual(AIService._get_provider(), "gemini")

    def test_provider_read_from_environment(self):
        with mock.patch.dict("os.environ", {"AI_PROVIDER": "OpenAI"}):
            # Provider name is normalised to lowercase.
            self.assertEqual(AIService._get_provider(), "openai")

    @override_settings(AI_PROVIDER="openai", OPENAI_MODEL="gpt-4o-mini",
                       GEMINI_MODEL="gemini-2.0-flash", AI_MAX_RETRIES=5)
    def test_model_and_retry_settings_are_honoured(self):
        self.assertEqual(AIService._get_provider(), "openai")
        self.assertEqual(AIService._get_openai_model(), "gpt-4o-mini")
        self.assertEqual(AIService._get_gemini_model(), "gemini-2.0-flash")
        self.assertEqual(AIService._get_max_retries(), 5)

    @override_settings(OPENAI_API_KEY="")
    def test_openai_client_unavailable_without_key(self):
        with self.assertRaises(AIProviderUnavailable):
            AIService._get_openai_client()

    @override_settings(OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    def test_openai_client_built_with_key(self, mock_openai):
        AIService._get_openai_client()
        mock_openai.assert_called_once_with(api_key="sk-test")

    @override_settings(GEMINI_API_KEY="")
    def test_gemini_model_unavailable_without_key(self):
        with self.assertRaises(AIProviderUnavailable):
            AIService._get_gemini_model_instance()

    @override_settings(GEMINI_API_KEY="gm-test", GEMINI_MODEL="gemini-test-model")
    @patch("apps.common.ai_service.genai")
    def test_gemini_model_instance_built_with_key(self, mock_genai):
        AIService._get_gemini_model_instance()
        mock_genai.configure.assert_called_once_with(api_key="gm-test")
        mock_genai.GenerativeModel.assert_called_once_with("gemini-test-model")


class AIServiceImageFetchTests(TestCase):
    """Image download for both providers — all HTTP mocked."""

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_openai_fetch_empty_url_raises(self):
        with self.assertRaises(ValueError):
            AIService._fetch_image_for_openai("")

    def test_openai_fetch_example_url_short_circuits_without_network(self):
        with patch("apps.common.ai_service.requests.get") as mock_get:
            result = AIService._fetch_image_for_openai("http://example.com/photo.jpg")
            mock_get.assert_not_called()
        self.assertTrue(result.startswith("data:image/jpeg;base64,"))

    def test_openai_fetch_encodes_real_download(self):
        payload = _png_bytes()
        resp = MagicMock(status_code=200, content=payload)
        resp.raise_for_status.return_value = None
        with patch("apps.common.ai_service.requests.get", return_value=resp) as mock_get:
            result = AIService._fetch_image_for_openai("https://cdn.nexucon-pilot.site/real.jpg")
            mock_get.assert_called_once_with("https://cdn.nexucon-pilot.site/real.jpg", timeout=20)
        import base64
        self.assertEqual(
            result, "data:image/jpeg;base64," + base64.b64encode(payload).decode()
        )

    def test_openai_fetch_download_failure_raises_value_error(self):
        with patch("apps.common.ai_service.requests.get",
                   side_effect=RuntimeError("network down")):
            with self.assertRaises(ValueError):
                AIService._fetch_image_for_openai("https://cdn.nexucon-pilot.site/gone.jpg")

    def test_gemini_fetch_empty_url_raises(self):
        with self.assertRaises(ValueError):
            AIService._fetch_image_for_gemini("")

    def test_gemini_fetch_example_url_returns_pil_image(self):
        with patch("apps.common.ai_service.requests.get") as mock_get:
            image = AIService._fetch_image_for_gemini("http://example.com/photo.jpg")
            mock_get.assert_not_called()
        self.assertIsInstance(image, Image.Image)
        self.assertEqual(image.mode, "RGB")

    def test_gemini_fetch_converts_non_rgb_modes(self):
        # A palette-mode PNG must be converted to RGB before analysis.
        payload = _png_bytes(color=5, size=(8, 8), mode="P")
        resp = MagicMock(status_code=200, content=payload)
        resp.raise_for_status.return_value = None
        with patch("apps.common.ai_service.requests.get", return_value=resp):
            image = AIService._fetch_image_for_gemini("https://cdn.nexucon-pilot.site/pal.png")
        self.assertIn(image.mode, ("RGB", "RGBA"))

    def test_gemini_fetch_download_failure_raises_value_error(self):
        with patch("apps.common.ai_service.requests.get",
                   side_effect=RuntimeError("timeout")):
            with self.assertRaises(ValueError):
                AIService._fetch_image_for_gemini("https://cdn.nexucon-pilot.site/x.png")


class AIServiceOpenAIRetryTests(TestCase):
    """OpenAI generation with retries — SDK client fully mocked."""

    @override_settings(OPENAI_MODEL="gpt-4o", AI_MAX_RETRIES=2)
    def test_success_returns_content_first_attempt(self):
        client = _openai_client_with_content('{"defects": []}')
        result = AIService._generate_with_openai_retry(
            client, [{"role": "user", "content": "hi"}], response_format="json_object"
        )
        self.assertEqual(result, '{"defects": []}')
        client.chat.completions.create.assert_called_once_with(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        )

    @override_settings(AI_MAX_RETRIES=2)
    def test_no_response_format_kwarg_when_not_requested(self):
        client = _openai_client_with_content("plain answer")
        AIService._generate_with_openai_retry(client, [{"role": "user", "content": "hi"}])
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertNotIn("response_format", kwargs)

    @override_settings(AI_MAX_RETRIES=2)
    def test_permanent_quota_exhaustion_raises_without_retry(self):
        from openai import RateLimitError as RealRateLimitError
        client = MagicMock()
        client.chat.completions.create.side_effect = RealRateLimitError(
            "You exceeded your current quota, please check your plan",
            response=MagicMock(), body=None,
        )
        with self.assertRaises(AIQuotaExceeded):
            AIService._generate_with_openai_retry(client, [])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    @override_settings(AI_MAX_RETRIES=2)
    @patch("apps.common.ai_service.time.sleep")
    def test_temporary_rate_limit_sleeps_parsed_delay_then_retries(self, mock_sleep):
        from openai import RateLimitError as RealRateLimitError
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            RealRateLimitError("Rate limit reached. Please try again in 2s",
                               response=MagicMock(), body=None),
            MagicMock(choices=[MagicMock(message=MagicMock(content='{"ok": 1}'))]),
        ]
        result = AIService._generate_with_openai_retry(client, [])
        self.assertEqual(result, '{"ok": 1}')
        # "try again in 2s" -> wait_time = 2 + 1 = 3 seconds
        mock_sleep.assert_called_once_with(3.0)

    @override_settings(AI_MAX_RETRIES=1)
    @patch("apps.common.ai_service.time.sleep")
    def test_rate_limit_exhausted_after_retries_raises(self, mock_sleep):
        from openai import RateLimitError as RealRateLimitError
        client = MagicMock()
        client.chat.completions.create.side_effect = RealRateLimitError(
            "Rate limit reached. Please try again in 1s",
            response=MagicMock(), body=None,
        )
        with self.assertRaises(AIServiceError):
            AIService._generate_with_openai_retry(client, [])
        self.assertEqual(client.chat.completions.create.call_count, 2)  # 1 + 1 retry
        mock_sleep.assert_called_once()

    @override_settings(AI_MAX_RETRIES=2)
    def test_generic_error_raises_immediately_without_retry(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        with self.assertRaises(AIServiceError):
            AIService._generate_with_openai_retry(client, [])
        self.assertEqual(client.chat.completions.create.call_count, 1)


class AIServiceGeminiRetryTests(TestCase):

    def test_quota_indicator_classification(self):
        self.assertTrue(AIService._is_gemini_quota_exhausted(
            Exception("You exceeded your current quota")))
        self.assertTrue(AIService._is_gemini_quota_exhausted(
            Exception("RESOURCE_EXHAUSTED: quota_exceeded for model")))
        # A retriable "retry in Xs" error is NOT permanent exhaustion.
        self.assertFalse(AIService._is_gemini_quota_exhausted(
            Exception("429 rate limited, please retry in 30s")))

    def test_daily_free_tier_quota_is_permanent_despite_retry_hint(self):
        # The REAL free-tier daily quota error (observed 10 Sep 2026) carries
        # a misleading "Please retry in 56s" hint alongside the daily quota
        # metric — it must fail fast, never enter the 60 s sleep-retry loop.
        self.assertTrue(AIService._is_gemini_quota_exhausted(
            Exception(
                "429 You exceeded your current quota, please check your plan "
                "and billing details. * Quota exceeded for metric: "
                "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                "limit: 20, model: gemini-3.8-flash. Please retry in "
                "56.00884797s. [violations { quota_id: "
                "GenerateRequestsPerDayPerProjectPerModel-FreeTier }]")))
        # ...while a plain per-minute throttle with the same hint stays
        # retriable (no daily/free-tier metric in the text).
        self.assertFalse(AIService._is_gemini_quota_exhausted(
            Exception("429 quota exceeded for metric "
                      "generate_content_per_minute_requests, please retry in 30s")))

    def test_invalid_api_key_is_permanent_not_retriable(self):
        # Google answers an invalid/expired key with 429 RESOURCE_EXHAUSTED —
        # it must be classified permanent so no 60 s retry sleeps happen.
        for text in ("429 RESOURCE_EXHAUSTED. API key not valid. "
                     "Please pass a valid API key.",
                     "permission_denied: API_KEY_INVALID",
                     "API key expired"):
            self.assertTrue(AIService._is_gemini_quota_exhausted(Exception(text)))
        # ...and the quota check runs before the temporary classifier, so the
        # '429' in those messages never reaches the retriable branch.
        self.assertTrue(AIService._is_gemini_temporary_rate_limit(
            Exception("429 RESOURCE_EXHAUSTED. API key not valid.")))

    def test_temporary_rate_limit_classification(self):
        for text in ("429 too many requests", "rate limit exceeded",
                     "RATE_LIMIT hit", "Too Many Requests"):
            self.assertTrue(AIService._is_gemini_temporary_rate_limit(Exception(text)))
        self.assertFalse(AIService._is_gemini_temporary_rate_limit(Exception("server error")))

    def test_retry_second_extraction(self):
        self.assertEqual(
            AIService._extract_gemini_retry_seconds(Exception("Please retry in 5s")), 6.0)
        self.assertEqual(
            AIService._extract_gemini_retry_seconds(Exception("RetryDelay: seconds: 10")), 11.0)
        self.assertEqual(
            AIService._extract_gemini_retry_seconds(Exception("no delay info")), 60.0)

    @override_settings(AI_MAX_RETRIES=2)
    def test_success_returns_text(self):
        model = MagicMock()
        model.generate_content.return_value = MagicMock(text='{"anomalies": []}')
        result = AIService._generate_with_gemini_retry(
            model, ["prompt"], generation_config={"response_mime_type": "application/json"}
        )
        self.assertEqual(result, '{"anomalies": []}')

    @override_settings(AI_MAX_RETRIES=2)
    def test_permanent_quota_exhaustion_raises_without_retry(self):
        model = MagicMock()
        model.generate_content.side_effect = Exception(
            "429 You exceeded your current quota, please top up")
        with self.assertRaises(AIQuotaExceeded):
            AIService._generate_with_gemini_retry(model, "prompt")
        self.assertEqual(model.generate_content.call_count, 1)

    @override_settings(AI_MAX_RETRIES=2)
    @patch("apps.common.ai_service.time.sleep")
    def test_temporary_rate_limit_retries_then_succeeds(self, mock_sleep):
        model = MagicMock()
        model.generate_content.side_effect = [
            Exception("429 rate limit, retry in 3s"),
            MagicMock(text='{"ok": true}'),
        ]
        result = AIService._generate_with_gemini_retry(model, "prompt")
        self.assertEqual(result, '{"ok": true}')
        mock_sleep.assert_called_once_with(4.0)

    @override_settings(AI_MAX_RETRIES=1)
    @patch("apps.common.ai_service.time.sleep")
    def test_temporary_rate_limit_exhausted_raises(self, mock_sleep):
        model = MagicMock()
        model.generate_content.side_effect = Exception("429 rate limit, retry in 3s")
        with self.assertRaises(AIServiceError):
            AIService._generate_with_gemini_retry(model, "prompt")
        self.assertEqual(model.generate_content.call_count, 2)

    @override_settings(AI_MAX_RETRIES=2)
    def test_generic_error_raises_immediately(self):
        model = MagicMock()
        model.generate_content.side_effect = RuntimeError("api broke")
        with self.assertRaises(AIServiceError):
            AIService._generate_with_gemini_retry(model, "prompt")
        self.assertEqual(model.generate_content.call_count, 1)


class AIServiceParsingTests(TestCase):

    def test_parse_empty_response_raises(self):
        for empty in ("", None):
            with self.assertRaises(ValueError):
                AIService._parse_json_response(empty)

    def test_parse_plain_json(self):
        self.assertEqual(AIService._parse_json_response(' {"a": 1} '), {"a": 1})

    def test_parse_fenced_json_block(self):
        self.assertEqual(
            AIService._parse_json_response('```json\n{"a": [1, 2]}\n```'), {"a": [1, 2]})
        self.assertEqual(
            AIService._parse_json_response('```\n{"b": 2}\n```'), {"b": 2})

    def test_parse_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            AIService._parse_json_response('{"defects": [}')

    def test_parse_non_json_text_raises(self):
        with self.assertRaises(ValueError):
            AIService._parse_json_response("Sorry, I cannot analyse that image.")

    def test_normalise_list_passthrough_and_extraction(self):
        self.assertEqual(AIService._normalise_list([1, 2]), [1, 2])
        self.assertEqual(AIService._normalise_list({"defects": [1, 2]}, key="defects"), [1, 2])
        # A bare dict with no matching key is returned as a single item.
        self.assertEqual(AIService._normalise_list({"a": 1}, key="defects"), [{"a": 1}])
        # Scalars and None degrade to an empty list — nothing is invented.
        self.assertEqual(AIService._normalise_list(42, key="defects"), [])
        self.assertEqual(AIService._normalise_list(None, key="defects"), [])


class AIServiceBboxNormalisationTests(TestCase):

    def test_no_items_passthrough(self):
        self.assertEqual(AIService._normalise_bboxes([], "http://example.com/i.jpg"), [])
        self.assertIsNone(AIService._normalise_bboxes(None, "http://example.com/i.jpg"))

    def test_already_normalised_bbox_unchanged(self):
        items = [{"image_bbox": {"xmin": 0.1, "ymin": 0.2, "xmax": 0.5, "ymax": 0.8}}]
        result = AIService._normalise_bboxes(items, "http://example.com/i.jpg")
        self.assertEqual(result[0]["image_bbox"],
                         {"xmin": 0.1, "ymin": 0.2, "xmax": 0.5, "ymax": 0.8})

    def test_pixel_coordinates_converted_with_real_image_dimensions(self):
        # A 200x100 image: pixel bboxes must be divided by width/height.
        resp = MagicMock(content=_png_bytes(size=(200, 100)))
        resp.raise_for_status.return_value = None
        items = [{"image_bbox": {"xmin": 20.0, "ymin": 10.0, "xmax": 60.0, "ymax": 40.0}}]
        with patch("apps.common.ai_service.requests.get", return_value=resp):
            result = AIService._normalise_bboxes(items, "https://cdn.example.test/img.png")
        bbox = result[0]["image_bbox"]
        self.assertAlmostEqual(bbox["xmin"], 0.1)
        self.assertAlmostEqual(bbox["ymin"], 0.1)
        self.assertAlmostEqual(bbox["xmax"], 0.3)
        self.assertAlmostEqual(bbox["ymax"], 0.4)

    def test_swapped_min_max_repaired(self):
        items = [{"image_bbox": {"xmin": 0.8, "ymin": 0.9, "xmax": 0.2, "ymax": 0.4}}]
        result = AIService._normalise_bboxes(items, "http://example.com/i.jpg")
        bbox = result[0]["image_bbox"]
        self.assertLessEqual(bbox["xmin"], bbox["xmax"])
        self.assertLessEqual(bbox["ymin"], bbox["ymax"])

    def test_degenerate_sliver_expanded_to_visible_minimum(self):
        items = [{"image_bbox": {"xmin": 0.5, "ymin": 0.5, "xmax": 0.5, "ymax": 0.5}}]
        result = AIService._normalise_bboxes(items, "http://example.com/i.jpg")
        bbox = result[0]["image_bbox"]
        self.assertGreaterEqual(bbox["xmax"] - bbox["xmin"], 0.05)
        self.assertGreaterEqual(bbox["ymax"] - bbox["ymin"], 0.05)

    def test_invalid_bboxes_become_none_not_guessed(self):
        items = [
            {"image_bbox": {"xmin": "a", "ymin": None, "xmax": 0.5, "ymax": 0.5}},
            {"image_bbox": "garbage"},
            {"no_bbox_here": True},
        ]
        result = AIService._normalise_bboxes(items, "http://example.com/i.jpg")
        self.assertEqual([item["image_bbox"] for item in result], [None, None, None])

    def test_non_dict_items_are_skipped_untouched(self):
        items = ["garbage", 42, {"image_bbox": {"xmin": 0.1, "ymin": 0.1,
                                                 "xmax": 0.4, "ymax": 0.4}}]
        result = AIService._normalise_bboxes(items, "http://example.com/i.jpg")
        self.assertEqual(result[0], "garbage")
        self.assertEqual(result[1], 42)
        self.assertEqual(result[2]["image_bbox"]["xmin"], 0.1)

    def test_pixel_bbox_with_unfetchable_image_is_clamped(self):
        with patch("apps.common.ai_service.requests.get",
                   side_effect=RuntimeError("offline")):
            items = [{"image_bbox": {"xmin": 10.0, "ymin": 20.0, "xmax": 300.0, "ymax": 400.0}}]
            result = AIService._normalise_bboxes(items, "https://cdn.example.test/gone.png")
        bbox = result[0]["image_bbox"]
        for value in bbox.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class AIServiceVisualDefectFlowTests(TestCase):
    """End-to-end visual defect detection with both provider paths mocked."""

    def test_empty_url_raises(self):
        with self.assertRaises(ValueError):
            AIService.detect_visual_defects("")

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    def test_openai_primary_wraps_defects_key(self, mock_openai):
        mock_openai.return_value = _openai_client_with_content(
            '{"defects": [{"type": "concrete_crack", "severity": "high", '
            '"description": "Flexural crack at mid-span.", "confidence_score": 0.91, '
            '"image_bbox": {"xmin": 0.1, "ymin": 0.1, "xmax": 0.4, "ymax": 0.5}}]}'
        )
        defects = AIService.detect_visual_defects("http://example.com/beam.jpg")
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["type"], "concrete_crack")
        self.assertEqual(defects[0]["confidence_score"], 0.91)
        self.assertEqual(defects[0]["image_bbox"]["xmax"], 0.4)

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test",
                       GEMINI_API_KEY="gm-test")
    @patch("google.generativeai.GenerativeModel")
    @patch("apps.common.ai_service.OpenAI")
    def test_malformed_primary_json_falls_back_to_secondary(self, mock_openai, mock_gm):
        mock_openai.return_value = _openai_client_with_content('{"defects": [broken')
        gemini_instance = MagicMock()
        gemini_instance.generate_content.return_value = MagicMock(
            text='{"defects": [{"type": "spalling", "severity": "low"}]}')
        mock_gm.return_value = gemini_instance
        defects = AIService.detect_visual_defects("http://example.com/slab.jpg")
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["type"], "spalling")

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    def test_empty_defects_list_is_preserved(self, mock_openai):
        # A clean image legitimately yields zero defects — not an error.
        mock_openai.return_value = _openai_client_with_content('{"defects": []}')
        self.assertEqual(
            AIService.detect_visual_defects("http://example.com/clean.jpg"), [])

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="", OPENAI_API_KEY="")
    def test_all_providers_unconfigured_returns_empty_no_fabrication(self):
        self.assertEqual(AIService.detect_visual_defects("http://example.com/x.jpg"), [])

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    def test_pixel_bbox_normalised_in_flow(self, mock_openai):
        mock_openai.return_value = _openai_client_with_content(
            '{"defects": [{"type": "corrosion", "severity": "medium", '
            '"image_bbox": {"xmin": 20.0, "ymin": 10.0, "xmax": 60.0, "ymax": 40.0}}]}'
        )
        resp = MagicMock(content=_png_bytes(size=(200, 100)))
        resp.raise_for_status.return_value = None
        with patch("apps.common.ai_service.requests.get", return_value=resp):
            defects = AIService.detect_visual_defects("http://example.com/col.jpg")
        self.assertAlmostEqual(defects[0]["image_bbox"]["xmax"], 0.3)


class AIServiceThermalFlowTests(TestCase):

    def test_empty_url_raises(self):
        with self.assertRaises(ValueError):
            AIService.detect_thermal_anomalies("")

    def test_mock_url_returns_empty_no_fabrication(self):
        self.assertEqual(AIService.detect_thermal_anomalies("mock_url"), [])
        self.assertEqual(AIService.detect_thermal_anomalies("http://x.test/mock.jpg"), [])

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_primary_thermal_anomalies(self, mock_gm):
        instance = MagicMock()
        instance.generate_content.return_value = MagicMock(
            text='{"anomalies": [{"temperature_variance": 4.5, "severity": "high", '
                 '"confidence_score": 0.88, '
                 '"image_bbox": {"xmin": 0.2, "ymin": 0.2, "xmax": 0.6, "ymax": 0.7}}]}')
        mock_gm.return_value = instance
        anomalies = AIService.detect_thermal_anomalies("http://example.com/thermal.jpg")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["temperature_variance"], 4.5)
        self.assertEqual(anomalies[0]["severity"], "high")

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test",
                       OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_failure_falls_back_to_openai(self, mock_gm, mock_openai):
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("gemini down")
        mock_gm.return_value = instance
        mock_openai.return_value = _openai_client_with_content(
            '{"anomalies": [{"temperature_variance": 1.2, "severity": "low"}]}')
        anomalies = AIService.detect_thermal_anomalies("http://example.com/t.jpg")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["severity"], "low")

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="", OPENAI_API_KEY="")
    def test_all_providers_fail_returns_empty_no_fabrication(self):
        self.assertEqual(
            AIService.detect_thermal_anomalies("http://example.com/t.jpg"), [])


class AIServiceDelaminationFlowTests(TestCase):

    def test_empty_urls_raise(self):
        with self.assertRaises(ValueError):
            AIService.detect_delamination_multimodal("", "http://example.com/v.jpg")
        with self.assertRaises(ValueError):
            AIService.detect_delamination_multimodal("http://example.com/t.jpg", "")

    def test_mock_urls_return_empty_no_fabrication(self):
        self.assertEqual(
            AIService.detect_delamination_multimodal("mock_url", "mock_url"), [])
        self.assertEqual(
            AIService.detect_delamination_multimodal(
                "http://x.test/mock.jpg", "http://example.com/v.jpg"), [])

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test")
    @patch("apps.common.ai_service.local_ml_pipeline")
    def test_local_pipeline_results_used_when_available(self, mock_pipeline):
        local_result = [{
            "type": "delamination", "severity": "high",
            "is_false_positive": False, "confidence_score": 0.85,
        }]
        mock_pipeline.process_images.return_value = local_result
        result = AIService.detect_delamination_multimodal(
            "http://example.com/t.jpg", "http://example.com/v.jpg")
        self.assertEqual(result, local_result)
        mock_pipeline.process_images.assert_called_once_with(
            "http://example.com/t.jpg", "http://example.com/v.jpg")

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.local_ml_pipeline")
    @patch("apps.common.ai_service.OpenAI")
    def test_local_pipeline_none_falls_back_to_provider(self, mock_openai, mock_pipeline):
        mock_pipeline.process_images.return_value = None
        mock_openai.return_value = _openai_client_with_content(
            '{"delaminations": [{"type": "delamination", "severity": "medium", '
            '"is_false_positive": false}]}')
        result = AIService.detect_delamination_multimodal(
            "http://example.com/t.jpg", "http://example.com/v.jpg")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "delamination")

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.local_ml_pipeline")
    @patch("apps.common.ai_service.OpenAI")
    def test_local_pipeline_error_falls_back_to_provider(self, mock_openai, mock_pipeline):
        mock_pipeline.process_images.side_effect = RuntimeError("torch exploded")
        mock_openai.return_value = _openai_client_with_content('{"delaminations": []}')
        result = AIService.detect_delamination_multimodal(
            "http://example.com/t.jpg", "http://example.com/v.jpg")
        self.assertEqual(result, [])

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test")
    @patch("apps.common.ai_service.local_ml_pipeline")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_provider_path_for_delamination(self, mock_gm, mock_pipeline):
        mock_pipeline.process_images.return_value = None
        instance = MagicMock()
        instance.generate_content.return_value = MagicMock(
            text='{"delaminations": [{"type": "delamination", "severity": "high", '
                 '"is_false_positive": false}]}')
        mock_gm.return_value = instance
        result = AIService.detect_delamination_multimodal(
            "http://example.com/t.jpg", "http://example.com/v.jpg")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["severity"], "high")

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="", OPENAI_API_KEY="")
    def test_all_providers_fail_returns_empty_no_fabrication(self):
        self.assertEqual(
            AIService.detect_delamination_multimodal(
                "http://example.com/t.jpg", "http://example.com/v.jpg"), [])


class AIServiceStructuredJSONTests(TestCase):

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    def test_openai_primary_returns_parsed_json(self, mock_openai):
        mock_openai.return_value = _openai_client_with_content('{"summary": "ok", "n": 3}')
        result = AIService.generate_structured_json("Summarise this inspection.")
        self.assertEqual(result, {"summary": "ok", "n": 3})

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_primary_returns_parsed_json(self, mock_gm):
        instance = MagicMock()
        instance.generate_content.return_value = MagicMock(text='{"via": "gemini"}')
        mock_gm.return_value = instance
        result = AIService.generate_structured_json("prompt")
        self.assertEqual(result, {"via": "gemini"})

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test",
                       OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_failure_falls_back_to_openai(self, mock_gm, mock_openai):
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("gemini 500")
        mock_gm.return_value = instance
        mock_openai.return_value = _openai_client_with_content('{"fallback": true}')
        result = AIService.generate_structured_json("prompt")
        self.assertEqual(result, {"fallback": True})

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test",
                       GEMINI_API_KEY="gm-test")
    @patch("google.generativeai.GenerativeModel")
    @patch("apps.common.ai_service.OpenAI")
    def test_all_providers_fail_raises_no_silent_fabrication(self, mock_openai, mock_gm):
        # Unlike the detection helpers, structured synthesis has no honest
        # empty value to return — it must raise so callers build their own
        # deterministic output from real data.
        mock_openai.return_value = _openai_client_with_content("not json at all")
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("down")
        mock_gm.return_value = instance
        with self.assertRaises(AIServiceError):
            AIService.generate_structured_json("prompt")


class AIServiceRecommendationsTests(TestCase):

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    def test_openai_recommendations_with_confidence(self, mock_openai):
        mock_openai.return_value = _openai_client_with_content(json.dumps({
            "recommendations": [
                {"recommendation": "Epoxy-inject the crack.", "priority": "Urgent",
                 "related_finding_id": "defect-1"},
            ],
            "text_confidence": 0.82,
        }))
        result = AIService.generate_recommendations(
            [{"type": "crack", "severity": "high"}],
            [{"temperature_variance": 3.0, "severity": "medium"}],
            deviation=0.012,
        )
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertEqual(result["recommendations"][0]["priority"], "Urgent")
        self.assertEqual(result["text_confidence"], 0.82)
        # The prompt must carry the real inspection inputs, not placeholders.
        sent_prompt = mock_openai.return_value.chat.completions.create.call_args.kwargs[
            "messages"][0]["content"]
        self.assertIn("0.012", sent_prompt)
        self.assertIn("crack", sent_prompt)
        self.assertIn("temperature_variance", sent_prompt)

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_recommendations_missing_confidence_is_none(self, mock_gm):
        instance = MagicMock()
        instance.generate_content.return_value = MagicMock(
            text='{"recommendations": [{"recommendation": "Monitor.", "priority": "Routine"}]}')
        mock_gm.return_value = instance
        result = AIService.generate_recommendations([], [], 0.0)
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertIsNone(result["text_confidence"])

    @override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="gm-test",
                       OPENAI_API_KEY="sk-test")
    @patch("apps.common.ai_service.OpenAI")
    @patch("google.generativeai.GenerativeModel")
    def test_primary_failure_falls_back_to_secondary(self, mock_gm, mock_openai):
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("quota")
        mock_gm.return_value = instance
        mock_openai.return_value = _openai_client_with_content(json.dumps({
            "recommendations": [{"recommendation": "Patch spalling.", "priority": "High"}],
            "text_confidence": 0.6,
        }))
        result = AIService.generate_recommendations(
            [{"type": "spalling", "severity": "medium"}], [], 0.05)
        self.assertEqual(result["recommendations"][0]["priority"], "High")
        self.assertEqual(result["text_confidence"], 0.6)

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="sk-test",
                       GEMINI_API_KEY="gm-test")
    @patch("google.generativeai.GenerativeModel")
    @patch("apps.common.ai_service.OpenAI")
    def test_total_failure_returns_honest_empty(self, mock_openai, mock_gm):
        mock_openai.return_value = _openai_client_with_content("{broken json")
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("down")
        mock_gm.return_value = instance
        result = AIService.generate_recommendations(
            [{"type": "crack", "severity": "low"}], [], 0.02)
        self.assertEqual(result, {"recommendations": [], "text_confidence": None})


# ============================================================================
# Trimble Connect service (apps/common/trimble_service.py)
# ============================================================================

from apps.common import trimble_service
from apps.common.trimble_service import TrimbleConnectService
from apps.scans.models import (
    ScanSession, Defect, ThermalAnomaly, ProgressValidationResult,
)


class TrimbleServiceTestCase(TestCase):
    """Common fixture: real DB session rows, isolated token cache, no network."""

    def setUp(self):
        # The service keeps tokens in a module-level cache; snapshot it so
        # tests can never leak tokens into each other.
        self._saved_cache = dict(trimble_service._token_cache)
        trimble_service._token_cache["access_token"] = None
        trimble_service._token_cache["refresh_token"] = None
        self.session = ScanSession.objects.create(scanner_id="TRIMBLE-SCANNER-01")

    def tearDown(self):
        trimble_service._token_cache.clear()
        trimble_service._token_cache.update(self._saved_cache)


class TrimbleOAuthFlowTests(TrimbleServiceTestCase):

    @override_settings(TRIMBLE_CLIENT_ID="client-abc",
                       TRIMBLE_REDIRECT_URI="https://cb.example.test/trimble")
    def test_authorization_url_contains_oauth2_parameters(self):
        url = TrimbleConnectService.get_authorization_url()
        self.assertTrue(url.startswith(trimble_service.TRIMBLE_AUTH_URL))
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=client-abc", url)
        self.assertIn("redirect_uri=https://cb.example.test/trimble", url)
        self.assertIn("scope=openid", url)

    @override_settings(
        BASE_DIR=Path(tempfile.mkdtemp(prefix="nexucon_trimble_test_")),
        TRIMBLE_CLIENT_ID="client-abc", TRIMBLE_CLIENT_SECRET="secret-xyz",
        TRIMBLE_REDIRECT_URI="https://cb.example.test/trimble",
    )
    @patch("requests.post")
    def test_exchange_code_for_tokens_success(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"access_token": "at-123", "refresh_token": "rt-456"},
        )
        with mock.patch.dict("os.environ", {}, clear=False):
            result = TrimbleConnectService.exchange_code_for_tokens("auth-code-1")
        self.assertTrue(result)
        mock_post.assert_called_once()
        call = mock_post.call_args
        self.assertEqual(call.args[0], trimble_service.TRIMBLE_TOKEN_URL)
        self.assertEqual(call.kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(call.kwargs["data"]["code"], "auth-code-1")
        self.assertEqual(call.kwargs["data"]["client_id"], "client-abc")
        self.assertEqual(call.kwargs["data"]["client_secret"], "secret-xyz")
        # Tokens cached in memory and refresh token persisted for restarts.
        self.assertEqual(trimble_service._token_cache["access_token"], "at-123")
        self.assertEqual(trimble_service._token_cache["refresh_token"], "rt-456")
        token_file = Path(str(settings.BASE_DIR)) / ".trimble_refresh_token"
        self.assertEqual(token_file.read_text(), "rt-456")
        token_file.unlink()

    @override_settings(TRIMBLE_CLIENT_ID="client-abc", TRIMBLE_CLIENT_SECRET="secret-xyz")
    @patch("requests.post", side_effect=requests.exceptions.ConnectionError("no route"))
    def test_exchange_code_for_tokens_network_failure_is_honest_false(self, mock_post):
        self.assertFalse(TrimbleConnectService.exchange_code_for_tokens("code"))
        self.assertIsNone(trimble_service._token_cache["access_token"])

    @override_settings(TRIMBLE_CLIENT_ID="client-abc", TRIMBLE_CLIENT_SECRET="secret-xyz")
    @patch("requests.post")
    def test_exchange_code_for_tokens_http_error_is_honest_false(self, mock_post):
        resp = MagicMock(status_code=400)
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("400")
        resp.json.return_value = {"error": "invalid_grant"}
        mock_post.return_value = resp
        self.assertFalse(TrimbleConnectService.exchange_code_for_tokens("bad-code"))
        self.assertIsNone(trimble_service._token_cache["access_token"])


class TrimbleAccessTokenTests(TrimbleServiceTestCase):

    def test_cached_token_returned_without_network(self):
        trimble_service._token_cache["access_token"] = "cached-at"
        with patch("requests.post") as mock_post:
            token = TrimbleConnectService._get_access_token()
            mock_post.assert_not_called()
        self.assertEqual(token, "cached-at")

    @override_settings(BASE_DIR=Path(tempfile.mkdtemp(prefix="nexucon_trimble_test_")),
                       TRIMBLE_CLIENT_ID="client-abc", TRIMBLE_CLIENT_SECRET="secret-xyz")
    @patch("requests.post")
    def test_refresh_token_loaded_from_disk_and_rotated(self, mock_post):
        token_file = Path(str(settings.BASE_DIR)) / ".trimble_refresh_token"
        token_file.write_text("stored-refresh-token")
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"access_token": "fresh-at", "refresh_token": "rotated-rt"},
        )
        token = TrimbleConnectService._get_access_token()
        self.assertEqual(token, "fresh-at")
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(mock_post.call_args.kwargs["data"]["refresh_token"],
                         "stored-refresh-token")
        # A rotated refresh token replaces the persisted one.
        self.assertEqual(token_file.read_text(), "rotated-rt")
        self.assertEqual(trimble_service._token_cache["refresh_token"], "rotated-rt")
        token_file.unlink()

    @override_settings(BASE_DIR=Path(tempfile.mkdtemp(prefix="nexucon_trimble_test_")),
                       TRIMBLE_CLIENT_ID="client-abc", TRIMBLE_CLIENT_SECRET="secret-xyz")
    @patch("requests.post")
    def test_refresh_response_without_new_refresh_token_keeps_old(self, mock_post):
        token_file = Path(str(settings.BASE_DIR)) / ".trimble_refresh_token"
        token_file.write_text("keep-me")
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"access_token": "at-only"})
        token = TrimbleConnectService._get_access_token()
        self.assertEqual(token, "at-only")
        self.assertEqual(token_file.read_text(), "keep-me")
        token_file.unlink()

    @override_settings(BASE_DIR=Path(tempfile.mkdtemp(prefix="nexucon_trimble_test_")),
                       TRIMBLE_CLIENT_ID="client-abc", TRIMBLE_CLIENT_SECRET="secret-xyz")
    @patch("requests.post", side_effect=requests.exceptions.Timeout("slow"))
    def test_refresh_failure_clears_token_and_returns_empty(self, mock_post):
        token_file = Path(str(settings.BASE_DIR)) / ".trimble_refresh_token"
        token_file.write_text("stale-refresh-token")
        self.assertEqual(TrimbleConnectService._get_access_token(), "")
        self.assertIsNone(trimble_service._token_cache["access_token"])
        token_file.unlink()

    @override_settings(BASE_DIR=Path(tempfile.mkdtemp(prefix="nexucon_trimble_test_")),
                       TRIMBLE_CLIENT_ID="", TRIMBLE_CLIENT_SECRET="")
    def test_no_credentials_and_no_token_returns_empty_without_network(self):
        # PENDING_CREDENTIALS honesty: without real credentials no token is
        # fabricated and no network call is made.
        with patch("requests.post") as mock_post:
            token = TrimbleConnectService._get_access_token()
            mock_post.assert_not_called()
        self.assertEqual(token, "")

    @override_settings(BASE_DIR=Path(tempfile.mkdtemp(prefix="nexucon_trimble_test_")),
                       TRIMBLE_CLIENT_ID="client-abc", TRIMBLE_CLIENT_SECRET="secret-xyz")
    def test_credentials_present_but_authorization_pending_returns_empty(self):
        with patch("requests.post") as mock_post:
            token = TrimbleConnectService._get_access_token()
            mock_post.assert_not_called()
        self.assertEqual(token, "")


class TrimbleFileGenerationTests(TrimbleServiceTestCase):
    """CSV / summary / overlay generation from real database rows."""

    def _defect(self, **kwargs):
        defaults = dict(
            session=self.session, type="spalling", severity="high",
            location_x=1.5, location_y=2.5, location_z=3.5,
            description="Severe cover spalling with exposed rebar.",
            confidence_score=0.93,
        )
        defaults.update(kwargs)
        return Defect.objects.create(**defaults)

    def _anomaly(self, **kwargs):
        defaults = dict(
            session=self.session, temperature_variance=6.4, severity="critical",
            location_x=4.0, location_y=5.0, location_z=6.0,
            description="Sustained hotspot consistent with pipe leakage.",
            confidence_score=0.87,
        )
        defaults.update(kwargs)
        return ThermalAnomaly.objects.create(**defaults)

    def test_defect_csv_contains_header_and_all_real_rows(self):
        defect = self._defect()
        anomaly = self._anomaly()
        csv_text = TrimbleConnectService.generate_defect_csv(self.session)
        rows = list(csv.reader(csv_text.splitlines()))
        self.assertEqual(rows[0][0], "Damage_ID")
        self.assertEqual(len(rows), 3)
        defect_row = rows[1]
        self.assertEqual(defect_row[0], str(defect.id))
        self.assertEqual(defect_row[1], "Visual_spalling")
        self.assertEqual(defect_row[2], "high")
        self.assertEqual(defect_row[3:6], ["1.5", "2.5", "3.5"])
        self.assertIn("exposed rebar", defect_row[6])
        anomaly_row = rows[2]
        self.assertEqual(anomaly_row[1], "Thermal_Anomaly")
        self.assertEqual(anomaly_row[2], "critical")

    def test_defect_csv_falls_back_description_for_blank_fields(self):
        self._defect(description="")
        self._anomaly(description="")
        csv_text = TrimbleConnectService.generate_defect_csv(self.session)
        rows = list(csv.reader(csv_text.splitlines()))
        self.assertEqual(rows[1][6], "AI-detected spalling")
        self.assertIn("variance 6.4", rows[2][6])

    def test_defect_csv_excludes_other_sessions(self):
        other = ScanSession.objects.create(scanner_id="OTHER-SCANNER")
        self._defect()
        self._defect(session=other)
        csv_text = TrimbleConnectService.generate_defect_csv(self.session)
        self.assertEqual(len(csv_text.strip().splitlines()), 2)  # header + own defect

    def test_inspection_summary_critical_rating(self):
        self._anomaly(severity="critical")
        self._defect(severity="low")
        summary = json.loads(TrimbleConnectService.generate_inspection_summary(self.session))
        self.assertEqual(summary["overall_condition_rating"], "Critical")
        self.assertEqual(summary["critical_issues"], 1)
        self.assertEqual(summary["total_defects_detected"], 1)
        self.assertEqual(summary["total_thermal_anomalies_detected"], 1)
        self.assertIsNone(summary["project_id"])  # session has no project
        self.assertEqual(summary["synced_by"], "Nexucon SiteSupervise")

    def test_inspection_summary_fair_rating_above_five_issues(self):
        for i in range(6):
            self._defect(severity="low")
        summary = json.loads(TrimbleConnectService.generate_inspection_summary(self.session))
        self.assertEqual(summary["overall_condition_rating"], "Fair")
        self.assertEqual(summary["critical_issues"], 0)

    def test_inspection_summary_good_rating_for_minor_issues(self):
        self._defect(severity="medium")
        summary = json.loads(TrimbleConnectService.generate_inspection_summary(self.session))
        self.assertEqual(summary["overall_condition_rating"], "Good")

    def test_inspection_summary_excellent_and_progress_defaults(self):
        # No findings and no progress result — honest zeros, not guesses.
        summary = json.loads(TrimbleConnectService.generate_inspection_summary(self.session))
        self.assertEqual(summary["overall_condition_rating"], "Excellent")
        self.assertEqual(summary["progress_score"], 0.0)
        self.assertEqual(summary["covered_area_sqm"], 0.0)

    def test_inspection_summary_uses_real_progress_metrics(self):
        self._defect(severity="low")
        ProgressValidationResult.objects.create(
            session=self.session, progress_score=0.72, covered_area_sqm=148.5)
        summary = json.loads(TrimbleConnectService.generate_inspection_summary(self.session))
        self.assertEqual(summary["progress_score"], 0.72)
        self.assertEqual(summary["covered_area_sqm"], 148.5)

    def test_ai_overlay_json_excludes_false_positives(self):
        confirmed = self._defect(location_x=None, location_y=None, location_z=None)
        self._defect(type="crack", severity="low", is_false_positive=True)
        overlay = json.loads(TrimbleConnectService.generate_ai_overlay_json(self.session))
        self.assertEqual(overlay["type"], "FeatureCollection")
        self.assertEqual(len(overlay["features"]), 1)
        feature = overlay["features"][0]
        self.assertEqual(feature["properties"]["defect_id"], str(confirmed.id))
        self.assertEqual(feature["properties"]["type"], "spalling")
        self.assertEqual(feature["geometry"]["coordinates"], [0.0, 0.0, 0.0])

    def test_ai_overlay_json_empty_when_no_confirmed_defects(self):
        overlay = json.loads(TrimbleConnectService.generate_ai_overlay_json(self.session))
        self.assertEqual(overlay["features"], [])


class TrimbleUploadTests(TrimbleServiceTestCase):

    def _upload_env(self):
        # project_id/folder_id are env-only settings (no Django attribute).
        return mock.patch.dict("os.environ", {
            "TRIMBLE_PROJECT_ID": "proj-42",
            "TRIMBLE_FOLDER_ID": "folder-7",
        })

    def test_upload_without_token_is_honest_pending_no_network(self):
        # PENDING_CREDENTIALS: no token means nothing was uploaded, so no
        # success is reported and no HTTP call is attempted.
        with override_settings(BASE_DIR=Path(tempfile.mkdtemp(prefix="nexucon_trimble_test_")),
                               TRIMBLE_CLIENT_ID="", TRIMBLE_CLIENT_SECRET=""), \
                patch("requests.post") as mock_post:
            result = TrimbleConnectService.upload_files_to_trimble(
                self.session, "a,b", "{}")
            mock_post.assert_not_called()
        self.assertFalse(result)

    def test_upload_success_posts_all_files(self):
        trimble_service._token_cache["access_token"] = "valid-at"
        ok = MagicMock(status_code=201)
        with self._upload_env(), patch("requests.post", return_value=ok) as mock_post:
            result = TrimbleConnectService.upload_files_to_trimble(
                self.session, "col1,col2", '{"summary": 1}',
                ai_overlay_json='{"type": "FeatureCollection"}',
                thermal_orthomosaic_url="https://r2.example.test/ortho.png",
            )
        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 4)  # csv + summary + overlay + thermal meta
        first_call = mock_post.call_args_list[0]
        self.assertIn("/projects/proj-42/folders/folder-7/files", first_call.args[0])
        self.assertEqual(first_call.kwargs["headers"], {"Authorization": "Bearer valid-at"})
        filename, content, content_type = first_call.kwargs["files"]["file"]
        self.assertTrue(filename.startswith("nexucon_defects_"))
        self.assertTrue(filename.endswith(".csv"))
        self.assertEqual(content_type, "text/csv")
        # Thermal orthomosaic reference is uploaded as real metadata JSON.
        thermal_call = mock_post.call_args_list[3]
        t_name, t_content, _ = thermal_call.kwargs["files"]["file"]
        self.assertTrue(t_name.startswith("nexucon_thermal_meta_"))
        self.assertIn("https://r2.example.test/ortho.png", t_content.decode())

    def test_upload_aborts_and_reports_false_on_http_failure(self):
        trimble_service._token_cache["access_token"] = "valid-at"
        responses = [MagicMock(status_code=201), MagicMock(status_code=500, text="boom")]
        with self._upload_env(), patch("requests.post", side_effect=responses) as mock_post:
            result = TrimbleConnectService.upload_files_to_trimble(
                self.session, "a,b", "{}")
        self.assertFalse(result)
        self.assertEqual(mock_post.call_count, 2)

    def test_upload_network_error_is_honest_false(self):
        trimble_service._token_cache["access_token"] = "valid-at"
        with self._upload_env(), \
                patch("requests.post", side_effect=requests.exceptions.Timeout("t")):
            result = TrimbleConnectService.upload_files_to_trimble(
                self.session, "a,b", "{}")
        self.assertFalse(result)

    def test_upload_unexpected_error_is_honest_false(self):
        trimble_service._token_cache["access_token"] = "valid-at"
        with self._upload_env(), \
                patch("requests.post", side_effect=RuntimeError("surprise")):
            result = TrimbleConnectService.upload_files_to_trimble(
                self.session, "a,b", "{}")
        self.assertFalse(result)


# ============================================================================
# ML pipeline (apps/common/ml_pipeline.py)
# ============================================================================

import importlib
import math
import sys
import types
from contextlib import contextmanager

from apps.common import ml_pipeline as ml_pipeline_module


class FakeTensor:
    """Minimal numeric tensor over a 2D grid — just enough for the pipeline."""

    def __init__(self, data):
        self.data = data

    @property
    def shape(self):
        return [1, 1, len(self.data), len(self.data[0])]

    def size(self):
        return [1]

    def unsqueeze(self, _dim):
        return self

    def __getitem__(self, idx):
        if isinstance(idx, tuple):
            rows = self.data[idx[2]]
            return FakeTensor([row[idx[3]] for row in rows])
        return FakeTensor(self.data[idx])

    def __gt__(self, other):
        return FakeTensor([[float(v > other) for v in row] for row in self.data])

    def float(self):
        return FakeTensor([[float(v) for v in row] for row in self.data])

    def sum(self):
        return float(sum(v for row in self.data for v in row))

    def view(self, *_shape):
        flat = [v for row in self.data for v in row]
        return FakeTensor([list(flat)])

    def item(self):
        return float(self.data[0][0])


def _build_fake_torch():
    """Build stand-in torch / torch.nn / torch.nn.functional / torchvision
    modules so the PyTorch code path of ml_pipeline can be exercised in this
    environment (where torch is not installed). Convolution is simulated by a
    deterministic per-layer bias, max-pooling by 2x decimation — uniform test
    images stay uniform, so the pipeline's behaviour is fully predictable.
    """
    nn = types.ModuleType("torch.nn")
    functional = types.ModuleType("torch.nn.functional")
    torch = types.ModuleType("torch")
    torchvision = types.ModuleType("torchvision")
    transforms = types.ModuleType("torchvision.transforms")

    class FakeModule:
        def load_state_dict(self, *args, **kwargs):
            pass

        def eval(self):
            return self

        def __call__(self, *args, **kwargs):
            return self.forward(*args, **kwargs)

    class FakeConv2d(FakeModule):
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, x):
            return FakeTensor([[v + 0.25 for v in row] for row in x.data])

    class FakeReLU(FakeModule):
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, x):
            return FakeTensor([[max(v, 0.0) for v in row] for row in x.data])

    class FakeMaxPool2d(FakeModule):
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, x, *args, **kwargs):
            return FakeTensor([row[::2] for row in x.data[::2]])

    class FakeLinear(FakeModule):
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, x):
            return x

    class FakeSequential(FakeModule):
        def __init__(self, *modules):
            self.modules = modules

        def __call__(self, x):
            for module in self.modules:
                x = module(x)
            return x

    nn.Module = FakeModule
    nn.Conv2d = FakeConv2d
    nn.ReLU = FakeReLU
    nn.MaxPool2d = FakeMaxPool2d
    nn.Linear = FakeLinear
    nn.Sequential = FakeSequential

    functional.relu = FakeReLU()
    functional.max_pool2d = FakeMaxPool2d()
    functional.sigmoid = lambda x: FakeTensor(
        [[1.0 / (1.0 + math.exp(-v)) for v in row] for row in x.data])

    def pairwise_distance(a, b):
        mean_a = sum(v for row in a.data for v in row) / sum(len(r) for r in a.data)
        mean_b = sum(v for row in b.data for v in row) / sum(len(r) for r in b.data)
        return FakeTensor([[abs(mean_a - mean_b) * 10.0]])

    functional.pairwise_distance = pairwise_distance

    @contextmanager
    def no_grad():
        yield None

    torch.load = MagicMock()
    torch.device = lambda *_a, **_k: object()
    torch.no_grad = no_grad
    torch.sigmoid = functional.sigmoid
    torch.nn = nn
    nn.functional = functional

    class FakeResize:
        def __init__(self, size):
            self.size = size  # (height, width)

        def __call__(self, x):
            th, tw = self.size
            if isinstance(x, FakeTensor):
                h, w = len(x.data), len(x.data[0])
                if (h, w) == (th, tw):
                    return x
                return FakeTensor([
                    [x.data[(r * h) // th][(c * w) // tw] for c in range(tw)]
                    for r in range(th)
                ])
            return x.resize((tw, th))

    class FakeToTensor:
        def __call__(self, img):
            img = img.convert("L")
            w, h = img.size
            pixels = list(img.getdata())
            return FakeTensor([
                [p / 255.0 for p in pixels[r * w:(r + 1) * w]] for r in range(h)
            ])

    class FakeCompose:
        def __init__(self, fns):
            self.fns = fns

        def __call__(self, x):
            for fn in self.fns:
                x = fn(x)
            return x

    transforms.Compose = FakeCompose
    transforms.Resize = FakeResize
    transforms.ToTensor = FakeToTensor
    torchvision.transforms = transforms

    return torch, nn, functional, torchvision, transforms


class MLPipelineFallbackTests(TestCase):
    """Honest behaviour of the pipeline actually deployed in this environment
    (PyTorch is not installed, so the pipeline must report "not available"
    rather than fabricate delamination findings)."""

    def test_fallback_pipeline_is_not_loaded_and_returns_none(self):
        pipeline = ml_pipeline_module.MultimodalPipeline("/nonexistent/cnn.pt",
                                                         "/nonexistent/snn.pt")
        self.assertFalse(pipeline.is_loaded)
        self.assertIsNone(pipeline.process_images("http://example.com/t.jpg",
                                                  "http://example.com/v.jpg"))

    def test_ai_service_local_pipeline_returns_none_for_real_urls(self):
        # The pipeline instance held by ai_service must never invent findings.
        from apps.common.ai_service import local_ml_pipeline
        self.assertFalse(local_ml_pipeline.is_loaded)
        self.assertIsNone(local_ml_pipeline.process_images(
            "http://example.com/t.jpg", "http://example.com/v.jpg"))


class MLPipelineTorchPathTests(TestCase):
    """The torch code path, exercised against lightweight stand-in modules."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake_torch, cls.fake_nn, cls.fake_f, cls.fake_tv, cls.fake_tf = \
            _build_fake_torch()
        cls._module_names = ["torch", "torch.nn", "torch.nn.functional",
                             "torchvision", "torchvision.transforms"]
        cls._saved_modules = {name: sys.modules.get(name)
                              for name in cls._module_names}
        sys.modules["torch"] = cls.fake_torch
        sys.modules["torch.nn"] = cls.fake_nn
        sys.modules["torch.nn.functional"] = cls.fake_f
        sys.modules["torchvision"] = cls.fake_tv
        sys.modules["torchvision.transforms"] = cls.fake_tf
        importlib.reload(ml_pipeline_module)

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        importlib.reload(ml_pipeline_module)  # restore the real no-torch module
        super().tearDownClass()

    def setUp(self):
        self.fake_torch.load = MagicMock()
        self.weights_dir = tempfile.TemporaryDirectory(prefix="nexucon_ml_test_")
        self.addCleanup(self.weights_dir.cleanup)

    def _weight_paths(self):
        cnn = Path(self.weights_dir.name) / "cnn.pt"
        snn = Path(self.weights_dir.name) / "snn.pt"
        cnn.write_bytes(b"weights")
        snn.write_bytes(b"weights")
        return str(cnn), str(snn)

    def test_not_loaded_without_weights(self):
        pipeline = ml_pipeline_module.MultimodalPipeline()
        self.assertFalse(pipeline.is_loaded)
        self.assertIsNone(pipeline.process_images("http://example.com/t.jpg",
                                                  "http://example.com/v.jpg"))

    def test_not_loaded_when_weight_files_are_missing(self):
        pipeline = ml_pipeline_module.MultimodalPipeline("/nonexistent/cnn.pt",
                                                         "/nonexistent/snn.pt")
        self.assertFalse(pipeline.is_loaded)

    def test_loaded_when_both_weight_files_exist(self):
        cnn_path, snn_path = self._weight_paths()
        pipeline = ml_pipeline_module.MultimodalPipeline(cnn_path, snn_path)
        self.assertTrue(pipeline.is_loaded)
        self.assertEqual(self.fake_torch.load.call_count, 2)

    def test_weight_load_failure_leaves_pipeline_unloaded(self):
        self.fake_torch.load = MagicMock(side_effect=RuntimeError("corrupt weights"))
        cnn_path, snn_path = self._weight_paths()
        pipeline = ml_pipeline_module.MultimodalPipeline(cnn_path, snn_path)
        self.assertFalse(pipeline.is_loaded)  # honest: no partial/pretend load

    @staticmethod
    def _http_response(png):
        return MagicMock(status_code=200, content=png)

    def _loaded_pipeline(self):
        cnn_path, snn_path = self._weight_paths()
        return ml_pipeline_module.MultimodalPipeline(cnn_path, snn_path)

    def test_process_images_returns_none_when_not_loaded(self):
        pipeline = ml_pipeline_module.MultimodalPipeline()
        with patch("requests.get") as mock_get:
            result = pipeline.process_images("http://example.com/t.jpg",
                                             "http://example.com/v.jpg")
            mock_get.assert_not_called()
        self.assertIsNone(result)

    def test_process_images_http_failure_returns_none(self):
        pipeline = self._loaded_pipeline()
        bad = MagicMock(status_code=404)
        with patch("requests.get", return_value=bad):
            self.assertIsNone(
                pipeline.process_images("http://example.com/t.jpg",
                                        "http://example.com/v.jpg"))

    def test_process_images_download_exception_returns_none(self):
        pipeline = self._loaded_pipeline()
        with patch("requests.get", side_effect=RuntimeError("offline")):
            self.assertIsNone(
                pipeline.process_images("http://example.com/t.jpg",
                                        "http://example.com/v.jpg"))

    def test_process_images_confirms_delamination_for_diverging_pair(self):
        # Hot thermal region over a visually distinct (dark) area: the CNN
        # flags the region and the SNN fails to match it to the visible
        # surface, so delamination is confirmed.
        pipeline = self._loaded_pipeline()
        thermal_png = _png_bytes(color=200, size=(100, 100), mode="L")
        visible_png = _png_bytes(color=50, size=(100, 100), mode="L")
        responses = [self._http_response(thermal_png), self._http_response(visible_png)]
        with patch("requests.get", side_effect=responses):
            results = pipeline.process_images("http://example.com/t.jpg",
                                              "http://example.com/v.jpg")
        self.assertTrue(len(results) > 0)
        for item in results:
            self.assertEqual(item["type"], "delamination")
            self.assertEqual(item["severity"], "high")
            self.assertFalse(item["is_false_positive"])
            self.assertEqual(item["confidence_score"], 0.85)
            self.assertIsInstance(item["location_x"], float)
            self.assertIsInstance(item["location_y"], float)
            self.assertEqual(item["location_z"], 0.0)

    def test_process_images_flags_matching_pair_as_false_positive(self):
        # Identical thermal and visible images: the SNN matches the regions,
        # so the CNN hit is reported as a false positive, not a delamination.
        pipeline = self._loaded_pipeline()
        same_png = _png_bytes(color=200, size=(100, 100), mode="L")
        responses = [self._http_response(same_png), self._http_response(same_png)]
        with patch("requests.get", side_effect=responses):
            results = pipeline.process_images("http://example.com/t.jpg",
                                              "http://example.com/v.jpg")
        self.assertTrue(len(results) > 0)
        for item in results:
            self.assertTrue(item["is_false_positive"])
            self.assertEqual(item["severity"], "low")
            self.assertEqual(item["confidence_score"], 0.20)
            self.assertIn("False Positive", item["description"])
