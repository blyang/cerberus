"""C — HTML Semantics & On-Page Extraction."""
from __future__ import annotations

import asyncio
import re

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
    soup = r.soup
    unmatched: list[str] = []   # no labeling mechanism at all
    broken: list[str] = []      # mechanism present but empty / dangling reference
    for inp in inputs:
        if (inp.get("type") or "").lower() in ("hidden", "submit", "button", "image", "reset"):
            continue
        if (inp.get("aria-label") or "").strip():
            continue
        labelledby = (inp.get("aria-labelledby") or "").strip()
        if labelledby:
            # The accessible name is the concatenation of every resolvable referenced node's
            # text; a missing id just contributes nothing. So the control is only unlabelled
            # when NO referenced id resolves to text (`aria-labelledby="missing-id"`, or all
            # refs empty). A partially-dangling list that still has one real label is fine.
            refs = [soup.find(id=tok) for tok in labelledby.split()]
            if not any((ref.get_text(strip=True) if ref else "") for ref in refs):
                broken.append(str(inp)[:120])
            continue
        wrapping = next((par for par in inp.parents if par.name == "label"), None)
        if wrapping is not None:
            if not wrapping.get_text(strip=True):
                broken.append(str(inp)[:120])
            continue
        inp_id = inp.get("id")
        if inp_id:
            lbl = soup.find("label", attrs={"for": inp_id})
            if lbl is not None:
                if not lbl.get_text(strip=True):
                    broken.append(str(inp)[:120])
                continue
        unmatched.append(str(inp)[:120])
    sub = [
        SubStep(
            "Every input has a labeling mechanism",
            Status.PASS if not unmatched else Status.FAIL,
            detail=(f"{len(unmatched)} unlabelled input(s): {unmatched[:5]}" if unmatched
                    else f"all {len(inputs)} controls reference a label or aria-label"),
        ),
        SubStep(
            "Label references resolve to non-empty text",
            Status.PASS if not broken else Status.FAIL,
            detail=(f"{len(broken)} input(s) with empty/dangling label refs: {broken[:5]}" if broken
                    else "all label references resolve"),
        ),
    ]
    return CheckResult.from_substeps(f"Form-control labeling across {len(inputs)} control(s).", sub)


# Filenames that signal a decorative/spacer image, where empty alt is correct.
_DECORATIVE_NAME_RE = re.compile(
    r"(spacer|blank|pixel|1x1|transparent|separator|divider|placeholder|clear|shim"
    r"|icon|arrow|bullet|chevron|caret|sprite|swatch)", re.I)


def _img_is_informative(img) -> bool:
    """An empty-alt image is *suspect* (empty alt may be wrong) when it still likely conveys
    meaning: it links somewhere, or its filename is descriptive. Explicit decorative markers
    (role=presentation/none, aria-hidden=true) or spacer-style filenames mean empty alt is
    correct, so those are not flagged."""
    if (img.get("role") or "").lower() in ("presentation", "none"):
        return False
    if (img.get("aria-hidden") or "").lower() == "true":
        return False
    if any(par.name == "a" for par in img.parents):
        return True
    src = img.get("src") or img.get("data-src") or ""
    stem = src.rsplit("/", 1)[-1].split("?", 1)[0].rsplit(".", 1)[0]
    if not stem or _DECORATIVE_NAME_RE.search(stem):
        return False
    return bool(re.search(r"[a-z]{3,}", stem, re.I))


@register("C5", section="C", severity=Severity.RECOMMENDED,
          title="All <img> elements have an alt attribute", estimate_ms=2_000)
async def c5(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    imgs = r.soup.find_all("img")
    if not imgs:
        return CheckResult(Status.NA, summary="No <img> elements on page.")
    missing = [img for img in imgs if "alt" not in img.attrs]
    # alt="" is *valid* for decorative images but a defect on informative ones — surface the
    # latter for review rather than passing every empty alt (the old presence-only check).
    empty_informative = [
        img for img in imgs
        if "alt" in img.attrs and not (img.get("alt") or "").strip() and _img_is_informative(img)
    ]
    sub = [
        SubStep(
            "All <img> have an alt attribute",
            Status.PASS if not missing else Status.FAIL,
            detail=(f"{len(missing)} of {len(imgs)} missing alt: {[str(i)[:100] for i in missing[:5]]}"
                    if missing else f"all {len(imgs)} present"),
        ),
        SubStep(
            "Informative images have non-empty alt text",
            Status.PASS if not empty_informative else Status.NEEDS_REVIEW,
            detail=(f"{len(empty_informative)} empty-alt image(s) look informative "
                    f"(linked or descriptive filename): {[str(i)[:100] for i in empty_informative[:5]]}"
                    if empty_informative else "no suspect empty-alt images"),
        ),
    ]
    return CheckResult.from_substeps(f"Alt-text coverage across {len(imgs)} image(s).", sub)


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
          title="No intrusive interstitial or overlay obscures primary content on arrival", estimate_ms=10_000)
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
