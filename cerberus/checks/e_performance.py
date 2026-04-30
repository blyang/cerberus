"""E — Performance, Mobile & Security."""
from __future__ import annotations

import re

from .. import browser, screenshots, vision
from ..lighthouse import FIXTURE_KEY as LIGHTHOUSE_FIXTURE_KEY
from ._utils import UA_CHROME_DESKTOP, UA_CHROME_MOBILE, fetch, fetch_url
from .base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    SubStep,
    register,
)

E4_INSTRUCTION = (
    "Open DevTools Device Mode at 375px. Scroll the page. Tap each form input and the CTA button. "
    "Confirm no horizontal scroll, no overlap, all body text ≥ 14px."
)


async def _fixture(ctx: CheckContext) -> dict | None:
    fx = ctx.cache.get(LIGHTHOUSE_FIXTURE_KEY)
    if fx is None:
        return None
    if hasattr(fx, "__await__"):
        try:
            return await fx
        except Exception:
            return None
    return fx


def _fixture_errors(fx: dict | None) -> list[str]:
    if not fx:
        return ["lighthouse fixture missing"]
    errs: list[str] = []
    for device in ("mobile", "desktop"):
        e = (fx.get(device) or {}).get("error")
        if e:
            errs.append(f"{device}: {e}")
    return errs


def _eval_metric(label: str, mobile_v: float | None, desktop_v: float | None, threshold: float,
                 op: str = "lte") -> SubStep:
    """op: 'lte' (≤), 'lt' (<), 'gte' (≥)."""
    def ok(v: float | None) -> bool:
        if v is None:
            return False
        if op == "lte":
            return v <= threshold
        if op == "lt":
            return v < threshold
        return v >= threshold
    if mobile_v is None and desktop_v is None:
        return SubStep(label, Status.FAIL, detail="lighthouse failed to produce metric")
    sym = {"lte": "≤", "lt": "<", "gte": "≥"}[op]
    detail = f"mobile={mobile_v}, desktop={desktop_v}, threshold={sym} {threshold}"
    if ok(mobile_v) and ok(desktop_v):
        return SubStep(label, Status.PASS, detail=detail)
    return SubStep(label, Status.FAIL, detail=detail)


@register("E1", section="E", severity=Severity.RECOMMENDED,
          title="LCP is in a good range", estimate_ms=1_000)
async def e1(ctx: CheckContext) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.FAIL, summary="Lighthouse fixture unavailable.")
    errs = _fixture_errors(fx)
    if errs and all(e for e in errs):
        # Both devices errored — surface the underlying error.
        return CheckResult(Status.FAIL, summary="Lighthouse failed.", details={"lighthouse_errors": errs})
    m_lcp = (fx.get("mobile") or {}).get("metrics", {}).get("lcp_ms")
    d_lcp = (fx.get("desktop") or {}).get("metrics", {}).get("lcp_ms")
    sub = [_eval_metric("LCP ≤ 2.5s", m_lcp, d_lcp, 2500, op="lte")]
    return CheckResult.from_substeps("LCP from Lighthouse.", sub)


@register("E2", section="E", severity=Severity.RECOMMENDED,
          title="CLS is in a good range", estimate_ms=1_000)
async def e2(ctx: CheckContext) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.FAIL, summary="Lighthouse fixture unavailable.")
    errs = _fixture_errors(fx)
    if errs and all(e for e in errs):
        # Both devices errored — surface the underlying error.
        return CheckResult(Status.FAIL, summary="Lighthouse failed.", details={"lighthouse_errors": errs})
    m_cls = (fx.get("mobile") or {}).get("metrics", {}).get("cls")
    d_cls = (fx.get("desktop") or {}).get("metrics", {}).get("cls")
    sub = [_eval_metric("CLS < 0.1", m_cls, d_cls, 0.1, op="lt")]
    return CheckResult.from_substeps("CLS from Lighthouse.", sub)


@register("E3", section="E", severity=Severity.RECOMMENDED,
          title="INP is in a good range", estimate_ms=1_000)
async def e3(ctx: CheckContext) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.FAIL, summary="Lighthouse fixture unavailable.")
    errs = _fixture_errors(fx)
    if errs and all(e for e in errs):
        # Both devices errored — surface the underlying error.
        return CheckResult(Status.FAIL, summary="Lighthouse failed.", details={"lighthouse_errors": errs})
    m_inp = (fx.get("mobile") or {}).get("metrics", {}).get("inp_ms")
    d_inp = (fx.get("desktop") or {}).get("metrics", {}).get("inp_ms")
    if m_inp is None and d_inp is None:
        return CheckResult(Status.NA, summary="Lighthouse did not produce INP (no interactions in synthetic run).")
    sub = [_eval_metric("INP < 200ms", m_inp, d_inp, 200, op="lt")]
    return CheckResult.from_substeps("INP from Lighthouse.", sub)


