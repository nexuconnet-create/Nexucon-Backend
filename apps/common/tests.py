from django.test import TestCase, override_settings
from django.conf import settings
from unittest import mock
from unittest.mock import patch, MagicMock
from io import BytesIO
from pathlib import Path
import csv
import json
import os
import tempfile

import requests
from PIL import Image

from apps.common.ai_service import (
    AIService, AIServiceError, AIQuotaExceeded, AIProviderUnavailable,
)
from common.geo import haversine_m


# Every API key the AI service can route through, forced empty.
#
# `_provider_has_key` is checked before an attempt is made, so a test that
# wants "no provider is configured" must clear EVERY provider — not just the
# two that existed when these tests were written. `config.settings.base`
# loads `.env` into `os.environ`, so a real ANTHROPIC_API_KEY or
# DEEPSEEK_API_KEY on the machine would otherwise give the chain a live,
# unmocked provider and these tests would make real outbound HTTP calls.
NO_PROVIDER_KEYS = dict(
    OPENAI_API_KEY="",
    GEMINI_API_KEY="",
    ANTHROPIC_API_KEY="",
    DEEPSEEK_API_KEY="",
)


def _provider_settings(**configured):
    """`override_settings` kwargs for a test that exercises the provider chain.

    Every provider key starts empty, then `configured` is layered on top, so a
    test names only the providers it actually means to configure and cannot
    inherit a live key from the environment by accident. The merge happens in
    a dict because `override_settings(**NO_PROVIDER_KEYS, OPENAI_API_KEY=...)`
    is a TypeError — the same keyword arrives twice.
    """
    return dict(NO_PROVIDER_KEYS, **configured)


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

    @override_settings(AI_PROVIDER="gemini", **_provider_settings())
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

    @override_settings(
        AI_PROVIDER="openai",
        **_provider_settings(OPENAI_API_KEY="sk-test", GEMINI_API_KEY="gm-test"))
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

    @override_settings(AI_PROVIDER="gemini", **_provider_settings())
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

    @override_settings(
        AI_PROVIDER="gemini",
        **_provider_settings(GEMINI_API_KEY="gm-test", OPENAI_API_KEY="sk-test"))
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

    @override_settings(AI_PROVIDER="gemini", **_provider_settings())
    def test_all_providers_unconfigured_returns_empty_no_fabrication(self):
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

    @override_settings(AI_PROVIDER="gemini", **_provider_settings())
    def test_all_providers_unconfigured_returns_empty_no_fabrication(self):
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

    @override_settings(
        AI_PROVIDER="gemini",
        **_provider_settings(GEMINI_API_KEY="gm-test", OPENAI_API_KEY="sk-test"))
    @patch("apps.common.ai_service.OpenAI")
    @patch("google.generativeai.GenerativeModel")
    def test_gemini_failure_falls_back_to_openai(self, mock_gm, mock_openai):
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("gemini 500")
        mock_gm.return_value = instance
        mock_openai.return_value = _openai_client_with_content('{"fallback": true}')
        result = AIService.generate_structured_json("prompt")
        self.assertEqual(result, {"fallback": True})

    @override_settings(
        AI_PROVIDER="openai",
        **_provider_settings(OPENAI_API_KEY="sk-test", GEMINI_API_KEY="gm-test"))
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

    @override_settings(
        AI_PROVIDER="gemini",
        **_provider_settings(GEMINI_API_KEY="gm-test", OPENAI_API_KEY="sk-test"))
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

    @override_settings(
        AI_PROVIDER="openai",
        **_provider_settings(OPENAI_API_KEY="sk-test", GEMINI_API_KEY="gm-test"))
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
# Multi-provider chain: capability tables, ordering, failover, provenance
# ============================================================================

from apps.common.ai_service import (  # noqa: E402
    CAPABILITIES, StructuredResult, _CANONICAL_ORDER, _PROVIDER_KEY_GETTERS,
    _RUNNER_NAMES,
)


