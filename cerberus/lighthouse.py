"""Lighthouse subprocess wrapper. One run per device; metrics reused across E1-E3b.

Note: uses asyncio.create_subprocess_exec with list args (no shell) — safe from injection.
URL is passed positionally; not interpolated into a shell command string.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Any

import httpx

from .config import LighthouseConfig

log = logging.getLogger(__name__)

# Key on CheckContext.cache where the fixture future/result lives.
FIXTURE_KEY = "lighthouse_fixture"

# PageSpeed Insights API: runs Lighthouse on Google's infra (matches pagespeed.web.dev).
# One call per strategy; target URL must be publicly reachable by Google.
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
# Total must stay under runner.PER_CHECK_TIMEOUT_S (90s): the E-checks await this
# fixture inside their own per-check budget, so a longer PSI ceiling would just
# surface as a check timeout instead of a clean per-device error.
PSI_TIMEOUT = httpx.Timeout(75.0, connect=10.0)
PSI_CATEGORIES = ("performance", "seo", "accessibility", "best-practices")


def _lighthouse_bin() -> str | None:
    return shutil.which("lighthouse")


def _chrome_path() -> str | None:
    """Find a usable Chromium binary. Prefer Playwright's bundled one, then system Chrome."""
    candidates = [
        os.environ.get("CHROME_PATH"),
        os.environ.get("CHROMIUM_PATH"),
    ]
    # Playwright cache layout.
    pw_cache = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(pw_cache):
        for entry in sorted(os.listdir(pw_cache), reverse=True):
            if entry.startswith("chromium-"):
                p = os.path.join(pw_cache, entry, "chrome-linux", "chrome")
                if os.path.exists(p):
                    candidates.append(p)
                    break
    candidates.extend([
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ])
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _build_cmd(bin_path: str, url: str, out_path: str, device: str) -> list[str]:
    base = [
        bin_path,
        url,
        "--output=json",
        f"--output-path={out_path}",
        "--quiet",
        "--chrome-flags=--headless --no-sandbox",
        "--throttling-method=devtools",
        "--only-categories=performance,seo,accessibility,best-practices",
    ]
    if device == "desktop":
        base.append("--preset=desktop")
    else:
        base.append("--form-factor=mobile")
    return base


async def _run(url: str, device: str) -> dict[str, Any]:
    """device: 'mobile' | 'desktop'. Returns parsed JSON or {error}."""
    from .checks._utils import UnsafeTargetURL, validate_target_url
    try:
        await validate_target_url(url)
    except UnsafeTargetURL as exc:
        return {"error": f"unsafe target: {exc}"}

    bin_path = _lighthouse_bin()
    if not bin_path:
        return {"error": "lighthouse CLI not on PATH; install via `npm i -g lighthouse`"}

    chrome = _chrome_path()
    if not chrome:
        return {"error": "no chromium found; run `python -m playwright install chromium` or install google-chrome"}

    env = os.environ.copy()
    env["CHROME_PATH"] = chrome

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "report.json")
        cmd = _build_cmd(bin_path, url, out_path, device)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            except asyncio.TimeoutError:
                proc.kill()
                return {"error": "lighthouse timed out (>180s)"}
            if proc.returncode != 0:
                return {"error": f"lighthouse exited {proc.returncode}: {stderr.decode()[:500]}"}
            with open(out_path) as f:
                report = json.load(f)
            return _extract(report)
        except FileNotFoundError as exc:
            return {"error": f"lighthouse spawn failed: {exc}"}


def _format_audit_item(it: dict) -> str | None:
    """Render a Lighthouse ``details.items[i]`` entry as one readable line.

    SEO-category audits use at least four item shapes — picking the wrong field
    silently surfaces a Python dict repr (``{'type': 'source-location', ...}``)
    or a bare URL when the actual signal lives elsewhere.
    """
    # Node-nested: image-alt, crawlable-anchors, tap-targets, font-size.
    node = it.get("node") if isinstance(it.get("node"), dict) else None
    if node:
        snip = node.get("snippet") or node.get("nodeLabel")
        if snip:
            return str(snip)
    # Link-shaped: link-text. Format as ``"visible text" → href`` so the
    # operator sees the failing anchor text alongside its destination.
    if "text" in it and "href" in it:
        return f'"{it["text"]}" → {it["href"]}'
    # Source-location wrapper: is-crawlable, robots-txt — points to a file/line.
    src = it.get("source") if isinstance(it.get("source"), dict) else None
    if src and src.get("type") == "source-location":
        url = src.get("url", "")
        line = src.get("line")
        col = src.get("column")
        if line is not None:
            return f"{url}:{line}:{col}" if col is not None else f"{url}:{line}"
        return url or None
    # Plain string fields as last resort.
    for k in ("source", "href", "url"):
        v = it.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _slim_seo_audit(a: dict) -> dict[str, Any]:
    """Minimal slice of a Lighthouse audit for SEO drill-down."""
    details = a.get("details")
    raw_items = details.get("items") if isinstance(details, dict) else None
    items = (raw_items if isinstance(raw_items, list) else [])[:3]
    snippets: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        snip = _format_audit_item(it)
        if snip:
            snippets.append(snip[:200])
    return {
        "score": a.get("score"),
        "scoreDisplayMode": a.get("scoreDisplayMode"),
        "title": a.get("title"),
        "errorMessage": a.get("errorMessage"),
        "snippets": snippets,
    }


