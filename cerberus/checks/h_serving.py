"""H — Serving Integrity & Search Policy Hygiene."""
from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher

from inscriptis import get_text as html_to_text

from .. import browser
from ._utils import (
    UA_CHROME_DESKTOP,
    UA_GOOGLEBOT,
    fetch,
    primary_content_text,
)  # H1 still uses the static constants for raw-HTTP cloaking diff (no browser involved).
from .base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    SubStep,
    register,
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
    # Fallback: if scoping yields empty text (e.g. <main> exists but is an empty SSR placeholder
    # while real content lives elsewhere in <body>), use the full document so we don't compare
    # an empty raw against a full rendered body.
    raw_main = html_to_text(_extract_main(raw.text or ""))
    raw_text = raw_main if raw_main.strip() else html_to_text(raw.text or "")
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


H3_INSTRUCTION = (
    "Diagnostic only: this is a local Chromium simulation of Googlebot, not Google's actual "
    "Web Rendering Service (WRS). Substantial differences between the Googlebot-UA render and "
    "the browser render are worth investigating; identical results don't prove WRS will agree. "
    "For ground truth, paste the URL into GSC URL Inspection → Test Live URL → View Tested Page."
)


@register("H3", section="H", severity=Severity.BLOCKING,
          title="Googlebot rendering matches browser rendering", estimate_ms=30_000)
async def h3(ctx: CheckContext) -> CheckResult:
    # Pin both UAs to the live browser Chrome version — version skew between bot and user
    # would otherwise show up as content diffs on sites with version-gated banners or shims.
    bot_ua = await browser.googlebot_wrs_ua()
    user_ua = await browser.chrome_desktop_ua()
    bot_render, user_render = await asyncio.gather(
        browser.render_page(ctx.url, user_agent=bot_ua),
        browser.render_page(ctx.url, user_agent=user_ua),
    )
    if bot_render.get("error"):
        return CheckResult(Status.FAIL, summary=f"Googlebot-UA render failed: {bot_render['error']}")
    if user_render.get("error"):
        return CheckResult(Status.FAIL, summary=f"user-UA render failed: {user_render['error']}")
    bot_status = bot_render.get("status") or 0
    user_status = user_render.get("status") or 0
    bot_main = primary_content_text(bot_render.get("html") or "")
    user_main = primary_content_text(user_render.get("html") or "")
    bot_norm = _normalize_for_compare(bot_main)
    user_norm = _normalize_for_compare(user_main)
    sim = _similarity(bot_norm, user_norm)
    channel = browser.get_channel_used()
    # A non-2xx response to the Googlebot UA (CDN block, anti-bot challenge, captcha) would
    # otherwise score similarity against the error page body — flag it explicitly so the
    # operator sees the bot was blocked rather than chasing a false content-mismatch verdict.
    sub = [
        SubStep("Googlebot-UA render returned 2xx",
                Status.PASS if 200 <= bot_status < 300 else Status.FAIL,
                detail=f"HTTP {bot_status}"),
        SubStep("user-UA render returned 2xx",
                Status.PASS if 200 <= user_status < 300 else Status.FAIL,
                detail=f"HTTP {user_status}"),
        SubStep("Googlebot-UA render returned primary content",
                Status.PASS if bot_main else Status.FAIL,
                detail=f"len: {len(bot_main)}; channel: {channel}"),
        SubStep("user-UA render returned primary content",
                Status.PASS if user_main else Status.FAIL,
                detail=f"len: {len(user_main)}"),
        SubStep(
            "Googlebot-UA vs user-UA primary content similarity ≥ 0.90",
            Status.PASS if sim >= 0.90 else (Status.NEEDS_REVIEW if sim >= 0.75 else Status.FAIL),
            detail=f"similarity: {sim:.3f}",
        ),
    ]
    result = CheckResult.from_substeps(
        f"Googlebot-UA↔user-UA render diff (similarity {sim:.3f}).", sub,
    )
    # Cap clean runs at NEEDS_REVIEW so the WRS-vs-simulation gap is honest and so the
    # operator-override UI path (Manual/NEEDS_REVIEW only — see report.py) stays open.
    if result.status == Status.PASS:
        result.status = Status.NEEDS_REVIEW
    result.details.update({
        "similarity": sim,
        "bot_status": bot_status,
        "user_status": user_status,
        "bot_len": len(bot_main),
        "user_len": len(user_main),
        "browser_channel": channel,
        "googlebot_ua": bot_ua,
    })
    result.instruction = H3_INSTRUCTION
    return result