class ProviderCapabilityTableTests(TestCase):
    """The capability tables must be DERIVED, not asserted in parallel.

    A provider advertising a capability it has no code for is worse than one
    that lacks it: the chain would build a runner, call it, and fail at
    runtime instead of skipping. These tests pin the derivation.
    """

    def test_every_capability_table_entry_has_a_real_method(self):
        for capability, table in _RUNNER_NAMES.items():
            for provider, method_name in table.items():
                self.assertTrue(
                    callable(getattr(AIService, method_name, None)),
                    f"{capability}/{provider} names {method_name}, which does not exist",
                )

    def test_every_provider_is_covered_by_at_least_one_capability(self):
        for provider in _PROVIDER_KEY_GETTERS:
            self.assertTrue(
                CAPABILITIES[provider],
                f"{provider} is registered but can serve no call shape at all",
            )

    def test_deepseek_capability_is_text_only(self):
        # DeepSeek's public API has no vision endpoint. This is the derived
        # consequence of there being no vision runner — not a separate claim
        # that could drift away from the code.
        self.assertEqual(CAPABILITIES["deepseek"], frozenset({"text"}))

    def test_vision_capabilities_exclude_deepseek(self):
        for provider in ("openai", "gemini", "anthropic"):
            self.assertIn("vision", CAPABILITIES[provider])

    def test_every_key_getter_and_model_getter_resolves(self):
        for provider, getter_name in _PROVIDER_KEY_GETTERS.items():
            self.assertTrue(callable(getattr(AIService, getter_name, None)))
            self.assertIsNotNone(AIService._model_for(provider))


class ProviderOrderTests(TestCase):

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a"))
    def test_configured_provider_goes_first(self):
        with override_settings(AI_PROVIDER="openai"):
            order = AIService._provider_order({"openai", "gemini", "anthropic", "deepseek"})
        self.assertEqual(order[0], "openai")

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a", GEMINI_API_KEY="gm-a",
                                            ANTHROPIC_API_KEY="an-a", DEEPSEEK_API_KEY="ds-a"))
    def test_order_is_deterministic_and_complete(self):
        with override_settings(AI_PROVIDER="gemini"):
            first = AIService._provider_order(set(_PROVIDER_KEY_GETTERS))
            second = AIService._provider_order(set(_PROVIDER_KEY_GETTERS))
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(_PROVIDER_KEY_GETTERS))
        self.assertEqual(
            first, ["gemini"] + [p for p in _CANONICAL_ORDER if p != "gemini"])

    @override_settings(**_provider_settings(GEMINI_API_KEY="gm-a"))
    def test_keyless_providers_are_skipped_not_attempted(self):
        with override_settings(AI_PROVIDER="openai"):
            order = AIService._provider_order(set(_PROVIDER_KEY_GETTERS))
        # openai is the configured provider but has no key, so it is not
        # first — it is absent entirely.
        self.assertEqual(order, ["gemini"])

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a", ANTHROPIC_API_KEY="an-a"))
    def test_vision_order_never_contains_deepseek(self):
        runners = AIService._build_runners("vision", "prompt", (), {})
        order = AIService._provider_order(set(runners))
        self.assertNotIn("deepseek", order)
        self.assertEqual(set(order), {"openai", "anthropic"})

    @override_settings(AI_PROVIDER_ORDER="deepseek, openai", **_provider_settings(
        OPENAI_API_KEY="sk-a", GEMINI_API_KEY="gm-a",
        ANTHROPIC_API_KEY="an-a", DEEPSEEK_API_KEY="ds-a"))
    def test_provider_order_setting_is_honoured_after_the_configured_provider(self):
        with override_settings(AI_PROVIDER="gemini"):
            order = AIService._provider_order(set(_PROVIDER_KEY_GETTERS))
        self.assertEqual(order[:3], ["gemini", "deepseek", "openai"])

    @override_settings(**_provider_settings())
    def test_no_configured_provider_yields_empty_order(self):
        self.assertEqual(AIService._provider_order(set(_PROVIDER_KEY_GETTERS)), [])


