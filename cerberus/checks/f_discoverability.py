"""F — Discoverability, Crawling & Access."""
from __future__ import annotations

import re
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from protego import Protego

from ._utils import (
    UA_CHROME_DESKTOP,
    UA_GOOGLEBOT,
    fetch,
    fetch_url,
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

F9_STEP4_INSTRUCTION = (
    "Log into the Cloudflare or CDN dashboard. Confirm no WAF rule or bot-management rule blocks "
    "or rate-limits the IP ranges of bots in the allowed-bots list."
)


def _sm_xmlns_strip(xml: str) -> str:
    """Strip default namespace so we can search with simple XPath."""
    return re.sub(r'\sxmlns="[^"]+"', "", xml, count=1)


async def _gather_sitemap_urls(ctx: CheckContext, sitemap_url: str, depth: int = 0) -> tuple[list[str], list[str]]:
    """Returns (urls, errors). Recurses into sitemap-index files (one level)."""
    if depth > 2:
        return ([], [f"max sitemap nesting reached at {sitemap_url}"])
    r = await fetch_url(ctx, sitemap_url)
    if r.status_code != 200:
        return ([], [f"{sitemap_url} returned {r.status_code}"])
    try:
        text = _sm_xmlns_strip(r.text)
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return ([], [f"{sitemap_url} parse error: {exc}"])
    if root.tag.endswith("sitemapindex"):
        urls: list[str] = []
        errs: list[str] = []
        for sm in root.findall(".//sitemap/loc"):
            sub_url = (sm.text or "").strip()
            if not sub_url:
                continue
            sub_urls, sub_errs = await _gather_sitemap_urls(ctx, sub_url, depth + 1)
            urls.extend(sub_urls)
            errs.extend(sub_errs)
        return (urls, errs)
    return (
        [(loc.text or "").strip() for loc in root.findall(".//url/loc") if loc.text],
        [],
    )


@register("F1", section="F", severity=Severity.BLOCKING,
          title="Page is in the sitemap", estimate_ms=8_000,
          applicable_envs={Env.PRODUCTION})
async def f1(ctx: CheckContext) -> CheckResult:
    host_cfg = ctx.site_config.for_url(ctx.url)
    if not host_cfg.sitemap_url:
        # Fallback: try to discover from robots.txt. If not declared there either, fail with
        # an explicit config-missing message (per brief TC4: "site config missing.").
        robots_r = await fetch_url(ctx, host_cfg.robots_url_resolved())
        m = re.search(r"^Sitemap:\s*(.+)$", robots_r.text or "", re.I | re.M)
        if not m:
            return CheckResult(
                Status.FAIL,
                summary=f"site config missing sitemap_url for host '{host_cfg.host}'.",
                details={"hint": f"Add hosts.{host_cfg.host}.sitemap_url to site_config.yaml, "
                                 f"or add a Sitemap: directive to robots.txt."},
            )
        sitemap_url = m.group(1).strip()
    else:
        sitemap_url = host_cfg.sitemap_url

    urls, errors = await _gather_sitemap_urls(ctx, sitemap_url)
    target = normalize_url(ctx.url)
    matches = [u for u in urls if normalize_url(u) == target]
    # If we couldn't enumerate a complete sitemap and the URL is missing, the missing-from-sitemap
    # finding is unreliable — report Needs Review with the underlying sitemap failure surfaced.
    incomplete = bool(errors) and not matches
    sub = [
        SubStep("Sitemap reachable + parses",
                Status.PASS if not errors else Status.FAIL,
                detail=f"sitemap: {sitemap_url}; URLs found: {len(urls)}" + (f"; errors: {errors[:3]}" if errors else "")),
        SubStep(
            "URL appears in sitemap",
            Status.PASS if matches else (Status.NEEDS_REVIEW if incomplete else Status.FAIL),
            detail=(f"matches: {len(matches)}; sample sitemap entries: {urls[:3]}"
                    if not incomplete else
                    f"could not verify: child sitemap unreachable ({errors[:2]})"),
        ),
        SubStep(
            "URL appears exactly once",
            Status.PASS if len(matches) == 1 else (Status.FAIL if len(matches) > 1 else Status.NA),
            detail=f"count: {len(matches)}",
        ),
    ]
    return CheckResult.from_substeps(f"Sitemap entry for {target}.", sub)


@register("F2", section="F", severity=Severity.RECOMMENDED,
          title="Sitemap is declared in robots.txt", estimate_ms=4_000,
          applicable_envs={Env.PRODUCTION})
async def f2(ctx: CheckContext) -> CheckResult:
    host_cfg = ctx.site_config.for_url(ctx.url)
    r = await fetch_url(ctx, host_cfg.robots_url_resolved())
    if r.status_code != 200:
        return CheckResult(Status.FAIL,
                           summary=f"robots.txt returned {r.status_code} at {host_cfg.robots_url_resolved()}")
    declared = re.findall(r"^Sitemap:\s*(.+)$", r.text or "", re.I | re.M)
    if not declared:
        return CheckResult(Status.FAIL, summary="No Sitemap: directive in robots.txt.")
    return CheckResult(Status.PASS, summary=f"{len(declared)} Sitemap directive(s).",
                       details={"sitemaps": [s.strip() for s in declared]})


@register("F3", section="F", severity=Severity.BLOCKING,
          title="robots.txt does not block the page", estimate_ms=4_000,
          applicable_envs={Env.PRODUCTION, Env.UAT})
async def f3(ctx: CheckContext) -> CheckResult:
    host_cfg = ctx.site_config.for_url(ctx.url)
    robots_url = host_cfg.robots_url_resolved()
    r = await fetch_url(ctx, robots_url)
    if r.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"{robots_url} returned {r.status_code}")
    try:
        rp = Protego.parse(r.text)
    except Exception as exc:
        return CheckResult(Status.FAIL, summary=f"protego parse error: {exc}")
    bots = host_cfg.allowed_bots or ["Googlebot", "Bingbot"]
    blocked = [b for b in bots if not rp.can_fetch(ctx.url, b)]
    sub: list[SubStep] = [
        SubStep("robots.txt reachable + parseable", Status.PASS, detail=f"size: {len(r.text)} bytes"),
        SubStep(
            f"page allowed for all configured bots ({len(bots)})",
            Status.PASS if not blocked else Status.FAIL,
            detail=f"blocked for: {blocked}" if blocked else f"allowed for: {bots}",
        ),
    ]
    # Static resources: collect /-prefixed src/href.
    page = await fetch(ctx)
    static_paths: set[str] = set()
    for m in re.finditer(r'(?:src|href)="(/[^"#?]*\.(?:js|css|png|jpg|jpeg|webp|svg|gif))', page.text or ""):
        static_paths.add(m.group(1))
    bot_for_static = "Googlebot"
    blocked_static = [p for p in static_paths if not rp.can_fetch(f"{urlparse(ctx.url).scheme}://{urlparse(ctx.url).hostname}{p}", bot_for_static)]
    sub.append(SubStep(
        f"static resources allowed for {bot_for_static}",
        Status.PASS if not blocked_static else Status.FAIL,
        detail=f"blocked: {blocked_static[:5]}" if blocked_static else f"checked {len(static_paths)} resources",
    ))
    return CheckResult.from_substeps("robots.txt allow check.", sub)