@register("E3a", section="E", severity=Severity.RECOMMENDED,
          title="PageSpeed Insights overall score ≥90 for SEO and Performance", estimate_ms=1_000)
async def e3a(ctx: CheckContext) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.FAIL, summary="Lighthouse fixture unavailable.")
    errs = _fixture_errors(fx)
    if errs and all(e for e in errs):
        # Both devices errored — surface the underlying error.
        return CheckResult(Status.FAIL, summary="Lighthouse failed.", details={"lighthouse_errors": errs})
    sub: list[SubStep] = []
    for label, mobile_score, desktop_score, threshold in [
        ("Performance ≥ 0.90",
         (fx.get("mobile") or {}).get("scores", {}).get("performance"),
         (fx.get("desktop") or {}).get("scores", {}).get("performance"),
         0.90),
        ("SEO ≥ 0.90",
         (fx.get("mobile") or {}).get("scores", {}).get("seo"),
         (fx.get("desktop") or {}).get("scores", {}).get("seo"),
         0.90),
    ]:
        if mobile_score is None or desktop_score is None:
            sub.append(SubStep(label, Status.FAIL, detail=f"mobile={mobile_score}, desktop={desktop_score}"))
        else:
            ok = mobile_score >= threshold and desktop_score >= threshold
            sub.append(SubStep(label, Status.PASS if ok else Status.FAIL,
                               detail=f"mobile={mobile_score:.2f} desktop={desktop_score:.2f}"))
    return CheckResult.from_substeps("Lighthouse Perf + SEO scores.", sub)


@register("E3b", section="E", severity=Severity.RECOMMENDED,
          title="Local Lighthouse Accessibility and Best Practices scores ≥ 90", estimate_ms=1_000)
async def e3b(ctx: CheckContext) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.FAIL, summary="Lighthouse fixture unavailable.")
    errs = _fixture_errors(fx)
    if errs and all(e for e in errs):
        # Both devices errored — surface the underlying error.
        return CheckResult(Status.FAIL, summary="Lighthouse failed.", details={"lighthouse_errors": errs})
    sub: list[SubStep] = []
    for label, key in [("Accessibility ≥ 0.90", "accessibility"), ("Best Practices ≥ 0.90", "best-practices")]:
        m_score = (fx.get("mobile") or {}).get("scores", {}).get(key)
        d_score = (fx.get("desktop") or {}).get("scores", {}).get(key)
        if m_score is None or d_score is None:
            sub.append(SubStep(label, Status.FAIL, detail=f"mobile={m_score} desktop={d_score}"))
        else:
            ok = m_score >= 0.90 and d_score >= 0.90
            sub.append(SubStep(label, Status.PASS if ok else Status.FAIL,
                               detail=f"mobile={m_score:.2f} desktop={d_score:.2f}"))
    return CheckResult.from_substeps("Lighthouse a11y + best-practices.", sub)


E4_SYSTEM_PROMPT = (
    "You inspect a screenshot of a web page rendered at 375px-wide mobile viewport. "
    "Answer: do interactive elements (buttons, form inputs, primary CTA) visually overlap or appear unreachable, "
    "and is the layout broken (e.g. text spilling off-screen, images squashed, controls hidden behind other elements)? "
    "PASS examples: clean stacked layout, controls clearly tappable, text fits viewport. "
    "FAIL examples: form inputs overlap each other or with text, CTA hidden behind a sticky banner, layout collapses. "
    "Return JSON: {verdict: 'pass'|'fail', confidence: 0..1, reason: 'short reason ≤200 chars'}."
)


@register("E4", section="E", severity=Severity.BLOCKING,
          title="Page is mobile-responsive", estimate_ms=10_000)
