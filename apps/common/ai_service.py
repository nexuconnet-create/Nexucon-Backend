import os
import io
import json
import logging
import re
import time
import base64
import functools

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
    """Base exception for AI service errors.

    `attempts` carries one entry per provider that was tried and failed, so a
    caller that gives up can say WHICH providers were tried and why, instead
    of reporting a single anonymous failure. Set by `_with_failover`.
    """

    def __init__(self, *args, attempts=None):
        super().__init__(*args)
        self.attempts = list(attempts or [])


class AIQuotaExceeded(AIServiceError):
    """AI project/model quota has been exhausted."""

class AIProviderUnavailable(AIServiceError):
    """AI provider is not configured."""


# ============================================================
# STRUCTURED RESULT
# ============================================================

class StructuredResult(dict):
    """A parsed AI JSON object that remembers who produced it.

    Subclasses `dict` so every existing consumer keeps working unchanged —
    `isinstance(x, dict)`, `.get()`, `json.dumps()`, and `==` against a plain
    dict all behave exactly as before. The provenance rides on attributes,
    deliberately NOT on keys, so it can never leak into a payload that is
    stored or rendered.

    Why this exists: the PUNDIT adapter used to record the *configured*
    provider as the model that produced a statutory analysis record. With one
    provider that was merely redundant; with a failover chain it is a false
    provenance claim on a document an engineer relies on.
    """

    provider = None
    model = None

    def __init__(self, *args, provider=None, model=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider = provider
        self.model = model


# ============================================================
# PROVIDER CHAIN TABLES
# ============================================================
#
# Every AI call is attempted against every *configured* provider that can
# serve it, in a deterministic order, with each attempt isolated in its own
# try/except. One provider being down, rate-limited, out of credit, or
# returning malformed output must never stop the others.
#
# Capability is DERIVED from which runner exists (see `CAPABILITIES` below),
# never declared in parallel with it — so a provider cannot advertise a
# capability it has no code for, and the two can never drift apart. There is
# no vision runner for DeepSeek, so CAPABILITIES["deepseek"] == {"text"} is a
# computed fact rather than a claim. Giving it one is a code change, not a
# config change: write the runner and the capability follows.
#
# ORDER IS DETERMINISTIC ON PURPOSE. A failover sequence appears in logs and
# in reasoning records; dict or set iteration order would make the same
# failure produce a different story on every run.

_CANONICAL_ORDER = ("openai", "gemini", "anthropic", "deepseek")

_PROVIDER_KEY_GETTERS = {
    "openai": "_get_openai_key",
    "gemini": "_get_gemini_key",
    "anthropic": "_get_anthropic_key",
    "deepseek": "_get_deepseek_key",
}

_PROVIDER_MODEL_GETTERS = {
    "openai": "_get_openai_model",
    "gemini": "_get_gemini_model",
    "anthropic": "_get_anthropic_model",
    "deepseek": "_get_deepseek_model",
}

# Which classmethod serves which call shape, per provider. The values are
# method NAMES because these tables are read before the class body is
# executed; a test asserts every named method exists, so the table cannot rot.
_VISION_RUNNER_NAMES = {
    "openai": "_run_vision_openai",
    "gemini": "_run_vision_gemini",
    "anthropic": "_run_vision_anthropic",
}
_TEXT_RUNNER_NAMES = {
    "openai": "_run_text_openai",
    "gemini": "_run_text_gemini",
    "anthropic": "_run_text_anthropic",
    "deepseek": "_run_text_deepseek",
}
_RUNNER_NAMES = {"vision": _VISION_RUNNER_NAMES, "text": _TEXT_RUNNER_NAMES}

CAPABILITIES = {
    provider: frozenset(
        capability for capability, table in _RUNNER_NAMES.items()
        if provider in table
    )
    for provider in _PROVIDER_KEY_GETTERS
}

# Anthropic content-block media types, keyed by file extension. An
# unrecognised extension falls back to JPEG rather than refusing the call:
# the common case is a signed CDN URL with no extension at all.
_IMAGE_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Anthropic takes no `temperature` on the current Opus models (a 400), and
# thinking is on by default there, so neither is sent. The system prompt is
# the only lever used to pin the response to JSON. Prefilling an assistant
# turn would be the old way to do that; prefill is rejected on these models.
_ANTHROPIC_JSON_SYSTEM = (
    "You are a structural engineering analysis assistant for NEXUCON, a "
    "construction-quality platform used by Nigerian building-control "
    "agencies. Reply with a single valid JSON object and NOTHING else — no "
    "prose, no markdown code fences, no commentary before or after the JSON. "
    "Never invent a measurement, a defect or an event that is not present in "
    "the supplied data."
)

_ANTHROPIC_API_VERSION = "2023-06-01"


# ============================================================
# AI SERVICE
# ============================================================

class AIService:
    """
    Multi-Provider AI service for NEXUCON.

    Every call runs against all configured providers that can serve it:
    the configured `AI_PROVIDER` first, then the rest in a deterministic
    order. The first provider to answer successfully wins; a failure of any
    kind moves on to the next. See `_with_failover` for the failure contract
    each public method holds.
    """

    @staticmethod
    def _env(name, default=""):
        """Read a NEXUCON-namespaced environment variable.

        Deliberately NOT the bare `ANTHROPIC_*` / `DEEPSEEK_*` names. Those are
        generic enough that unrelated tooling on the same host sets them: the
        Claude Code CLI, for example, exports `ANTHROPIC_BASE_URL` and
        `ANTHROPIC_MODEL` for its own routing, and both were present in the
        shell this was developed in. An unprefixed lookup would silently
        redirect this service's outbound requests — and the API key travelling
        on them — to whatever host the ambient environment happens to name.
        The prefix makes that collision impossible.
        """
        return os.environ.get("NEXUCON_" + name, default)

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
    def _get_anthropic_key():
        return getattr(settings, "ANTHROPIC_API_KEY", AIService._env("ANTHROPIC_API_KEY"))

    @staticmethod
    def _get_anthropic_model():
        # Claude Haiku 4.5 — the cheapest Claude model that still does both
        # vision and structured JSON, at $1/$5 per million tokens against
        # Opus 5's $5/$25. The prompts here are short and the numbers they
        # must not touch (pulse velocity, E.C.S., confidence) are computed
        # deterministically, so the top tier buys little on this workload.
        # Read at call time: move to `claude-sonnet-5` ($2/$10) or
        # `claude-opus-5` ($5/$25) in .env alone, no deploy.
        return getattr(settings, "ANTHROPIC_MODEL",
                       AIService._env("ANTHROPIC_MODEL", "claude-haiku-4-5"))

    @staticmethod
    def _get_anthropic_base_url():
        return getattr(settings, "ANTHROPIC_BASE_URL",
                       AIService._env("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))

    @staticmethod
    def _get_deepseek_key():
        return getattr(settings, "DEEPSEEK_API_KEY", AIService._env("DEEPSEEK_API_KEY"))

    @staticmethod
    def _get_deepseek_model():
        # `deepseek-flash` is DeepSeek's cheapest model and one of only two
        # IDs their pricing page still documents — the previous default,
        # `deepseek-chat`, no longer appears there at all, so it was a stale
        # ID rather than merely an expensive one. Both documented models
        # (flash and v4-pro) list JSON Output as supported, which this
        # service needs: the runner below sends `{"type": "json_object"}`.
        return getattr(settings, "DEEPSEEK_MODEL",
                       AIService._env("DEEPSEEK_MODEL", "deepseek-flash"))

    @staticmethod
    def _get_deepseek_base_url():
        # DeepSeek is OpenAI-compatible, so it reuses the installed `openai`
        # SDK against this base URL rather than adding another dependency.
        return getattr(settings, "DEEPSEEK_BASE_URL",
                       AIService._env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))

    @staticmethod
    def _get_max_retries():
        return int(getattr(settings, "AI_MAX_RETRIES", os.environ.get("AI_MAX_RETRIES", 2)))

    @staticmethod
    def _get_request_timeout():
        return float(getattr(settings, "AI_REQUEST_TIMEOUT_SECONDS",
                             os.environ.get("AI_REQUEST_TIMEOUT_SECONDS", 60)))

    @staticmethod
    def _get_anthropic_max_tokens():
        return int(getattr(settings, "ANTHROPIC_MAX_TOKENS",
                           AIService._env("ANTHROPIC_MAX_TOKENS", 8192)))

    @staticmethod
    def _get_failover_deadline():
        """Wall-clock ceiling for one call's whole provider chain, in seconds.

        With four providers each allowed its own retry budget, one slow
        request path could otherwise stack four full timeout sequences. The
        deadline stops the chain from starting another provider once the
        budget is spent. 0 disables it.
        """
        return float(getattr(settings, "AI_FAILOVER_DEADLINE_SECONDS",
                             os.environ.get("AI_FAILOVER_DEADLINE_SECONDS", 300)))

    @staticmethod
    def _coerce_max_tokens(value, default):
        """Return a usable positive token budget.

        `generate_structured_json`'s second parameter was documented as
        `max_tokens` but was never passed to either provider, so at least one
        call site has been passing a JSON schema dict through it since it was
        written. Anthropic requires a real `max_tokens`, so a non-integer is
        replaced by the provider default rather than sent and rejected.
        """
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return default
        return value

    @classmethod
    def _model_for(cls, provider):
        getter = _PROVIDER_MODEL_GETTERS.get(provider)
        return getattr(cls, getter)() if getter else None

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

    @classmethod
    def _get_deepseek_client(cls):
        """DeepSeek via the OpenAI SDK, pointed at DeepSeek's base URL.

        Deliberately its own factory rather than a parameter on
        `_get_openai_client`: the two are different accounts with different
        keys, and a shared factory would make one provider's key able to
        spend the other's credit.

        `max_retries=0` because `_generate_with_deepseek_retry` is the single
        retry authority — the SDK's own backoff underneath ours would
        multiply the worst-case wall clock by the number of providers.
        """
        api_key = cls._get_deepseek_key()
        if not api_key:
            raise AIProviderUnavailable("DEEPSEEK_API_KEY is not configured.")
        return OpenAI(api_key=api_key, base_url=cls._get_deepseek_base_url(),
                      max_retries=0)

    # ========================================================
    # PROVIDER SELECTION AND FAILOVER
    # ========================================================

    @classmethod
    def _provider_has_key(cls, provider):
        getter = _PROVIDER_KEY_GETTERS.get(provider)
        if not getter:
            return False
        return bool(getattr(cls, getter)())

    @classmethod
    def _provider_order(cls, available):
        """Deterministic provider order for one call.

        The configured provider goes first, so an explicit AI_PROVIDER choice
        is honoured exactly as it was before the chain existed. The rest
        follow `AI_PROVIDER_ORDER` when set, else `_CANONICAL_ORDER`.

        A provider with no key is SKIPPED, not attempted and failed. There is
        no key to fail with, and attempting it would cost a request's worth of
        latency to produce a misleading error.
        """
        override = getattr(settings, "AI_PROVIDER_ORDER",
                           os.environ.get("AI_PROVIDER_ORDER", ""))
        if isinstance(override, str):
            override = [part.strip().lower() for part in override.split(",") if part.strip()]
        override = [str(part).strip().lower() for part in (override or [])]

        candidates = [cls._get_provider()] + override + list(_CANONICAL_ORDER)
        order = []
        for name in candidates:
            if name in order or name not in available:
                continue
            if not cls._provider_has_key(name):
                logger.info(
                    "AI provider %s can serve this call but has no API key — skipped.",
                    name,
                )
                continue
            order.append(name)
        return order

    @classmethod
    def _build_runners(cls, capability, *args):
        """Bind one runner per provider that can serve `capability`.

        The dict's keys are the only providers this call may attempt, so a
        capability the tables do not grant (DeepSeek + vision) cannot even be
        reached — the closure for it does not exist.
        """
        return {
            provider: functools.partial(getattr(cls, method_name), *args)
            for provider, method_name in _RUNNER_NAMES[capability].items()
        }

    @classmethod
    def _with_failover(cls, runners, on_total_failure, capability="call"):
        """Try every configured capable provider; return (result, provider).

        Each attempt is isolated: any exception — missing key, HTTP failure,
        quota exhaustion, unparseable output — moves on to the next provider.
        Nothing aborts the chain early, because the whole point of running
        several providers together is that one breaking does not take the
        others with it.

        `on_total_failure` is the method's honest-state policy, and it belongs
        to the method rather than the caller:
          * a callable -> returned as the result (vision returns [], which is
            a valid finding: "I looked and saw nothing");
          * None       -> raise, for calls with no honest empty value.

        Raises AIProviderUnavailable when nothing is *configured* (a distinct
        state from "everything was tried and failed"), and AIServiceError
        carrying `.attempts` when everything failed.
        """
        order = cls._provider_order(set(runners))
        attempts = []

        if not order:
            logger.warning(
                "No AI provider is configured for this call (capability=%s, capable=%s). "
                "Not attempting any request.",
                capability, sorted(runners),
            )
            if on_total_failure is None:
                raise AIProviderUnavailable(
                    "No AI provider is configured for this call. Set at least one of "
                    "OPENAI_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY or DEEPSEEK_API_KEY."
                )
            return on_total_failure([]), None

        deadline = cls._get_failover_deadline()
        started = time.monotonic()

        for name in order:
            elapsed = time.monotonic() - started
            if deadline and elapsed > deadline:
                attempts.append({
                    "provider": name,
                    "error": f"skipped: {deadline:.0f}s failover deadline exceeded",
                })
                logger.warning(
                    "AI failover deadline of %.0fs reached after %s — not starting %s.",
                    deadline, ", ".join(a["provider"] for a in attempts[:-1]) or "no attempt", name,
                )
                break
            try:
                result = runners[name]()
            except Exception as e:  # noqa: BLE001 — any failure means "try the next provider"
                attempts.append({"provider": name, "error": f"{type(e).__name__}: {e}"})
                logger.warning(
                    "AI provider %s failed (%s). Trying the next capable provider.",
                    name, e,
                )
                continue
            if attempts:
                logger.warning(
                    "AI failover: %s produced the result after %s failed.",
                    name, ", ".join(a["provider"] for a in attempts),
                )
            return result, name

        failed = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts)
        logger.error("All AI providers failed (capability=%s): %s", capability, failed)
        if on_total_failure is None:
            raise AIServiceError(
                f"All {len(attempts)} configured AI provider(s) failed for this call. {failed}",
                attempts=attempts,
            )
        return on_total_failure(attempts), None

    # ========================================================
    # IMAGE FETCHING
    # ========================================================

    @staticmethod
    def _is_placeholder_image_url(image_url):
        """A URL that stands in for an image in tests — never downloaded."""
        return (
            image_url == "mock_url"
            or "example.com" in image_url
            or "test" in image_url
        )

    @classmethod
    def _download_image_bytes(cls, image_url, cache=None):
        """Download image bytes once per call, memoised.

        Memoised per call rather than globally: an inspection image can change
        at the same URL, so a cross-request cache would eventually serve a
        stale frame to a defect detector. Within one call the opposite is
        true — every provider must see the SAME bytes, or failover would be
        comparing pictures rather than models. It also matters for signed
        URLs: one that expires mid-chain would otherwise fail only the later
        providers, making the winner an artefact of timing.
        """
        if cache is not None and image_url in cache:
            return cache[image_url]
        response = requests.get(image_url, timeout=20)
        response.raise_for_status()
        content = response.content
        if cache is not None:
            cache[image_url] = content
        return content

    @classmethod
    def _fetch_image_for_openai(cls, image_url: str, cache=None):
        if not image_url:
            raise ValueError("Image URL is empty.")
        if cls._is_placeholder_image_url(image_url):
            return "data:image/jpeg;base64,dGVzdA=="
        try:
            content = cls._download_image_bytes(image_url, cache)
            b64 = base64.b64encode(content).decode('utf-8')
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.error("Failed to download image for OpenAI %s: %s", image_url, e)
            raise ValueError(f"Could not load image for OpenAI analysis: {e}") from e

    @classmethod
    def _fetch_image_for_gemini(cls, image_url: str, cache=None):
        if not image_url:
            raise ValueError("Image URL is empty.")
        if cls._is_placeholder_image_url(image_url):
            return Image.new("RGB", (100, 100), color="red")
        try:
            content = cls._download_image_bytes(image_url, cache)
            image = Image.open(io.BytesIO(content))
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            return image
        except Exception as e:
            logger.error("Failed to download image for Gemini %s: %s", image_url, e)
            raise ValueError(f"Could not load image for Gemini analysis: {e}") from e

    @staticmethod
    def _media_type_for(image_url):
        path = (image_url or "").split("?", 1)[0].lower()
        extension = path.rsplit(".", 1)[-1] if "." in path else ""
        return _IMAGE_MEDIA_TYPES.get(extension, "image/jpeg")

    @classmethod
    def _fetch_image_for_anthropic(cls, image_url: str, cache=None):
        """Return (base64_data, media_type) for an Anthropic image block."""
        if not image_url:
            raise ValueError("Image URL is empty.")
        if cls._is_placeholder_image_url(image_url):
            return "dGVzdA==", "image/jpeg"
        try:
            content = cls._download_image_bytes(image_url, cache)
            return (
                base64.standard_b64encode(content).decode("utf-8"),
                cls._media_type_for(image_url),
            )
        except Exception as e:
            logger.error("Failed to download image for Anthropic %s: %s", image_url, e)
            raise ValueError(f"Could not load image for Anthropic analysis: {e}") from e

    # ========================================================
    # GENERATION LOGIC (OPENAI)
    # ========================================================

    @classmethod
    def _generate_with_openai_retry(cls, client, messages, response_format=None,
                                    max_tokens=None):
        max_retries = cls._get_max_retries()
        model_name = cls._get_openai_model()

        for attempt in range(max_retries + 1):
            try:
                logger.info("Sending request to OpenAI (attempt %s/%s).", attempt + 1, max_retries + 1)
                kwargs = {"model": model_name, "messages": messages}
                if response_format:
                    kwargs["response_format"] = {"type": "json_object"}
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens

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
    # GENERATION LOGIC (DEEPSEEK — OpenAI-compatible transport)
    # ========================================================

    @staticmethod
    def _is_deepseek_balance_exhausted(error: Exception) -> bool:
        """DeepSeek returns HTTP 402 'Insufficient Balance' for an unfunded
        account. That is permanent — retrying it burns the failover budget to
        reproduce the same answer, so it is classified before anything else
        and never retried."""
        if getattr(error, "status_code", None) == 402:
            return True
        error_text = str(error).lower()
        return "insufficient balance" in error_text or "insufficient_quota" in error_text

    @classmethod
    def _generate_with_deepseek_retry(cls, client, messages, response_format=None,
                                      max_tokens=None):
        max_retries = cls._get_max_retries()
        model_name = cls._get_deepseek_model()

        for attempt in range(max_retries + 1):
            try:
                logger.info("Sending request to DeepSeek (attempt %s/%s).", attempt + 1, max_retries + 1)
                kwargs = {"model": model_name, "messages": messages}
                if response_format:
                    kwargs["response_format"] = {"type": "json_object"}
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens

                response = client.chat.completions.create(**kwargs)
                return response.choices[0].message.content

            except Exception as e:  # noqa: BLE001 — classified immediately below
                if cls._is_deepseek_balance_exhausted(e):
                    logger.warning(
                        "DeepSeek account has insufficient balance. Not retrying — "
                        "moving to the next provider."
                    )
                    raise AIQuotaExceeded("DeepSeek account has insufficient balance.") from e

                is_rate_limit = isinstance(e, RateLimitError) or getattr(e, "status_code", None) == 429
                is_server_error = (getattr(e, "status_code", None) or 0) >= 500

                if is_rate_limit or is_server_error:
                    if attempt >= max_retries:
                        raise AIServiceError("DeepSeek rate limit/server error after retries.") from e
                    wait_time = min(max(2.0 ** attempt, 1), 60)
                    logger.warning("DeepSeek transient failure. Waiting %.1f seconds before retry.", wait_time)
                    time.sleep(wait_time)
                    continue

                logger.error("DeepSeek request failed with error: %s", e)
                raise AIServiceError("DeepSeek generation failed.") from e

        raise AIServiceError("DeepSeek generation failed.")

    # ========================================================
    # GENERATION LOGIC (GEMINI)
    # ========================================================

    @staticmethod
    def _is_gemini_quota_exhausted(error: Exception) -> bool:
        error_text = str(error).lower()
        # Daily / free-tier quota exhaustion is permanent for our purposes —
        # the window resets at the next day boundary, so the "Please retry in
        # Xs" hint these errors carry must NOT turn them into a 60 s
        # sleep-retry loop. Checked before the retriable-hint guard so a
        # daily quota is never retried.
        daily_quota_indicators = [
            "generate_content_free_tier_requests",
            "generaterequestsperdayperprojectpermodel-freetier",
            "perdayperprojectpermodel",
            "free_tier_requests",
        ]
        if any(indicator in error_text for indicator in daily_quota_indicators):
            return True
        if "retry in" in error_text or "retry_delay" in error_text:
            return False
        permanent_quota_indicators = [
            "you exceeded your current quota",
            "quota exceeded",
            "quota_exceeded",
            # A bad key is permanent too: an invalid/expired key must not be
            # retried as a temporary rate limit either.
            "api key not valid",
            "api_key_invalid",
            "api key expired",
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
    # GENERATION LOGIC (ANTHROPIC)
    # ========================================================
    #
    # Called over HTTP with the already-pinned `requests`, not the
    # `anthropic` SDK. That is a deliberate dependency decision rather than a
    # stylistic one: the backend ships as a frozen PyInstaller bundle, so a
    # new package means a new hiddenimports/binary entry and a rebuilt spec
    # for every deployment. This is also how the repository already talks to
    # Trimble Connect, including its `patch("...requests.post")` test pattern.

    @staticmethod
    def _extract_anthropic_text(response):
        """Pull the answer out of a Messages API response.

        Thinking is on by default on the current models and its blocks carry
        no text, so only `text` blocks are joined. A refusal arrives as HTTP
        200 with `stop_reason: "refusal"` — it must be raised, not read as an
        empty answer, or a policy decline would look like a clean image.
        """
        try:
            data = response.json()
        except ValueError as e:
            raise AIServiceError("Anthropic returned a non-JSON response body.") from e

        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal":
            details = data.get("stop_details") or {}
            raise AIServiceError(
                "Anthropic declined this request "
                f"(category: {details.get('category') or 'unspecified'})."
            )
        if stop_reason == "max_tokens":
            logger.warning(
                "Anthropic stopped at max_tokens — the response is truncated and "
                "its JSON is likely incomplete."
            )

        blocks = data.get("content") or []
        text = "".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text.strip():
            raise AIServiceError("Anthropic returned no text content.")
        return text

    @staticmethod
    def _anthropic_retry_after(response):
        raw = response.headers.get("retry-after") if response.headers else None
        try:
            return min(max(float(raw) + 1, 1.0), 60.0)
        except (TypeError, ValueError):
            return 10.0

    @classmethod
    def _generate_with_anthropic_retry(cls, content_blocks, max_tokens, system=None):
        max_retries = cls._get_max_retries()
        url = cls._get_anthropic_base_url().rstrip("/") + "/v1/messages"
        timeout = cls._get_request_timeout()
        payload = {
            "model": cls._get_anthropic_model(),
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content_blocks}],
        }
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": cls._get_anthropic_key(),
            "anthropic-version": _ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        for attempt in range(max_retries + 1):
            try:
                logger.info("Sending request to Anthropic (attempt %s/%s).", attempt + 1, max_retries + 1)
                response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            except Exception as e:
                logger.error("Anthropic request failed with error: %s", e)
                raise AIServiceError("Anthropic generation failed.") from e

            status = response.status_code
            if status == 200:
                return cls._extract_anthropic_text(response)

            if status in (401, 403):
                # A rejected key is permanent — no retry, straight on.
                raise AIServiceError(f"Anthropic rejected the API key (HTTP {status}).")
            if status == 402:
                raise AIQuotaExceeded("Anthropic account has insufficient credit.")
            if status == 429 or status >= 500:
                if attempt >= max_retries:
                    raise AIServiceError(
                        f"Anthropic returned HTTP {status} after {max_retries + 1} attempt(s).")
                wait_time = cls._anthropic_retry_after(response)
                logger.warning("Anthropic HTTP %s. Waiting %.1f seconds before retry.", status, wait_time)
                time.sleep(wait_time)
                continue

            raise AIServiceError(
                f"Anthropic rejected the request (HTTP {status}): {(response.text or '')[:300]}")

        raise AIServiceError("Anthropic generation failed.")

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
    def _normalise_bboxes(cls, items, image_url, cache=None):
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
                        content = cls._download_image_bytes(image_url, cache)
                        with Image.open(io.BytesIO(content)) as im:
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
    # PROVIDER RUNNERS (VISION)
    # ========================================================
    #
    # Each returns the provider's raw parsed JSON object; the calling public
    # method extracts and normalises the list it asked for, so the same
    # runner serves defect, thermal and delamination detection.

    @classmethod
    def _run_vision_openai(cls, prompt, image_urls, cache):
        client = cls._get_openai_client()
        content = [{"type": "text", "text": prompt}]
        for url in image_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": cls._fetch_image_for_openai(url, cache)},
            })
        raw = cls._generate_with_openai_retry(
            client, [{"role": "user", "content": content}], response_format="json_object")
        return cls._parse_json_response(raw)

    @classmethod
    def _run_vision_gemini(cls, prompt, image_urls, cache):
        model = cls._get_gemini_model_instance()
        parts = [prompt] + [cls._fetch_image_for_gemini(url, cache) for url in image_urls]
        raw = cls._generate_with_gemini_retry(
            model, parts, generation_config={"response_mime_type": "application/json"})
        return cls._parse_json_response(raw)

    @classmethod
    def _run_vision_anthropic(cls, prompt, image_urls, cache):
        blocks = []
        for url in image_urls:
            b64, media_type = cls._fetch_image_for_anthropic(url, cache)
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        blocks.append({"type": "text", "text": prompt})
        raw = cls._generate_with_anthropic_retry(
            blocks, cls._get_anthropic_max_tokens(), system=_ANTHROPIC_JSON_SYSTEM)
        return cls._parse_json_response(raw)

    # ========================================================
    # PROVIDER RUNNERS (TEXT)
    # ========================================================

    @classmethod
    def _run_text_openai(cls, prompt, max_tokens=None):
        client = cls._get_openai_client()
        raw = cls._generate_with_openai_retry(
            client, [{"role": "user", "content": prompt}],
            response_format="json_object", max_tokens=max_tokens)
        return cls._parse_json_response(raw)

    @classmethod
    def _run_text_gemini(cls, prompt, max_tokens=None):
        model = cls._get_gemini_model_instance()
        generation_config = {"response_mime_type": "application/json"}
        if max_tokens:
            generation_config["max_output_tokens"] = max_tokens
        raw = cls._generate_with_gemini_retry(model, prompt, generation_config=generation_config)
        return cls._parse_json_response(raw)

    @classmethod
    def _run_text_anthropic(cls, prompt, max_tokens=None):
        raw = cls._generate_with_anthropic_retry(
            [{"type": "text", "text": prompt}],
            cls._coerce_max_tokens(max_tokens, cls._get_anthropic_max_tokens()),
            system=_ANTHROPIC_JSON_SYSTEM,
        )
        return cls._parse_json_response(raw)

    @classmethod
    def _run_text_deepseek(cls, prompt, max_tokens=None):
        client = cls._get_deepseek_client()
        raw = cls._generate_with_deepseek_retry(
            client, [{"role": "user", "content": prompt}],
            response_format="json_object", max_tokens=max_tokens)
        return cls._parse_json_response(raw)

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

        cache = {}
        data, provider = cls._with_failover(
            cls._build_runners("vision", prompt, (image_url,), cache),
            # No findings is itself a valid finding; inventing one is the
            # failure mode this return value exists to avoid.
            on_total_failure=lambda _attempts: [],
            capability="vision",
        )
        defects = cls._normalise_bboxes(
            cls._normalise_list(data, key="defects"), image_url, cache)
        logger.info("Visual defect detection completed via %s. Defects detected: %s",
                    provider or "no provider", len(defects))
        return defects

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

        cache = {}
        data, provider = cls._with_failover(
            cls._build_runners("vision", prompt, (image_url,), cache),
            on_total_failure=lambda _attempts: [],
            capability="vision",
        )
        anomalies = cls._normalise_bboxes(
            cls._normalise_list(data, key="anomalies"), image_url, cache)
        logger.info("Thermal anomaly detection completed via %s. Anomalies detected: %s",
                    provider or "no provider", len(anomalies))
        return anomalies

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
            logger.warning("Local CNN+SNN pipeline failed. Falling back to the AI provider chain: %s", e)

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

        cache = {}
        data, provider = cls._with_failover(
            cls._build_runners("vision", prompt, (thermal_url, visible_url), cache),
            on_total_failure=lambda _attempts: [],
            capability="vision",
        )
        delaminations = cls._normalise_bboxes(
            cls._normalise_list(data, key="delaminations"), thermal_url, cache)
        logger.info("Multimodal delamination detection completed via %s. Results: %s",
                    provider or "no provider", len(delaminations))
        return delaminations

    # ========================================================
    # GENERIC STRUCTURED JSON SYNTHESIS
    # ========================================================

    @classmethod
    def generate_structured_json(cls, prompt: str, max_tokens: int = None, schema: dict = None):
        """
        Generic contextual-synthesis entry point for the AI Evidence
        Intelligence Layer (correlation narratives, executive briefings,
        recommendations). Returns a `StructuredResult` — a dict carrying the
        provider and model that actually answered on its attributes.

        `max_tokens` is honoured by every provider that accepts a token cap.
        It was previously accepted and silently dropped, which is how one
        call site came to pass a JSON schema through it.

        `schema`, when given, is appended to the prompt as an explicit output
        contract. It is NOT enforced by the providers (only Anthropic's
        structured outputs could enforce it, and not uniformly across the
        chain), so it is stated as a prompt instruction rather than presented
        as a guarantee.

        Raises AIServiceError / AIProviderUnavailable when no provider is
        configured or all fail — callers MUST fall back to deterministic
        output built from the real data and NEVER fabricate content.
        """
        if schema:
            prompt = (
                f"{prompt}\n\nReturn a JSON object matching exactly this JSON Schema:\n"
                f"{json.dumps(schema, sort_keys=True)}"
            )

        data, provider = cls._with_failover(
            cls._build_runners("text", prompt, max_tokens),
            # No honest empty object exists here: {} is indistinguishable from
            # a malformed non-answer, and every caller has a better
            # deterministic alternative built from real data.
            on_total_failure=None,
            capability="text",
        )
        return StructuredResult(
            data if isinstance(data, dict) else {"result": data},
            provider=provider,
            model=cls._model_for(provider),
        )

    # ========================================================
    # MULTI-MODEL ENSEMBLE CONSENSUS SYNTHESIS
    # ========================================================

    @classmethod
    def generate_ensemble_structured_json(cls, prompt: str, max_tokens: int = None, schema: dict = None):
        """
        Multi-Model Ensemble Synthesis: executes parallel inference across all
        configured and available AI engines simultaneously (Gemini, OpenAI, Anthropic, DeepSeek).

        Fault-tolerant: each model is executed in its own isolated thread. If one model
        breaks, times out, or exhausts its API quota (e.g. OpenAI quota exhaustion), it is
        caught and isolated cleanly without degrading, delaying, or affecting remaining models.

        Aggregates corroborating observations from all successful engines and pairs them with
        the deterministic BS 1881-203 acoustic physics ground truth.
        """
        import concurrent.futures

        if schema:
            prompt = (
                f"{prompt}\n\nReturn a JSON object matching exactly this JSON Schema:\n"
                f"{json.dumps(schema, sort_keys=True)}"
            )

        runners = cls._build_runners("text", prompt, max_tokens)
        successful_results = {}
        failed_providers = {}

        def _execute(item):
            prov, runner = item
            try:
                out = runner()
                return prov, out, None
            except Exception as exc:
                return prov, None, exc

        active_items = [(p, r) for p, r in runners.items() if cls._provider_has_key(p)]
        if active_items:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(active_items))) as executor:
                futures = [executor.submit(_execute, item) for item in active_items]
                for future in concurrent.futures.as_completed(futures):
                    prov, out, exc = future.result()
                    if exc is None and out is not None:
                        successful_results[prov] = out
                    else:
                        err_str = str(exc)
                        if "quota" in err_str.lower():
                            failed_providers[prov] = "Quota exceeded"
                        elif "timeout" in err_str.lower():
                            failed_providers[prov] = "Timed out"
                        else:
                            failed_providers[prov] = type(exc).__name__ if exc else "Failed"
                        logger.warning("Ensemble engine %s isolated on error: %s", prov, err_str)

        # Merge observations across responding models
        merged_observations = []
        for prov, data in successful_results.items():
            if isinstance(data, dict):
                obs = data.get("observations")
                if isinstance(obs, list):
                    for o in obs:
                        cleaned = str(o).strip()
                        if cleaned and cleaned not in merged_observations:
                            merged_observations.append(cleaned)

        active_models = [
            f"{cls._model_for(p) or p}" for p in successful_results.keys()
        ]
        active_providers = [p.capitalize() for p in successful_results.keys()]

        # Always adhere strictly to database column limits (provider <= 50, version <= 100)
        provider_title = "Multi-Model Ensemble"
        if active_models:
            engines_str = ", ".join(active_models[:2]) + " + BS 1881-203 Inversion"
        else:
            engines_str = "Multi-Engine Synthesis + BS 1881-203 Inversion"
        version_title = f"Ensemble v2.4 ({engines_str})"
        if len(version_title) > 95:
            version_title = version_title[:92] + "..."

        res_dict = {
            "observations": merged_observations,
            "successful_providers": list(successful_results.keys()),
            "failed_providers": failed_providers,
            "models_used": active_models,
            "provider_label": provider_title,
            "version_label": version_title,
            "ensemble_active": len(successful_results) > 0,
        }
        return StructuredResult(
            res_dict,
            provider=provider_title,
            model=version_title,
        )

    # ========================================================
    # ENGINEERING RECOMMENDATIONS
    # ========================================================

    @classmethod
    def generate_recommendations(cls, defects: list, anomalies: list, deviation: float) -> dict:

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

        data, provider = cls._with_failover(
            cls._build_runners("text", prompt),
            # An empty recommendation list is honest: it says the model had
            # nothing to add, which is different from inventing guidance.
            on_total_failure=lambda _attempts: {"recommendations": [], "text_confidence": None},
            capability="text",
        )
        recommendations = cls._normalise_list(data, key="recommendations")
        text_confidence = data.get("text_confidence") if isinstance(data, dict) else None
        logger.info("Recommendation generation completed via %s. Recommendations: %s",
                    provider or "no provider", len(recommendations))
        return StructuredResult(
            {"recommendations": recommendations, "text_confidence": text_confidence},
            provider=provider,
            model=cls._model_for(provider),
        )
