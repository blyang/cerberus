"""Shared check utilities: HTTP fetch, HTML parse, header inspection.

Heavy resources are memoized on the CheckContext via get_or_set, so each is fetched once per run.
"""
from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .base import CheckContext

# UA strings used throughout. Real Googlebot UA per Google's published guidance.
UA_CHROME_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
UA_CHROME_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class UnsafeTargetURL(ValueError):
    """Raised when a URL targets a non-public host (loopback, RFC1918, link-local, etc.).

    The tool fetches both operator-supplied URLs and secondary URLs (canonicals, sitemaps,
    images) discovered during a run. Both classes are user-influenced, so we resolve and
    reject any host that points to private/internal infrastructure.
    """


_DNS_CACHE: dict[str, list[str]] = {}


async def _resolve(host: str) -> list[str]:
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        _DNS_CACHE[host] = []
        return []
    ips = sorted({info[4][0] for info in infos})
    _DNS_CACHE[host] = ips
    return ips


def _is_safe_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return False
    if ip.is_unspecified:
        return False
    # Block AWS/GCP/Azure metadata endpoints explicitly (169.254.169.254 is link_local already; just being explicit).
    return True


async def validate_target_url(url: str) -> None:
    """Raise UnsafeTargetURL for non-http(s) schemes or hosts pointing to private/internal IPs.

    Run before every outbound fetch. DNS-resolves the host; rejects any address that's loopback,
    RFC1918, link-local (169.254/16), multicast, or reserved.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise UnsafeTargetURL(f"unsupported scheme: {p.scheme!r}")
    host = (p.hostname or "").strip()
    if not host:
        raise UnsafeTargetURL("missing host")
    # Literal IP in URL.
    try:
        ip_obj = ipaddress.ip_address(host)
        if not _is_safe_ip(str(ip_obj)):
            raise UnsafeTargetURL(f"target IP is private/internal: {host}")
        return
    except ValueError:
        pass
    # Resolve hostname.
    ips = await _resolve(host)
    if not ips:
        raise UnsafeTargetURL(f"could not resolve host: {host}")
    bad = [ip for ip in ips if not _is_safe_ip(ip)]
    if bad:
        raise UnsafeTargetURL(f"host {host} resolves to private/internal address(es): {bad}")


@dataclasses.dataclass
class FetchResult:
    url: str
    status_code: int
    headers: dict[str, str]
    text: str
    final_url: str
    history: list[tuple[int, str]]  # [(status_code, location)]
    content: bytes = b""  # raw bytes — needed for binary content like images
    error: str | None = None

    @property
    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.text, "lxml")


MAX_REDIRECT_HOPS = 8


async def _fetch(
    url: str,
    method: str = "GET",
    user_agent: str = UA_CHROME_DESKTOP,
    follow_redirects: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """SSRF-safe fetch.

    httpx's built-in `follow_redirects=True` doesn't re-run our `validate_target_url` guard on
    each Location target, so an attacker who controls any public web server could 302-redirect
    us into 127.0.0.1 / 169.254.169.254 / RFC1918. We disable httpx's auto-follow and walk the
    chain ourselves, calling `validate_target_url` on every hop.
    """
    try:
        await validate_target_url(url)
    except UnsafeTargetURL as exc:
        return FetchResult(url=url, status_code=0, headers={}, text="", final_url=url, history=[], error=f"unsafe target: {exc}")
    headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
    if extra_headers:
        headers.update(extra_headers)
    history: list[tuple[int, str]] = []
    current_url = url
    current_method = method
    try:
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
            http2=True,
            headers=headers,
        ) as client:
            for hop in range(MAX_REDIRECT_HOPS + 1):
                r = await client.request(current_method, current_url)
                if not follow_redirects or r.status_code not in (301, 302, 303, 307, 308):
                    return FetchResult(
                        url=url,
                        status_code=r.status_code,
                        headers={k.lower(): v for k, v in r.headers.items()},
                        text=r.text if current_method != "HEAD" else "",
                        content=r.content if current_method != "HEAD" else b"",
                        final_url=str(r.url),
                        history=history,
                    )
                location = r.headers.get("location")
                if not location:
                    return FetchResult(
                        url=url, status_code=r.status_code,
                        headers={k.lower(): v for k, v in r.headers.items()},
                        text="", content=b"", final_url=str(r.url), history=history,
                        error=f"redirect with no Location header (status {r.status_code})",
                    )
                next_url = str(httpx.URL(current_url).join(location))
                history.append((r.status_code, next_url))
                # SSRF guard: every redirect target must independently pass validation.
                try:
                    await validate_target_url(next_url)
                except UnsafeTargetURL as exc:
                    return FetchResult(
                        url=url, status_code=0, headers={}, text="", content=b"",
                        final_url=current_url, history=history,
                        error=f"redirect to unsafe target: {exc}",
                    )
                # 303 always switches to GET; 301/302 typically do too for non-GET; 307/308 preserve method.
                if r.status_code == 303 or (r.status_code in (301, 302) and current_method != "HEAD"):
                    current_method = "GET"
                current_url = next_url
            return FetchResult(
                url=url, status_code=0, headers={}, text="", content=b"",
                final_url=current_url, history=history,
                error=f"redirect chain exceeded {MAX_REDIRECT_HOPS} hops",
            )
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
        return FetchResult(
            url=url,
            status_code=0,
            headers={},
            text="",
            final_url=url,
            history=history,
            error=f"{type(exc).__name__}: {exc}",
        )


async def fetch(ctx: CheckContext, user_agent: str = UA_CHROME_DESKTOP, key_suffix: str = "") -> FetchResult:
    """Memoized GET on the context's URL with a given UA."""
    key = f"fetch:{user_agent}:{key_suffix}:{ctx.url}"

    async def factory() -> FetchResult:
        return await _fetch(ctx.url, user_agent=user_agent)

    return await ctx.get_or_set(key, factory)