async def e4(ctx: CheckContext) -> CheckResult:
    """Hybrid: deterministic DOM checks (horizontal scroll, font sizes) + vision check for layout overlap."""
    vcfg = ctx.site_config.vision if ctx.site_config else None
    sub: list[SubStep] = []

    # DOM-side checks: render at 375px and inspect via Playwright evaluate.
    bp = await browser.get_browser()
    dom_signals: dict = {"horiz_scroll": None, "min_font_px": None, "small_text_count": None}
    try:
        bctx = await bp.new_context(viewport={"width": 375, "height": 812})
        try:
            page = await bctx.new_page()
            await page.goto(ctx.url, wait_until="networkidle", timeout=30_000)
            try:
                dom_signals["horiz_scroll"] = await page.evaluate(
                    "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
            except Exception:
                dom_signals["horiz_scroll"] = None
            # Sample body text font sizes — find smallest non-empty text element's computed font-size.
            try:
                font_data = await page.evaluate(
                    """
                    () => {
                      const out = [];
                      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                      let node;
                      while ((node = walker.nextNode())) {
                        if (!node.textContent || !node.textContent.trim()) continue;
                        const el = node.parentElement;
                        if (!el) continue;
                        if (['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)) continue;
                        const fs = parseFloat(getComputedStyle(el).fontSize || '0');
                        if (fs > 0) out.push(fs);
                      }
                      return {min: Math.min(...out), small_count: out.filter(f => f < 12).length, total: out.length};
                    }
                    """
                )
                if isinstance(font_data, dict):
                    dom_signals["min_font_px"] = font_data.get("min")
                    dom_signals["small_text_count"] = font_data.get("small_count")
            except Exception:
                pass
            # Capture screenshot for vision pass.
            try:
                png = await page.screenshot(full_page=False, type="png")
            except Exception:
                png = b""
        finally:
            await bctx.close()
    except Exception as exc:
        return CheckResult(Status.FAIL, summary=f"E4 setup failed: {exc}",
                           details={"error": str(exc)})

    horiz = dom_signals.get("horiz_scroll")
    sub.append(SubStep(
        "No horizontal scroll at 375px",
        Status.PASS if (horiz is not None and horiz <= 1) else Status.FAIL,
        detail=f"scrollWidth - clientWidth = {horiz}",
    ))
    min_font = dom_signals.get("min_font_px")
    small_count = dom_signals.get("small_text_count") or 0
    sub.append(SubStep(
        "Body text ≥ 12px (footnotes/legal exception allowed)",
        Status.PASS if (min_font is None or min_font >= 12 or small_count <= 5) else Status.FAIL,
        detail=f"min font: {min_font}px; nodes < 12px: {small_count}",
    ))

    # Vision sub-step: layout/overlap.
    if not vcfg or not vcfg.enabled:
        sub.append(SubStep(
            "No interactive-element overlap (manual)",
            Status.MANUAL,
            detail="vision disabled; verify by hand.",
            instruction=E4_INSTRUCTION,
        ))
        return CheckResult.from_substeps("Mobile responsiveness (DOM signals + manual layout).", sub)
    if not png:
        sub.append(SubStep(
            "No interactive-element overlap (manual)",
            Status.MANUAL,
            detail="couldn't capture 375px screenshot; verify by hand.",
            instruction=E4_INSTRUCTION,
        ))
        return CheckResult.from_substeps("Mobile responsiveness (screenshot capture failed).", sub)

    if ctx.run_id:
        screenshots.save_png(ctx.run_id, "E4_mobile_375px", png)

    verdict = await vision.classify(
        vcfg,
        system_prompt=E4_SYSTEM_PROMPT,
        user_prompt="Screenshot is the page at 375px-wide mobile viewport. Output JSON only.",
        images=[png],
        image_mime="image/png",
    )

    if verdict.error and verdict.provider == "fallback-to-manual":
        sub.append(SubStep(
            "No interactive-element overlap (manual)",
            Status.MANUAL,
            detail=f"vision unavailable ({verdict.error})",
            instruction=E4_INSTRUCTION,
        ))
    else:
        v_status = Status.PASS if verdict.verdict == "pass" else Status.FAIL
        sub.append(SubStep(
            "No interactive-element overlap or layout breakage",
            v_status,
            detail=f"vision verdict: {verdict.verdict} (conf {verdict.confidence:.2f} via {verdict.provider}/{verdict.model}). {verdict.reason}",
        ))
    result = CheckResult.from_substeps("Mobile responsiveness.", sub)
    result.details.update({
        "horiz_scroll_px": horiz,
        "min_font_px": min_font,
        "small_text_count": small_count,
        "vision_verdict": verdict.verdict if not verdict.error else None,
        "vision_confidence": verdict.confidence if not verdict.error else None,
        "vision_provider": verdict.provider,
        "vision_model": verdict.model,
        "screenshots": ["E4_mobile_375px"] if ctx.run_id and png else [],
    })
    return result


@register("E5", section="E", severity=Severity.RECOMMENDED,
          title="Touch targets are adequately sized", estimate_ms=1_000)
async def e5(ctx: CheckContext) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.NEEDS_REVIEW, summary="Lighthouse fixture unavailable; rerun manually.")
    score = (fx.get("mobile") or {}).get("tap_targets_audit")
    if score is None:
        return CheckResult(Status.NEEDS_REVIEW, summary="Lighthouse did not include tap-targets audit.")
    if score >= 0.9:
        return CheckResult(Status.PASS, summary=f"tap-targets audit: {score:.2f}")
    return CheckResult(Status.FAIL, summary=f"tap-targets audit: {score:.2f} (< 0.9 — small/overlapping)")