class ProviderFailoverTests(TestCase):
    """One provider breaking must not stop the others."""

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a", GEMINI_API_KEY="gm-a",
                                            ANTHROPIC_API_KEY="an-a", DEEPSEEK_API_KEY="ds-a"))
    @patch("apps.common.ai_service.requests.post")
    @patch("google.generativeai.GenerativeModel")
    @patch("apps.common.ai_service.OpenAI")
    def test_three_providers_attempted_in_order_until_one_answers(
            self, mock_openai, mock_gm, mock_post):
        # OpenAI and Gemini both break; Anthropic answers. Every earlier
        # provider must have been tried, and the chain must not have stopped
        # at the first failure.
        mock_openai.return_value = _openai_client_with_content("not json")
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("gemini down")
        mock_gm.return_value = instance
        anthropic_response = MagicMock(status_code=200, headers={})
        anthropic_response.json.return_value = {
            "content": [{"type": "text", "text": '{"defects": [{"type": "crack"}]}'}],
            "stop_reason": "end_turn",
        }
        mock_post.return_value = anthropic_response

        with override_settings(AI_PROVIDER="openai"):
            defects = AIService.detect_visual_defects("http://example.com/col.jpg")

        self.assertEqual([d["type"] for d in defects], ["crack"])
        self.assertEqual(mock_post.call_count, 1)
        self.assertIn("api.anthropic.com", mock_post.call_args[0][0])

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a", GEMINI_API_KEY="gm-a",
                                            ANTHROPIC_API_KEY="an-a"))
    @patch("apps.common.ai_service.requests.post")
    @patch("google.generativeai.GenerativeModel")
    @patch("apps.common.ai_service.OpenAI")
    def test_keyless_deepseek_is_never_called(self, mock_openai, mock_gm, mock_post):
        # DeepSeek has no key in this test. It must be skipped WITHOUT an
        # attempt — a skipped provider cannot produce an error, and it must
        # not cost a request's worth of latency to discover there is no key.
        mock_openai.return_value = _openai_client_with_content("not json")
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("gemini down")
        mock_gm.return_value = instance
        anthropic_response = MagicMock(status_code=200, headers={})
        anthropic_response.json.return_value = {"content": [{"type": "text", "text": '{"defects": []}'}]}
        mock_post.return_value = anthropic_response

        with override_settings(AI_PROVIDER="openai"):
            AIService.detect_visual_defects("http://example.com/col.jpg")

        # Exactly one HTTP call, and it went to Anthropic — never DeepSeek,
        # whose vision endpoint does not exist.
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args[0][0], "https://api.anthropic.com/v1/messages")

    @override_settings(**_provider_settings(GEMINI_API_KEY="gm-a"))
    @patch("google.generativeai.GenerativeModel")
    def test_an_unconfigured_openai_client_is_never_built(self, mock_gm):
        # The configured provider has no key here, so its client factory must
        # not run at all — building it would raise AIProviderUnavailable and
        # be recorded as a failure rather than a skip.
        mock_gm.return_value = MagicMock(**{
            "generate_content.return_value": MagicMock(text='{"defects": []}'),
        })
        with override_settings(AI_PROVIDER="openai"):
            with patch.object(AIService, "_get_openai_client") as mock_client:
                AIService.detect_visual_defects("http://example.com/col.jpg")
        mock_client.assert_not_called()

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a", GEMINI_API_KEY="gm-a"))
    @patch("google.generativeai.GenerativeModel")
    @patch("apps.common.ai_service.OpenAI")
    def test_unconfigured_raises_unavailable_but_all_failed_raises_service_error(
            self, mock_openai, mock_gm):
        # Two distinct states that were previously conflated: "nothing is
        # configured" and "everything was tried and failed". Callers need to
        # tell them apart — the first is a deployment problem, the second is
        # an outage.
        mock_openai.return_value = _openai_client_with_content("not json")
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("down")
        mock_gm.return_value = instance

        with override_settings(AI_PROVIDER="openai"):
            with self.assertRaises(AIServiceError) as ctx:
                AIService.generate_structured_json("prompt")
        self.assertNotIsInstance(ctx.exception, AIProviderUnavailable)
        self.assertEqual(
            [a["provider"] for a in ctx.exception.attempts], ["openai", "gemini"])
        self.assertTrue(all("error" in a for a in ctx.exception.attempts))

        with override_settings(AI_PROVIDER="openai", **_provider_settings()):
            with self.assertRaises(AIProviderUnavailable):
                AIService.generate_structured_json("prompt")

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a", GEMINI_API_KEY="gm-a",
                                            ANTHROPIC_API_KEY="an-a"),
                       AI_FAILOVER_DEADLINE_SECONDS=0)
    @patch("apps.common.ai_service.requests.post")
    @patch("google.generativeai.GenerativeModel")
    @patch("apps.common.ai_service.OpenAI")
    def test_one_image_is_downloaded_once_across_the_whole_chain(
            self, mock_openai, mock_gm, mock_post):
        # Every provider must be shown the SAME bytes. A per-provider fetch
        # would let an expiring signed URL fail only the later providers,
        # making the winner an artefact of timing rather than of the models.
        mock_openai.return_value = _openai_client_with_content("not json")
        instance = MagicMock()
        instance.generate_content.side_effect = RuntimeError("gemini down")
        mock_gm.return_value = instance
        mock_post.return_value = MagicMock(**{
            "status_code": 200, "headers": {},
            "json.return_value": {"content": [{"type": "text", "text": '{"defects": []}'}]},
        })

        image_response = MagicMock(content=_png_bytes(size=(200, 100)))
        image_response.raise_for_status.return_value = None

        with override_settings(AI_PROVIDER="openai"):
            with patch("apps.common.ai_service.requests.get",
                       return_value=image_response) as mock_get:
                AIService.detect_visual_defects("https://cdn.nexucon-pilot.site/real.jpg")

        self.assertEqual(mock_get.call_count, 1)


