"""D — Structured Data."""
from __future__ import annotations

import json
from typing import Any

import extruct
import httpx

from .. import browser
from ._utils import fetch, normalize_url
from .base import (
    CheckContext,
    CheckResult,
    Severity,
    Status,
    SubStep,
    register,
)

D3_MANUAL_INSTRUCTION = (
    "Review the JSON-LD blocks shown in the check details. Confirm no free-text field "
    "(description, name, etc.) makes claims that contradict what visitors see on the page."
)


def _extract_json_ld(html: str, url: str) -> list[dict[str, Any]]:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld"])
        return data.get("json-ld") or []
    except Exception:
        return []


@register("D2", section="D", severity=Severity.CONDITIONAL,
          title="Structured data validates", estimate_ms=8_000)
async def d2(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    blocks = _extract_json_ld(r.text, ctx.url)
    if not blocks:
        return CheckResult(Status.NA, summary="No JSON-LD on page.")
    # Hit Google's rich-results-style validator endpoint (validator.schema.org).
    errors: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://validator.schema.org/validate",
                data={"url": ctx.url},
                headers={"Accept": "application/json"},
            )
        if resp.status_code == 200:
            try:
                payload = resp.json()
                # Schema.org returns nested groups; surface any error counts.
                groups = payload.get("groups", []) if isinstance(payload, dict) else []
                for g in groups:
                    for n in (g.get("nodes") or []):
                        for e in (n.get("errors") or []):
                            errors.append(str(e.get("description") or e))
            except json.JSONDecodeError:
                errors.append(f"validator returned non-JSON (status {resp.status_code})")
        else:
            errors.append(f"validator HTTP {resp.status_code}")
    except Exception as exc:
        errors.append(f"validator unreachable: {type(exc).__name__}: {exc}")
    if errors:
        return CheckResult(Status.FAIL,
                           summary=f"{len(errors)} validation error(s).",
                           details={"errors": errors[:20], "block_count": len(blocks)})
    return CheckResult(Status.PASS, summary=f"{len(blocks)} JSON-LD block(s) validated.",
                       details={"block_count": len(blocks)})


@register("D3", section="D", severity=Severity.CONDITIONAL,
          title="Structured data matches visible content", estimate_ms=20_000)
async def d3(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    blocks = _extract_json_ld(r.text, ctx.url)
    if not blocks:
        return CheckResult(Status.NA, summary="No JSON-LD on page.")
    rendered = await browser.render_page(ctx.url)
    body_text = (rendered.get("text") or "")
    sub: list[SubStep] = []

    breadcrumbs_checked = 0
    breadcrumbs_missing: list[str] = []
    url_mismatches: list[str] = []
    json_ld_summaries: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        nonlocal breadcrumbs_checked
        if isinstance(obj, list):
            for o in obj:
                walk(o)
            return
        if not isinstance(obj, dict):
            return
        t = obj.get("@type")
        if t == "BreadcrumbList":
            for item in (obj.get("itemListElement") or []):
                breadcrumbs_checked += 1
                name = (item.get("name") if isinstance(item, dict) else None) or \
                       ((item.get("item") or {}).get("name") if isinstance(item, dict) else None)
                if name and name not in body_text:
                    breadcrumbs_missing.append(str(name))
        # URL-bearing fields.
        for key in ("url", "@id"):
            v = obj.get(key)
            if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
                # Allow if matches canonical or appears in any href.
                if normalize_url(v) == normalize_url(ctx.url):
                    continue
                if v in r.text:
                    continue
                url_mismatches.append(f"{t or 'obj'}.{key} = {v}")
        # Recurse into children.
        for v in obj.values():
            if isinstance(v, (list, dict)):
                walk(v)
        if t:
            json_ld_summaries.append({"type": t, "name": obj.get("name"), "description": obj.get("description")})

    for block in blocks:
        walk(block)

    sub.append(SubStep("JSON-LD parses + extracted",
                       Status.PASS if blocks else Status.FAIL,
                       detail=f"{len(blocks)} block(s)"))
    sub.append(SubStep(
        "BreadcrumbList names appear in rendered text",
        Status.PASS if breadcrumbs_checked == 0 or not breadcrumbs_missing else Status.FAIL,
        detail=f"{breadcrumbs_checked} breadcrumb names; missing: {breadcrumbs_missing}" if breadcrumbs_checked else "(no BreadcrumbList)",
    ))
    sub.append(SubStep(
        "URL fields match canonical or visible links",
        Status.PASS if not url_mismatches else Status.NEEDS_REVIEW,
        detail=f"unmatched: {url_mismatches[:5]}" if url_mismatches else "ok",
    ))
    sub.append(SubStep(
        "Free-text fields don't contradict visible content (manual)",
        Status.MANUAL,
        detail="See JSON-LD summaries below.",
        instruction=D3_MANUAL_INSTRUCTION,
    ))
    result = CheckResult.from_substeps("JSON-LD ↔ visible content.", sub)
    result.details["json_ld_summaries"] = json_ld_summaries[:10]
    result.details["json_ld_blocks"] = blocks[:5]
    result.instruction = D3_MANUAL_INSTRUCTION
    return result


@register("D5", section="D", severity=Severity.CONDITIONAL,
          title="BreadcrumbList structured data present", estimate_ms=500)
async def d5(_: CheckContext) -> CheckResult:
    # Per brief: "D5, F1 step 3, F6 — implement skeletons that report N/A" until hub architecture
    # exists. Even when BreadcrumbList is found on the page, the full check (does breadcrumb
    # match the hub-page hierarchy?) requires the hub model. Always N/A in MVP.
    return CheckResult(Status.NA, summary="N/A until hub architecture exists (per brief).")


def _has_type(obj: Any, target: str) -> bool:
    if isinstance(obj, list):
        return any(_has_type(o, target) for o in obj)
    if not isinstance(obj, dict):
        return False
    t = obj.get("@type")
    if t == target or (isinstance(t, list) and target in t):
        return True
    for v in obj.values():
        if _has_type(v, target):
            return True
    return False