@register("E6", section="E", severity=Severity.BLOCKING,
          title="Mobile and desktop expose the same primary content + directives", estimate_ms=6_000)
async def e6(ctx: CheckContext) -> CheckResult:
    mobile = await fetch(ctx, user_agent=UA_CHROME_MOBILE, key_suffix="mobile")
    desktop = await fetch(ctx, user_agent=UA_CHROME_DESKTOP)
    if mobile.status_code != 200 or desktop.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"mobile={mobile.status_code} desktop={desktop.status_code}")
    sub: list[SubStep] = []
    # title / desc / canonical
    def head_dump(html: str) -> dict[str, str]:
        m = re.search(r"<title[^>]*>([^<]*)</title>", html, re.I)
        title = (m.group(1).strip() if m else "")
        m2 = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html, re.I)
        desc = (m2.group(1).strip() if m2 else "")
        m3 = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]*)"', html, re.I)
        can = (m3.group(1).strip() if m3 else "")
        return {"title": title, "description": desc, "canonical": can}
    h_m, h_d = head_dump(mobile.text), head_dump(desktop.text)
    sub.append(SubStep(
        "title/description/canonical match",
        Status.PASS if h_m == h_d else Status.FAIL,
        detail=f"mobile={h_m} desktop={h_d}",
    ))
    # Robots directives
    def robots(html: str) -> str:
        m = re.search(r'<meta[^>]+name="robots"[^>]+content="([^"]*)"', html, re.I)
        return (m.group(1).strip().lower() if m else "")
    sub.append(SubStep(
        "robots directives match",
        Status.PASS if robots(mobile.text) == robots(desktop.text) else Status.FAIL,
        detail=f"mobile={robots(mobile.text)!r} desktop={robots(desktop.text)!r}",
    ))
    # Internal hrefs (sets, ignore order)
    def hrefs(html: str) -> set[str]:
        return set(re.findall(r'<a [^>]*href="([^"]*)"', html))
    diff = hrefs(mobile.text) ^ hrefs(desktop.text)
    sub.append(SubStep(
        "internal <a href> set is identical",
        Status.PASS if not diff else Status.FAIL,
        detail=f"diff size: {len(diff)}; sample: {list(diff)[:5]}",
    ))
    return CheckResult.from_substeps("Mobile↔desktop parity.", sub)


@register("E7", section="E", severity=Severity.BLOCKING,
          title="Page loads over HTTPS with no mixed content", estimate_ms=2_000)
async def e7(ctx: CheckContext) -> CheckResult:
    if not ctx.url.startswith("https://"):
        return CheckResult(Status.FAIL, summary="URL does not use https.")
    r = await fetch(ctx)
    # Find http:// references in src/href.
    insecure = re.findall(r'(?:src|href)="(http://[^"]+)"', r.text)
    if insecure:
        return CheckResult(Status.FAIL,
                           summary=f"{len(insecure)} insecure http:// reference(s) on page.",
                           details={"sample": insecure[:5]})
    return CheckResult(Status.PASS, summary="HTTPS with no http:// resources detected.")


@register("E8", section="E", severity=Severity.RECOMMENDED,
          title="HSTS response header present", estimate_ms=2_000)
async def e8(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    hsts = (r.headers.get("strict-transport-security") or "")
    if not hsts:
        return CheckResult(Status.FAIL, summary="No Strict-Transport-Security header.")
    m = re.search(r"max-age\s*=\s*(\d+)", hsts, re.I)
    max_age = int(m.group(1)) if m else 0
    if max_age >= 15_552_000:
        return CheckResult(Status.PASS, summary=f"HSTS max-age={max_age} ({hsts!r})", details={"hsts": hsts})
    return CheckResult(Status.FAIL, summary=f"HSTS max-age={max_age} (< 15552000).",
                       details={"hsts": hsts})
