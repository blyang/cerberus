"""Vision-LLM client. Auto-classifies visual checks (A2, C7, E4 overlap).

Determinism strategy (per research):
  - temperature 0.0, top_p 0.1, single candidate
  - structured JSON output via responseSchema (Gemini) / json_schema (OpenAI-compatible)
  - 2-shot anchored prompts with explicit positive/negative class definitions
  - send images at native resolution (no aggressive downsampling)

Fallback path: if primary returns confidence < threshold OR errors, retry on the secondary
(Qwen-VL via DashScope's OpenAI-compatible Beijing endpoint). If that also fails or gives a
low-confidence verdict, return a Manual result with the screenshots embedded for the operator.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
from typing import Any, Sequence

from .config import VisionConfig, VisionProviderConfig

log = logging.getLogger(__name__)


@dataclasses.dataclass
class VisionVerdict:
    verdict: str            # "pass" | "fail"
    confidence: float       # 0..1
    reason: str             # ≤ 200 chars
    provider: str           # "gemini" | "qwen" | "fallback-to-manual"
    model: str              # actual model id used
    raw: dict[str, Any] | None = None
    error: str | None = None


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reason"],
}


def _api_key(provider_cfg: VisionProviderConfig) -> str | None:
    return os.environ.get(provider_cfg.api_key_env, "").strip() or None


async def classify(
    cfg: VisionConfig | None,
    *,
    system_prompt: str,
    user_prompt: str,
    images: Sequence[bytes],     # PNG/JPEG bytes
    image_mime: str = "image/png",
) -> VisionVerdict:
    """Run primary; fall back to secondary on error or low confidence. Always returns a verdict
    (verdict='fail' only when the model says fail; provider='fallback-to-manual' if both providers
    failed/were unconfigured)."""
    if not cfg or not cfg.enabled:
        return VisionVerdict("pass", 0.0, "vision disabled in site_config", "fallback-to-manual", "", error="vision disabled")

    # Primary attempt.
    primary = cfg.primary
    if primary and _api_key(primary):
        result = await _call(primary, system_prompt, user_prompt, images, image_mime)
        if result.error is None and result.confidence >= cfg.confidence_threshold:
            return result
        log.info("vision: primary %s low/error (conf=%s err=%s), trying fallback", primary.model, result.confidence, result.error)
        primary_result = result
    else:
        primary_result = VisionVerdict("pass", 0.0, "primary not configured", "fallback-to-manual",
                                       primary.model if primary else "", error="missing api key")

    fallback = cfg.fallback
    if fallback and _api_key(fallback):
        fb_result = await _call(fallback, system_prompt, user_prompt, images, image_mime)
        if fb_result.error is None and fb_result.confidence >= cfg.confidence_threshold:
            return fb_result
        log.info("vision: fallback %s low/error (conf=%s err=%s)", fallback.model, fb_result.confidence, fb_result.error)

    # Both attempts left us without a confident verdict — surface the manual sentinel so the
    # caller routes to Status.MANUAL rather than silently emitting a low-confidence Pass/Fail.
    return VisionVerdict(
        "pass", 0.0, "both providers below confidence threshold or failed — falling back to manual",
        "fallback-to-manual", "",
        error=primary_result.error or "below confidence threshold",
    )


async def _call(
    pcfg: VisionProviderConfig,
    system_prompt: str,
    user_prompt: str,
    images: Sequence[bytes],
    image_mime: str,
) -> VisionVerdict:
    if pcfg.provider == "gemini":
        return await _call_gemini(pcfg, system_prompt, user_prompt, images, image_mime)
    if pcfg.provider == "qwen":
        return await _call_qwen(pcfg, system_prompt, user_prompt, images, image_mime)
    return VisionVerdict("pass", 0.0, f"unknown provider {pcfg.provider!r}", pcfg.provider, pcfg.model,
                         error=f"unknown provider {pcfg.provider!r}")


# --- Gemini ---

async def _call_gemini(
    pcfg: VisionProviderConfig,
    system_prompt: str,
    user_prompt: str,
    images: Sequence[bytes],
    image_mime: str,
) -> VisionVerdict:
    api_key = _api_key(pcfg)
    if not api_key:
        return VisionVerdict("pass", 0.0, "no Gemini key", "gemini", pcfg.model, error="no api key")
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        return VisionVerdict("pass", 0.0, "google-genai not installed", "gemini", pcfg.model, error=f"{exc}")

    client = genai.Client(api_key=api_key)
    parts: list[Any] = [user_prompt]
    for blob in images:
        parts.append(types.Part.from_bytes(data=blob, mime_type=image_mime))

    cfg_obj = types.GenerateContentConfig(
        temperature=0.0,
        top_p=0.1,
        candidate_count=1,
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
        system_instruction=system_prompt,
    )
    try:
        # google-genai is sync — run in a thread to keep the event loop free.
        import asyncio
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=pcfg.model,
            contents=parts,
            config=cfg_obj,
        )
    except Exception as exc:
        log.warning("gemini call failed: %s", exc)
        return VisionVerdict("pass", 0.0, "gemini api error", "gemini", pcfg.model, error=f"{type(exc).__name__}: {exc}")

    text = (getattr(resp, "text", "") or "").strip()
    parsed = _parse_verdict_json(text)
    if parsed is None:
        return VisionVerdict("pass", 0.0, "gemini malformed response", "gemini", pcfg.model,
                             error=f"could not parse: {text[:200]!r}", raw={"text": text})
    return VisionVerdict(
        verdict=parsed["verdict"],
        confidence=float(parsed.get("confidence", 0.0)),
        reason=str(parsed.get("reason", ""))[:300],
        provider="gemini",
        model=pcfg.model,
        raw={"text": text},
    )


# --- Qwen via DashScope OpenAI-compatible endpoint ---

async def _call_qwen(
    pcfg: VisionProviderConfig,
    system_prompt: str,
    user_prompt: str,
    images: Sequence[bytes],
    image_mime: str,
) -> VisionVerdict:
    api_key = _api_key(pcfg)
    if not api_key:
        return VisionVerdict("pass", 0.0, "no DashScope key", "qwen", pcfg.model, error="no api key")
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        return VisionVerdict("pass", 0.0, "openai sdk not installed", "qwen", pcfg.model, error=f"{exc}")

    base_url = pcfg.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    content_parts: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for blob in images:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{base64.b64encode(blob).decode('ascii')}"},
        })

    try:
        resp = await client.chat.completions.create(
            model=pcfg.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_parts},
            ],
            temperature=0.0,
            top_p=0.1,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        log.warning("qwen call failed: %s", exc)
        return VisionVerdict("pass", 0.0, "qwen api error", "qwen", pcfg.model, error=f"{type(exc).__name__}: {exc}")

    text = (resp.choices[0].message.content or "").strip()
    parsed = _parse_verdict_json(text)
    if parsed is None:
        return VisionVerdict("pass", 0.0, "qwen malformed response", "qwen", pcfg.model,
                             error=f"could not parse: {text[:200]!r}", raw={"text": text})
    return VisionVerdict(
        verdict=parsed["verdict"],
        confidence=float(parsed.get("confidence", 0.0)),
        reason=str(parsed.get("reason", ""))[:300],
        provider="qwen",
        model=pcfg.model,
        raw={"text": text},
    )


def _parse_verdict_json(text: str) -> dict[str, Any] | None:
    """Tolerantly extract a verdict JSON object from a model response."""
    if not text:
        return None
    # Strip code-fence wrappers if present.
    t = text.strip()
    if t.startswith("```"):
        t = t.lstrip("`")
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        # Try to find the first JSON object substring.
        start = t.find("{")
        end = t.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            obj = json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get("verdict") not in ("pass", "fail"):
        return None
    return obj
