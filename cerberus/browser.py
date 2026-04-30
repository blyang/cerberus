"""Shared Playwright Chromium pool. One browser per process; new context per run."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .checks._utils import UnsafeTargetURL, validate_target_url

log = logging.getLogger(__name__)

_browser_lock = asyncio.Lock()
_playwright: Any = None
_browser: Any = None


async def get_browser() -> Any:
    """Acquire (or initialise) the shared Chromium browser. Cleans up partial state on failure."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is not None:
            return _browser
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        except Exception:
            try:
                await pw.stop()
            except Exception:
                pass
            raise
        _playwright = pw
        _browser = browser
        return _browser


async def render_page(url: str, user_agent: str | None = None, viewport: dict | None = None, wait_until: str = "networkidle") -> dict:
    """Returns {url, status, html, text, error?}. Validates URL before launching a context."""
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
            return {"url": url, "status": status, "html": html, "text": text or ""}
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