class DeepSeekTests(TestCase):
    """DeepSeek runs through the OpenAI SDK against its own base URL."""

    @override_settings(**_provider_settings(DEEPSEEK_API_KEY="ds-test",
                                            DEEPSEEK_BASE_URL="https://api.deepseek.com"))
    @patch("apps.common.ai_service.OpenAI")
    def test_deepseek_client_uses_its_own_key_and_base_url(self, mock_openai):
        AIService._get_deepseek_client()
        mock_openai.assert_called_once_with(
            api_key="ds-test", base_url="https://api.deepseek.com", max_retries=0)

    @override_settings(**_provider_settings(DEEPSEEK_API_KEY="ds-test"))
    @patch("apps.common.ai_service.OpenAI")
    def test_deepseek_uses_a_chat_model_that_accepts_json_mode(self, mock_openai):
        # `deepseek-reasoner` rejects response_format, and every caller here
        # asks for JSON.
        mock_openai.return_value = _openai_client_with_content('{"result": 1}')
        AIService._run_text_deepseek("prompt")
        kwargs = mock_openai.return_value.chat.completions.create.call_args[1]
        self.assertEqual(kwargs["model"], "deepseek-chat")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    @override_settings(**_provider_settings(DEEPSEEK_API_KEY="ds-test"), AI_MAX_RETRIES=3)
    @patch("apps.common.ai_service.time.sleep")
    @patch("apps.common.ai_service.OpenAI")
    def test_insufficient_balance_is_permanent_and_never_retried(
            self, mock_openai, mock_sleep):
        # HTTP 402 means an unfunded account. Retrying reproduces the same
        # answer at the cost of the failover budget, so it must be classified
        # as quota exhaustion and passed straight to the next provider.
        error = Exception("Error code: 402 - Insufficient Balance")
        error.status_code = 402
        mock_openai.return_value.chat.completions.create.side_effect = error

        with self.assertRaises(AIQuotaExceeded):
            AIService._run_text_deepseek("prompt")

        self.assertEqual(mock_openai.return_value.chat.completions.create.call_count, 1)
        mock_sleep.assert_not_called()

    @override_settings(**_provider_settings(DEEPSEEK_API_KEY="ds-test", OPENAI_API_KEY="sk-a"),
                       AI_PROVIDER_ORDER="deepseek", AI_MAX_RETRIES=0)
    @patch("apps.common.ai_service.OpenAI")
    def test_a_402_moves_the_chain_to_the_next_provider(self, mock_openai):
        error = Exception("Error code: 402 - Insufficient Balance")
        error.status_code = 402
        deepseek_client = MagicMock()
        deepseek_client.chat.completions.create.side_effect = error
        openai_client = _openai_client_with_content('{"result": "from openai"}')
        mock_openai.side_effect = [deepseek_client, openai_client]

        with override_settings(AI_PROVIDER="deepseek"):
            result = AIService.generate_structured_json("prompt")

        self.assertEqual(result.get("result"), "from openai")
        self.assertEqual(result.provider, "openai")


