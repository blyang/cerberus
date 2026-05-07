"""G — Internationalization.

If the audited page declares no <link rel="alternate" hreflang="..."> tags, the cluster doesn't
exist and all G checks resolve N/A (matches the brief's Phase-1 single-locale assumption).

When alternates DO exist, we fetch each locale URL once (concurrently, capped) and run the real
cluster-level assertions: liveness, self-canonical, hreflang reciprocity, x-default, locale-
adaptive serving.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

from ._utils import (
    UA_CHROME_DESKTOP,
    UA_GOOGLEBOT,
    fetch,
    fetch_url,
    get_canonical_href,
    normalize_url,
)
from .base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    SubStep,
    register,
)

NA_NO_CLUSTER = "No hreflang alternates declared on this page → no locale cluster to evaluate."

# Hard cap to keep cluster runs from blowing up ETA on pages with 50+ locales.
MAX_LOCALES = 30
LOCALE_FETCH_CONCURRENCY = 6

# Process-lifetime cache for cerberus's own outbound IP geolocation (used by G6 to know
# what vantage point its fetches represent). Looked up at most once per process; degrades
# gracefully on network failure.
_VANTAGE_CACHE: dict[str, str] | None = None


async def _vantage_info() -> dict[str, str]:
    global _VANTAGE_CACHE
    if _VANTAGE_CACHE is not None:
        return _VANTAGE_CACHE
    out = {"ip": "unknown", "country": "unknown", "city": "unknown"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://ipinfo.io/json")
        if r.status_code == 200:
            d = r.json()
            out = {
                "ip": str(d.get("ip") or "unknown"),
                "country": str(d.get("country") or "unknown"),
                "city": str(d.get("city") or "unknown"),
            }
    except Exception:
        pass
    _VANTAGE_CACHE = out
    return out


def _parse_hreflang_links(html: str) -> list[tuple[str, str]]:
    """Return [(hreflang, href)] from <link rel="alternate"> declarations. Order preserved."""
    out: list[tuple[str, str]] = []
    # Both attribute orders ('hreflang' before 'href' and after) appear in real HTML.
    pattern = re.compile(
        r'<link\b[^>]*?\brel\s*=\s*["\']?alternate["\']?[^>]*>',
        re.I,
    )
    for tag in pattern.findall(html):
        hl_match = re.search(r'\bhreflang\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        href_match = re.search(r'\bhref\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if hl_match and href_match:
            hl = hl_match.group(1).strip()
            href = href_match.group(1).strip()
            if hl and href:
                out.append((hl, href))
    return out


async def _build_cluster(ctx: CheckContext) -> dict:
    """Fetch the audited page + every alternate locale once. Memoized on ctx.cache.

    Returns:
        {
          'alternates': [(hreflang, url), ...],   # from audited page (incl. x-default)
          'has_x_default': bool,
          'per_locale': {url: {'status', 'canonical', 'alternates': [(hreflang,url),...], 'error'}}
        }
    """
    cached = ctx.cache.get("g_cluster")
    if cached is not None:
        if asyncio.iscoroutine(cached) or hasattr(cached, "__await__"):
            return await cached
        return cached

    async def factory() -> dict:
        page = await fetch(ctx)
        # Same filter applied to per-locale alternates so G3 reciprocity stays apples-to-apples.
        host_cfg = ctx.site_config.for_url(ctx.url) if ctx.site_config else None
        supported = host_cfg.supported_languages if host_cfg else None
        allowed: set[str] | None = {s.lower() for s in supported} if supported else None

        def _filter(pairs: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
            if allowed is None:
                return pairs, []
            kept: list[tuple[str, str]] = []
            dropped: list[tuple[str, str]] = []
            for hl, url in pairs:
                if hl.lower() == "x-default" or hl.lower().split("-", 1)[0] in allowed:
                    kept.append((hl, url))
                else:
                    dropped.append((hl, url))
            return kept, dropped

        alternates, unsupported_dropped = _filter(_parse_hreflang_links(page.text or ""))
        has_x_default = any(hl.lower() == "x-default" for hl, _ in alternates)
        # Strip x-default from the locale set we crawl since x-default's target overlaps another locale.
        locale_targets: list[str] = []
        seen: set[str] = set()
        for hl, url in alternates:
            n = normalize_url(url)
            if hl.lower() == "x-default":
                continue
            if n in seen:
                continue
            seen.add(n)
            locale_targets.append(url)
        truncated_locales = locale_targets[MAX_LOCALES:]
        locale_targets = locale_targets[:MAX_LOCALES]

        sem = asyncio.Semaphore(LOCALE_FETCH_CONCURRENCY)

        async def _fetch_one(u: str) -> tuple[str, dict]:
            async with sem:
                # Fetch without following redirects so G1/G5 can see 301/302s.
                head = await fetch_url(ctx, u, follow_redirects=False)
                if head.status_code in (301, 302, 307, 308):
                    return u, {
                        "status": head.status_code,
                        "location": head.headers.get("location", ""),
                        "canonical": None,
                        "alternates": [],
                        "error": f"redirect to {head.headers.get('location','')!r}",
                    }
                # Need the body for canonical + reciprocity, so do a real GET (memoized via fetch_url).
                full = await fetch_url(ctx, u)
                their_alternates, _ = _filter(_parse_hreflang_links(full.text or ""))
                return u, {
                    "status": full.status_code,
                    "location": "",
                    "canonical": get_canonical_href(full.soup) if full.text else None,
                    "alternates": their_alternates,
                    "error": full.error,
                }

        per_locale: dict[str, dict] = {}
        if locale_targets:
            results = await asyncio.gather(*(_fetch_one(u) for u in locale_targets), return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    continue
                url, data = r
                per_locale[url] = data

        return {
            "alternates": alternates,
            "has_x_default": has_x_default,
            "per_locale": per_locale,
            "audited_url": ctx.url,
            "audited_alternates_normalized": _normalize_alt_set(alternates),
            "unsupported_dropped": unsupported_dropped,
            "truncated_locales": truncated_locales,
        }

    task = asyncio.ensure_future(factory())
    ctx.cache["g_cluster"] = task
    result = await task
    ctx.cache["g_cluster"] = result
    return result


def _normalize_alt_set(alternates: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Lowercase + normalize the URL side so two pages declaring the same alternates set-equal."""
    return {(hl.lower(), normalize_url(url)) for hl, url in alternates}


