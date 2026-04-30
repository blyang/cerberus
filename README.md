# Cerberus — SEO/GEO Pre-Flight Checker

A web tool that runs **57 SEO and generative-engine-optimization checks** against any URL and produces a prioritized markdown report for the dev team. Built to replace an hour of manual `curl`-by-hand auditing per page.

- **Backend**: FastAPI + SSE for live progress, SQLite for persistence
- **Frontend**: vanilla JS + Tailwind CDN, no build step
- **Vision**: Gemini 2.5 Flash-Lite primary + Qwen-VL fallback for the previously-manual checks (visual flash-on-load, intrusive interstitial, mobile responsiveness)
- **Performance**: Lighthouse subprocess, mobile + desktop in parallel
- **Browser**: Playwright Chromium (shared pool)

## What it checks

Eight sections, A through H, ~57 checks total:

| Section | Coverage |
|---|---|
| **A** — Rendering & status | SSR vs client-rendered text, HTTP status, fragments, flash-on-load (vision) |
| **B** — Indexability | title/description, canonical, redirect canonicalization, host normalization, noindex/nosnippet, lang/charset/viewport, OG + Twitter, og:image dimensions, favicon, lowercase URLs |
| **C** — Semantics | heading structure, form labels, alt text, semantic wrappers, intrusive interstitials (vision) |
| **D** — Structured data | JSON-LD validation, JSON-LD ↔ visible content match |
| **E** — Performance | LCP, CLS, INP, perf+SEO+a11y+best-practices scores, mobile responsiveness (DOM + vision), tap targets, mobile↔desktop parity, mixed content, HSTS |
| **F** — Discoverability | sitemap entry, robots.txt, bot access, soft-404 detection, internal linking, AI crawler policy |
| **G** — Internationalization | hreflang reciprocity, x-default, locale URL liveness, locale-adaptive serving (real cluster evaluation when alternates exist) |
| **H** — Serving integrity | bot-vs-user cloaking, hidden content, Googlebot↔browser render diff (via GSC URL Inspection API) |

## Quick start

```bash
# Install (Python 3.11+, Node 18+, sudo for Playwright system deps)
./setup.sh

# Optional: enable vision-classified checks (A2, C7, E4)
cp .env.example .env
# Edit .env to add GEMINI_API_KEY and DASHSCOPE_API_KEY

# Configure target sites
$EDITOR site_config.yaml

# Run
source .venv/bin/activate
python run.py
```

Open `http://127.0.0.1:8000` (or `http://<tailnet-ip>:8000` from another device on the same Tailscale network). Paste a URL, click Run, watch the live progress, then download the prioritized markdown report when complete.

## Architecture

```
cerberus/
  checks/{a..h}_*.py    — 57 checks registered via @register decorator
  runner.py             — async worker, per-run pub/sub, bounded parallelism
  store.py              — SQLite persistence
  lighthouse.py         — subprocess wrapper, parallel mobile + desktop
  browser.py            — Playwright pool, screenshot helpers, Slow-3G frame capture
  vision.py             — Gemini Flash-Lite primary + Qwen-VL fallback, structured JSON output
  report.py + verify_steps.py
                        — prioritized dev-team report with reproduce + pass-condition steps
frontend/
  index.html, app.js, style.css
                        — single-page UI, SSE-driven progress, refresh recovery via ?run=<id>
```

Each check is a Python `async` function returning `CheckResult(status, summary, details, sub_steps)`. To add a new check, drop a function into the appropriate section file with the `@register(...)` decorator. ETA estimates and runtime tracking happen automatically.

## Determinism for vision checks

Vision-classified checks (A2, C7, E4 overlap) run with `temperature=0`, `top_p=0.1`, structured JSON output via `responseSchema`, and 2-shot anchored prompts. The fallback chain emits `Status.MANUAL` rather than auto-passing when both primary and fallback return low confidence, so the operator always knows when to verify by hand.

## Cost

At Gemini 2.5 Flash-Lite pricing (~$0.0002 per image), full-run vision cost is ~$0.0006 per audit. Runs are budgeted at $0.10/run as a safety cap. Lighthouse, Playwright, and HTTP fetches have no per-run dollar cost.

## Source-of-truth checklist

The pass conditions and verify steps come from a Google Sheet (referenced in `seo_geo_checker_brief.md`). When that sheet's "How to Verify" or "Pass Condition" columns change, the corresponding entry in `cerberus/verify_steps.py` should be updated.

## Threat model & known limitations

Cerberus is designed for use on a **private VM bound to Tailscale**. The brief explicitly out-of-scopes authentication on the checker itself; identity comes from the operator's tailnet.

The tool fetches operator-supplied URLs and **secondary URLs derived from each page** (canonical hrefs, sitemap entries, hreflang alternates, og:image, JSON-LD URLs). Those secondary URLs are attacker-influenceable if the audited page contains attacker-controlled content. To bound that surface:

- ✅ **Initial-host SSRF guard** (`cerberus/checks/_utils.py:validate_target_url`): every URL is DNS-resolved and rejected if the host is loopback / RFC1918 / link-local / multicast / reserved / unspecified. Applied to the operator's URL and every secondary fetch.
- ✅ **Redirect-chain SSRF guard**: httpx auto-redirects are disabled; we walk redirects manually and re-validate every Location target, capped at 8 hops.
- ⚠️ **DNS-rebinding residual risk**: an attacker controlling DNS for `attacker.example` could return a public IP at validation time and a private IP when the actual request is issued (a subsequent resolution by httpx / Playwright / Lighthouse). Mitigation requires pinning a single resolved IP through the entire request lifecycle across three different networking stacks; not implemented. **Don't run Cerberus against URLs you suspect are adversarial.**
- ⚠️ **No request-time auth**: any device on the Tailscale tailnet that can reach `:8000` can list and read all runs and submit new ones. This is by design (per the brief) and relies on Tailscale's identity boundary. If you ever expose port 8000 beyond the tailnet, add an auth layer first.
- ⚠️ **Error verbosity**: HTTP fetch / Playwright / Lighthouse / parser exceptions are persisted into `check_results.details_json` and exposed via `/api/runs/{id}`. They can include URLs, parse internals, and library error text. No credentials leak (those go through the redacted `gsc.py` path), but consider this when sharing run data.

## License

MIT — see `LICENSE`.
