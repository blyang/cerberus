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