def _no_cluster() -> CheckResult:
    return CheckResult(Status.NA, summary=NA_NO_CLUSTER)


@register("G1", section="G", severity=Severity.BLOCKING,
          title="Each locale version lives on a unique stable URL", estimate_ms=8_000)
async def g1(ctx: CheckContext) -> CheckResult:
    cluster = await _build_cluster(ctx)
    if not cluster["alternates"]:
        return _no_cluster()
    sub: list[SubStep] = []
    bad: list[str] = []
    redirects: list[str] = []
    for url, data in cluster["per_locale"].items():
        if data["status"] in (301, 302, 307, 308):
            redirects.append(f"{url} → {data['location']!r}")
        elif data["status"] != 200:
            bad.append(f"{url} status={data['status']}")
    sub.append(SubStep(
        "Every locale URL returns 200",
        Status.PASS if not bad and not redirects else Status.FAIL,
        detail=(f"redirects: {redirects[:3]}" if redirects else "")
        + (f" non-200: {bad[:3]}" if bad else "") or f"all {len(cluster['per_locale'])} ok",
    ))
    # Uniqueness: every locale must map to a distinct URL.
    norm_urls = [normalize_url(u) for u in cluster["per_locale"].keys()]
    duplicates = [u for u in set(norm_urls) if norm_urls.count(u) > 1]
    sub.append(SubStep(
        "Locale URLs are distinct",
        Status.PASS if not duplicates else Status.FAIL,
        detail=f"duplicates: {duplicates}" if duplicates else f"{len(set(norm_urls))} unique URLs",
    ))
    return CheckResult.from_substeps(f"hreflang cluster: {len(cluster['per_locale'])} locale(s).", sub)


@register("G2", section="G", severity=Severity.BLOCKING,
          title="Each locale page is canonical to itself", estimate_ms=4_000)