def _extract(report: dict) -> dict[str, Any]:
    cats = report.get("categories", {})
    audits = report.get("audits", {})

    def num(audit_id: str) -> float | None:
        a = audits.get(audit_id) or {}
        v = a.get("numericValue")
        return float(v) if v is not None else None

    seo_audits: dict[str, dict[str, Any]] = {}
    for ref in cats.get("seo", {}).get("auditRefs", []) or []:
        aid = ref.get("id")
        a = audits.get(aid) if aid else None
        if aid and a:
            seo_audits[aid] = _slim_seo_audit(a)

    return {
        "scores": {
            "performance": cats.get("performance", {}).get("score"),
            "seo": cats.get("seo", {}).get("score"),
            "accessibility": cats.get("accessibility", {}).get("score"),
            "best-practices": cats.get("best-practices", {}).get("score"),
        },
        "metrics": {
            "lcp_ms": num("largest-contentful-paint"),
            "cls": num("cumulative-layout-shift"),
            "inp_ms": num("interaction-to-next-paint") or num("experimental-interaction-to-next-paint"),
            "fcp_ms": num("first-contentful-paint"),
            "tbt_ms": num("total-blocking-time"),
            "tti_ms": num("interactive"),
        },
        "seo_audits": seo_audits,
    }


async def _psi_run(client: httpx.AsyncClient, url: str, device: str, api_key: str | None) -> dict[str, Any]:
    """One PSI API call for one strategy. Returns parsed fixture dict or {error}.

    PSI's `lighthouseResult` field IS a Lighthouse report, so it feeds straight
    into `_extract` — the same shape the local CLI path produces.
    """
    params: list[tuple[str, str]] = [("url", url), ("strategy", device)]
    params += [("category", c) for c in PSI_CATEGORIES]
    if api_key:
        params.append(("key", api_key))
    try:
        resp = await client.get(PSI_ENDPOINT, params=params)
    except httpx.HTTPError as exc:
        return {"error": f"PSI request failed ({device}): {type(exc).__name__}: {exc}"}
    if resp.status_code != 200:
        msg = ""
        try:
            msg = (resp.json().get("error") or {}).get("message") or ""
        except Exception:
            msg = (resp.text or "")[:200]
        return {"error": f"PSI HTTP {resp.status_code} ({device}): {msg}".strip()}
    try:
        payload = resp.json()
    except Exception as exc:
        return {"error": f"PSI returned unparseable JSON ({device}): {exc}"}
    lhr = payload.get("lighthouseResult")
    if not isinstance(lhr, dict):
        return {"error": f"PSI response missing lighthouseResult ({device})"}
    runtime_err = lhr.get("runtimeError")
    if isinstance(runtime_err, dict) and runtime_err.get("code"):
        return {"error": f"PSI runtime error ({device}): {runtime_err.get('code')} "
                         f"{runtime_err.get('message', '')}".strip()}
    return _extract(lhr)


async def _build_fixture_psi(url: str, api_key: str | None) -> dict[str, Any]:
    """Fetch the fixture from the PageSpeed Insights API (mobile + desktop concurrently)."""
    # Validate before forwarding the URL to Google — keeps the PSI path consistent
    # with the local `_run` path's SSRF guard rather than silently exempting it.
    from .checks._utils import UnsafeTargetURL, validate_target_url
    try:
        await validate_target_url(url)
    except UnsafeTargetURL as exc:
        err = {"error": f"unsafe target: {exc}"}
        return {"mobile": dict(err), "desktop": dict(err)}
    log.info("lighthouse: fetching PSI fixture for %s (api key: %s)",
             url, "set" if api_key else "none — expect tight rate limits")
    async with httpx.AsyncClient(timeout=PSI_TIMEOUT) as client:
        mobile, desktop = await asyncio.gather(
            _psi_run(client, url, "mobile", api_key),
            _psi_run(client, url, "desktop", api_key),
        )
    return {"mobile": mobile, "desktop": desktop}


async def _build_fixture_local(url: str) -> dict[str, Any]:
    """Run the local `lighthouse` CLI for mobile + desktop concurrently.

    Each device spawns its own Chromium subprocess (Lighthouse picks a free debug port per run),
    so they don't conflict. Wall-clock for the fixture drops from ~mobile+desktop to ~max(mobile, desktop).
    Expect a CPU spike for the duration since both Chromium processes run simultaneously.
    """
    log.info("lighthouse: starting parallel local fixture for %s", url)
    mobile, desktop = await asyncio.gather(_run(url, "mobile"), _run(url, "desktop"))
    return {"mobile": mobile, "desktop": desktop}


async def build_fixture(url: str, lh_config: LighthouseConfig | None = None) -> dict[str, Any]:
    """Build the {mobile, desktop} performance fixture consumed by checks E1–E3b.

    Source is chosen by `lh_config.source`: "psi" (PageSpeed Insights API) or
    "local" (local lighthouse CLI). Defaults to local when no config is passed.
    Each device value is either parsed metrics/scores/seo_audits or {"error": ...};
    the E-checks already degrade gracefully on the error shape.
    """
    lh_config = lh_config or LighthouseConfig()
    if lh_config.source == "psi":
        api_key = os.environ.get(lh_config.psi_api_key_env, "").strip() or None
        return await _build_fixture_psi(url, api_key)
    return await _build_fixture_local(url)
