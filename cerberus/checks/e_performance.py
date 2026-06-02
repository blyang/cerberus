"""E — Performance, Mobile & Security."""
from __future__ import annotations

import re

from .. import browser, screenshots, vision
from ..lighthouse import FIXTURE_KEY as LIGHTHOUSE_FIXTURE_KEY
from ._utils import (
    UA_CHROME_DESKTOP,
    UA_CHROME_MOBILE,
    collect_insecure_urls,
    fetch,
    fetch_url,
    get_canonical_href,
    get_meta,
    get_title_text,
)
from .base import (
    CheckContext,
    CheckResult,
    Env,
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


def _score_substep(label: str, m_score: float | None, d_score: float | None, threshold: float) -> SubStep:
    if m_score is None or d_score is None:
        return SubStep(label, Status.FAIL, detail=f"mobile={m_score}, desktop={d_score}")
    ok = m_score >= threshold and d_score >= threshold
    return SubStep(label, Status.PASS if ok else Status.FAIL,
                   detail=f"mobile={m_score:.2f} desktop={d_score:.2f}")


async def _lighthouse_score(ctx: CheckContext, label: str, score_key: str, threshold: float, summary: str) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.FAIL, summary="Lighthouse fixture unavailable.")
    errs = _fixture_errors(fx)
    if errs and all(e for e in errs):
        return CheckResult(Status.FAIL, summary="Lighthouse failed.", details={"lighthouse_errors": errs})
    m_score = (fx.get("mobile") or {}).get("scores", {}).get(score_key)
    d_score = (fx.get("desktop") or {}).get("scores", {}).get(score_key)
    return CheckResult.from_substeps(summary, [_score_substep(label, m_score, d_score, threshold)])


# E3a was previously a single check covering both Performance and SEO. Split because the two
# metrics have very different env sensitivity: Performance scores legitimately drop on preprod
# (no minification, no prod CDN cache), while SEO scores measure meta tags, viewport, mobile-
# friendliness, robots policy, canonical, structured-data presence — almost all of which is
# identical across envs. Tagging the combined check as production-only would suppress real SEO
# findings before deploy.
@register("E3a-perf", section="E", severity=Severity.RECOMMENDED,
          title="Lighthouse Performance score ≥ 0.90", estimate_ms=1_000,
          applicable_envs={Env.PRODUCTION})
async def e3a_perf(ctx: CheckContext) -> CheckResult:
    return await _lighthouse_score(ctx, "Performance ≥ 0.90", "performance", 0.90,
                                   "Lighthouse Performance score.")


E3A_SEO_AUDIT_CAP = 10


def _seo_audit_failed(audit: dict) -> bool:
    """A surfacable Lighthouse SEO audit failure.

    Skips ``manual``/``notApplicable``/``informative``: those have ``score=null`` by
    design and aren't pass/fail signals. Manual audits are operator TODOs that don't
    affect Lighthouse's score gate; surfacing them as failures would lie about what
    Lighthouse said.
    """
    mode = audit.get("scoreDisplayMode")
    score = audit.get("score")
    if mode == "error":
        return True
    if mode in ("binary", "numeric") and score is not None and score < 1.0:
        return True
    return False


def _seo_audit_substeps(fx: dict) -> tuple[list[SubStep], int]:
    """Build per-audit sub-steps for SEO failures across both devices.

    An audit can fail on one device and pass on the other (e.g. mobile-only audits
    return ``notApplicable`` on desktop). Surface any audit failing on either device
    and tag which side(s) failed so the operator knows where to reproduce.
    Returns (sub-steps, count truncated past the cap).
    """
    mobile_audits = (fx.get("mobile") or {}).get("seo_audits") or {}
    desktop_audits = (fx.get("desktop") or {}).get("seo_audits") or {}
    all_ids = sorted(set(mobile_audits) | set(desktop_audits))

    failures: list[SubStep] = []
    for aid in all_ids:
        m = mobile_audits.get(aid) or {}
        d = desktop_audits.get(aid) or {}
        m_bad = _seo_audit_failed(m)
        d_bad = _seo_audit_failed(d)
        if not (m_bad or d_bad):
            continue
        # Read fields from the failing side first so a passing-but-present entry
        # on the other side can't shadow stale title/error data.
        def _from_failing(field: str):
            return (m if m_bad else d).get(field) or (d if d_bad else m).get(field)
        title = _from_failing("title") or aid
        devices = ", ".join(name for name, bad in (("mobile", m_bad), ("desktop", d_bad)) if bad)
        parts = [f"failed on {devices}"]
        seen: set[str] = set()
        merged: list[str] = []
        for side, bad in ((m, m_bad), (d, d_bad)):
            if not bad:
                continue
            for s in (side.get("snippets") or []):
                if s not in seen:
                    seen.add(s)
                    merged.append(s)
        if merged:
            parts.append("e.g. " + " | ".join(merged[:3]))
        err = _from_failing("errorMessage")
        if err:
            parts.append(f"error: {str(err)[:120]}")
        failures.append(SubStep(f"{aid}: {title}", Status.FAIL, detail="; ".join(parts)))

    truncated = max(0, len(failures) - E3A_SEO_AUDIT_CAP)
    return failures[:E3A_SEO_AUDIT_CAP], truncated


