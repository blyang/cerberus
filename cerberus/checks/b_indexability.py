"""B — Indexability, Canonicalization & Head Controls."""
from __future__ import annotations

import io
import re
from urllib.parse import urljoin, urlparse

from PIL import Image

from ._utils import (
    UA_CHROME_MOBILE,
    fetch,
    fetch_url,
    get_canonical_href,
    get_meta,
    normalize_url,
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


@register("B1", section="B", severity=Severity.BLOCKING,
          title="<title> present and non-empty", estimate_ms=2_000)
async def b1(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    if r.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"Page returned {r.status_code}; cannot evaluate.")
    titles = r.soup.find_all("title")
    if len(titles) != 1:
        return CheckResult(Status.FAIL, summary=f"Found {len(titles)} <title> elements (expected exactly 1).")
    text = (titles[0].string or "").strip()
    if not text:
        return CheckResult(Status.FAIL, summary="<title> is empty.")
    return CheckResult(Status.PASS, summary=f"<title>: {text!r}", details={"title": text, "length": len(text)})


@register("B2", section="B", severity=Severity.BLOCKING,
          title="<meta name=\"description\"> present and non-empty", estimate_ms=2_000)
async def b2(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    desc = get_meta(r.soup, name="description")
    if not desc:
        return CheckResult(Status.FAIL, summary="No non-empty <meta name=\"description\"> found.")
    return CheckResult(Status.PASS, summary=f"description ({len(desc)} chars): {desc!r}",
                       details={"description": desc, "length": len(desc)})


@register("B3", section="B", severity=Severity.BLOCKING,
          title="Canonical points to the preferred URL for this exact page version", estimate_ms=2_000)
async def b3(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    canonicals = r.soup.find_all("link", attrs={"rel": lambda v: v and "canonical" in (v if isinstance(v, list) else v.lower().split())})
    if len(canonicals) != 1:
        return CheckResult(Status.FAIL, summary=f"Found {len(canonicals)} canonical link tags (expected 1).")
    href = (canonicals[0].get("href") or "").strip()
    if not href:
        return CheckResult(Status.FAIL, summary="Canonical link has empty href.")
    if not href.startswith("http://") and not href.startswith("https://"):
        return CheckResult(Status.FAIL, summary=f"Canonical href is not absolute: {href!r}",
                           details={"canonical_href": href})
    if normalize_url(href) != normalize_url(ctx.url):
        return CheckResult(Status.FAIL,
                           summary=f"Canonical href ≠ audited URL.\nfound: {href}\nexpected: {ctx.url}",
                           details={"canonical_href": href, "audited_url": ctx.url})
    return CheckResult(Status.PASS, summary=f"Canonical self-references: {href}",
                       details={"canonical_href": href})


@register("B4", section="B", severity=Severity.BLOCKING,
          title="Canonical target is healthy", estimate_ms=4_000)
async def b4(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    href = get_canonical_href(r.soup)
    if not href:
        return CheckResult(Status.NA, summary="No canonical tag (B3 will fail).")
    # First fetch without following redirects: a canonical that 301s elsewhere
    # is by definition unstable, even if the eventual destination is 200/healthy.
    target = await fetch_url(ctx, href, follow_redirects=False)
    if target.status_code in (301, 302, 307, 308):
        loc = target.headers.get("location", "")
        return CheckResult(
            Status.FAIL,
            summary=f"Canonical href {href} redirects ({target.status_code} → {loc}); target is not stable.",
            details={"canonical_href": href, "redirect_to": loc, "status": target.status_code,
                     "advice": "Set canonical to the final, non-redirecting URL."},
        )
    sub: list[SubStep] = []
    sub.append(SubStep("Canonical target returns 200", Status.PASS if target.status_code == 200 else Status.FAIL,
                       detail=f"status: {target.status_code} {target.error or ''}".strip()))
    # Check robots-meta on target.
    meta_robots = (get_meta(target.soup, name="robots") or "").lower()
    xrobots = (target.headers.get("x-robots-tag") or "").lower()
    has_noindex = "noindex" in meta_robots or "noindex" in xrobots
    sub.append(SubStep("No noindex on canonical target", Status.FAIL if has_noindex else Status.PASS,
                       detail=f"meta robots: {meta_robots!r}; X-Robots-Tag: {xrobots!r}"))
    target_canonical = get_canonical_href(target.soup)
    self_ref = bool(target_canonical) and normalize_url(target_canonical) == normalize_url(href)
    sub.append(SubStep("Canonical target self-references",
                       Status.PASS if self_ref else Status.FAIL,
                       detail=f"target's canonical: {target_canonical!r}"))
    return CheckResult.from_substeps(f"Canonical target health: {href}", sub)


def _redirect_matches(headers: dict, request_url: str, expected_url: str) -> bool:
    """True if the Location header (resolved against `request_url`) normalizes to `expected_url`.

    Used by B5/B6 to assert that variant URLs redirect to the right preferred URL,
    not just to *some* URL with a 301 status.
    """
    loc = (headers.get("location") or "").strip()
    if not loc:
        return False
    abs_loc = urljoin(request_url, loc)
    return normalize_url(abs_loc) == normalize_url(expected_url)


def _variant_redirect_status(request_url: str, fetch_result, expected_url: str) -> tuple[Status, str]:
    """Verdict for a host-normalization redirect. 302/307 stay Needs Review; 301/308
    must additionally point at `expected_url` to count as Pass.
    """
    loc = fetch_result.headers.get("location", "")
    sc = fetch_result.status_code
    detail = f"status={sc} loc={loc!r} expected={expected_url!r}"
    if sc in (301, 308):
        if _redirect_matches(fetch_result.headers, request_url, expected_url):
            return Status.PASS, detail
        return Status.FAIL, detail + " (Location does not match expected preferred URL)"
    if sc in (302, 307):
        return Status.NEEDS_REVIEW, detail + " (302/307 — should be 301 for permanent host normalization)"
    return Status.FAIL, detail


@register("B5", section="B", severity=Severity.BLOCKING,
          title="Duplicate URL variants consolidate cleanly", estimate_ms=8_000)
async def b5(ctx: CheckContext) -> CheckResult:
    p = urlparse(ctx.url)
    host = (p.hostname or "").lower()
    apex = host[4:] if host.startswith("www.") else host
    canonical_url = ctx.url
    expected_preferred = f"{p.scheme}://{host}{p.path}"
    sub: list[SubStep] = []

    # 1. UTM variant returns 200 with same canonical.
    utm_url = ctx.url + ("&" if p.query else "?") + "utm_source=cerberus_test"
    utm_r = await fetch_url(ctx, utm_url)
    canon = get_canonical_href(utm_r.soup) if utm_r.text else None
    sub.append(SubStep(
        "UTM variant 200 + canonical preserved",
        Status.PASS if utm_r.status_code == 200 and canon and normalize_url(canon) == normalize_url(canonical_url) else Status.FAIL,
        detail=f"status={utm_r.status_code} canonical={canon!r}",
    ))
    # 2. http://www.<host> 301s to https://www.<host>.
    if p.scheme == "https":
        http_variant = "http://" + host + p.path + (("?" + p.query) if p.query else "")
        hr = await fetch_url(ctx, http_variant, follow_redirects=False)
        status, detail = _variant_redirect_status(http_variant, hr, expected_preferred)
        sub.append(SubStep("http:// variant 301s to expected https:// URL", status, detail=detail))
    # 3. apex variant 301s to www. Apex→www is CDN-edge-rule territory: at strikingly
    # it's only configured on prod (per backend dev), so non-prod envs lack the redirect
    # by deployment design rather than by bug. Surface the skip as an NA sub-step
    # instead of dropping it silently — keeps the gap visible in the report.
    if apex != host and host.startswith("www."):
        if ctx.environment == Env.PRODUCTION:
            apex_variant = f"{p.scheme}://{apex}{p.path}"
            ar = await fetch_url(ctx, apex_variant, follow_redirects=False)
            status, detail = _variant_redirect_status(apex_variant, ar, expected_preferred)
            sub.append(SubStep("apex variant 301s to expected www URL", status, detail=detail))
        else:
            sub.append(SubStep(
                "apex variant 301s to expected www URL",
                Status.NA,
                detail=f"apex→www is a CDN edge rule only configured on prod; skipped on env={ctx.environment.value}",
            ))
    return CheckResult.from_substeps("Duplicate URL variant consolidation.", sub)


@register("B6", section="B", severity=Severity.BLOCKING,
          title="Host normalization matches the site's URL architecture", estimate_ms=4_000,
          applicable_envs={Env.PRODUCTION})
async def b6(ctx: CheckContext) -> CheckResult:
    p = urlparse(ctx.url)
    host = (p.hostname or "").lower()
    if not host.startswith("www."):
        # Not a www-style host; skip rather than apply a strikingly-specific rule.
        return CheckResult(Status.NA, summary=f"Host '{host}' isn't www-prefixed; B6 only applies to www architectures.")
    apex = host[4:]
    apex_https = f"https://{apex}{p.path}"
    apex_http = f"http://{apex}{p.path}"
    r1 = await fetch_url(ctx, apex_https, follow_redirects=False)
    r2 = await fetch_url(ctx, apex_http, follow_redirects=False)
    expected_loc = f"{p.scheme}://{host}{p.path}"
    s1, d1 = _variant_redirect_status(apex_https, r1, expected_loc)
    s2, d2 = _variant_redirect_status(apex_http, r2, expected_loc)
    sub = [
        SubStep("https://apex 301→https://www (expected URL)", s1, detail=d1),
        SubStep("http://apex 301→https://www (expected URL)", s2, detail=d2),
    ]
    return CheckResult.from_substeps("Apex→www normalization.", sub)


@register("B7", section="B", severity=Severity.BLOCKING,
          title="No noindex directive", estimate_ms=2_000)
async def b7(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    xrobots = (r.headers.get("x-robots-tag") or "")
    meta_robots = get_meta(r.soup, name="robots") or ""
    meta_googlebot = get_meta(r.soup, name="googlebot") or ""
    has_noindex_header = "noindex" in xrobots.lower()
    has_noindex_meta = "noindex" in meta_robots.lower() or "noindex" in meta_googlebot.lower()
    if has_noindex_header or has_noindex_meta:
        return CheckResult(Status.FAIL,
                           summary="noindex detected.",
                           details={"x_robots_tag": xrobots, "meta_robots": meta_robots, "meta_googlebot": meta_googlebot})
    return CheckResult(Status.PASS, summary="No noindex directives found.",
                       details={"meta_robots": meta_robots or "(none)", "x_robots_tag": xrobots or "(none)"})


@register("B8", section="B", severity=Severity.BLOCKING,
          title="No accidental restrictive preview controls", estimate_ms=2_000)
async def b8(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    bad_directives = ["nosnippet", "max-snippet:0", "max-image-preview:none", "unavailable_after"]
    xrobots = (r.headers.get("x-robots-tag") or "").lower()
    meta_robots = (get_meta(r.soup, name="robots") or "").lower()
    meta_googlebot = (get_meta(r.soup, name="googlebot") or "").lower()
    found = [d for d in bad_directives if d in xrobots or d in meta_robots or d in meta_googlebot]
    nosnippet_attr = bool(re.search(r"data-nosnippet", r.text or "", re.I))
    sub: list[SubStep] = []
    sub.append(SubStep("No restrictive directives in headers/meta",
                       Status.PASS if not found else Status.FAIL,
                       detail=f"found: {found}" if found else "none"))
    sub.append(SubStep("No data-nosnippet attribute",
                       Status.PASS if not nosnippet_attr else Status.FAIL,
                       detail="found data-nosnippet" if nosnippet_attr else "not present"))
    return CheckResult.from_substeps("Preview-control directives.", sub)


@register("B9", section="B", severity=Severity.RECOMMENDED,
          title="<html lang> attribute present", estimate_ms=2_000)
async def b9(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    html_el = r.soup.find("html")
    lang = (html_el.get("lang") if html_el else "") or ""
    if lang.strip():
        return CheckResult(Status.PASS, summary=f"<html lang=\"{lang}\">", details={"lang": lang})
    return CheckResult(Status.FAIL, summary="<html> has no lang attribute.")


@register("B10", section="B", severity=Severity.RECOMMENDED,
          title="<meta charset=\"UTF-8\"> present", estimate_ms=2_000)
async def b10(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    head_first_2k = (r.text or "")[:2048]
    m = re.search(r'<meta[^>]+charset\s*=\s*["\']?([\w-]+)', head_first_2k, re.I)
    if not m:
        return CheckResult(Status.FAIL, summary="No <meta charset> in first 2KB of HTML.")
    charset = m.group(1)
    if charset.lower() == "utf-8":
        return CheckResult(Status.PASS, summary=f"<meta charset=\"{charset}\">",
                           details={"charset": charset})
    return CheckResult(Status.FAIL, summary=f"charset is {charset!r}, expected utf-8.",
                       details={"charset": charset})


@register("B11", section="B", severity=Severity.RECOMMENDED,
          title="<meta name=\"viewport\"> present", estimate_ms=2_000)
async def b11(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    content = get_meta(r.soup, name="viewport")
    if not content:
        return CheckResult(Status.FAIL, summary="No <meta name=\"viewport\">.")
    if "width=device-width" not in content.lower():
        return CheckResult(Status.FAIL, summary=f"viewport content missing width=device-width: {content!r}")
    return CheckResult(Status.PASS, summary=f"viewport: {content!r}", details={"viewport": content})


@register("B12", section="B", severity=Severity.RECOMMENDED,
          title="Open Graph and Twitter Card tags present and non-empty", estimate_ms=2_000)
async def b12(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    og_keys = ["og:title", "og:description", "og:type", "og:url", "og:image", "og:site_name", "og:locale"]
    tw_keys = ["twitter:card", "twitter:site", "twitter:title", "twitter:description", "twitter:image"]
    og = {k: get_meta(r.soup, prop=k) for k in og_keys}
    tw = {k: get_meta(r.soup, name=k) for k in tw_keys}
    og_missing = [k for k, v in og.items() if not v]
    tw_missing = [k for k, v in tw.items() if not v]
    sub = [
        SubStep("All 7 OG tags present + non-empty",
                Status.PASS if not og_missing else Status.FAIL,
                detail=f"missing: {og_missing}" if og_missing else "all present"),
        SubStep("All 5 Twitter Card tags present + non-empty",
                Status.PASS if not tw_missing else Status.FAIL,
                detail=f"missing: {tw_missing}" if tw_missing else "all present"),
    ]
    return CheckResult.from_substeps("OG + Twitter Card tags.", sub)


@register("B13", section="B", severity=Severity.BLOCKING,
          title="URL uses all-lowercase characters", estimate_ms=2_000)
async def b13(ctx: CheckContext) -> CheckResult:
    p = urlparse(ctx.url)
    upper_in_path = bool(re.search(r"[A-Z]", p.path))
    sub: list[SubStep] = [
        SubStep("Audited URL path is all-lowercase",
                Status.FAIL if upper_in_path else Status.PASS,
                detail=f"path: {p.path}"),
    ]
    # Internal links lowercase check.
    r = await fetch(ctx)
    bad_links: list[str] = []
    for a in r.soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/") or (p.hostname or "") in href:
            ph = urlparse(href).path or href
            if re.search(r"[A-Z]", ph):
                bad_links.append(href)
        if len(bad_links) >= 10:
            break
    sub.append(SubStep("No internal <a href> contains uppercase",
                       Status.PASS if not bad_links else Status.FAIL,
                       detail=f"sample: {bad_links}" if bad_links else "none"))
    return CheckResult.from_substeps("URL casing.", sub)


@register("B14", section="B", severity=Severity.RECOMMENDED,
          title="og:image meets minimum dimensions and format", estimate_ms=8_000)
async def b14(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    og_image = get_meta(r.soup, prop="og:image")
    tw_image = get_meta(r.soup, name="twitter:image")
    sub: list[SubStep] = []
    for label, src in [("og:image", og_image), ("twitter:image", tw_image)]:
        if not src:
            sub.append(SubStep(f"{label} present", Status.FAIL, detail="(missing)"))
            continue
        absolute = urljoin(ctx.url, src)
        head = await fetch_url(ctx, absolute, method="HEAD")
        ct = (head.headers.get("content-type") or "").lower()
        ok_ct = any(ct.startswith(t) for t in ("image/jpeg", "image/png", "image/webp"))
        size = int(head.headers.get("content-length") or 0)
        if head.status_code != 200 or not ok_ct:
            sub.append(SubStep(f"{label} reachable + correct content-type", Status.FAIL,
                               detail=f"status={head.status_code} content-type={ct!r} size={size}"))
            continue
        full = await fetch_url(ctx, absolute, method="GET")
        dims: tuple[int, int] | None = None
        try:
            with Image.open(io.BytesIO(full.content)) as im:
                dims = im.size
        except Exception as exc:
            sub.append(SubStep(f"{label} dimensions ≥ 1200×630", Status.FAIL,
                               detail=f"could not read dimensions: {exc}"))
            continue
        if dims and dims[0] >= 1200 and dims[1] >= 630:
            sub.append(SubStep(f"{label} dimensions ≥ 1200×630", Status.PASS,
                               detail=f"{dims[0]}×{dims[1]}, type={ct}, size={size}"))
        else:
            sub.append(SubStep(f"{label} dimensions ≥ 1200×630", Status.FAIL,
                               detail=f"actual {dims[0]}×{dims[1]}" if dims else "unknown"))
    return CheckResult.from_substeps("Social card image specs.", sub)


@register("B15", section="B", severity=Severity.RECOMMENDED,
          title="Favicon present and referenced", estimate_ms=4_000)
async def b15(ctx: CheckContext) -> CheckResult:
    p = urlparse(ctx.url)
    favicon_url = f"{p.scheme}://{p.hostname}/favicon.ico"
    fav = await fetch_url(ctx, favicon_url, method="HEAD")
    sub = [SubStep(
        "/favicon.ico returns image",
        Status.PASS if fav.status_code == 200 and (fav.headers.get("content-type") or "").lower().startswith(("image/", "application/x-icon")) else Status.FAIL,
        detail=f"status={fav.status_code} content-type={fav.headers.get('content-type','')!r}",
    )]
    r = await fetch(ctx)
    icons = r.soup.find_all("link", rel=lambda v: v and any(t in (v if isinstance(v, list) else v.lower().split())
                                                            for t in ("icon", "shortcut", "apple-touch-icon")))
    bad: list[str] = []
    if icons:
        for l in icons[:6]:
            href = l.get("href")
            if not href:
                bad.append(str(l))
                continue
            absolute = urljoin(ctx.url, href)
            fr = await fetch_url(ctx, absolute, method="HEAD")
            ct = (fr.headers.get("content-type") or "").lower()
            if fr.status_code != 200 or not ct.startswith("image/"):
                bad.append(f"{absolute} status={fr.status_code} content-type={ct}")
    sub.append(SubStep(
        "<link rel=\"icon\"> referenced and resolvable",
        (Status.PASS if icons and not bad else Status.FAIL),
        detail=f"{len(icons)} link refs; {len(bad)} bad" + (f" (sample: {bad[:2]})" if bad else ""),
    ))
    return CheckResult.from_substeps("Favicon reachability.", sub)
