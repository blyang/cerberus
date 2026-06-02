"""Shared Playwright Chromium pool. One browser per process; new context per run."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .checks._utils import (
    UA_CHROME_DESKTOP_TEMPLATE,
    UA_GOOGLEBOT_WRS_TEMPLATE,
    UnsafeTargetURL,
    validate_target_url,
)

log = logging.getLogger(__name__)

_browser_lock = asyncio.Lock()
_playwright: Any = None
_browser: Any = None


async def get_browser() -> Any:
    """Acquire (or initialise) the shared browser. Prefers stable Chrome channel
    (matches Google's evergreen WRS) and falls back to bundled Chromium when Chrome
    isn't installed. Cleans up partial state on failure."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is not None:
            return _browser
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        launch_args = {"headless": True, "args": ["--no-sandbox"]}
        try:
            try:
                browser = await pw.chromium.launch(channel="chrome", **launch_args)
                channel = "chrome"
            except Exception as exc:
                log.warning(
                    "browser: stable Chrome channel unavailable (%s); falling back to bundled Chromium. "
                    "Run `playwright install chrome` for closer parity with Google's WRS.",
                    exc,
                )
                browser = await pw.chromium.launch(**launch_args)
                channel = "chromium-bundled"
            # Stash the channel on the browser handle so it travels with the launched instance
            # — avoids a parallel global that has to be reset in lockstep with _browser.
            browser._cerberus_channel = channel
        except Exception:
            try:
                await pw.stop()
            except Exception:
                pass
            raise
        _playwright = pw
        _browser = browser
        return _browser


def _chrome_version_from(version_string: str) -> str:
    """Extract the dotted Chrome version (e.g. '120.0.6099.71') from Browser.version()."""
    m = re.search(r"\d+(?:\.\d+){2,3}", version_string or "")
    return m.group(0) if m else "120.0.0.0"


async def _versioned_ua(template: str) -> str:
    browser = await get_browser()
    return template.format(chrome_version=_chrome_version_from(browser.version))


async def googlebot_wrs_ua() -> str:
    """Googlebot WRS UA pinned to the live launched-browser Chrome version (matches Google's
    evergreen WRS shape: Chrome-versioned compatible token)."""
    return await _versioned_ua(UA_GOOGLEBOT_WRS_TEMPLATE)


async def chrome_desktop_ua() -> str:
    """Desktop Chrome UA pinned to the live launched-browser Chrome version. Pair with
    googlebot_wrs_ua() so bot-vs-user diffs aren't confounded by Chrome-version skew."""
    return await _versioned_ua(UA_CHROME_DESKTOP_TEMPLATE)


# Detects text rendered with classic SEO-cloaking style signatures via *computed* style —
# white-on-white, off-screen text-indent, or sub-1px font. Deliberately NOT display:none /
# visibility:hidden (those dominate legit UI: menus, tabs, accordions, sr-only) so H2 stays
# low-false-positive. Returns up to 20 {reason, sample} records.
_HIDDEN_TEXT_JS = """
() => {
  const out = [];
  // Alpha-aware: a fully-transparent color (alpha 0, incl. the default 'rgba(0,0,0,0)') is
  // NOT an opaque [0,0,0] — return null so we keep looking up the stack. Without this, a page
  // with a default transparent <body> reads as black background and ALL black text flags.
  const parse = s => {
    const m = (s || '').match(/[\\d.]+/g);
    if (!m) return null;
    if (m.length >= 4 && Number(m[3]) === 0) return null;
    return m.slice(0, 3).map(Number);
  };
  const near = (a, b) => a && b && (Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1]) + Math.abs(a[2]-b[2])) < 30;
  // Whether `color` matches the effective background: walk ancestors for the first opaque
  // background-color. Bail (return false) if any ancestor paints a background-image/gradient
  // before an opaque color is found — the visible backdrop is then an image we can't sample,
  // so light-text-over-hero is NOT white-on-white. White is assumed only as the canvas default.
  const colorMatchesBg = el => {
    const col = parse(getComputedStyle(el).color);
    if (!col) return false;
    let p = el;
    while (p && p.nodeType === 1) {
      const pcs = getComputedStyle(p);
      if (pcs.backgroundImage && pcs.backgroundImage !== 'none') return false;
      const c = parse(pcs.backgroundColor);
      if (c) return near(col, c);
      p = p.parentElement;
    }
    return near(col, [255, 255, 255]);
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
  let node; const seen = new Set();
  while ((node = walker.nextNode())) {
    const t = (node.textContent || '').trim();
    if (t.length < 25) continue;
    const el = node.parentElement;
    if (!el || seen.has(el)) continue;
    if (['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;  // legit-UI, skip
    // display:none isn't inherited in computed style, so a node inside an ancestor-collapsed
    // menu/tab still reads display:block here. getClientRects() is empty when any ancestor is
    // display:none (or the node is detached) but non-empty for genuinely-cloaked text (off-screen
    // text-indent, 1px font, white-on-white all still lay out) — so it excludes collapsed UI only.
    if (el.getClientRects().length === 0) continue;
    let reason = null;
    const fs = parseFloat(cs.fontSize || '0');
    const ti = parseFloat(cs.textIndent || '0');
    if (fs > 0 && fs <= 1) reason = 'font-size<=1px';
    else if (ti <= -1000) reason = 'text-indent off-screen';
    else if (colorMatchesBg(el)) reason = 'text color matches background';
    if (reason) { seen.add(el); out.push({reason, sample: t.slice(0, 80)}); if (out.length >= 20) break; }
  }
  return out;
}
"""


async def render_page(url: str, user_agent: str | None = None, viewport: dict | None = None,
                      wait_until: str = "networkidle", collect_hidden: bool = False) -> dict:
    """Returns {url, status, html, text, error?}. Validates URL before launching a context.
    With collect_hidden=True, also returns `hidden`: a list of computed-style-cloaked text
    blocks (see _HIDDEN_TEXT_JS) found on the same render — no extra page load."""
    try:
        await validate_target_url(url)
    except UnsafeTargetURL as exc:
        return {"url": url, "status": 0, "html": "", "text": "", "error": f"unsafe target: {exc}"}
    try:
        browser = await get_browser()
    except Exception as exc:
        return {"url": url, "status": 0, "html": "", "text": "", "error": f"browser init: {exc}"}
    ctx = None
    try:
        ctx = await browser.new_context(user_agent=user_agent, viewport=viewport or {"width": 1280, "height": 1080})
        try:
            page = await ctx.new_page()
            response = await page.goto(url, wait_until=wait_until, timeout=30_000)
            status = response.status if response else 0
            html = await page.content()
            try:
                text = await page.evaluate("() => (document.querySelector('main, article') || document.body || {innerText: ''}).innerText")
            except Exception:
                text = ""
            result = {"url": url, "status": status, "html": html, "text": text or ""}
            if collect_hidden:
                try:
                    result["hidden"] = await page.evaluate(_HIDDEN_TEXT_JS)
                except Exception:
                    result["hidden"] = []
            return result
        finally:
            await ctx.close()
    except Exception as exc:
        return {"url": url, "status": 0, "html": "", "text": "", "error": f"{type(exc).__name__}: {exc}"}


async def screenshot_at_viewport(url: str, viewport: dict, *, full_page: bool = False, wait_until: str = "networkidle") -> dict:
    """Returns {png: bytes, status, error?} — single screenshot at the given viewport."""
    try:
        await validate_target_url(url)
    except UnsafeTargetURL as exc:
        return {"png": b"", "status": 0, "error": f"unsafe target: {exc}"}
    try:
        browser = await get_browser()
    except Exception as exc:
        return {"png": b"", "status": 0, "error": f"browser init: {exc}"}
    ctx = None
    try:
        ctx = await browser.new_context(viewport=viewport)
        try:
            page = await ctx.new_page()
            response = await page.goto(url, wait_until=wait_until, timeout=30_000)
            status = response.status if response else 0
            png = await page.screenshot(full_page=full_page, type="png")
            return {"png": png, "status": status}
        finally:
            await ctx.close()
    except Exception as exc:
        return {"png": b"", "status": 0, "error": f"{type(exc).__name__}: {exc}"}


async def capture_load_frames(url: str, *, viewport: dict | None = None, throttle_slow_3g: bool = True) -> dict:
    """Capture screenshots at intervals during page load, plus innerText length per frame.

    Returns:
        {
          'frames': [{'t_ms': int, 'png': bytes, 'text_len': int}, ...],
          'final_html': str,
          'error': str?,
        }
    """
    try:
        await validate_target_url(url)
    except UnsafeTargetURL as exc:
        return {"frames": [], "final_html": "", "error": f"unsafe target: {exc}"}
    try:
        browser = await get_browser()
    except Exception as exc:
        return {"frames": [], "final_html": "", "error": f"browser init: {exc}"}
    ctx = None
    try:
        ctx = await browser.new_context(viewport=viewport or {"width": 1280, "height": 800})
        try:
            page = await ctx.new_page()
            if throttle_slow_3g:
                # Slow 3G profile per Chrome DevTools: 400Kbps down/400Kbps up, 400ms RTT.
                cdp = await ctx.new_cdp_session(page)
                await cdp.send("Network.enable")
                await cdp.send("Network.emulateNetworkConditions", {
                    "offline": False,
                    "downloadThroughput": 50_000,   # 400 Kbps -> 50,000 bytes/s
                    "uploadThroughput": 50_000,
                    "latency": 400,
                })
            # 60s budget: Slow-3G throttling at 50KB/s means even a modestly-sized initial HTML
            # payload (~1MB) can take 20+s to first byte; need headroom over the 9s capture window.
            await page.goto(url, wait_until="commit", timeout=60_000)
            frames: list[dict] = []
            checkpoints_ms = [500, 1000, 2000, 4000, 6000, 9000]
            import asyncio as _asyncio
            t0 = _asyncio.get_event_loop().time()
            for target_ms in checkpoints_ms:
                now_ms = int((_asyncio.get_event_loop().time() - t0) * 1000)
                wait_ms = target_ms - now_ms
                if wait_ms > 0:
                    await _asyncio.sleep(wait_ms / 1000)
                try:
                    text_len = await page.evaluate(
                        "() => (document.body && document.body.innerText && document.body.innerText.length) || 0"
                    )
                except Exception:
                    text_len = 0
                try:
                    png = await page.screenshot(full_page=False, type="png")
                except Exception:
                    png = b""
                frames.append({"t_ms": target_ms, "png": png, "text_len": int(text_len or 0)})
            try:
                final_html = await page.content()
            except Exception:
                final_html = ""
            return {"frames": frames, "final_html": final_html}
        finally:
            await ctx.close()
    except Exception as exc:
        return {"frames": [], "final_html": "", "error": f"{type(exc).__name__}: {exc}"}


async def shutdown() -> None:
    global _playwright, _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


def get_channel_used() -> str | None:
    """Return the launch channel of the shared browser ('chrome' or 'chromium-bundled'),
    or None if no browser has been launched yet."""
    return getattr(_browser, "_cerberus_channel", None) if _browser is not None else None