@register("F4", section="F", severity=Severity.BLOCKING,
          title="Bot access is not blocked outside robots.txt", estimate_ms=6_000,
          applicable_envs={Env.PRODUCTION})
async def f4(ctx: CheckContext) -> CheckResult:
    bot_r = await fetch(ctx, user_agent=UA_GOOGLEBOT, key_suffix="googlebot")
    user_r = await fetch(ctx)
    sub: list[SubStep] = []
    sub.append(SubStep(
        "Googlebot fetch returns 200",
        Status.PASS if bot_r.status_code == 200 else Status.FAIL,
        detail=f"bot status={bot_r.status_code}; user status={user_r.status_code}",
    ))
    challenge_indicators = ["cf-challenge", "captcha", "are you human", "challenge-platform", "/cdn-cgi/"]
    body_lower = (bot_r.text or "").lower()
    suspicious = [s for s in challenge_indicators if s in body_lower]
    sub.append(SubStep(
        "No challenge/captcha markers in bot response",
        Status.PASS if not suspicious else Status.FAIL,
        detail=f"matches: {suspicious}" if suspicious else "clean",
    ))
    # Compare body sizes — wildly different may indicate cloaking/blocking.
    if bot_r.status_code == 200 and user_r.status_code == 200:
        ratio = (len(bot_r.text) + 1) / (len(user_r.text) + 1)
        if ratio < 0.5 or ratio > 2.0:
            sub.append(SubStep("Body size parity (bot vs user)", Status.NEEDS_REVIEW,
                               detail=f"bot={len(bot_r.text)}B user={len(user_r.text)}B ratio={ratio:.2f}"))
        else:
            sub.append(SubStep("Body size parity (bot vs user)", Status.PASS,
                               detail=f"bot={len(bot_r.text)}B user={len(user_r.text)}B ratio={ratio:.2f}"))
    return CheckResult.from_substeps("Bot access (outside robots.txt).", sub)


