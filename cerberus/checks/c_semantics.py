"""C — HTML Semantics & On-Page Extraction."""
from __future__ import annotations

import asyncio

from .. import browser, screenshots, vision
from ._utils import fetch
from .base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    SubStep,
    register,
)

C7_INSTRUCTION = (
    "Load the page in a fresh incognito window at 375px width, then again at 1440px width. "
    "Confirm no modal, overlay, newsletter gate, cookie wall, or app-install prompt obscures primary "
    "content on first paint. Cookie consent banners are acceptable only if limited to legal-minimum GDPR/CCPA."
)


@register("C1", section="C", severity=Severity.BLOCKING,
          title="Heading structure is present and logical", estimate_ms=2_000)
async def c1(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    h1s = r.soup.find_all("h1")
    sub: list[SubStep] = []
    sub.append(SubStep(
        "Exactly one <h1>",
        Status.PASS if len(h1s) == 1 else Status.FAIL,
        detail=f"found {len(h1s)} h1 element(s)",
    ))
    h1_text = (h1s[0].get_text(strip=True) if h1s else "")
    sub.append(SubStep(
        "<h1> non-empty",
        Status.PASS if h1_text else Status.FAIL,
        detail=f"text: {h1_text!r}" if h1s else "no h1",
    ))
    # Heading skip check: collect levels in order.
    levels: list[int] = []
    for tag in r.soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        try:
            levels.append(int(tag.name[1]))
        except (ValueError, IndexError):
            continue
    skips: list[str] = []
    for i in range(1, len(levels)):
        if levels[i] - levels[i - 1] > 1:
            skips.append(f"h{levels[i-1]}→h{levels[i]} at index {i}")
    sub.append(SubStep(
        "No heading-level skips",
        Status.PASS if not skips else Status.FAIL,
        detail=f"sequence: {levels}" + (f"; skips: {skips}" if skips else ""),
    ))
    return CheckResult.from_substeps("Heading structure.", sub)


@register("C4", section="C", severity=Severity.RECOMMENDED,
          title="All form inputs have associated <label> elements", estimate_ms=2_000)
async def c4(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    inputs = r.soup.find_all(["input", "textarea", "select"])
    if not inputs:
        return CheckResult(Status.NA, summary="No form inputs on page.")
    unmatched: list[str] = []
    for inp in inputs:
        if (inp.get("type") or "").lower() in ("hidden", "submit", "button", "image", "reset"):
            continue
        if inp.get("aria-label") or inp.get("aria-labelledby"):
            continue
        # Wrapped <label>?
        if any(parent.name == "label" for parent in inp.parents):
            continue
        inp_id = inp.get("id")
        if inp_id and r.soup.find("label", attrs={"for": inp_id}):
            continue
        unmatched.append(str(inp)[:120])
    if unmatched:
        return CheckResult(Status.FAIL,
                           summary=f"{len(unmatched)} unlabelled input(s).",
                           details={"unmatched_sample": unmatched[:5]})
    return CheckResult(Status.PASS, summary=f"All {len(inputs)} form controls have labels or aria-label.")


@register("C5", section="C", severity=Severity.RECOMMENDED,
          title="All <img> elements have an alt attribute", estimate_ms=2_000)
async def c5(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    imgs = r.soup.find_all("img")
    if not imgs:
        return CheckResult(Status.NA, summary="No <img> elements on page.")
    missing = [img for img in imgs if "alt" not in img.attrs]
    if missing:
        return CheckResult(Status.FAIL,
                           summary=f"{len(missing)} of {len(imgs)} <img> missing alt attribute.",
                           details={"missing_sample": [str(i)[:120] for i in missing[:5]]})
    return CheckResult(Status.PASS, summary=f"All {len(imgs)} <img> have alt attributes.")


@register("C6", section="C", severity=Severity.RECOMMENDED,
          title="Core content is wrapped in semantic HTML5 tags", estimate_ms=2_000)
async def c6(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    soup = r.soup
    has_main = bool(soup.find("main") or soup.find("article"))
    chrome = {tag: bool(soup.find(tag)) for tag in ("nav", "header", "footer", "aside")}
    sub: list[SubStep] = [
        SubStep("Has <main> or <article>", Status.PASS if has_main else Status.FAIL,
                detail=f"<main>: {bool(soup.find('main'))}, <article>: {bool(soup.find('article'))}"),
        SubStep("Has at least one boilerplate semantic wrapper",
                Status.PASS if any(chrome.values()) else Status.FAIL,
                detail=str(chrome)),
    ]
    # Primary not nested inside boilerplate.
    main_or_article = soup.find("main") or soup.find("article")
    nested = False
    if main_or_article:
        for parent in main_or_article.parents:
            if parent.name in ("nav", "header", "footer", "aside"):
                nested = True
                break
    sub.append(SubStep(
        "Primary content not nested inside boilerplate",
        Status.PASS if not nested else Status.FAIL,
        detail="nested inside boilerplate wrapper" if nested else "ok",
    ))
    return CheckResult.from_substeps("Semantic HTML structure.", sub)


C7_SYSTEM_PROMPT = (
    "You inspect a screenshot of a web page on first paint and answer: does any modal, overlay, "
    "newsletter gate, cookie wall, or app-install prompt obscure the page's primary visible content? "
    "POSITIVE EXAMPLES (these = pass, NOT intrusive): "
    "- A small chat widget or help bubble in the corner; "
    "- A cookie banner with legal-minimum copy ('We use cookies. Accept / Reject') that sits along the bottom or top edge "
    "  and does not cover the main hero/CTA area. "
    "NEGATIVE EXAMPLES (these = fail, ARE intrusive): "
    "- A full-screen modal that covers most of the viewport; "
    "- A newsletter signup popup; "
    "- An app-install banner that pushes the page content offscreen; "
    "- A cookie banner that blocks the hero/CTA area or covers more than ~25% of the viewport height. "
    "You will receive TWO screenshots: the first at 375px width (mobile), the second at 1440px width (desktop). "
    "Both must pass for verdict=pass; if either is intrusive, verdict=fail. "
    "Return JSON: {verdict: 'pass'|'fail', confidence: 0..1, reason: 'short reason ≤200 chars'}."
)


@register("C7", section="C", severity=Severity.BLOCKING,
          title="No intrusive interstitial obscures primary content on arrival", estimate_ms=10_000)
async def c7(ctx: CheckContext) -> CheckResult:
    """Vision-classified. Captures first-paint screenshots at mobile + desktop viewports."""
    vcfg = ctx.site_config.vision if ctx.site_config else None
    if not vcfg or not vcfg.enabled:
        return CheckResult(Status.MANUAL, summary="Manual: visual interstitial check (vision disabled).",
                           instruction=C7_INSTRUCTION)

    mobile, desktop = await asyncio.gather(
        browser.screenshot_at_viewport(ctx.url, viewport={"width": 375, "height": 812}, wait_until="networkidle"),
        browser.screenshot_at_viewport(ctx.url, viewport={"width": 1440, "height": 900}, wait_until="networkidle"),
    )
    if mobile.get("error") or desktop.get("error") or not mobile.get("png") or not desktop.get("png"):
        return CheckResult(Status.MANUAL,
                           summary=f"Couldn't capture screenshots ({mobile.get('error') or desktop.get('error') or 'empty png'}); manual check required.",
                           instruction=C7_INSTRUCTION,
                           details={"mobile_error": mobile.get("error"), "desktop_error": desktop.get("error")})

    if ctx.run_id:
        screenshots.save_png(ctx.run_id, "C7_mobile_375px", mobile["png"])
        screenshots.save_png(ctx.run_id, "C7_desktop_1440px", desktop["png"])

    user_prompt = (
        "FIRST screenshot: 375px-wide (mobile) viewport, first paint. "
        "SECOND screenshot: 1440px-wide (desktop) viewport, first paint. "
        "Both must show primary content unobstructed by intrusive overlays for verdict=pass. "
        "Output JSON only."
    )
    verdict = await vision.classify(
        vcfg,
        system_prompt=C7_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        images=[mobile["png"], desktop["png"]],
        image_mime="image/png",
    )

    saved_screenshots = ["C7_mobile_375px", "C7_desktop_1440px"] if ctx.run_id else []

    if verdict.error and verdict.provider == "fallback-to-manual":
        return CheckResult(
            Status.MANUAL,
            summary=f"Vision unavailable ({verdict.error}); manual check required.",
            instruction=C7_INSTRUCTION,
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
            "screenshots": saved_screenshots,
        },
    )