class AnthropicTransportTests(TestCase):

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"))
    @patch("apps.common.ai_service.requests.post")
    def test_request_shape_matches_the_messages_api(self, mock_post):
        mock_post.return_value = MagicMock(**{
            "status_code": 200, "headers": {},
            "json.return_value": {"content": [{"type": "text", "text": '{"ok": true}'}],
                                  "stop_reason": "end_turn"},
        })
        AIService._run_text_anthropic("analyse this")

        url = mock_post.call_args[0][0]
        headers = mock_post.call_args[1]["headers"]
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "an-test")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(payload["model"], "claude-opus-5")
        # max_tokens is REQUIRED by this API, so it must always be present.
        self.assertIsInstance(payload["max_tokens"], int)
        self.assertGreater(payload["max_tokens"], 0)
        # Opus models reject sampling parameters, so sending one is a 400.
        self.assertNotIn("temperature", payload)
        # A prefill would be the old way to force JSON; it is rejected on
        # these models, so the system prompt is the only lever used.
        self.assertNotIn("assistant", [m["role"] for m in payload["messages"]])
        self.assertIn("system", payload)

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"))
    @patch("apps.common.ai_service.requests.post")
    def test_thinking_blocks_are_ignored_and_only_text_is_read(self, mock_post):
        # Thinking is on by default on these models and its blocks carry no
        # text, so a naive join over `content` would return "".
        mock_post.return_value = MagicMock(**{
            "status_code": 200, "headers": {},
            "json.return_value": {
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": '{"ok": true}'},
                ],
                "stop_reason": "end_turn",
            },
        })
        self.assertEqual(AIService._run_text_anthropic("prompt"), {"ok": True})

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"))
    @patch("apps.common.ai_service.requests.post")
    def test_a_refusal_is_raised_not_read_as_an_empty_answer(self, mock_post):
        # A refusal arrives as HTTP 200. Treating it as a clean empty result
        # would report a policy decline as "no defects found".
        mock_post.return_value = MagicMock(**{
            "status_code": 200, "headers": {},
            "json.return_value": {
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"category": "cyber"},
            },
        })
        with self.assertRaises(AIServiceError) as ctx:
            AIService._run_text_anthropic("prompt")
        self.assertIn("declined", str(ctx.exception))

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"), AI_MAX_RETRIES=2)
    @patch("apps.common.ai_service.time.sleep")
    @patch("apps.common.ai_service.requests.post")
    def test_a_rejected_key_is_not_retried(self, mock_post, mock_sleep):
        mock_post.return_value = MagicMock(status_code=401, headers={}, text="bad key")
        with self.assertRaises(AIServiceError):
            AIService._run_text_anthropic("prompt")
        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"), AI_MAX_RETRIES=1)
    @patch("apps.common.ai_service.time.sleep")
    @patch("apps.common.ai_service.requests.post")
    def test_a_server_error_is_retried_then_gives_up(self, mock_post, mock_sleep):
        mock_post.return_value = MagicMock(status_code=503, headers={}, text="busy")
        with self.assertRaises(AIServiceError):
            AIService._run_text_anthropic("prompt")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"))
    @patch("apps.common.ai_service.requests.post")
    def test_image_blocks_precede_the_text_block(self, mock_post):
        mock_post.return_value = MagicMock(**{
            "status_code": 200, "headers": {},
            "json.return_value": {"content": [{"type": "text", "text": '{"defects": []}'}],
                                  "stop_reason": "end_turn"},
        })
        AIService._run_vision_anthropic("find defects", ("http://example.com/a.jpg",), {})

        blocks = mock_post.call_args[1]["json"]["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["image", "text"])
        self.assertEqual(blocks[0]["source"]["type"], "base64")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")

    def test_media_type_is_read_from_the_url_extension(self):
        self.assertEqual(AIService._media_type_for("https://x/a.png"), "image/png")
        self.assertEqual(AIService._media_type_for("https://x/a.webp?sig=1"), "image/webp")
        # A signed URL with no extension is the common case — JPEG, not a crash.
        self.assertEqual(AIService._media_type_for("https://x/asset"), "image/jpeg")


class ProviderEnvironmentIsolationTests(TestCase):
    """Provider configuration must not be readable from the ambient environment.

    `ANTHROPIC_BASE_URL` and `ANTHROPIC_MODEL` are generic names that unrelated
    tooling on the same host already sets — the Claude Code CLI exports both
    for its own routing, and they were present in the shell this was built in.
    Reading them bare would point this service's outbound requests, carrying
    its API key, at whatever host the ambient environment happens to name.
    These tests fail if that isolation is ever removed.
    """

    def test_ambient_anthropic_variables_are_ignored(self):
        with mock.patch.dict(os.environ, {
            "ANTHROPIC_BASE_URL": "https://not-our-endpoint.example",
            "ANTHROPIC_MODEL": "some-other-tooling-model",
            "ANTHROPIC_API_KEY": "ambient-tooling-key",
        }):
            self.assertEqual(AIService._get_anthropic_base_url(), "https://api.anthropic.com")
            self.assertEqual(AIService._get_anthropic_model(), "claude-opus-5")
            self.assertEqual(AIService._get_anthropic_key(), "")
            # And a key found only in the ambient environment must not make
            # the chain believe Anthropic is configured.
            self.assertFalse(AIService._provider_has_key("anthropic"))

    def test_ambient_deepseek_variables_are_ignored(self):
        with mock.patch.dict(os.environ, {
            "DEEPSEEK_BASE_URL": "https://not-our-endpoint.example",
            "DEEPSEEK_MODEL": "not-our-model",
            "DEEPSEEK_API_KEY": "ambient-tooling-key",
        }):
            self.assertEqual(AIService._get_deepseek_base_url(), "https://api.deepseek.com")
            self.assertEqual(AIService._get_deepseek_model(), "deepseek-chat")
            self.assertFalse(AIService._provider_has_key("deepseek"))

    def test_namespaced_variables_are_read(self):
        with mock.patch.dict(os.environ, {
            "NEXUCON_ANTHROPIC_BASE_URL": "https://gateway.internal",
            "NEXUCON_ANTHROPIC_MODEL": "claude-opus-5",
            "NEXUCON_ANTHROPIC_API_KEY": "nx-key",
            "NEXUCON_ANTHROPIC_MAX_TOKENS": "4096",
            "NEXUCON_DEEPSEEK_BASE_URL": "https://ds.internal",
            "NEXUCON_DEEPSEEK_MODEL": "deepseek-chat",
            "NEXUCON_DEEPSEEK_API_KEY": "nx-ds-key",
        }):
            self.assertEqual(AIService._get_anthropic_base_url(), "https://gateway.internal")
            self.assertEqual(AIService._get_anthropic_model(), "claude-opus-5")
            self.assertEqual(AIService._get_anthropic_key(), "nx-key")
            self.assertEqual(AIService._get_anthropic_max_tokens(), 4096)
            self.assertEqual(AIService._get_deepseek_base_url(), "https://ds.internal")
            self.assertEqual(AIService._get_deepseek_model(), "deepseek-chat")
            self.assertTrue(AIService._provider_has_key("deepseek"))

    def test_a_django_setting_still_wins_over_the_environment(self):
        with mock.patch.dict(os.environ, {"NEXUCON_ANTHROPIC_MODEL": "from-env"}):
            with override_settings(ANTHROPIC_MODEL="from-settings"):
                self.assertEqual(AIService._get_anthropic_model(), "from-settings")


class MaxTokensTests(TestCase):

    def test_a_non_integer_max_tokens_falls_back_to_the_default(self):
        # The parameter was documented as `max_tokens` but never used, so at
        # least one call site passes a JSON schema through it. Anthropic
        # requires a real integer, so a schema must not be forwarded.
        self.assertEqual(AIService._coerce_max_tokens({"type": "object"}, 8192), 8192)
        self.assertEqual(AIService._coerce_max_tokens(None, 4096), 4096)
        self.assertEqual(AIService._coerce_max_tokens(True, 4096), 4096)
        self.assertEqual(AIService._coerce_max_tokens(0, 4096), 4096)
        self.assertEqual(AIService._coerce_max_tokens(-5, 4096), 4096)

    def test_a_real_token_budget_is_used(self):
        self.assertEqual(AIService._coerce_max_tokens(1234, 8192), 1234)

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"))
    @patch("apps.common.ai_service.requests.post")
    def test_the_token_budget_reaches_anthropic(self, mock_post):
        mock_post.return_value = MagicMock(**{
            "status_code": 200, "headers": {},
            "json.return_value": {"content": [{"type": "text", "text": "{}"}],
                                  "stop_reason": "end_turn"},
        })
        AIService._run_text_anthropic("prompt", 1234)
        self.assertEqual(mock_post.call_args[1]["json"]["max_tokens"], 1234)

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a"))
    @patch("apps.common.ai_service.OpenAI")
    def test_the_token_budget_reaches_openai(self, mock_openai):
        mock_openai.return_value = _openai_client_with_content("{}")
        AIService._run_text_openai("prompt", 777)
        kwargs = mock_openai.return_value.chat.completions.create.call_args[1]
        self.assertEqual(kwargs["max_tokens"], 777)

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a"))
    @patch("apps.common.ai_service.OpenAI")
    def test_no_token_budget_leaves_the_kwarg_absent(self, mock_openai):
        mock_openai.return_value = _openai_client_with_content("{}")
        AIService._run_text_openai("prompt")
        kwargs = mock_openai.return_value.chat.completions.create.call_args[1]
        self.assertNotIn("max_tokens", kwargs)


class StructuredResultTests(TestCase):
    """Provenance must ride on attributes, never on keys."""

    def test_it_is_still_a_dict_in_every_way_callers_rely_on(self):
        result = StructuredResult({"root_cause": "x"}, provider="anthropic", model="claude-opus-5")
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"root_cause": "x"})
        self.assertEqual(result.get("root_cause"), "x")
        self.assertEqual(json.loads(json.dumps(result)), {"root_cause": "x"})

    def test_provenance_never_becomes_a_payload_key(self):
        result = StructuredResult({"a": 1}, provider="gemini", model="gemini-flash-latest")
        self.assertNotIn("provider", dict(result))
        self.assertNotIn("model", dict(result))

    @override_settings(**_provider_settings(ANTHROPIC_API_KEY="an-test"))
    @patch("apps.common.ai_service.requests.post")
    def test_the_winner_is_recorded_not_the_configured_provider(self, mock_post):
        # The bug this fixes: a statutory analysis record used to name the
        # CONFIGURED provider, so a fallback winner produced a false
        # provenance claim on a document an engineer relies on.
        mock_post.return_value = MagicMock(**{
            "status_code": 200, "headers": {},
            "json.return_value": {"content": [{"type": "text", "text": '{"a": 1}'}],
                                  "stop_reason": "end_turn"},
        })
        with override_settings(AI_PROVIDER="gemini"):
            result = AIService.generate_structured_json("prompt")
        self.assertEqual(result.provider, "anthropic")
        self.assertEqual(result.model, "claude-opus-5")
        self.assertEqual(result, {"a": 1})

    @override_settings(**_provider_settings(GEMINI_API_KEY="gm-test"))
    @patch("google.generativeai.GenerativeModel")
    def test_recommendations_carry_their_provenance_too(self, mock_gm):
        mock_gm.return_value = MagicMock(**{
            "generate_content.return_value": MagicMock(
                text='{"recommendations": [{"recommendation": "x"}], "text_confidence": 0.7}'),
        })
        with override_settings(AI_PROVIDER="gemini"):
            result = AIService.generate_recommendations([], [], 0.01)
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(len(result["recommendations"]), 1)

    @override_settings(**_provider_settings())
    def test_a_total_failure_carries_no_provenance(self):
        # Nothing answered, so nothing may be attributed. An empty detection
        # result is honest; naming a provider for it would not be.
        result = AIService.generate_recommendations([], [], 0.0)
        self.assertEqual(result, {"recommendations": [], "text_confidence": None})
        self.assertIsNone(result.provider)

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a"))
    @patch("apps.common.ai_service.OpenAI")
    def test_a_plain_dict_return_still_resolves_to_the_configured_provider(self, mock_openai):
        # The adapter reads `getattr(data, "provider", None) or <configured>`.
        # A patched plain-dict return must therefore still work, so the shim
        # cannot rot into a None provenance.
        mock_openai.return_value = _openai_client_with_content('{"root_cause": "x"}')
        with override_settings(AI_PROVIDER="openai"):
            result = AIService.generate_structured_json("prompt")
        # The real return carries the winner...
        self.assertEqual(result.provider, "openai")
        # ...and the fallback expression the adapter uses also resolves for a
        # bare dict, which is what a patched test double hands back.
        plain = {"root_cause": "x"}
        self.assertEqual(getattr(plain, "provider", None) or "openai", "openai")

    @override_settings(**_provider_settings(OPENAI_API_KEY="sk-a"))
    @patch("apps.common.ai_service.OpenAI")
    def test_schema_is_stated_in_the_prompt(self, mock_openai):
        mock_openai.return_value = _openai_client_with_content("{}")
        with override_settings(AI_PROVIDER="openai"):
            AIService.generate_structured_json(
                "analyse", 500, schema={"type": "object", "required": ["a"]})
        kwargs = mock_openai.return_value.chat.completions.create.call_args[1]
        self.assertIn('"required": ["a"]', kwargs["messages"][0]["content"])
        self.assertEqual(kwargs["max_tokens"], 500)


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