@register("E3a-seo", section="E", severity=Severity.RECOMMENDED,
          title="Lighthouse SEO score ≥ 0.90", estimate_ms=1_000)
async def e3a_seo(ctx: CheckContext) -> CheckResult:
    fx = await _fixture(ctx)
    if not fx:
        return CheckResult(Status.FAIL, summary="Lighthouse fixture unavailable.")
    # Only bail outright when BOTH devices errored. If one is healthy, surface
    # its per-audit drilldown and emit a NEEDS_REVIEW step naming the broken side.
    # (E1/E2/E3/E3a-perf/E3b use a looser ``errs and all(...)`` guard that bails on
    # any error; that's fine for score-only checks but throws away usable signal here.)
    errs = _fixture_errors(fx)
    if (fx.get("mobile") or {}).get("error") and (fx.get("desktop") or {}).get("error"):
        return CheckResult(Status.FAIL, summary="Lighthouse failed.", details={"lighthouse_errors": errs})

    m_score = (fx.get("mobile") or {}).get("scores", {}).get("seo")
    d_score = (fx.get("desktop") or {}).get("scores", {}).get("seo")
    score_step = _score_substep("SEO ≥ 0.90", m_score, d_score, 0.90)

    # If exactly one device errored, the audit set is partial: the broken side
    # contributed nothing. Per CLAUDE.md "surface what wasn't evaluated", emit a
    # NEEDS_REVIEW sub-step naming which side was unrepresented so the operator
    # doesn't read a clean per-audit list as comprehensive.
    partial_steps: list[SubStep] = []
    for device in ("mobile", "desktop"):
        if (fx.get(device) or {}).get("error"):
            partial_steps.append(SubStep(
                f"{device} Lighthouse run errored — per-audit data unavailable for this side",
                Status.NEEDS_REVIEW,
                detail=str((fx.get(device) or {}).get("error"))[:160]))

    audit_steps, truncated = _seo_audit_substeps(fx)
    if truncated:
        # The truncated audits are confirmed failures, not unknowns. Marking the
        # tail as FAIL keeps the badge color faithful to what Lighthouse reported.
        audit_steps.append(SubStep(
            f"+{truncated} more SEO audit failure(s) not shown",
            Status.FAIL,
            detail=f"surfaced cap = {E3A_SEO_AUDIT_CAP}; see full Lighthouse report"))

    return CheckResult.from_substeps("Lighthouse SEO score + per-audit drill-down.",
                                     [score_step, *partial_steps, *audit_steps])


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
                        const cs = getComputedStyle(el);
                        // Skip hidden text — a hidden 8px node shouldn't fail (or pass) the
                        // visible-body-text check. (Element-level only: offsetParent is null
                        // for visible body-level / display:contents text too, so don't use it
                        // as a visibility proxy — it would drop real copy and stall the check.)
                        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                        if (parseFloat(cs.opacity || '1') === 0) continue;
                        const fs = parseFloat(cs.fontSize || '0');
                        if (fs > 0) out.push(fs);
                      }
                      // null (not Infinity) when nothing measurable, so the check can tell
                      // "extraction failed" apart from "measured and fine".
                      return {min: out.length ? Math.min(...out) : null,
                              small_count: out.filter(f => f < 12).length, total: out.length};
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
    if min_font is None:
        # Extraction failed / no visible text measured — don't certify the body-text size.
        sub.append(SubStep(
            "Body text ≥ 12px (footnotes/legal exception allowed)",
            Status.NEEDS_REVIEW,
            detail="could not measure rendered font sizes (no visible text nodes or evaluate failed)",
        ))
    else:
        sub.append(SubStep(
            "Body text ≥ 12px (footnotes/legal exception allowed)",
            Status.PASS if (min_font >= 12 or small_count <= 5) else Status.FAIL,
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


_TAP_TARGET_JS = """
() => {
  const MIN = 44;
  const vw = window.innerWidth;
  const sel = 'a[href], button, input, select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="radio"]';
  const rects = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) continue;
    if (style.pointerEvents === 'none') continue;
    // Skip elements off-canvas (carousels, menus hidden above/beside viewport).
    // Keep below-fold elements (r.top >= vh) — those are valid page content.
    if (r.right <= 0 || r.left >= vw || r.bottom <= 0) continue;
    rects.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 60),
      w: Math.round(r.width),
      h: Math.round(r.height),
      top: Math.round(r.top),
      left: Math.round(r.left),
      bottom: Math.round(r.bottom),
      right: Math.round(r.right),
      small: r.width < MIN || r.height < MIN,
    });
  }
  // Detect pairwise overlaps among tappable targets.
  const overlapping = [];
  for (let i = 0; i < rects.length; i++) {
    for (let j = i + 1; j < rects.length; j++) {
      const a = rects[i], b = rects[j];
      if (a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top) {
        overlapping.push([i, j]);
      }
    }
  }
  return {rects, overlapping};
}
"""


@register("E5", section="E", severity=Severity.RECOMMENDED,
          title="Touch targets are adequately sized", estimate_ms=6_000)
async def e5(ctx: CheckContext) -> CheckResult:
    try:
        bp = await browser.get_browser()
        bctx = await bp.new_context(
            viewport={"width": 375, "height": 812},
            user_agent=UA_CHROME_MOBILE,
            has_touch=True,
        )
        try:
            page = await bctx.new_page()
            await page.goto(ctx.url, wait_until="networkidle", timeout=30_000)
            elements = await page.evaluate(_TAP_TARGET_JS)
        finally:
            await bctx.close()
    except Exception as exc:
        return CheckResult(Status.NEEDS_REVIEW, summary=f"Tap-target scan failed: {exc}")

    if elements is None:
        return CheckResult(Status.NEEDS_REVIEW, summary="Tap-target JS returned None — evaluate may have failed silently.")
    rects = elements.get("rects") or []
    overlapping = elements.get("overlapping") or []
    if not rects:
        return CheckResult(Status.NEEDS_REVIEW, summary="No tappable interactive elements found at 375px — visibility filters may have culled everything.")

    small = [e for e in rects if e.get("small")]
    total = len(rects)
    issues: list[str] = []
    if small:
        issues.append(f"{len(small)}/{total} element(s) smaller than 44×44px")
    if overlapping:
        issues.append(f"{len(overlapping)} overlapping pair(s)")
    if not issues:
        return CheckResult(Status.PASS, summary=f"All {total} interactive element(s) ≥ 44×44px, no overlaps.",
                           details={"total": total, "small_targets": [], "overlapping_pairs": []})
    small_detail = [{"tag": e["tag"], "text": e["text"], "w": e["w"], "h": e["h"]} for e in small[:10]]
    overlap_detail = [
        {"a": {"tag": rects[i]["tag"], "text": rects[i]["text"]},
         "b": {"tag": rects[j]["tag"], "text": rects[j]["text"]}}
        for i, j in overlapping[:5]
    ]
    return CheckResult(
        Status.FAIL,
        summary="; ".join(issues) + ".",
        details={"total": total, "small_count": len(small), "small_targets": small_detail,
                 "overlapping_pairs": overlap_detail},
    )


@register("E6", section="E", severity=Severity.BLOCKING,
          title="Mobile page exposes the same primary content and critical directives as desktop", estimate_ms=6_000)
async def e6(ctx: CheckContext) -> CheckResult:
    mobile = await fetch(ctx, user_agent=UA_CHROME_MOBILE, key_suffix="mobile")
    desktop = await fetch(ctx, user_agent=UA_CHROME_DESKTOP)
    if mobile.status_code != 200 or desktop.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"mobile={mobile.status_code} desktop={desktop.status_code}")
    sub: list[SubStep] = []
    # title / desc / canonical — BS4-based so single-quoted attributes parse.
    mobile_soup = mobile.soup
    desktop_soup = desktop.soup

    def head_dump(soup) -> dict[str, str]:
        return {
            "title": get_title_text(soup),
            "description": (get_meta(soup, name="description") or ""),
            "canonical": (get_canonical_href(soup) or ""),
        }
    h_m, h_d = head_dump(mobile_soup), head_dump(desktop_soup)
    sub.append(SubStep(
        "title/description/canonical match",
        Status.PASS if h_m == h_d else Status.FAIL,
        detail=f"mobile={h_m} desktop={h_d}",
    ))
    # Robots directives
    def robots(soup) -> str:
        return (get_meta(soup, name="robots") or "").lower()
    sub.append(SubStep(
        "robots directives match",
        Status.PASS if robots(mobile_soup) == robots(desktop_soup) else Status.FAIL,
        detail=f"mobile={robots(mobile_soup)!r} desktop={robots(desktop_soup)!r}",
    ))
    # Internal hrefs (sets, ignore order)
    def hrefs(soup) -> set[str]:
        return {(a.get("href") or "").strip() for a in soup.find_all("a", href=True)}
    diff = hrefs(mobile_soup) ^ hrefs(desktop_soup)
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
    # BS4-based scan covers single-quoted attrs and widens beyond src/href to
    # srcset, poster, action, data-src, plus a regex pass for CSS url(...).
    insecure = collect_insecure_urls(r.soup)
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