async def g2(ctx: CheckContext) -> CheckResult:
    cluster = await _build_cluster(ctx)
    if not cluster["alternates"]:
        return _no_cluster()
    bad: list[str] = []
    missing: list[str] = []
    for url, data in cluster["per_locale"].items():
        if data["status"] != 200:
            continue  # G1 already flags non-200s; skip canonical eval
        canon = data["canonical"]
        if not canon:
            missing.append(url)
            continue
        if normalize_url(canon) != normalize_url(url):
            bad.append(f"{url} canonical → {canon}")
    sub = [
        SubStep("Every locale has a canonical tag",
                Status.PASS if not missing else Status.FAIL,
                detail=f"missing canonical: {missing[:3]}" if missing else f"{len(cluster['per_locale'])} ok"),
        SubStep("Canonicals are self-references",
                Status.PASS if not bad else Status.FAIL,
                detail=f"cross-canonicals: {bad[:3]}" if bad else "all self-referential"),
    ]
    return CheckResult.from_substeps("Locale canonical self-reference.", sub)


@register("G3", section="G", severity=Severity.BLOCKING,
          title="hreflang annotations are reciprocal and complete", estimate_ms=8_000)
async def g3(ctx: CheckContext) -> CheckResult:
    cluster = await _build_cluster(ctx)
    if not cluster["alternates"]:
        # If supported_languages filtered out every declared alternate, the page
        # *does* declare a cluster — we just can't evaluate it under config. Surface
        # that explicitly instead of reporting NA, which would hide the situation.
        dropped_only = cluster.get("unsupported_dropped") or []
        if dropped_only:
            sample = ", ".join(f"{hl}→{u}" for hl, u in dropped_only[:3])
            return CheckResult(
                Status.NEEDS_REVIEW,
                summary=f"{len(dropped_only)} declared hreflang alternate(s) all dropped by supported_languages filter — cluster not evaluated.",
                details={"unsupported_dropped": dropped_only,
                         "sample": sample,
                         "advice": "If these locales are intentionally unsupported, mark this check Pass; otherwise add them to supported_languages in site_config.yaml."},
            )
        return _no_cluster()
    # Alternates declared but no locale was actually fetched — reciprocity substeps
    # below would pass vacuously (zero counter-examples ≠ all-good). Causes: every
    # locale fetch hit an SSRF guard or network error, or the only alternate was
    # `x-default` (skipped from locale_targets). Surface as Needs Review instead.
    if not cluster["per_locale"]:
        return CheckResult(
            Status.NEEDS_REVIEW,
            summary=f"{len(cluster['alternates'])} hreflang alternate(s) declared but no locale was successfully fetched — reciprocity not evaluated.",
            details={"alternates": [(hl, u) for hl, u in cluster["alternates"]],
                     "advice": "Either every locale URL failed to fetch (network/SSRF) or the only alternate is x-default. Inspect the alternates list and rerun."},
        )
    audited_set = cluster["audited_alternates_normalized"]
    audited_url = ctx.url
    # Audited page must self-reference in its own hreflang.
    self_in_audited = any(normalize_url(u) == normalize_url(audited_url) for _, u in cluster["alternates"])

    asymmetric: list[str] = []
    empty_hreflang: list[str] = []
    for url, data in cluster["per_locale"].items():
        if data["status"] != 200:
            continue
        their_set = _normalize_alt_set(data["alternates"])
        if not their_set:
            empty_hreflang.append(url)
            continue
        if their_set != audited_set:
            missing_from_them = audited_set - their_set
            extra_in_them = their_set - audited_set
            asymmetric.append(
                f"{url}: missing {len(missing_from_them)}, extra {len(extra_in_them)}"
            )

    # Each unique target URL across the cluster must return 200.
    all_targets: set[str] = {url for _, url in cluster["alternates"] if not _ or _.lower() != "x-default"}
    all_targets.add(audited_url)
    bad_status: list[str] = []
    for u, data in cluster["per_locale"].items():
        if data["status"] != 200 and u in all_targets:
            bad_status.append(f"{u} status={data['status']}")

    sub = [
        SubStep(
            "Audited page self-references in hreflang",
            Status.PASS if self_in_audited else Status.FAIL,
            detail=f"self-reference present" if self_in_audited else "audited URL missing from its own hreflang set",
        ),
        SubStep(
            "Every locale page returns hreflang declarations",
            Status.PASS if not empty_hreflang else Status.FAIL,
            detail=f"empty: {empty_hreflang[:3]}" if empty_hreflang else f"{len(cluster['per_locale'])} ok",
        ),
        SubStep(
            "Cluster is reciprocal (every locale declares the identical set)",
            Status.PASS if not asymmetric else Status.FAIL,
            detail=f"asymmetric: {asymmetric[:3]}" if asymmetric else f"all {len(cluster['per_locale'])} sets match",
        ),
        SubStep(
            "Every cluster URL returns 200",
            Status.PASS if not bad_status else Status.FAIL,
            detail=f"bad: {bad_status[:3]}" if bad_status else f"{len(all_targets)} URLs all 200",
        ),
    ]
    # Surface filter-dropped + MAX_LOCALES-truncated locales so reciprocity verdicts
    # aren't trusted blindly when the evaluated set is smaller than what the page declares.
    dropped = cluster.get("unsupported_dropped") or []
    truncated = cluster.get("truncated_locales") or []
    if dropped or truncated:
        bits: list[str] = []
        if dropped:
            sample = ", ".join(f"{hl}→{u}" for hl, u in dropped[:3])
            bits.append(f"{len(dropped)} dropped by supported_languages filter (e.g. {sample})")
        if truncated:
            bits.append(
                f"{len(truncated)} truncated past MAX_LOCALES={MAX_LOCALES} "
                f"(e.g. {', '.join(truncated[:3])})"
            )
        sub.append(SubStep(
            "Full locale set evaluated",
            Status.NEEDS_REVIEW,
            detail="; ".join(bits),
        ))
    result = CheckResult.from_substeps(
        f"hreflang reciprocity ({len(cluster['per_locale'])} locale(s)).",
        sub,
    )
    result.details["unsupported_dropped"] = dropped
    result.details["truncated_locales"] = truncated
    return result


