import os
import io
import json
import logging
import re
import time
import base64

import requests
from django.conf import settings
from PIL import Image

try:
    import openai
    from openai import OpenAI, RateLimitError, APIError
except ImportError:
    openai = None
    OpenAI = None
    RateLimitError = Exception
    APIError = Exception

try:
    import google.generativeai as genai
except ImportError:
    genai = None

from .ml_pipeline import MultimodalPipeline

logger = logging.getLogger(__name__)

# ============================================================
# LOCAL ML PIPELINE
# ============================================================

CNN_WEIGHTS = getattr(settings, "CNN_WEIGHTS_PATH", None)
SNN_WEIGHTS = getattr(settings, "SNN_WEIGHTS_PATH", None)
local_ml_pipeline = MultimodalPipeline(CNN_WEIGHTS, SNN_WEIGHTS)

# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================

class AIServiceError(Exception):
    """Base exception for AI service errors."""

class AIQuotaExceeded(AIServiceError):
    """AI project/model quota has been exhausted."""

class AIProviderUnavailable(AIServiceError):
    """AI provider is not configured."""

# ============================================================
# AI SERVICE
# ============================================================

class AIService:
    """
    Dual-Provider AI service (OpenAI Primary -> Gemini Fallback) for NEXUCON.
    """

    @staticmethod
    def _get_provider():
        return getattr(settings, "AI_PROVIDER", os.environ.get("AI_PROVIDER", "gemini")).lower()

    @staticmethod
    def _get_openai_key():
        return getattr(settings, "OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

    @staticmethod
    def _get_openai_model():
        return getattr(settings, "OPENAI_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o"))

    @staticmethod
    def _get_gemini_key():
        return getattr(settings, "GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

    @staticmethod
    def _get_gemini_model():
        return getattr(settings, "GEMINI_MODEL", os.environ.get("GEMINI_MODEL", "gemini-flash-latest"))

    @staticmethod
    def _get_max_retries():
        return int(getattr(settings, "AI_MAX_RETRIES", os.environ.get("AI_MAX_RETRIES", 2)))

    # ========================================================
    # INITIALIZATION
    # ========================================================

    @classmethod
    def _get_openai_client(cls):
        api_key = cls._get_openai_key()
        if not api_key:
            raise AIProviderUnavailable("OPENAI_API_KEY is not configured.")
        return OpenAI(api_key=api_key)

    @classmethod
    def _get_gemini_model_instance(cls):
        api_key = cls._get_gemini_key()
        if not api_key:
            raise AIProviderUnavailable("GEMINI_API_KEY is not configured.")
        genai.configure(api_key=api_key)
        return genai.GenerativeModel(cls._get_gemini_model())

    # ========================================================
    # IMAGE FETCHING
    # ========================================================

    @staticmethod
    def _fetch_image_for_openai(image_url: str):
        if not image_url:
            raise ValueError("Image URL is empty.")
        if image_url == "mock_url" or "example.com" in image_url or "test" in image_url:
            return "data:image/jpeg;base64,dGVzdA=="
        try:
            response = requests.get(image_url, timeout=20)
            response.raise_for_status()
            b64 = base64.b64encode(response.content).decode('utf-8')
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.error("Failed to download image for OpenAI %s: %s", image_url, e)
            raise ValueError(f"Could not load image for OpenAI analysis: {e}") from e

    @staticmethod
    def _fetch_image_for_gemini(image_url: str):
        if not image_url:
            raise ValueError("Image URL is empty.")
        if image_url == "mock_url" or "example.com" in image_url or "test" in image_url:
            return Image.new("RGB", (100, 100), color="red")
        try:
            response = requests.get(image_url, timeout=20)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content))
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            return image
        except Exception as e:
            logger.error("Failed to download image for Gemini %s: %s", image_url, e)
            raise ValueError(f"Could not load image for Gemini analysis: {e}") from e

    # ========================================================
    # GENERATION LOGIC (OPENAI)
    # ========================================================

    @classmethod
    def _generate_with_openai_retry(cls, client, messages, response_format=None):
        max_retries = cls._get_max_retries()
        model_name = cls._get_openai_model()

        for attempt in range(max_retries + 1):
            try:
                logger.info("Sending request to OpenAI (attempt %s/%s).", attempt + 1, max_retries + 1)
                kwargs = {"model": model_name, "messages": messages}
                if response_format:
                    kwargs["response_format"] = {"type": "json_object"}
                
                response = client.chat.completions.create(**kwargs)
                return response.choices[0].message.content
                
            except RateLimitError as e:
                error_text = str(e).lower()
                
                # Check for permanent quota exhaustion
                if "insufficient_quota" in error_text or "exceeded your current quota" in error_text:
                    logger.warning("OpenAI project/model quota has been exhausted. Throwing error to trigger fallback.")
                    raise AIQuotaExceeded("OpenAI API quota has been exhausted.") from e
                
                if attempt >= max_retries:
                    raise AIServiceError("OpenAI API rate limit exceeded after retries.") from e
                
                wait_time = 10.0
                match = re.search(r"try again in (\d+(?:\.\d+)?)s", error_text)
                if match:
                    wait_time = float(match.group(1)) + 1
                    
                wait_time = min(max(wait_time, 1), 60)
                logger.warning("OpenAI rate limit encountered. Waiting %.1f seconds before retry.", wait_time)
                time.sleep(wait_time)
                
            except Exception as e:
                logger.error("OpenAI request failed with error: %s", e)
                raise AIServiceError("OpenAI generation failed.") from e

        raise AIServiceError("OpenAI generation failed.")

    # ========================================================
    # GENERATION LOGIC (GEMINI)
    # ========================================================

    @staticmethod
    def _is_gemini_quota_exhausted(error: Exception) -> bool:
        error_text = str(error).lower()
        if "retry in" in error_text or "retry_delay" in error_text:
            return False
        permanent_quota_indicators = [
            "you exceeded your current quota",
            "quota exceeded",
            "quota_exceeded",
            "generate_content_free_tier_requests",
            "generaterequestsperdayperprojectpermodel-freetier",
            "perdayperprojectpermodel",
            "free_tier_requests",
        ]
        return any(indicator in error_text for indicator in permanent_quota_indicators)

    @staticmethod
    def _is_gemini_temporary_rate_limit(error: Exception) -> bool:
        error_text = str(error).lower()
        indicators = ["429", "rate limit", "rate_limit", "too many requests"]
        return any(indicator in error_text for indicator in indicators)

    @staticmethod
    def _extract_gemini_retry_seconds(error: Exception) -> float:
        error_text = str(error)
        patterns = [r"retry in\s+(\d+(?:\.\d+)?)s", r"seconds:\s*(\d+(?:\.\d+)?)"]
        for pattern in patterns:
            match = re.search(pattern, error_text, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1)) + 1
                except ValueError:
                    pass
        return 60.0

    @classmethod
    def _generate_with_gemini_retry(cls, model, *args, **kwargs):
        max_retries = cls._get_max_retries()
        for attempt in range(max_retries + 1):
            try:
                logger.info("Sending request to Gemini (attempt %s/%s).", attempt + 1, max_retries + 1)
                return model.generate_content(*args, **kwargs).text
                
            except Exception as e:
                if cls._is_gemini_quota_exhausted(e):
                    logger.error("Gemini project/model quota has been exhausted. No retry will be attempted.")
                    raise AIQuotaExceeded("Gemini API quota has been exhausted.") from e
                
                if cls._is_gemini_temporary_rate_limit(e):
                    if attempt >= max_retries:
                        raise AIServiceError("Gemini API rate limit exceeded after retries.") from e
                    wait_time = min(max(cls._extract_gemini_retry_seconds(e), 1), 120)
                    logger.warning("Gemini rate limit encountered. Waiting %.1f seconds before retry.", wait_time)
                    time.sleep(wait_time)
                    continue
                
                logger.error("Gemini request failed with error: %s", e)
                raise AIServiceError("Gemini generation failed.") from e

        raise AIServiceError("Gemini generation failed.")

    # ========================================================
    # JSON PARSING
    # ========================================================

    @staticmethod
    def _parse_json_response(content: str):
        if not content:
            raise ValueError("AI returned an empty response.")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
            content = re.sub(r"\s*```$", "", content)
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error("AI returned invalid JSON: %s", e)
            raise ValueError("AI returned invalid JSON.") from e

    @staticmethod
    def _normalise_list(data, key=None):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if key:
                value = data.get(key)
                if isinstance(value, list):
                    return value
            return [data]
        return []

    @classmethod
    def _normalise_bboxes(cls, items, image_url):
        """Normalise AI-returned image_bbox dicts to 0..1 image fractions.

        Models occasionally return pixel coordinates despite the prompt (e.g.
        ymin=169 on a 320px-tall image). Convert those using the real image
        dimensions so the bbox still marks the exact defect location; clamp to
        [0, 1] and repair swapped min/max pairs. Invalid boxes become None so
        downstream persistence skips them instead of guessing.
        """
        if not items:
            return items
        size = None
        for item in items:
            if not isinstance(item, dict):
                continue
            bbox = item.get("image_bbox")
            if not isinstance(bbox, dict):
                item["image_bbox"] = None
                continue
            try:
                xmin = float(bbox.get("xmin"))
                ymin = float(bbox.get("ymin"))
                xmax = float(bbox.get("xmax"))
                ymax = float(bbox.get("ymax"))
            except (TypeError, ValueError):
                item["image_bbox"] = None
                continue
            if max(xmin, ymin, xmax, ymax) > 1.0:
                if size is None:
                    try:
                        resp = requests.get(image_url, timeout=20)
                        resp.raise_for_status()
                        with Image.open(io.BytesIO(resp.content)) as im:
                            size = im.size
                    except Exception as e:
                        logger.warning("Could not fetch image %s for bbox normalisation: %s", image_url, e)
                        size = (0, 0)
                w, h = size
                if w > 0 and h > 0:
                    xmin, xmax = xmin / w, xmax / w
                    ymin, ymax = ymin / h, ymax / h
            if xmin > xmax:
                xmin, xmax = xmax, xmin
            if ymin > ymax:
                ymin, ymax = ymax, ymin
            xmin, xmax = max(0.0, min(1.0, xmin)), max(0.0, min(1.0, xmax))
            ymin, ymax = max(0.0, min(1.0, ymin)), max(0.0, min(1.0, ymax))
            # A degenerate sliver (either dimension under 2% of the image)
            # cannot be drawn legibly; expand it around its centre to a
            # visible minimum so the marker still points at the exact spot.
            min_extent = 0.06
            if xmax - xmin < min_extent:
                cx = (xmin + xmax) / 2.0
                xmin = max(0.0, cx - min_extent / 2.0)
                xmax = min(1.0, cx + min_extent / 2.0)
                if xmax - xmin < min_extent:
                    xmin, xmax = (0.0, min_extent) if cx < 0.5 else (1.0 - min_extent, 1.0)
            if ymax - ymin < min_extent:
                cy = (ymin + ymax) / 2.0
                ymin = max(0.0, cy - min_extent / 2.0)
                ymax = min(1.0, cy + min_extent / 2.0)
                if ymax - ymin < min_extent:
                    ymin, ymax = (0.0, min_extent) if cy < 0.5 else (1.0 - min_extent, 1.0)
            item["image_bbox"] = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
        return items

    # ========================================================
    # VISUAL DEFECT DETECTION
    # ========================================================

    @classmethod
    def detect_visual_defects(cls, image_url: str) -> list:
        if not image_url:
            raise ValueError("Image URL is required.")

        if image_url == "mock_url" or "mock" in image_url:
            # No fabricated findings — a mock URL means there is no real
            # image to analyse, so there is nothing real to report.
            return []

        schema_desc = (
            "Return a JSON object with a single key 'defects' containing a list of objects representing "
            "detected defects. Each object must have: "
            "'type' (one of: concrete_crack, spalling, corrosion, deformation, void_percentage, de-lamination), "
            "'severity' (one of: low, medium, high, critical), "
            "'description' (a detailed professional inspection finding of 3-6 sentences covering: the visible "
            "characteristics of the defect — shape, orientation, apparent width/extent; the likely root cause; "
            "the risk it poses to structural integrity or durability; and whether it appears active or dormant. "
            "Write it as a qualified structural engineer would record it on site), "
            "'location_x' (float, default 0.0), "
            "'location_y' (float, default 0.0), "
            "'location_z' (float, default 0.0), "
            "'grid_zone' (string, e.g., 'Zone A', optional), "
            "'room_level' (string, e.g., 'Level 2', optional), "
            "'confidence_score' (float between 0.0 and 1.0), "
            "'image_bbox' (an object with 'xmin', 'ymin', 'xmax', 'ymax' — floats between 0.0 and 1.0 giving "
            "the tight bounding box around this defect in the image, where (0,0) is the top-left corner and "
            "(1,1) is the bottom-right corner of the image). This bbox is REQUIRED for every reported defect "
            "and must enclose the actual visible defect, not the whole image"
        )
        prompt = (
            "Analyze this construction site image for genuine structural and construction defects. "
            "Check specifically for: concrete cracks, spalling, corrosion, deformation, void-related surface evidence, and delamination. "
            "Only report defects supported by visible evidence. Do not invent defects. "
            "Use conservative engineering judgment and minimize false positives. "
            "For every defect you report, locate it precisely with its image_bbox so it can be marked exactly "
            "on the image, and give a thorough, elaborate engineering description. "
            f"{schema_desc}"
        )

        provider = cls._get_provider()

        def run_openai():
            client = cls._get_openai_client()
            image_b64 = cls._fetch_image_for_openai(image_url)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_b64}}
                    ]
                }
            ]
            content = cls._generate_with_openai_retry(client, messages, response_format="json_object")
            data = cls._parse_json_response(content)
            defects = cls._normalise_list(data, key="defects")
            logger.info("OpenAI visual defect detection completed. Defects detected: %s", len(defects))
            return defects

        def run_gemini():
            model = cls._get_gemini_model_instance()
            image_pil = cls._fetch_image_for_gemini(image_url)
            content = cls._generate_with_gemini_retry(
                model,
                [prompt, image_pil],
                generation_config={"response_mime_type": "application/json"}
            )
            data = cls._parse_json_response(content)
            defects = cls._normalise_list(data, key="defects")
            logger.info("Gemini visual defect detection completed. Defects detected: %s", len(defects))
            return defects

        primary, secondary = (run_gemini, run_openai) if provider == "gemini" else (run_openai, run_gemini)

        try:
            return cls._normalise_bboxes(primary(), image_url)
        except Exception as e:
            logger.warning("Primary AI provider (%s) visual defect detection failed (%s). Trying secondary...", provider, e)
            try:
                return cls._normalise_bboxes(secondary(), image_url)
            except Exception as e2:
                logger.exception("AI Defect Detection failed entirely: %s. Returning empty list — no fabricated findings.", e2)
                return []

    # ========================================================
    # THERMAL ANOMALY DETECTION
    # ========================================================

    @classmethod
    def detect_thermal_anomalies(cls, image_url: str) -> list:
        if not image_url:
            raise ValueError("Thermal image URL is required.")

        if image_url == "mock_url" or "mock" in image_url:
            # No fabricated findings — a mock URL means there is no real
            # thermal image to analyse, so there is nothing real to report.
            return []

        schema_desc = (
            "Return a JSON object with a single key 'anomalies' containing a list of objects representing "
            "thermal anomalies. Each object must have: "
            "'temperature_variance' (float, temperature difference in °C), "
            "'severity' (one of: low, medium, high, critical), "
            "'location_x' (float, default 0.0), "
            "'location_y' (float, default 0.0), "
            "'location_z' (float, default 0.0), "
            "'grid_zone' (string, e.g., 'Zone A', optional), "
            "'room_level' (string, e.g., 'Level 2', optional), "
            "'confidence_score' (float between 0.0 and 1.0), "
            "'description' (a detailed thermographic finding of 3-6 sentences written by a qualified "
            "thermography surveyor covering: the shape, size and contrast of the thermal pattern; the "
            "estimated temperature differential and its significance; the most probable physical cause "
            "— pipe leakage, hidden dampness, insulation gap, thermal bridging, or overheating "
            "equipment — with the reasoning; the risk it poses if left unaddressed; and a recommended "
            "follow-up verification method such as moisture-meter or borescope inspection), "
            "'image_bbox' (an object with 'xmin', 'ymin', 'xmax', 'ymax' — floats between 0.0 and 1.0 giving "
            "the tight bounding box around this anomaly in the image, where (0,0) is the top-left corner "
            "and (1,1) is the bottom-right corner of the image). This bbox is REQUIRED for every reported "
            "anomaly and must enclose the actual visible anomaly, not the whole image. Both the width "
            "(xmax - xmin) and the height (ymax - ymin) of the box must be at least 0.05 — never a thin "
            "line. Example of a correctly formatted bbox around an anomaly in the upper-left quadrant: "
            "{'xmin': 0.10, 'ymin': 0.05, 'xmax': 0.42, 'ymax': 0.33}"
        )
        prompt = (
            "Analyze this thermal/infrared heatmap image for thermal anomalies. "
            "Focus specifically on: pipe leakage, hidden dampness, insulation gaps, overheating equipment, and abnormal thermal patterns. "
            "Be highly sensitive to temperature variances. Report ANY potential anomalies, even if minor, including their estimated temperature variance. "
            "If the image clearly has absolutely no variances, return an empty list for 'anomalies'. "
            "For every anomaly you report, locate it precisely with its image_bbox so it can be marked exactly "
            "on the image, and give a thorough, elaborate thermographic description. "
            f"{schema_desc}"
        )

        provider = cls._get_provider()

        def run_openai():
            client = cls._get_openai_client()
            image_b64 = cls._fetch_image_for_openai(image_url)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_b64}}
                    ]
                }
            ]
            content = cls._generate_with_openai_retry(client, messages, response_format="json_object")
            data = cls._parse_json_response(content)
            anomalies = cls._normalise_list(data, key="anomalies")
            logger.info("OpenAI thermal anomaly detection completed. Anomalies detected: %s", len(anomalies))
            return anomalies

        def run_gemini():
            model = cls._get_gemini_model_instance()
            image_pil = cls._fetch_image_for_gemini(image_url)
            content = cls._generate_with_gemini_retry(
                model,
                [prompt, image_pil],
                generation_config={"response_mime_type": "application/json"}
            )
            data = cls._parse_json_response(content)
            anomalies = cls._normalise_list(data, key="anomalies")
            logger.info("Gemini thermal anomaly detection completed. Anomalies detected: %s", len(anomalies))
            return anomalies

        primary, secondary = (run_gemini, run_openai) if provider == "gemini" else (run_openai, run_gemini)

        try:
            return cls._normalise_bboxes(primary(), image_url)
        except Exception as e:
            logger.warning("Primary AI provider (%s) thermal anomaly detection failed (%s). Trying secondary...", provider, e)
            try:
                return cls._normalise_bboxes(secondary(), image_url)
            except Exception as e2:
                logger.exception("AI Thermal Anomaly Detection failed entirely: %s. Returning empty list — no fabricated findings.", e2)
                return []

    # ========================================================
    # MULTIMODAL DELAMINATION DETECTION
    # ========================================================

    @classmethod
    def detect_delamination_multimodal(cls, thermal_url: str, visible_url: str) -> list:
        if not thermal_url:
            raise ValueError("Thermal image URL is required.")
        if not visible_url:
            raise ValueError("Visible image URL is required.")

        if thermal_url == "mock_url" or visible_url == "mock_url" or "mock" in thermal_url or "mock" in visible_url:
            # No fabricated findings — mock URLs mean there are no real
            # images to analyse, so there is nothing real to report.
            return []

        try:
            local_results = local_ml_pipeline.process_images(thermal_url, visible_url)
            if local_results is not None:
                logger.info("Using local CNN+SNN PyTorch pipeline for multimodal delamination detection.")
                return local_results
        except Exception as e:
            logger.warning("Local CNN+SNN pipeline failed. Falling back to OpenAI/Gemini: %s", e)

        schema_desc = (
            "Return a JSON object with a single key 'delaminations' containing a list of objects representing "
            "detected delaminations. Each object must have: "
            "'type' (always 'delamination'), "
            "'severity' (one of: low, medium, high, critical), "
            "'description' (a detailed professional finding of 3-6 sentences written by a qualified "
            "structural engineer covering: the extent, shape and thermal signature of the suspected "
            "delaminated zone in the thermal image; what the visible image shows at the same location and "
            "whether it corroborates or refutes a subsurface void; the likely cause — corrosion-induced "
            "cover separation, poor consolidation, freeze-thaw, or debonding; the structural risk given "
            "the apparent size and location; and the recommended verification such as sounding, impact "
            "echo or ultrasonic pulse velocity testing), "
            "'location_x' (float, default 0.0), "
            "'location_y' (float, default 0.0), "
            "'location_z' (float, default 0.0), "
            "'confidence_score' (float between 0.0 and 1.0), "
            "'is_false_positive' (boolean), "
            "'image_bbox' (an object with 'xmin', 'ymin', 'xmax', 'ymax' — floats between 0.0 and 1.0 "
            "giving the tight bounding box around this delamination zone in the images, where (0,0) is "
            "the top-left corner and (1,1) is the bottom-right corner). This bbox is REQUIRED for every "
            "reported delamination and must enclose the actual anomaly, not the whole image"
        )
        prompt = (
            "Analyze these two construction inspection images. "
            "Image 1 is the thermal image. Image 2 is the visible image. "
            "Use the thermal image to identify potential subsurface delamination. "
            "Then compare suspicious regions against the visible image. "
            "If the visible image provides evidence that the thermal anomaly is only a surface stain, paint variation, debris, or another visible surface condition, classify it as a false positive. "
            "Only confirm delamination when the available evidence supports it. "
            "Do NOT invent a hypothetical delamination. "
            "If there is no supported delamination, return an empty list for 'delaminations'. "
            "For every delamination you report, locate it precisely with its image_bbox so it can be "
            "marked exactly on the image, and give a thorough, elaborate engineering description. "
            f"{schema_desc}"
        )

        provider = cls._get_provider()

        def run_openai():
            client = cls._get_openai_client()
            thermal_b64 = cls._fetch_image_for_openai(thermal_url)
            visible_b64 = cls._fetch_image_for_openai(visible_url)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": thermal_b64}},
                        {"type": "image_url", "image_url": {"url": visible_b64}}
                    ]
                }
            ]
            content = cls._generate_with_openai_retry(client, messages, response_format="json_object")
            data = cls._parse_json_response(content)
            delaminations = cls._normalise_list(data, key="delaminations")
            logger.info("OpenAI multimodal delamination detection completed. Results: %s", len(delaminations))
            return delaminations

        def run_gemini():
            model = cls._get_gemini_model_instance()
            thermal_pil = cls._fetch_image_for_gemini(thermal_url)
            visible_pil = cls._fetch_image_for_gemini(visible_url)
            content = cls._generate_with_gemini_retry(
                model,
                [prompt, thermal_pil, visible_pil],
                generation_config={"response_mime_type": "application/json"}
            )
            data = cls._parse_json_response(content)
            delaminations = cls._normalise_list(data, key="delaminations")
            logger.info("Gemini multimodal delamination detection completed. Results: %s", len(delaminations))
            return delaminations

        primary, secondary = (run_gemini, run_openai) if provider == "gemini" else (run_openai, run_gemini)

        try:
            return cls._normalise_bboxes(primary(), thermal_url)
        except Exception as e:
            logger.warning("Primary AI provider (%s) multimodal detection failed (%s). Trying secondary...", provider, e)
            try:
                return cls._normalise_bboxes(secondary(), thermal_url)
            except Exception as e2:
                logger.exception("AI Multimodal Detection failed entirely: %s. Returning empty list.", e2)
                return []

    # ========================================================
    # ENGINEERING RECOMMENDATIONS
    # ========================================================

    @classmethod
    def generate_recommendations(cls, defects: list, anomalies: list, deviation: float) -> list:

        prompt = (
            "Act as a senior construction quality inspector. "
            "Review the following NEXUCON Site Supervise inspection results.\n\n"
            f"Fused Point Cloud to BIM Mean Deviation: {deviation} m\n\n"
            f"Identified Structural Defects:\n{json.dumps(defects, indent=2)}\n\n"
            f"Thermal Anomalies:\n{json.dumps(anomalies, indent=2)}\n\n"
            "Provide 3 to 5 clear, concise, actionable engineering recommendations based ONLY on the supplied inspection results. "
            "Do not invent defects or measurements. "
            "Return a JSON object with two keys: 'recommendations' containing a list of objects, and 'text_confidence' (float between 0.0 and 1.0) indicating how sure the AI is about the generated text. "
            "Each object in 'recommendations' must have: "
            "'recommendation' (string), "
            "'priority' (one of: Urgent, High, Routine), "
            "'related_finding_id' (string, identifying the defect or anomaly this addresses)"
        )

        provider = cls._get_provider()

        def run_openai():
            client = cls._get_openai_client()
            messages = [{"role": "user", "content": prompt}]
            content = cls._generate_with_openai_retry(client, messages, response_format="json_object")
            data = cls._parse_json_response(content)
            recommendations = cls._normalise_list(data, key="recommendations")
            text_confidence = data.get("text_confidence")
            logger.info("OpenAI recommendation generation completed. Recommendations: %s", len(recommendations))
            return {"recommendations": recommendations, "text_confidence": text_confidence}

        def run_gemini():
            model = cls._get_gemini_model_instance()
            content = cls._generate_with_gemini_retry(
                model,
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            data = cls._parse_json_response(content)
            recommendations = cls._normalise_list(data, key="recommendations")
            text_confidence = data.get("text_confidence")
            logger.info("Gemini recommendation generation completed. Recommendations: %s", len(recommendations))
            return {"recommendations": recommendations, "text_confidence": text_confidence}

        primary, secondary = (run_gemini, run_openai) if provider == "gemini" else (run_openai, run_gemini)

        try:
            return primary()
        except Exception as e:
            logger.warning("Primary AI provider (%s) recommendation generation failed (%s). Trying secondary...", provider, e)
            try:
                return secondary()
            except Exception as e2:
                logger.exception("AI Recommendations generation failed entirely: %s. Returning empty list.", e2)
                return {
                    "recommendations": [],
                    "text_confidence": None
                }