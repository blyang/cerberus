"""Google Search Console URL Inspection client (H3).

Service-account auth via the credentials file referenced in site_config.
Returns {render_html, status, error?}; the caller diffs against headless browser render.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


_GSC_HTTP_TIMEOUT_S = 30


def _build_service(creds_path: str):
    import httplib2  # type: ignore
    from google.oauth2 import service_account  # type: ignore
    from google_auth_httplib2 import AuthorizedHttp  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=[_GSC_SCOPE])
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_GSC_HTTP_TIMEOUT_S))
    return build("searchconsole", "v1", http=http, cache_discovery=False)


async def inspect_url(url: str, creds_path: str | None, site_url: str | None = None) -> dict[str, Any]:
    """Run synchronously off the event loop. Returns dict with index_status, render_html, error."""
    if not creds_path or not Path(creds_path).exists():
        return {"error": "gsc credentials not configured"}

    def _do() -> dict[str, Any]:
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except Exception as exc:
            return {"error": f"google-api-python-client missing: {exc}"}
        try:
            service = _build_service(creds_path)
            body = {
                "inspectionUrl": url,
                "siteUrl": site_url or _site_url_from(url),
                "languageCode": "en-US",
            }
            req = service.urlInspection().index().inspect(body=body)
            resp = req.execute()
            return {"raw": resp}
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status == 403:
                return {"error": f"gsc forbidden (service account not added as user on property?): {exc}"}
            if status == 429:
                return {"error": "gsc quota exhausted"}
            return {"error": f"gsc http error: {exc}"}
        except Exception as exc:
            return {"error": f"gsc call failed: {type(exc).__name__}: {exc}"}

    return await asyncio.to_thread(_do)


def _site_url_from(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"