@register("G4", section="G", severity=Severity.RECOMMENDED,
          title="x-default exists where appropriate", estimate_ms=2_000)
async def g4(ctx: CheckContext) -> CheckResult:
    cluster = await _build_cluster(ctx)
    if not cluster["alternates"]:
        return _no_cluster()
    x_default_targets = [u for hl, u in cluster["alternates"] if hl.lower() == "x-default"]
    if not x_default_targets:
        return CheckResult(Status.FAIL, summary="No x-default declared in hreflang set.")
    # x-default href should typically be the canonical/English version.
    target = x_default_targets[0]
    canon_target_match = (normalize_url(target) == normalize_url(ctx.url))
    sub = [
        SubStep("x-default declared", Status.PASS, detail=f"href={target}"),
        SubStep(
            "x-default points to the audited (canonical/English) URL",
            Status.PASS if canon_target_match else Status.NEEDS_REVIEW,
            detail=("matches audited URL" if canon_target_match
                    else f"x-default→{target}, audited={ctx.url} (acceptable if audited isn't the global default)"),
        ),
    ]
    return CheckResult.from_substeps("x-default presence.", sub)


@register("G5", section="G", severity=Severity.BLOCKING,
          title="Locale alternates use hreflang, not redirects/canonicals", estimate_ms=6_000)
async def g5(ctx: CheckContext) -> CheckResult:
    cluster = await _build_cluster(ctx)
    if not cluster["alternates"]:
        return _no_cluster()
    redirects: list[str] = []
    cross_canonicals: list[str] = []
    for url, data in cluster["per_locale"].items():
        if data["status"] in (301, 302, 307, 308):
            loc = data["location"]
            # Redirect to a different locale URL within the cluster is the failure mode.
            if any(normalize_url(loc) == normalize_url(u) for _, u in cluster["alternates"]):
                redirects.append(f"{url} → {loc}")
            else:
                redirects.append(f"{url} → {loc} (off-cluster)")
            continue
        if data["status"] != 200:
            continue
        canon = data["canonical"]
        if canon and normalize_url(canon) != normalize_url(url):
            # Canonical points elsewhere — is it another locale in the cluster?
            if any(normalize_url(canon) == normalize_url(u) for _, u in cluster["alternates"]):
                cross_canonicals.append(f"{url} canonical→{canon}")
    sub = [
        SubStep(
            "No locale URL 301/302s to another locale",
            Status.PASS if not redirects else Status.FAIL,
            detail=f"redirects: {redirects[:3]}" if redirects else "no redirects in cluster",
        ),
        SubStep(
            "No locale's canonical points at another locale",
            Status.PASS if not cross_canonicals else Status.FAIL,
            detail=f"cross-canonicals: {cross_canonicals[:3]}" if cross_canonicals else "all self-canonical",
        ),
    ]
    return CheckResult.from_substeps("Locale variants linked via hreflang only.", sub)