@register("F5", section="F", severity=Severity.BLOCKING,
          title="Invalid URLs return proper 404 or 410", estimate_ms=4_000)
async def f5(ctx: CheckContext) -> CheckResult:
    host_cfg = ctx.site_config.for_url(ctx.url)
    if not host_cfg.pipeline_url_prefixes:
        return CheckResult(
            Status.FAIL,
            summary=f"site config missing pipeline_url_prefixes for host '{host_cfg.host}'.",
            details={"hint": f"Add hosts.{host_cfg.host}.pipeline_url_prefixes to site_config.yaml — "
                             f"a list of URL path prefixes for this pipeline (e.g. ['/s/'])."},
        )
    p = urlparse(ctx.url)
    prefixes = host_cfg.pipeline_url_prefixes
    nonexistent = f"{p.scheme}://{p.hostname}{prefixes[0].rstrip('/')}/cerberus-nonexistent-{abs(hash(ctx.url)) % 100000}"
    r = await fetch_url(ctx, nonexistent, follow_redirects=False)
    if r.status_code in (404, 410):
        return CheckResult(Status.PASS, summary=f"{nonexistent} → {r.status_code}",
                           details={"tested_url": nonexistent, "status": r.status_code})
    return CheckResult(Status.FAIL,
                       summary=f"{nonexistent} returned {r.status_code} (expected 404/410)",
                       details={"tested_url": nonexistent, "status": r.status_code,
                                "location": r.headers.get("location", "")})


@register("F6", section="F", severity=Severity.BLOCKING,
          title="Page is internally linked from at least one indexable page and links to others",
          estimate_ms=500)
async def f6(_: CheckContext) -> CheckResult:
    # Per brief: F6 reports N/A until hub architecture exists. The "page is linked from a parent
    # hub" half can't be evaluated without a hub model, and the brief explicitly lists F6 as a
    # skeleton-N/A check. Always N/A in MVP.
    return CheckResult(Status.NA, summary="N/A until hub architecture exists (per brief).")


@register("F7", section="F", severity=Severity.BLOCKING,
          title="Internal links to the page are crawlable HTML links",
          estimate_ms=2_000)
async def f7(ctx: CheckContext) -> CheckResult:
    return CheckResult(Status.NA,
                       summary="Requires parent/hub page identification (per brief, hub architecture pending).")


@register("F8", section="F", severity=Severity.RECOMMENDED,
          title="Internal links use stable preferred URLs", estimate_ms=8_000)
