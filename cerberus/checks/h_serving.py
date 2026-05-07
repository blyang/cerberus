"""H — Serving Integrity & Search Policy Hygiene."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from inscriptis import get_text as html_to_text

from .. import browser, gsc
from ._utils import (
    UA_CHROME_DESKTOP,
    UA_GOOGLEBOT,
    fetch,
    primary_content_text,
)
from .base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    SubStep,
    register,
)

H3_FALLBACK_INSTRUCTION = (
    "Optional: run GSC URL Inspection → Test Live URL on this page. Compare the rendered HTML to "
    "the headless browser render shown in the check details. Subject to GSC daily quota."
)


def _extract_main(html: str) -> str:
    m = re.search(r"<main\b[^>]*>(.*?)</main>", html, re.I | re.S)
    if m:
        return m.group(1)
    m = re.search(r"<article\b[^>]*>(.*?)</article>", html, re.I | re.S)
    if m:
        return m.group(1)
    return html


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:50_000], b[:50_000]).ratio()


def _normalize_for_compare(s: str) -> str:
    """Casefold + collapse whitespace + drop nbsp. Keeps SequenceMatcher from being fooled
    by CSS text-transform (rendered innerText is uppercase; raw HTML is mixed-case)."""
    return " ".join(s.replace("\xa0", " ").split()).casefold()


@register("H1", section="H", severity=Severity.BLOCKING,
          title="No cloaking or materially different bot-vs-user content", estimate_ms=8_000)
async def h1(ctx: CheckContext) -> CheckResult:
    bot = await fetch(ctx, user_agent=UA_GOOGLEBOT, key_suffix="googlebot")
    user = await fetch(ctx, user_agent=UA_CHROME_DESKTOP)
    if bot.status_code != 200 or user.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"bot={bot.status_code} user={user.status_code}")
    bot_text = primary_content_text(bot.text)
    user_text = primary_content_text(user.text)
    if not bot_text or not user_text:
        return CheckResult(Status.FAIL, summary="Empty <main>/<article> in one variant.",
                           details={"bot_text_len": len(bot_text), "user_text_len": len(user_text)})
    sim = _similarity(bot_text, user_text)
    sub = [
        SubStep("bot.html has primary content", Status.PASS if bot_text else Status.FAIL,
                detail=f"len: {len(bot_text)}"),
        SubStep("user.html has primary content", Status.PASS if user_text else Status.FAIL,
                detail=f"len: {len(user_text)}"),
        SubStep(
            "Primary content overlaps ≥ 90%",
            Status.PASS if sim >= 0.90 else (Status.NEEDS_REVIEW if sim >= 0.75 else Status.FAIL),
            detail=f"similarity: {sim:.3f}",
        ),
    ]
    return CheckResult.from_substeps("Bot vs. user cloaking diff.", sub)


@register("H2", section="H", severity=Severity.BLOCKING,
          title="No hidden content in rendered HTML", estimate_ms=20_000)
async def h2(ctx: CheckContext) -> CheckResult:
    raw = await fetch(ctx)
    rendered = await browser.render_page(ctx.url)
    if rendered.get("error"):
        return CheckResult(Status.FAIL, summary=f"render failed: {rendered['error']}")
    # Match scopes: rendered comes from Playwright's `main, article || body` innerText,
    # so reduce raw HTML to the same region before extracting text. Without this, raw includes
    # nav/footer/scripts and similarity tanks even on identical primary content.
    raw_text = html_to_text(_extract_main(raw.text or ""))
    rendered_text = rendered.get("text") or html_to_text(rendered.get("html") or "")
    raw_norm = _normalize_for_compare(raw_text)
    rendered_norm = _normalize_for_compare(rendered_text)
    sim = _similarity(raw_norm, rendered_norm)
    # CSS/inline tricks check.
    suspicious_html_patterns = [
        r"display\s*:\s*none",
        r"visibility\s*:\s*hidden",
        r"text-indent\s*:\s*-?\d{4,}",
        r"color\s*:\s*#?(?:fff|ffffff|white)\b",
    ]
    flagged: list[str] = []
    for pat in suspicious_html_patterns:
        if re.search(pat, raw.text or "", re.I):
            flagged.append(pat)
    sub = [
        SubStep(
            "raw vs. rendered text similarity",
            Status.PASS if sim >= 0.85 else (Status.NEEDS_REVIEW if sim >= 0.7 else Status.FAIL),
            detail=f"similarity: {sim:.3f}; raw_len={len(raw_norm)}; rendered_len={len(rendered_norm)}",
        ),
        SubStep(
            "no CSS hiding tricks in raw HTML",
            Status.PASS if not flagged else Status.NEEDS_REVIEW,
            detail=f"flagged patterns: {flagged}" if flagged else "clean",
        ),
    ]
    return CheckResult.from_substeps("Hidden-content scan.", sub)


@register("H3", section="H", severity=Severity.BLOCKING,
          title="Googlebot rendering matches browser rendering", estimate_ms=30_000)
async def h3(ctx: CheckContext) -> CheckResult:
    creds_path = ctx.site_config.gsc_credentials_path
    inspection = await gsc.inspect_url(ctx.url, creds_path)
    if inspection.get("error"):
        return CheckResult(
            Status.MANUAL,
            summary=f"GSC unavailable: {inspection['error']}. Falling back to manual instruction.",
            instruction=H3_FALLBACK_INSTRUCTION,
            details={"gsc_error": inspection["error"]},
        )
    raw_payload = inspection.get("raw") or {}
    inspect_result = (raw_payload.get("inspectionResult") or {})
    index_status = (inspect_result.get("indexStatusResult") or {}).get("verdict")
    rendered_html = ((inspect_result.get("liveInspectionResult") or {}).get("renderedPageInfo") or {}).get("html")
    if not rendered_html:
        return CheckResult(
            Status.MANUAL,
            summary="GSC returned no rendered HTML (live inspection may need to be re-run interactively).",
            instruction=H3_FALLBACK_INSTRUCTION,
            details={"index_status": index_status},
        )
    browser_render = await browser.render_page(ctx.url)
    if browser_render.get("error"):
        return CheckResult(Status.FAIL, summary=f"headless render failed: {browser_render['error']}")
    gsc_main = primary_content_text(rendered_html)
    browser_main = primary_content_text(browser_render["html"])
    sim = _similarity(gsc_main, browser_main)
    # Per brief R2.1, H3 belongs in the "Needs Review" bucket — partly automated, requires a final
    # human glance. We surface the diff metric as diagnostic detail but do not auto-pass/fail.
    return CheckResult(
        Status.NEEDS_REVIEW,
        summary=f"Googlebot↔browser render diff (similarity {sim:.3f}). Final review required.",
        instruction=H3_FALLBACK_INSTRUCTION,
        details={
            "similarity": sim,
            "gsc_len": len(gsc_main),
            "browser_len": len(browser_main),
            "index_status": (inspect_result.get("indexStatusResult") or {}).get("verdict"),
        },
    )
