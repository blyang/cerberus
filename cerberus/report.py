"""Render a run's results as a prioritized markdown report for the dev team.

Sections, in fix-order priority:
  1. Failed (Blocking) — must fix before launch
  2. Failed (Other)    — Recommended/Conditional severity
  3. Needs Review      — partly automated, requires human glance
  4. Manual            — human-only checks with instructions
  5. Passed            — included as appendix
  6. N/A               — included as appendix

Each failed/review entry includes: assertion, sub-step results, the actual values found,
and a deep link back to the corresponding checklist row.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import screenshots as _screenshots
from . import verify_steps

CHECKLIST_BASE = (
    "https://docs.google.com/spreadsheets/d/"
    "1Kka3oAavdaWHCEDGYbBZFG8vAwq1C_1fpWlV226ozKE/edit"
)


def _checklist_link(check_id: str) -> str:
    return f"{CHECKLIST_BASE}#gid=795173280&range=A:A&search={quote(check_id)}"


def _severity_rank(sev: str) -> int:
    return {"Blocking": 0, "Recommended": 1, "Conditional": 2}.get(sev, 3)


def _bucket(check: dict[str, Any]) -> str:
    status = check.get("status", "")
    sev = (check.get("details") or {}).get("severity", "")
    if status == "Fail":
        return "blocking" if sev == "Blocking" else "other_fail"
    if status == "Needs Review":
        return "needs_review"
    if status == "Manual":
        return "manual"
    if status == "Pass":
        return "pass"
    if status == "N/A":
        return "na"
    return "skipped"


def _format_dt(ms: int | None) -> str:
    if not ms:
        return "—"
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _render_substeps(sub_steps: list[dict[str, Any]]) -> list[str]:
    if not sub_steps:
        return []
    lines = ["", "**Sub-steps:**", ""]
    for s in sub_steps:
        label = s.get("label", "")
        st = s.get("status", "")
        detail = s.get("detail", "")
        marker = {"Pass": "✓", "Fail": "✗", "Manual": "✋", "Needs Review": "⚠", "N/A": "—"}.get(st, "•")
        lines.append(f"- {marker} **[{st}]** {label}" + (f" — {detail}" if detail else ""))
        if s.get("instruction"):
            lines.append(f"  > {s['instruction']}")
    return lines


def _render_evidence(details: dict[str, Any]) -> list[str]:
    """Pluck the most useful evidence keys per common check type, falling back to a JSON dump."""
    if not details:
        return []
    payload = {k: v for k, v in details.items()
               if not k.startswith("_")
               and k not in ("title", "severity", "section", "instruction", "sub_steps", "runtime_ms")}
    if not payload:
        return []
    # Prefer flat key:value rendering for small payloads; JSON code block for large.
    if all(isinstance(v, (str, int, float, bool)) or v is None for v in payload.values()):
        lines = ["", "**Evidence:**", ""]
        for k, v in payload.items():
            lines.append(f"- `{k}`: {v}")
        return lines
    # Mixed/structured: code block.
    truncated = json.dumps(payload, indent=2, default=str)
    if len(truncated) > 4000:
        truncated = truncated[:4000] + "\n... (truncated)"
    return ["", "**Evidence:**", "", "```json", truncated, "```"]


def _render_check(check: dict[str, Any], audited_url: str = "") -> str:
    cid = check["check_id"]
    details = check.get("details") or {}
    title = details.get("title", "")
    severity = details.get("severity", "")
    summary = check.get("summary", "")
    runtime_ms = details.get("runtime_ms")
    runtime_str = f"{runtime_ms/1000:.2f}s" if isinstance(runtime_ms, (int, float)) else "—"

    lines: list[str] = []
    lines.append(f"### {cid} — {title}")
    lines.append("")
    lines.append(f"- **Severity:** {severity}")
    status_str = check.get("status", "")
    if details.get("_marked_by_operator"):
        original = details.get("_original_status", "")
        status_str = f"{status_str} (operator-marked, was: {original})"
    lines.append(f"- **Status:** {status_str}")
    if summary:
        lines.append(f"- **Result:** {summary}")
    lines.append(f"- **Runtime:** {runtime_str}")
    lines.append(f"- **Checklist row:** [{cid} on the source-of-truth sheet]({_checklist_link(cid)})")

    instruction = details.get("instruction")
    if instruction:
        lines.append("")
        lines.append("**Instructions for the manual step:**")
        lines.append("")
        lines.append(f"> {instruction}")

    lines.extend(_render_substeps(details.get("sub_steps") or []))
    lines.extend(_render_screenshots(details, run_id=details.get("_run_id", "")))
    lines.extend(_render_evidence(details))
    lines.extend(_render_verify_steps(cid, audited_url))
    lines.append("")
    return "\n".join(lines)


def _render_screenshots(details: dict[str, Any], run_id: str) -> list[str]:
    """Embed inline thumbnails (data:image/jpeg) for any saved screenshots referenced in details."""
    names = details.get("screenshots") or []
    if not names or not run_id:
        return []
    out: list[str] = ["", "**Screenshots (inline thumbnails):**", ""]
    for name in names:
        png_path = _screenshots.SCREENSHOT_DIR / run_id / f"{name}.png"
        if not png_path.exists():
            continue
        try:
            data_url = _screenshots.thumbnail_data_url(png_path.read_bytes())
        except Exception:
            data_url = ""
        if data_url:
            out.append(f"_{name}:_")
            out.append("")
            out.append(f"![{name}]({data_url})")
            out.append("")
    return out if len(out) > 3 else []


def _render_verify_steps(check_id: str, audited_url: str) -> list[str]:
    """Render the reproduce-and-verify block — copy-pasteable curl/devtools steps + pass conditions."""
    if not audited_url:
        return []
    entry = verify_steps.for_check(check_id, audited_url)
    if not entry:
        return []
    verify, pass_conditions = entry
    out: list[str] = ["", "**Reproduce:**", "", "```bash", verify, "```"]
    if pass_conditions:
        out.extend(["", "**Pass conditions** (Cerberus's expected result; if your manual rerun returns something different, verify against this):", "", "```text", pass_conditions, "```"])
    return out


def _render_compact_table(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return ""
    lines = ["", "| ID | Severity | Title |", "|---|---|---|"]
    for c in checks:
        details = c.get("details") or {}
        title = details.get("title", "").replace("|", "\\|")
        if details.get("_marked_by_operator"):
            title += f" _(operator-marked, was: {details.get('_original_status', '')})_"
        lines.append(f"| {c['check_id']} | {details.get('severity','')} | {title} |")
    lines.append("")
    return "\n".join(lines)


def render_markdown(run: dict[str, Any], checks: list[dict[str, Any]],
                    manual_toggles: dict[str, str]) -> str:
    # Stamp the run_id onto each check's details so _render_check can resolve screenshot paths
    # without a wider signature change.
    run_id = run.get("id", "")
    for c in checks:
        details = c.get("details") or {}
        details["_run_id"] = run_id
        c["details"] = details
    """Build the full markdown report for a run."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "blocking": [], "other_fail": [], "needs_review": [], "manual": [], "pass": [], "na": [], "skipped": [],
    }
    for c in checks:
        # Apply operator's manual override on top of the auto-result. The override applies to
        # checks parked in either Manual (pure-manual checks like A2/C7/E4 when vision is off)
        # or Needs Review (partly-automated checks like D3 step 5 where the operator made the
        # final call). Auto-classified Pass/Fail are not overridable from the UI.
        # Preserve the original status in details so _render_check can label the override
        # ("Pass (operator-marked, was: Manual)") instead of silently rewriting.
        marked = manual_toggles.get(c["check_id"])
        if marked and c["status"] in ("Manual", "Needs Review"):
            original_status = c["status"]
            c = dict(c)
            c["status"] = marked
            c_details = dict(c.get("details") or {})
            c_details["_original_status"] = original_status
            c_details["_marked_by_operator"] = True
            c["details"] = c_details
        buckets[_bucket(c)].append(c)
    for k in buckets:
        buckets[k].sort(key=lambda c: (_severity_rank((c.get("details") or {}).get("severity", "")), c["check_id"]))

    started = _format_dt(run.get("started_at"))
    completed = _format_dt(run.get("completed_at"))
    duration = "—"
    if run.get("started_at") and run.get("completed_at"):
        duration = f"{(run['completed_at'] - run['started_at']) / 1000:.1f}s"

    summary_counts = {
        "Failed (Blocking)": len(buckets["blocking"]),
        "Failed (Other)": len(buckets["other_fail"]),
        "Needs Review": len(buckets["needs_review"]),
        "Manual": len(buckets["manual"]),
        "Passed": len(buckets["pass"]),
        "N/A": len(buckets["na"]),
    }
    # Audit trail: surface operator overrides in their own section so a Pass that
    # was actually a manually-marked Manual or Needs-Review doesn't disappear into
    # the compact-table appendix without context.
    overridden = [c for bucket in buckets.values() for c in bucket
                  if (c.get("details") or {}).get("_marked_by_operator")]

    lines: list[str] = []
    lines.append("# Cerberus SEO/GEO Pre-Flight Report")
    lines.append("")
    lines.append(f"- **Page tested:** {run.get('url', '')}")
    lines.append(f"- **Run ID:** `{run.get('id', '')}`")
    lines.append(f"- **Started:** {started}")
    lines.append(f"- **Completed:** {completed}")
    lines.append(f"- **Duration:** {duration}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Bucket | Count |")
    lines.append("|---|---|")
    for label, count in summary_counts.items():
        lines.append(f"| {label} | {count} |")
    lines.append("")

    # Audit trail for operator overrides — visible at the top so a Pass that was
    # really a Manual/Needs-Review override is never silently buried.
    if overridden:
        lines.append("## Operator-marked overrides")
        lines.append("")
        lines.append("These checks were re-classified by the operator after a manual review:")
        lines.append("")
        for c in overridden:
            d = c.get("details") or {}
            orig = d.get("_original_status", "")
            lines.append(f"- **{c['check_id']}**: {orig} → {c.get('status', '')}"
                         f" — {c.get('summary', '').splitlines()[0] if c.get('summary') else ''}")
        lines.append("")

    # Quick fix-list at the top.
    if buckets["blocking"] or buckets["other_fail"] or buckets["needs_review"] or buckets["manual"]:
        lines.append("## Action items (in priority order)")
        lines.append("")
        if buckets["blocking"]:
            lines.append(f"- 🔴 **{len(buckets['blocking'])} blocking failure(s)** — fix before launch:"
                         f" {', '.join(c['check_id'] for c in buckets['blocking'])}")
        if buckets["other_fail"]:
            lines.append(f"- 🟠 **{len(buckets['other_fail'])} non-blocking failure(s)**:"
                         f" {', '.join(c['check_id'] for c in buckets['other_fail'])}")
        if buckets["needs_review"]:
            lines.append(f"- 🟡 **{len(buckets['needs_review'])} item(s) need review**:"
                         f" {', '.join(c['check_id'] for c in buckets['needs_review'])}")
        if buckets["manual"]:
            lines.append(f"- ✋ **{len(buckets['manual'])} manual check(s)**:"
                         f" {', '.join(c['check_id'] for c in buckets['manual'])}")
        lines.append("")

    if buckets["blocking"]:
        lines.append("---")
        lines.append("")
        lines.append("## 🔴 Failed — Blocking severity")
        lines.append("")
        lines.append("These prevent successful indexing or significantly hurt SEO. Fix before launch.")
        lines.append("")
        for c in buckets["blocking"]:
            lines.append(_render_check(c, audited_url=run.get("url", "")))

    if buckets["other_fail"]:
        lines.append("---")
        lines.append("")
        lines.append("## 🟠 Failed — Recommended / Conditional severity")
        lines.append("")
        lines.append("Best-practice failures. Fix when reasonable.")
        lines.append("")
        for c in buckets["other_fail"]:
            lines.append(_render_check(c, audited_url=run.get("url", "")))

    if buckets["needs_review"]:
        lines.append("---")
        lines.append("")
        lines.append("## 🟡 Needs Review")
        lines.append("")
        lines.append("These were partly automated; a human needs to confirm the final call.")
        lines.append("")
        for c in buckets["needs_review"]:
            lines.append(_render_check(c, audited_url=run.get("url", "")))

    if buckets["manual"]:
        lines.append("---")
        lines.append("")
        lines.append("## ✋ Manual checks")
        lines.append("")
        lines.append("Run these by hand and record the outcome (the tool stores marked Pass/Fail per run).")
        lines.append("")
        for c in buckets["manual"]:
            lines.append(_render_check(c, audited_url=run.get("url", "")))

    # Appendices: passes + N/A as compact tables, no per-check detail.
    if buckets["pass"]:
        lines.append("---")
        lines.append("")
        lines.append(f"## ✅ Passed ({len(buckets['pass'])})")
        lines.append("")
        lines.append(_render_compact_table(buckets["pass"]))

    if buckets["na"]:
        lines.append("---")
        lines.append("")
        lines.append(f"## ◯ N/A ({len(buckets['na'])})")
        lines.append("")
        lines.append("Skipped because they don't apply to this page (e.g. internationalization checks for a single-locale page, or hub-architecture-dependent checks).")
        lines.append("")
        lines.append(_render_compact_table(buckets["na"]))

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"_Generated by Cerberus on {_format_dt(int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000))}._")
    lines.append("")
    return "\n".join(lines)