async def f8(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    host = (urlparse(ctx.url).hostname or "").lower()
    internal: list[str] = []
    for a in r.soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/") or (host in href):
            internal.append(href)
    if not internal:
        return CheckResult(Status.NA, summary="No internal <a href> on page.")
    bad_redirects: list[str] = []
    tracked: list[str] = []
    sample = internal[:25]  # cap to avoid 1000s of HEADs
    for href in sample:
        if "?" in href and re.search(r"\?(utm_|sid=|session=|ref=)", href, re.I):
            tracked.append(href)
        absolute = href if href.startswith("http") else f"{urlparse(ctx.url).scheme}://{host}{href}"
        head = await fetch_url(ctx, absolute, method="HEAD", follow_redirects=False)
        if head.status_code in (301, 302, 307, 308):
            bad_redirects.append(f"{absolute} → {head.headers.get('location','')!r}")
    sub = [
        SubStep("No internal links go through redirects",
                Status.PASS if not bad_redirects else Status.FAIL,
                detail=f"sample: {bad_redirects[:3]}" if bad_redirects else f"checked {len(sample)} of {len(internal)}"),
        SubStep("No tracking params in internal links",
                Status.PASS if not tracked else Status.FAIL,
                detail=f"sample: {tracked[:3]}" if tracked else "clean"),
    ]
    return CheckResult.from_substeps("Internal link health.", sub)


@register("F9", section="F", severity=Severity.BLOCKING,
          title="AI crawler/search policy is intentional", estimate_ms=4_000,
          applicable_envs={Env.PRODUCTION})
async def f9(ctx: CheckContext) -> CheckResult:
    host_cfg = ctx.site_config.for_url(ctx.url)
    if not host_cfg.allowed_bots:
        return CheckResult(
            Status.FAIL,
            summary=f"site config missing allowed_bots for host '{host_cfg.host}'.",
            details={"hint": "Add allowed_bots under defaults: or hosts.<host>: in site_config.yaml."},
        )
    r = await fetch_url(ctx, host_cfg.robots_url_resolved())
    if r.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"robots.txt returned {r.status_code}")
    text = r.text or ""
    sub: list[SubStep] = []
    bots = host_cfg.allowed_bots
    rp = Protego.parse(text)
    blocked: list[str] = []
    explicit: list[str] = []
    for bot in bots:
        if re.search(rf"(?im)^User-agent:\s*{re.escape(bot)}\s*$", text):
            explicit.append(bot)
        if not rp.can_fetch(ctx.url, bot):
            blocked.append(bot)
    sub.append(SubStep(
        "Each allowed bot has an explicit User-agent block",
        Status.PASS if len(explicit) == len(bots) and bots else Status.NEEDS_REVIEW,
        detail=f"explicit: {explicit}; missing: {[b for b in bots if b not in explicit]}",
    ))
    sub.append(SubStep(
        "No allowed bot is Disallowed for this URL",
        Status.PASS if not blocked else Status.FAIL,
        detail=f"blocked: {blocked}" if blocked else "all clear",
    ))
    sub.append(SubStep("CDN/WAF policy review (manual)", Status.MANUAL,
                       detail="See instructions.", instruction=F9_STEP4_INSTRUCTION))
    result = CheckResult.from_substeps("AI crawler policy.", sub)
    result.instruction = F9_STEP4_INSTRUCTION
    return result


@register("F10", section="F", severity=Severity.BLOCKING,
          title="Sitemap URL reachable and valid", estimate_ms=4_000,
          applicable_envs={Env.PRODUCTION})
async def f10(ctx: CheckContext) -> CheckResult:
    host_cfg = ctx.site_config.for_url(ctx.url)
    if not host_cfg.sitemap_url:
        return CheckResult(Status.NA, summary=f"No sitemap_url configured for {host_cfg.host}.")
    r = await fetch_url(ctx, host_cfg.sitemap_url)
    if r.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"sitemap returned {r.status_code}")
    ct = (r.headers.get("content-type") or "").lower()
    if "xml" not in ct:
        return CheckResult(Status.FAIL, summary=f"sitemap content-type is {ct!r}, expected xml.",
                           details={"content_type": ct})
    try:
        ET.fromstring(r.text)
        return CheckResult(Status.PASS, summary=f"sitemap reachable + parses ({len(r.text)} bytes).",
                           details={"size": len(r.text)})
    except ET.ParseError as exc:
        return CheckResult(Status.FAIL, summary=f"sitemap XML parse error: {exc}")


@register("F11", section="F", severity=Severity.BLOCKING,
          title="robots.txt reachable and valid", estimate_ms=2_000)
async def f11(ctx: CheckContext) -> CheckResult:
    host_cfg = ctx.site_config.for_url(ctx.url)
    r = await fetch_url(ctx, host_cfg.robots_url_resolved())
    if r.status_code != 200:
        return CheckResult(Status.FAIL, summary=f"robots.txt returned {r.status_code}")
    ct = (r.headers.get("content-type") or "").lower()
    has_ua = bool(re.search(r"(?im)^User-agent:", r.text or ""))
    sub = [
        SubStep("Returns 200 with text/plain", Status.PASS if "text/plain" in ct else Status.FAIL,
                detail=f"content-type: {ct!r}"),
        SubStep("Has at least one User-agent directive",
                Status.PASS if has_ua else Status.FAIL,
                detail=f"size: {len(r.text)}B"),
    ]
    return CheckResult.from_substeps("robots.txt validity.", sub)