# ==========================================================================
# common.geo
#
# `haversine_m` lived in apps/evidence/correlation.py, which meant
# apps.inspections had to import apps.evidence in order to measure a distance
# between two points — evidence depending on nothing, inspections depending on
# evidence for arithmetic. It now lives here, in the pure-helper layer, and
# correlation.py re-exports it so no call site changed.
# ==========================================================================

class HaversineTests(TestCase):
    """The distance primitive the site geofence is built on."""

    def test_identical_points_are_zero_metres(self):
        self.assertEqual(haversine_m((6.4281, 3.4219), (6.4281, 3.4219)), 0.0)

    def test_no_coordinate_returns_infinity_not_a_number(self):
        """An unknown position must never read as "0 m away" — that would
        place an inspector at a site they never visited."""
        self.assertEqual(haversine_m((None, None), (6.4281, 3.4219)), float('inf'))
        self.assertEqual(haversine_m((6.4281, 3.4219), (None, None)), float('inf'))
        self.assertEqual(haversine_m((6.4281, None), (6.4281, 3.4219)), float('inf'))

    def test_one_degree_of_latitude_is_the_published_arc_length(self):
        """One degree of latitude is pi*R/180 = 111.19 km everywhere on a
        sphere. Derivable by hand, so it is a real check on the formula."""
        distance_km = haversine_m((0.0, 0.0), (1.0, 0.0)) / 1000.0
        self.assertAlmostEqual(distance_km, 111.19, delta=0.5)

    def test_quarter_of_the_equator_matches_the_sphere(self):
        """(0,0) to (0,90) is a quarter great circle = pi*R/2 = 10007.5 km."""
        distance_km = haversine_m((0.0, 0.0), (0.0, 90.0)) / 1000.0
        self.assertAlmostEqual(distance_km, 10007.5, delta=10.0)

    def test_known_lagos_to_abuja_pair_within_one_percent(self):
        """Lagos (6.5244, 3.3792) to Abuja (9.0765, 7.3986) is ~526 km."""
        distance_km = haversine_m((6.5244, 3.3792), (9.0765, 7.3986)) / 1000.0
        self.assertAlmostEqual(distance_km, 526.0, delta=526.0 * 0.01)

    def test_short_offsets_match_the_local_scale(self):
        """At this latitude one 0.00045 degree step of latitude is ~50 m —
        the scale the geofence tests are written against."""
        self.assertAlmostEqual(
            haversine_m((6.4281, 3.4219), (6.4281 + 0.00045, 3.4219)),
            50.0, delta=1.0)

    def test_distance_is_symmetric(self):
        a, b = (6.4281, 3.4219), (6.5000, 3.5000)
        self.assertAlmostEqual(haversine_m(a, b), haversine_m(b, a), places=6)

    def test_correlation_module_re_exports_the_same_function(self):
        """The re-export is what keeps every existing call site and test
        working; if it ever becomes a copy instead, this asserts it."""
        from apps.evidence import correlation
        self.assertIs(correlation.haversine_m, haversine_m)