@register("G6", section="G", severity=Severity.BLOCKING,
          title="Page is not locale-adaptive in a way that hides versions from crawlers",
          estimate_ms=8_000)
async def g6(ctx: CheckContext) -> CheckResult:
    cluster = await _build_cluster(ctx)
    if not cluster["alternates"]:
        return _no_cluster()
    # Pick a non-default locale to test against (use the first non-x-default in cluster).
    test_lang = "ar"
    for hl, _ in cluster["alternates"]:
        if hl.lower() not in ("x-default", "en", "en-us", "en-gb"):
            test_lang = hl
            break

    # Baseline: Googlebot UA, no Accept-Language, no cookies.
    baseline = await fetch(ctx, user_agent=UA_GOOGLEBOT, key_suffix="googlebot")
    # Variant: Accept-Language set to non-default locale.
    from ._utils import _fetch  # private fetch lets us pass extra headers without polluting cache key
    variant_lang = await _fetch(
        ctx.url, user_agent=UA_GOOGLEBOT, extra_headers={"Accept-Language": test_lang}
    )
    variant_cookie = await _fetch(
        ctx.url, user_agent=UA_GOOGLEBOT, extra_headers={"Cookie": f"locale={test_lang}"}
    )

    def _signature(html: str) -> str:
        # Comparison signature: title + canonical + h1, which is enough to tell a swap from the same page.
        title = (re.search(r"<title[^>]*>([^<]*)</title>", html or "", re.I) or [None, ""])[1].strip()
        canon = (re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]*)"', html or "", re.I) or [None, ""])[1].strip()
        h1 = (re.search(r"<h1[^>]*>([^<]*)</h1>", html or "", re.I) or [None, ""])[1].strip()
        return f"{title}||{canon}||{h1}"

    base_sig = _signature(baseline.text)
    lang_sig = _signature(variant_lang.text)
    cookie_sig = _signature(variant_cookie.text)
    sub = [
        SubStep(
            "Baseline (Googlebot, no Accept-Language) returns 200",
            Status.PASS if baseline.status_code == 200 else Status.FAIL,
            detail=f"status={baseline.status_code}",
        ),
        SubStep(
            f"Accept-Language: {test_lang} does not switch content",
            Status.PASS if base_sig == lang_sig else Status.FAIL,
            detail=(f"baseline title/canonical/h1 = variant" if base_sig == lang_sig
                    else f"diverged — content switched via Accept-Language"),
        ),
        SubStep(
            f"Cookie locale={test_lang} does not switch content",
            Status.PASS if base_sig == cookie_sig else Status.FAIL,
            detail=(f"baseline = cookie variant" if base_sig == cookie_sig
                    else "diverged — content switched via cookie"),
        ),
    ]
    # Geo redirect: cerberus's outbound IP IS the vantage. If the audited URL redirects to
    # a different path/host from this vantage, treat it as evidence of geo-routing.
    # Trailing-slash AND scheme differences are stripped (TLS upgrades, e.g. http→https on
    # the same path, are not geo-redirects). Operators in a US datacenter won't get useful
    # signal here; everywhere else this catches the common case.
    def _host_path(url: str) -> str:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        port = p.port
        # Drop default ports so http://h:80 and https://h match on a TLS upgrade.
        if (p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443):
            port = None
        netloc = f"{host}:{port}" if port else host
        path = p.path.rstrip("/") or "/"
        return f"{netloc}{path}"

    vantage = await _vantage_info()
    redirected = (baseline.final_url
                  and _host_path(baseline.final_url) != _host_path(ctx.url))
    if redirected:
        sub.append(SubStep(
            f"No geo-based redirect from {vantage['country']} vantage ({vantage['ip']})",
            Status.FAIL,
            detail=f"audited URL redirected to {baseline.final_url}; chain: {baseline.history[:3]}",
        ))
    else:
        sub.append(SubStep(
            f"No geo-based redirect from {vantage['country']} vantage ({vantage['ip']})",
            Status.PASS,
            detail=f"baseline stayed at {ctx.url}",
        ))
    return CheckResult.from_substeps("Locale-adaptive serving.", sub)
