# Cerberus — Notes for Claude

The README covers install, architecture, and what the 57 checks do. This file
captures only the gotchas Claude sessions have hit that aren't visible from code.

## Running ad-hoc checks

Don't write your own driver if you only need to run one check against a URL.
Use the venv directly:

```python
# .venv/bin/python -c '...'
import asyncio
from cerberus import config as cfg
from cerberus.checks import find_check
from cerberus.checks.base import CheckContext, Env
ctx = CheckContext(url="https://example.com/page", site_config=cfg.load(), environment=Env.PRODUCTION)
result = asyncio.run(find_check("A1").func(ctx))
```

## Daemon + schema migrations

Cerberus is typically run as a long-lived `python run.py` process on `:8000`.
`store.init()` runs on startup, so any `ALTER TABLE` migration in `store.py`
only takes effect after a restart. When testing schema changes, smoke-test on
a different port (`uvicorn` directly on `:8001`) without disturbing the daemon
the operator is using.

## Comparing rendered text vs raw HTML

Playwright's `innerText` applies CSS `text-transform`, so a heading styled
`text-transform: uppercase` is `CREATE YOUR SITE` in rendered output and
`Create Your Site` in raw HTML. Any check comparing the two must casefold AND
match scopes (`<main>/<article>` on both sides) — see A1 (`a_rendering.py`)
and H2 (`h_serving.py`) for the canonical pattern. Bot-vs-user comparisons
(H1) are raw-vs-raw and don't need this.

## YAML config additions

`yaml.safe_load` parses bare `no`, `yes`, `on`, `off`, `true`, `false` as
Python bools. When adding any new list/scalar field to `site_config.yaml` that
the loader will iterate, `str()`-coerce values before downstream string ops
(see `config.py:supported_languages` for the precedent). Operators may not
know to quote `'no'` in YAML.

## Pre-push ceremony

Use `/pre-push` per the global CLAUDE.md before pushing. Codex (Phase 2) is
mandatory; the optional phases are scope-gated. This is a real gate, not
ceremony — it has caught actual bugs in this codebase (e.g., the empty-`<main>`
H2 fallback was a Phase 2 finding).

## Parsing HTML attributes

Don't regex attribute values. A regex like `name="description"\s+content="([^"]*)"`
silently captures nothing on single-quoted pages, so the check sees `""` and
passes by accident. Use the BS4 helpers in `cerberus/checks/_utils.py`
(`get_meta`, `get_title_text`, `get_canonical_href`, `collect_static_paths`,
`collect_insecure_urls`); add siblings there, don't inline. E6/E7/F3 each
had this bug class until they were converted.

## Asserting redirects: validate Location, not just status

`status_code in (301, 308)` is necessary, not sufficient — a 301 to the wrong
host or path passed B5 silently until B4/B5/B6 were tightened (verified on
strikingly preprod, where the apex variant 301s to a different mystrikingly.com
host). Canonical pattern: `_variant_redirect_status` in `b_indexability.py`.
It resolves relative Locations, normalizes both sides, and keeps 302/307 as
Needs Review (not Pass).

## Surface what the check didn't actually evaluate

When a filter, cap, or parse-error path silently shrinks the evaluated set,
the verdict only applies to the subset — silent shrinking invites false Pass.
Examples: G3's `supported_languages` filter dropping locales, F8's 50-link
cap, MAX_LOCALES=30 truncation tail, extruct silently dropping malformed
JSON-LD blocks. Surface the dropped/truncated/unchecked count as a Needs-
Review sub-step when non-zero. If the entire set was filtered out, escalate
to NEEDS_REVIEW — don't fall through to NA, that hides the gap.

The same surfacing principle applies to env-incompatible sub-steps, but the
right status is different. When a sub-step is *expectedly* skipped because
the env doesn't have the infra (e.g., B5's apex→www depends on a CDN edge
rule only configured on prod), emit `Status.NA` with the reason in `detail`
rather than omitting the SubStep. NA — not NEEDS_REVIEW — because the gap
is known and explained. `from_substeps` treats `{PASS, NA}` as PASS, so
semantics don't shift, but the report stays self-documenting about what
was skipped and why. Use NEEDS_REVIEW for *uncertainty* (filter/cap drops);
use NA for *known incompatibility* (env-class skips). Whole-check
incompatibility still goes through `applicable_envs` at @register, not
inline.