async def fetch_url(ctx: CheckContext, url: str, user_agent: str = UA_CHROME_DESKTOP, follow_redirects: bool = True, method: str = "GET") -> FetchResult:
    """Memoized GET/HEAD for any URL (e.g. canonical target, sitemap)."""
    key = f"fetchurl:{method}:{user_agent}:{follow_redirects}:{url}"

    async def factory() -> FetchResult:
        return await _fetch(url, method=method, user_agent=user_agent, follow_redirects=follow_redirects)

    return await ctx.get_or_set(key, factory)


# --- HTML helpers ---

def normalize_url(url: str) -> str:
    """Lowercase scheme+host, strip trailing slash, drop fragment+query for canonical compare."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"


def get_meta(soup: BeautifulSoup, name: str | None = None, prop: str | None = None) -> str | None:
    """Return content attr for matching <meta>."""
    attrs: dict[str, Any] = {}
    if name is not None:
        attrs["attrs"] = {"name": re.compile(f"^{re.escape(name)}$", re.I)}
    if prop is not None:
        attrs.setdefault("attrs", {})["property"] = re.compile(f"^{re.escape(prop)}$", re.I)
    el = soup.find("meta", **attrs) if attrs else None
    if not el:
        return None
    content = el.get("content")
    if content is None:
        return None
    return str(content).strip() or None


def get_canonical_href(soup: BeautifulSoup) -> str | None:
    el = soup.find("link", attrs={"rel": lambda v: v and "canonical" in (v if isinstance(v, list) else v.lower().split())})
    if not el:
        return None
    href = el.get("href")
    return str(href).strip() if href else None


def primary_content_text(html: str) -> str:
    """Best-effort extract of <main> or <article> innerText. Falls back to body."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.find("main") or soup.find("article") or soup.body
    if not el:
        return ""
    return el.get_text("\n", strip=True)


def all_text_lines(text: str, min_len: int = 8) -> list[str]:
    """Non-empty, deduped lines from rendered text. Filters very short lines (UI chrome)."""
    seen: set[str] = set()
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < min_len:
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines
