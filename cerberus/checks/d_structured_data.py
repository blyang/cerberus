"""D — Structured Data."""
from __future__ import annotations

import json
from typing import Any

import extruct
import httpx

from .. import browser, vision
from ._utils import DEFAULT_TIMEOUT, fetch, normalize_url
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

D3_SYSTEM_PROMPT = (
    "You are an SEO auditor checking whether JSON-LD structured data is consistent with "
    "the visible page content. You will receive the JSON-LD blocks and the rendered page text. "
    "Answer: do any free-text fields in the JSON-LD (name, description, or similar) make claims "
    "that a visitor reading the page would find contradicted, unsupported, or materially misleading? "
    "Minor wording differences are fine. Only flag clear factual contradictions or invented claims. "
    "Return JSON: {verdict: 'pass'|'fail', confidence: 0..1, reason: 'short reason ≤200 chars'}."
)

SMV_URL = "https://validator.schema.org/validate"
SMV_TOOL_ID = "validator.schema.org"


def _extract_json_ld(html: str, url: str) -> list[dict[str, Any]]:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld"])
        return data.get("json-ld") or []
    except Exception:
        return []


def _count_json_ld_tags(soup) -> int:
    """How many <script type='application/ld+json'> tags appear, regardless of validity.

    Used to distinguish "page declares no JSON-LD at all" (NA) from "page declares
    JSON-LD but extruct couldn't parse any of it" (NEEDS_REVIEW).
    """
    return len(soup.find_all(
        "script",
        attrs={"type": lambda v: isinstance(v, str) and "ld+json" in v.lower()},
    ))


def _parse_smv_response(body: str) -> dict[str, Any] | None:
    """Schema Markup Validator wraps its JSON in a `)]}'\\n` XSSI prefix; strip and parse."""
    text = body.lstrip()
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1] if "\n" in text else text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _collect_smv_errors(payload: dict[str, Any]) -> list[str]:
    """Surface error descriptions from the SMV response.

    Walks only the documented path so unrelated `errors` keys elsewhere in the payload
    can't leak through: tripleGroups[].nodes[].{types,properties,nodeProperties,errors}.
    `nodeProperties[].target` is itself a node, so recurse.
    """
    out: list[str] = []

    def add(errors: Any) -> None:
        if not isinstance(errors, list):
            return
        for e in errors:
            if isinstance(e, dict):
                out.append(str(e.get("description") or e.get("message") or json.dumps(e)))
            elif e:
                out.append(str(e))

    def visit_node(node: Any) -> None:
        if not isinstance(node, dict):
            return
        add(node.get("errors"))
        for sub in node.get("types") or []:
            if isinstance(sub, dict):
                add(sub.get("errors"))
        for sub in node.get("properties") or []:
            if isinstance(sub, dict):
                add(sub.get("errors"))
        for sub in node.get("nodeProperties") or []:
            if isinstance(sub, dict):
                add(sub.get("errors"))
                visit_node(sub.get("target"))

    for group in payload.get("tripleGroups") or []:
        for node in (group.get("nodes") if isinstance(group, dict) else None) or []:
            visit_node(node)
    return out


@register("D2", section="D", severity=Severity.CONDITIONAL,
          title="Structured data validates (Schema Markup Validator)", estimate_ms=8_000)
async def d2(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    blocks = _extract_json_ld(r.text, ctx.url)
    if not blocks:
        tag_count = _count_json_ld_tags(r.soup)
        if tag_count:
            return CheckResult(
                Status.NEEDS_REVIEW,
                summary=f"{tag_count} <script type='application/ld+json'> block(s) on page but extruct parsed zero — JSON-LD is malformed.",
                details={"tag_count": tag_count,
                         "advice": "Inspect the JSON-LD blocks for syntax errors (trailing commas, unescaped quotes, etc.)."},
            )
        return CheckResult(Status.NA, summary="No JSON-LD on page.")
    # validator.schema.org is the Schema Markup Validator — full schema.org vocabulary check.
    # Different from Google's Rich Results Test (search.google.com/test/rich-results), which
    # only checks eligibility for specific Google rich-result types. Operators sometimes confuse
    # them; surface the distinction explicitly in the result.
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                SMV_URL,
                data={"url": ctx.url},
                headers={"Accept": "application/json"},
            )
    except Exception as exc:
        return CheckResult(
            Status.NEEDS_REVIEW,
            summary=f"Schema Markup Validator unreachable ({type(exc).__name__}); JSON-LD parses locally.",
            details={"block_count": len(blocks),
                     "validator_error": f"{type(exc).__name__}: {exc}",
                     "tool": SMV_TOOL_ID},
        )
    if resp.status_code != 200:
        return CheckResult(
            Status.NEEDS_REVIEW,
            summary=f"Schema Markup Validator returned HTTP {resp.status_code}; JSON-LD parses locally.",
            details={"block_count": len(blocks),
                     "validator_status": resp.status_code,
                     "tool": SMV_TOOL_ID},
        )
    payload = _parse_smv_response(resp.text)
    if payload is None:
        return CheckResult(
            Status.NEEDS_REVIEW,
            summary="Schema Markup Validator returned unparseable JSON; JSON-LD parses locally.",
            details={"block_count": len(blocks),
                     "validator_body_prefix": resp.text[:200],
                     "tool": SMV_TOOL_ID},
        )
    num_errors = int(payload.get("numErrors") or 0)
    num_warnings = int(payload.get("numWarnings") or 0)
    if num_errors:
        return CheckResult(
            Status.FAIL,
            summary=f"{num_errors} schema.org error(s) across {len(blocks)} block(s).",
            details={"errors": _collect_smv_errors(payload)[:20],
                     "block_count": len(blocks),
                     "num_warnings": num_warnings,
                     "tool": SMV_TOOL_ID,
                     "note": "Different tool from Google Rich Results Test; an RRT pass doesn't override SMV errors."},
        )
    summary = f"{len(blocks)} JSON-LD block(s) validated (0 errors"
    if num_warnings:
        summary += f", {num_warnings} warnings"
    summary += ")."
    return CheckResult(
        Status.PASS,
        summary=summary,
        details={"block_count": len(blocks), "num_warnings": num_warnings,
                 "tool": "validator.schema.org"},
    )


@register("D3", section="D", severity=Severity.CONDITIONAL,
          title="Structured data matches visible content", estimate_ms=20_000)
async def d3(ctx: CheckContext) -> CheckResult:
    r = await fetch(ctx)
    blocks = _extract_json_ld(r.text, ctx.url)
    if not blocks:
        tag_count = _count_json_ld_tags(r.soup)
        if tag_count:
            return CheckResult(
                Status.NEEDS_REVIEW,
                summary=f"{tag_count} JSON-LD block(s) declared but unparseable; visible-content match cannot be evaluated.",
                details={"tag_count": tag_count},
            )
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
    vcfg = ctx.site_config.vision if ctx.site_config else None
    if vcfg and vcfg.enabled:
        body_truncated = len(body_text) > 6000
        blocks_truncated = len(blocks) > 5
        truncated = body_truncated or blocks_truncated
        user_prompt = (
            f"JSON-LD blocks{' (first 5 of ' + str(len(blocks)) + ')' if blocks_truncated else ''}:\n"
            f"{json.dumps(blocks[:5], indent=2)}\n\n"
            f"Rendered page text{' (first 6000 chars only)' if body_truncated else ''}:\n{body_text[:6000]}"
        )
        verdict = await vision.classify(
            vcfg,
            system_prompt=D3_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            images=[],
        )
        if verdict.provider == "fallback-to-manual":
            sub.append(SubStep(
                "Free-text fields don't contradict visible content",
                Status.MANUAL,
                detail=f"LLM unavailable ({verdict.error}); review manually.",
                instruction=D3_MANUAL_INSTRUCTION,
            ))
        else:
            # Downgrade fail to NEEDS_REVIEW when body was truncated — the LLM may
            # not have seen the later content that would support the JSON-LD claims.
            if verdict.verdict == "pass":
                llm_status = Status.PASS
            elif truncated:
                llm_status = Status.NEEDS_REVIEW
            else:
                llm_status = Status.FAIL
            detail = f"{verdict.reason} (conf {verdict.confidence:.2f} via {verdict.provider}/{verdict.model})"
            if truncated and verdict.verdict == "fail":
                truncation_note = " + ".join(filter(None, [
                    "body >6000 chars" if body_truncated else "",
                    f"{len(blocks)} blocks (first 5 sent)" if blocks_truncated else "",
                ]))
                detail += f" [truncated: {truncation_note} — verify against full page]"
            sub.append(SubStep(
                "Free-text fields don't contradict visible content",
                llm_status,
                detail=detail,
            ))
    else:
        sub.append(SubStep(
            "Free-text fields don't contradict visible content",
            Status.MANUAL,
            detail="Vision/LLM disabled; review manually.",
            instruction=D3_MANUAL_INSTRUCTION,
        ))
    result = CheckResult.from_substeps("JSON-LD ↔ visible content.", sub)
    result.details["json_ld_summaries"] = json_ld_summaries[:10]
    result.details["json_ld_blocks"] = blocks[:5]
    if any(s.status == Status.MANUAL for s in sub):
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
