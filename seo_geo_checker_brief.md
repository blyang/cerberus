# SEO/GEO Pre-Flight Checker — Dev Brief

*Version 1.0 — March 2026*

## Glossary

| Term | Definition |
|---|---|
| **Checklist** | The SEO/GEO Pre-Flight Checklist Google Sheet. Source of truth for which checks exist and what they verify. Link: https://docs.google.com/spreadsheets/d/1Kka3oAavdaWHCEDGYbBZFG8vAwq1C_1fpWlV226ozKE/edit |
| **Checker** | The tool being built. A web app that runs checklist items against a URL and reports results. |
| **Run** | One execution of the checker against one URL. |
| **Check** | One row from the checklist (e.g., A1, B3, F9). |
| **Operator** | Person using the checker. Initially Ben; later anyone on the team. |
| **Site config** | A small config file containing site-level constants (sitemap URL, pipeline path prefixes, allowed-bot list). |

## Scope

**Objective**

Build a web tool where the operator pastes a URL, clicks Run, and gets a sorted report of which checklist items pass, fail, or need manual review. The checker handles all automatable checks itself and gives clear instructions for the manual ones.

**In Scope (MVP)**

- Web frontend with a URL input and Run button
- Live progress display during a run, including which checks have completed and a rough ETA
- Automated execution of every checklist item that can sensibly be automated
- Lighthouse CI integration for the performance/accessibility/best-practices items
- Results display sorted by severity, with failures and manual items at the top
- Per-failure detail showing what was checked, what was expected, and what was found
- Per-manual-item advice telling the operator exactly what to do
- A small site config file the operator can edit (sitemap URL, pipeline prefixes, allowed bots)

**Out of Scope (deferred)**

- Authentication on the checker itself
- Per-page content correctness (verifying the right copy from the i18n sheet shipped — this is a separate tool)
- Bulk testing across many URLs (Phase 2)
- Persistent results history across runs
- CI integration (Phase 2)
- The checklist items marked N/A until hub architecture exists (D5, F1 step 3, F6) — implement skeletons that report N/A

## What the Operator Does

1. Opens the checker in a browser.
2. Pastes a URL into the input field.
3. Clicks Run.
4. Watches checks complete in real time, with status appearing per row as each finishes.
5. Reviews the sorted results when the run completes.

## What the Operator Sees During a Run

**R1.1 — Input and start**

The page shows a single text input labeled "Page URL to test" and a Run button. The operator pastes a URL and clicks Run. The button changes to "Running..." and is disabled until the run completes.

**R1.2 — Progress display**

Below the input, a progress section shows:

- A summary line: "Running 47 of 54 checks. ETA: ~22s remaining."
- A list of all checks, each row showing: check ID (e.g., B3), severity badge (Blocking / Recommended / Conditional), short title, and current status (Pending / Running / Pass / Fail / Manual / N/A).
- Rows update live as checks complete. The currently-running check is visually highlighted.
- ETA recalculates as each check finishes, based on average duration of completed checks.

**R1.3 — Run completion**

When the last check finishes, the progress section collapses to a summary banner ("3 failed, 2 need review, 4 manual, 42 passed, 3 N/A") and the full sorted results table appears below it.

## What the Operator Sees After a Run

**R2.1 — Results sort order**

Results are grouped and sorted in this order, top to bottom:

1. **Failed (Blocking severity)** — most urgent
2. **Failed (Recommended or Conditional severity)**
3. **Needs Review** — checks that were partly automated but require a final human glance (D3 step 5, H3)
4. **Manual** — checks that must be done by a human, with instructions
5. **Passed** — collapsed by default; click a row to expand
6. **N/A** — collapsed by default

**R2.2 — Failed item display**

Each failed row shows:

- Check ID and severity
- The check title (from the checklist)
- The specific assertion that failed (e.g., "Canonical href = https://example.com/page; expected = https://www.strikingly.com/s/foo")
- The raw command output or relevant snippet (collapsible)
- A link back to the corresponding row in the checklist sheet for full context

**R2.3 — Manual item display**

Each manual row shows:

- Check ID and severity
- The check title
- A clear instruction telling the operator exactly what to do (see R2.5 for the text)
- A "Mark as Pass" / "Mark as Fail" toggle the operator can click after completing the manual check (state is local to this run; not persisted)

**R2.4 — Passed item display**

Collapsed by default. Each row shows just the check ID, title, and a green checkmark. Clicking expands to show the assertion that passed and the value found.

**R2.5 — Manual item instructions**

The checker shows these exact instructions for the manual items:

| Check | Instruction |
|---|---|
| **A2** (no flash on load) | Open the page in Chrome DevTools Performance panel. Enable Screenshots and set Network throttling to Slow 3G. Record a fresh page load in incognito. Step through the frame-by-frame screenshots. Confirm the first text-bearing frame shows the same primary content as the final frame. |
| **C7** (no intrusive interstitial) | Load the page in a fresh incognito window at 375px width, then again at 1440px width. Confirm no modal, overlay, newsletter gate, cookie wall, or app-install prompt obscures primary content on first paint. Cookie consent banners are acceptable only if limited to legal-minimum GDPR/CCPA. |
| **E4** (mobile responsive) | Open DevTools Device Mode at 375px. Scroll the page. Tap each form input and the CTA button. Confirm no horizontal scroll, no overlap, all body text ≥ 14px. |
| **F9 step 4** (CDN/WAF policy) | Log into the Cloudflare or CDN dashboard. Confirm no WAF rule or bot-management rule blocks or rate-limits the IP ranges of bots in the allowed-bots list. |
| **D3 step 5** (free-text JSON-LD sanity) | Review the JSON-LD blocks shown in the check details below. Confirm no free-text field (description, name, etc.) makes claims that contradict what visitors see on the page. |
| **H3** (Googlebot render diff) | Optional. If desired, run GSC URL Inspection → Test Live URL on this page. Compare the rendered HTML to the headless browser render shown in the check details. Subject to GSC daily quota. |

