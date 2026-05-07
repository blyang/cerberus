"""A — Rendering & Status."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .. import browser, screenshots, vision
from ._utils import all_text_lines, fetch, fetch_url
from .base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    SubStep,
    register,
)

def _line_present(line_cf: str, raw_cf: str) -> bool:
    """Word-boundary aware containment check for A1.

    Long lines (>=10 chars) keep substring containment — collisions are vanishingly
    unlikely. Short lines use lookarounds so e.g. `EN` does not match inside `GENERAL`.
    Lookarounds are `(?<!\\w)` / `(?!\\w)` (not `\\b`) so they also work for tokens
    starting/ending with non-word chars like `$99` or `→`.
    """
    if len(line_cf) < 2:
        return True
    if not re.search(r"\w", line_cf):
        return True
    if len(line_cf) >= 10:
        return line_cf in raw_cf
    pattern = rf"(?<!\w){re.escape(line_cf)}(?!\w)"
    return re.search(pattern, raw_cf) is not None


A2_INSTRUCTION = (
    "Open the page in Chrome DevTools Performance panel. Enable Screenshots and set Network throttling "
    "to Slow 3G. Record a fresh page load in incognito. Step through the frame-by-frame screenshots. "
    "Confirm the first text-bearing frame shows the same primary content as the final frame."
)

A2_SYSTEM_PROMPT = (
    "You compare two screenshots of a web page taken during a Slow-3G page load. "
    "The FIRST screenshot is the earliest frame where text content is visible. "
    "The SECOND screenshot is the final settled state of the page. "
    "You must answer: does the primary visible content materially differ between the two? "
    "Pass means: hero/headline/primary CTA/main image are essentially the same in both frames "
    "(minor differences in image lazy-loading, ad slot placeholders, or chat-widget appearance are OK). "
    "Fail means: the first text-bearing frame shows materially different headline/hero/CTA than the final frame "
    "— this hurts perceived performance and can confuse Googlebot's snapshotting. "
    "Return JSON: {verdict: 'pass'|'fail', confidence: 0..1, reason: 'short reason ≤200 chars'}. "
    "Use confidence ≥ 0.8 only when you're certain; below that signals the operator should re-check."
)


@register("A1", section="A", severity=Severity.BLOCKING,
          title="All primary visible text is present in raw HTML", estimate_ms=20_000)
async def a1(ctx: CheckContext) -> CheckResult:
    raw = await fetch(ctx)
    if raw.status_code != 200 or not raw.text:
        return CheckResult(Status.FAIL,
                           summary=f"Raw HTML fetch failed: status={raw.status_code} {raw.error or ''}".strip())

    rendered = await browser.render_page(ctx.url, viewport={"width": 1280, "height": 1080})
    if rendered.get("error") or not rendered.get("text"):
        return CheckResult(Status.FAIL,
                           summary=f"Headless render failed: {rendered.get('error') or 'empty content'}",
                           details={"render_error": rendered.get("error")})

    rendered_lines = all_text_lines(rendered["text"], min_len=2)
    if not rendered_lines:
        return CheckResult(Status.FAIL,
                           summary="Rendered <main>/<article> is empty (no visible content)",
                           details={"rendered_text_sample": rendered["text"][:500]})

    soup = BeautifulSoup(raw.text, "lxml")
    for tag in soup(["script", "style", "template", "noscript"]):
        tag.decompose()
    raw_visible_text = soup.get_text(" ", strip=False).replace("\xa0", " ")
    raw_visible_collapsed = " ".join(raw_visible_text.split())
    raw_visible_collapsed_cf = raw_visible_collapsed.casefold()

    missing: list[str] = []
    for line in rendered_lines:
        collapsed = " ".join(line.replace("\xa0", " ").split())
        # CSS text-transform (uppercase/lowercase) makes innerText diverge from raw HTML;
        # entities are already decoded by BeautifulSoup's get_text.
        if _line_present(collapsed.casefold(), raw_visible_collapsed_cf):
            continue
        missing.append(line)

    if not missing:
        return CheckResult(Status.PASS,
                           summary=f"All {len(rendered_lines)} rendered lines present in raw HTML.",
                           details={"checked_lines": len(rendered_lines)})

    return CheckResult(
        Status.FAIL,
        summary=f"{len(missing)} of {len(rendered_lines)} rendered lines missing from raw HTML.",
        details={
            "missing_lines": missing[:20],
            "missing_count": len(missing),
            "checked_lines": len(rendered_lines),
            "advice": "Page likely renders primary content client-side. Add SSR for SEO-critical text.",
        },
    )


@register("A2", section="A", severity=Severity.BLOCKING,
          title="No flash of materially different content on load", estimate_ms=15_000)
async def a2(ctx: CheckContext) -> CheckResult:
    """Vision-classified. Captures Slow-3G load frames; compares first text-bearing vs final.

    Falls back to Manual when vision is disabled, both providers error, or no text-bearing frame
    appears (page failed to render text within the capture window).
    """
    vcfg = ctx.site_config.vision if ctx.site_config else None
    if not vcfg or not vcfg.enabled:
        return CheckResult(Status.MANUAL, summary="Manual: visual flash check (vision disabled).",
                           instruction=A2_INSTRUCTION)

    capture = await browser.capture_load_frames(ctx.url, throttle_slow_3g=True)
    if capture.get("error") or not capture.get("frames"):
        return CheckResult(Status.MANUAL,
                           summary=f"Couldn't capture load frames ({capture.get('error', 'no frames')}); falling back to manual.",
                           instruction=A2_INSTRUCTION,
                           details={"capture_error": capture.get("error")})

    frames = capture["frames"]
    # First text-bearing: the first frame whose body innerText length crosses 200 chars.
    first_text_frame = next((f for f in frames if f["text_len"] >= 200 and f["png"]), None)
    final_frame = next((f for f in reversed(frames) if f["png"]), None)
    if not first_text_frame or not final_frame:
        return CheckResult(Status.MANUAL,
                           summary="No text-bearing frame appeared within the 9s capture window; manual check required.",
                           instruction=A2_INSTRUCTION,
                           details={"frame_text_lens": [(f["t_ms"], f["text_len"]) for f in frames]})

    if first_text_frame["t_ms"] == final_frame["t_ms"]:
        return CheckResult(Status.PASS,
                           summary="First text-bearing frame is the final frame — no flash possible.",
                           details={"text_appeared_at_ms": first_text_frame["t_ms"]})

    # Persist frames for the report.
    if ctx.run_id:
        screenshots.save_png(ctx.run_id, "A2_first_text", first_text_frame["png"])
        screenshots.save_png(ctx.run_id, "A2_final", final_frame["png"])

    user_prompt = (
        f"FIRST screenshot (first text-bearing frame at t={first_text_frame['t_ms']}ms during Slow-3G load). "
        f"SECOND screenshot (final settled frame at t={final_frame['t_ms']}ms). "
        "Compare their primary visible content. Output JSON only."
    )
    verdict = await vision.classify(
        vcfg,
        system_prompt=A2_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        images=[first_text_frame["png"], final_frame["png"]],
        image_mime="image/png",
    )

    saved_screenshots = ["A2_first_text", "A2_final"] if ctx.run_id else []

    if verdict.error and verdict.provider == "fallback-to-manual":
        return CheckResult(
            Status.MANUAL,
            summary=f"Vision unavailable ({verdict.error}); manual check required.",
            instruction=A2_INSTRUCTION,
            details={"vision_error": verdict.error, "screenshots": saved_screenshots},
        )

    status = Status.PASS if verdict.verdict == "pass" else Status.FAIL
    summary = (
        f"Vision verdict: {verdict.verdict} (confidence {verdict.confidence:.2f} "
        f"via {verdict.provider}/{verdict.model}). {verdict.reason}"
    )
    return CheckResult(
        status,
        summary=summary,
        details={
            "verdict": verdict.verdict,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "vision_provider": verdict.provider,
            "vision_model": verdict.model,
            "first_text_frame_ms": first_text_frame["t_ms"],
            "final_frame_ms": final_frame["t_ms"],
            "screenshots": saved_screenshots,
        },
    )


@register("A3", section="A", severity=Severity.BLOCKING,
          title="Page returns the correct HTTP status", estimate_ms=2_000)
async def a3(ctx: CheckContext) -> CheckResult:
    r = await fetch_url(ctx, ctx.url, follow_redirects=False)
    if r.error:
        return CheckResult(Status.FAIL, summary=f"Fetch error: {r.error}")
    if r.status_code == 200:
        return CheckResult(Status.PASS, summary="Returned 200.", details={"status": 200})
    if r.status_code in (404, 410):
        return CheckResult(Status.PASS, summary=f"Returned {r.status_code} (acceptable for nonexistent URL)",
                           details={"status": r.status_code})
    if r.status_code in (301, 302, 307, 308):
        loc = r.headers.get("location", "")
        return CheckResult(Status.FAIL,
                           summary=f"Returned {r.status_code} → {loc} (redirect, not a self-200).",
                           details={"status": r.status_code, "location": loc})
    return CheckResult(Status.FAIL, summary=f"Returned {r.status_code} (expected 200).",
                       details={"status": r.status_code})


@register("A4", section="A", severity=Severity.BLOCKING,
          title="URL does not rely on fragments for unique indexable content", estimate_ms=2_000)
async def a4(ctx: CheckContext) -> CheckResult:
    p = urlparse(ctx.url)
    if p.fragment:
        return CheckResult(Status.FAIL,
                           summary=f"URL contains fragment '#{p.fragment}' — content must be addressable without it.",
                           details={"fragment": p.fragment})
    raw = await fetch(ctx)
    indicators = ["window.location.hash", "router", "hash"]
    matches = [s for s in indicators if s in raw.text.lower()]
    sub_steps = [
        SubStep("URL has no fragment", Status.PASS, detail=f"path: {p.path}"),
        SubStep("No obvious hash-routing patterns", Status.PASS if not matches else Status.NEEDS_REVIEW,
                detail=f"raw matches: {matches}" if matches else "no matches"),
    ]
    return CheckResult.from_substeps("URL fragment handling.", sub_steps)