## `from_substeps` promotes MANUAL → NEEDS_REVIEW

`CheckResult.from_substeps()` resolves `Status.MANUAL` sub-steps to
`Status.NEEDS_REVIEW` on the parent result. So after calling it,
`result.status == Status.MANUAL` is **always False**. To conditionally
set `result.instruction`, check the sub-steps directly:

```python
if any(s.status == Status.MANUAL for s in sub):
    result.instruction = MY_MANUAL_INSTRUCTION
```

## Automating manual sub-steps with the LLM

`vision.classify()` accepts `images=[]` for text-only calls, so any
previously MANUAL sub-step that compares text can be automated (see D3
in `d_structured_data.py` for the canonical pattern). Two rules:

- Use `verdict.provider == "fallback-to-manual"` as the sentinel for
  degrading back to `Status.MANUAL` (not `verdict.verdict`).
- When LLM input is truncated, downgrade `fail` → `NEEDS_REVIEW`; the
  model only saw partial data and a hard FAIL would be misleading.

## No public API returns Google-WRS-rendered HTML

The GSC URL Inspection API does **not** return rendered HTML — its public
schema (`UrlInspectionResult`) has no `liveInspectionResult` or
`renderedPageInfo` field. H3 historically read those fields and silently
fell through to MANUAL on every run. The Mobile-Friendly Test API used to
expose `renderedHtml` but was deprecated 2023-12-04 with no replacement.
PSI returns only a screenshot, not DOM. Don't reintroduce a "call Google
for the rendered HTML" dependency — the only paths now are local headless
Chromium (canonical: H3) or human paste from the GSC web UI.

## Operator-override is gated to Manual / NEEDS_REVIEW

`report.py` (`if marked and c["status"] in ("Manual", "Needs Review")`)
only honors operator manual overrides on those two statuses. A check that
auto-returns PASS (or FAIL) cannot be downgraded by the operator from the
UI. Partly-automated checks where a human still wants the option to
escalate (H3 since the GSC removal — the local Chromium simulation isn't
WRS ground truth) must cap their best verdict at NEEDS_REVIEW. See the
post-`from_substeps` cap in `h_serving.py` H3.

## Bot-vs-user UA diffs: pin both UAs to the same Chrome version

When diffing two browser renders that differ only by UA (the H3 pattern),
both UAs must come from the same launched-browser Chrome version.
Statically-versioned `UA_CHROME_DESKTOP` (Chrome/120) against a live
Googlebot UA (Chrome/147) caused false content mismatches on sites with
version-gated banners or compatibility shims. Use `browser.googlebot_wrs_ua()`
and `browser.chrome_desktop_ua()` — both go through `_versioned_ua` and
share the live Chrome version. The static `UA_CHROME_DESKTOP` constant is
fine for raw httpx fetches (no rendering, no version-gated UI), which is
what H1 still uses.

## Lighthouse details.items shape varies per audit

Lighthouse audits in the SEO category emit `audit.details.items[*]` with at
least four shapes, none of which are documented in the LHR schema:

- **Node-nested**: `{"node": {"snippet": "<...>", "nodeLabel": "..."}}` —
  `image-alt`, `crawlable-anchors`, `tap-targets`, `font-size`
- **Link-shaped (flat)**: `{"href": "...", "text": "...", "textLang": "en"}` —
  `link-text` (no `node` wrapper at all)
- **Source-location wrapper**: `{"source": {"type": "source-location", "url",
  "line", "column"}}` — `is-crawlable`, `robots-txt`
- **Plain URL**: `{"url": "..."}` — performance-side audits

A single fallback chain like `node.snippet → href → url` silently produces
wrong output: a `link-text` failure renders as just the URL (hiding "Learn
More" — the actual non-descriptive text), and `is-crawlable` renders as a
Python dict repr. Dispatch by shape (see `_format_audit_item` in
`cerberus/lighthouse.py`) — and verify against a real fixture, not the
schema docs.