## The Site Config File

A single YAML file the operator edits when site-level constants change. Example:

```yaml
sitemap_url: https://www.strikingly.com/sitemap-seo-pages.xml
pipeline_url_prefixes:
  - /s/website/
  - /s/portfolio/
  - /s/landing-page/
allowed_bots:
  - Googlebot
  - Bingbot
  - OAI-SearchBot
  - ClaudeBot
  - PerplexityBot
gsc_credentials_path: ./gsc-creds.json   # optional, for H3
```

The checker reads this file at run start. If a required field is missing, the corresponding checks fail with a clear "site config missing X" message rather than silently passing.

## Performance Checks (Lighthouse)

Performance items E1, E2, E3, E3a, E3b run via Lighthouse CI under the hood, one run per check, mobile and desktop. Pass conditions follow the checklist exactly (LCP ≤ 2.5s, CLS < 0.1, INP < 200ms, scores ≥ 90).

This is a single-run check, not the 10-runs-averaged statistical assessment from the ASB landing page spec. The 10-run statistical version is a separate concern and not part of this checker.

## Verification

**TC1: Healthy page passes most checks**

Run against the newly revamped pre-prod ASB landing page. Expected: most blocking checks pass; any failures point to real issues; manual items appear with instructions.

**TC2: Page with noindex fails appropriately**

Run against a test page with `<meta name="robots" content="noindex">`. Expected: B7 fails with the actual noindex value shown; all other checks complete normally.

**TC3: Nonexistent URL**

Run against a URL that returns 404. Expected: A3 passes (404 is correct for a nonexistent URL), but downstream checks that depend on a 200 response are skipped or fail clearly with "page returned 404, cannot run."

**TC4: Site config missing**

Run with no site config file. Expected: F1, F2, F3, F5, F9 fail with "site config missing." Other checks complete normally.

**TC5: Manual items never auto-pass**

Run any URL. Confirm A2, C7, E4, F9-step-4 always appear in the Manual section with instructions, never in Passed or Failed automatically.

**TC6: Progress and ETA**

Start a run. Confirm the progress list updates as checks complete, the ETA decreases roughly proportionally, and no check sits as "Running" for longer than its actual runtime.

## Architecture Notes (Non-Prescriptive)

*Suggestions for engineering. Not requirements.*

a) **Frontend.** Single-page web app. React + Tailwind is fine; plain HTML + vanilla JS is also fine. The UI is one input, one button, one live-updating list, one results table. No routing, no state library needed.

b) **Backend.** Python preferred (familiar territory; clean subprocess calls to curl/grep/xmllint/Lighthouse CI). Node is acceptable if Lighthouse CI integration is meaningfully easier there. FastAPI or Flask is enough.

c) **Live updates.** Server-Sent Events (SSE) is simplest. WebSocket also fine. Each completed check emits one event the frontend appends to the list.

d) **Check implementation.** Each check is a Python function returning `{status, summary, details}`. Functions are mapped by check ID. Adding a new checklist row means adding a function and registering it.

e) **Lighthouse CI.** Invoke as a subprocess. Parse the JSON report and pull LCP, CLS, INP, scores. One mobile run + one desktop run per check that needs it.

f) **ETA computation.** Track average runtime per check across past runs (in-memory, optionally persisted to a JSON file). For the first run with no history, hardcode reasonable estimates (curl checks ~1s, Lighthouse ~30s).

g) **GSC API for H3.** Use Google's Search Console API (`webmasters.googleapis.com`) URL Inspection endpoint. Service account auth via the credentials file referenced in site config. Rate-limited; if quota is exhausted, mark H3 as Manual instead of failing.

h) **Hosting.** localhost is sufficient for MVP. The operator runs `python run.py`, opens `http://localhost:8000`, and uses it. Production deployment is a Phase 2 concern.

i) **Headless browser.** Playwright in Python. Used for A1 (rendered DOM extraction), D3 (rendered text), H1 (browser-render diff against bot-UA curl), H2 (rendered vs raw diff), H3 (browser side of the GSC diff).

j) **Schema validation for D2.** Use the schema.org validator HTTP endpoint or the `structured-data-testing-tool` npm package as a subprocess. Skip the manual "paste into Rich Results Test" workflow.

k) **Image dimension check for B14.** Use `Pillow` to read dimensions after downloading the og:image and twitter:image. No need for ImageMagick.

l) **Sitemap parsing for F1.** Fetch sitemap, parse XML, look for the URL. Handle sitemap index files (the parent points to child sitemaps; recurse one level).

## Estimated Engineering Effort (MVP)

| Task | Estimate |
|---|---|
| Frontend (input, progress, results) | 1.5–2 days |
| Backend skeleton + SSE streaming | 1 day |
| Implement all curl-based checks (~30 of them) | 2–3 days |
| Lighthouse CI integration (E1–E3b) | 0.5–1 day |
| Headless browser checks (A1, D3, H1, H2) | 1 day |
| Site config loader + per-config-dependent checks | 0.5 day |
| Manual item display + instruction text | 0.5 day |
| Sorting, results table, pass/fail toggles | 0.5 day |
| Test cases TC1–TC6 | 0.5 day |
| **Total** | **8–10 days** |

## Source-of-Truth Reminder

The checklist Google Sheet is the source of truth for *what* each check verifies. The checker hardcodes the *implementation* of each check (the actual Python function). When a checklist row is added or modified, the checker must be updated to match. The checker does not read verify-step text from the sheet at runtime — that text is for humans, not code.
