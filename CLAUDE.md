# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Create venv (requires Python 3.12+)
python3.12 -m venv .venv

# Install (dev mode)
.venv/bin/pip install -e "."

# Run the server (auto-reload enabled)
.venv/bin/python run.py
# or
.venv/bin/python -m datatype_inspector

# Server runs at http://127.0.0.1:8000
```

## Architecture

Web tool that connects to every MySQL database in a Teleport cluster and reports what data type a given column uses in each connection. Results stream in real-time via SSE, color-coded green (match) / red (mismatch), with mismatches sorted to top.

**Tech stack**: Python 3.12, FastAPI, Jinja2 templates, HTMX (CDN), vanilla JS for SSE, PyMySQL, sse-starlette.

### Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Main page with form, cluster dropdown, data type dropdown |
| POST | `/api/login` | Trigger `tsh login <cluster>` for SSO |
| GET | `/api/login-status` | HTMX poll (every 2s) to detect SSO completion |
| GET | `/api/inspect` | **SSE endpoint** — streams results as each DB is queried |
| GET | `/api/history` | History sidebar HTML partial |
| GET | `/api/history/{index}` | Past session full results |

### Key Files

- `app.py` — FastAPI app, routes, SSE event stream, in-memory session history
- `inspector.py` — Async generator: tunnels to each DB sequentially, queries `information_schema.COLUMNS` for `DATA_TYPE`. Uses `asyncio.to_thread()` to avoid blocking the event loop.
- `teleport.py` — `tsh` CLI integration: find binary, list clusters/databases, start/stop tunnels, SSO login. Uses `--proxy=` instead of `--cluster=` for `tsh db ls` and `tsh db login`/`tsh proxy db` (required when the target cluster isn't the active profile). Cluster-aware login status (`get_login_status`) checks both `active` and `profiles` arrays. Thread-safe tunnel registry (`register_tunnel`/`unregister_tunnel`/`cleanup_all`) tracks all active tunnels for cleanup on shutdown. Timeouts on all subprocess calls (10s status, 30s db login/ls). **Note**: `tsh status` returns exit code 1 even when logged in — never use `check=True` with it.
- `models.py` — Dataclasses (`InspectionResult`, `InspectionSession`, `InspectionQuery`, `DatabaseEntry`), `InspectionStatus` enum, `MYSQL_DATA_TYPES` category dict
- `templates/index.html` — Main page with form + vanilla JS `EventSource` handler that sorts mismatches to top
- `templates/partials/` — HTMX fragments: `result_row.html`, `progress.html`, `not_found.html`, `login_status.html`, `history.html`, `history_detail.html`

### Data Flow

1. User fills form (cluster, database name(s), table, column, expected type). Database name field accepts comma-separated list (whitespace trimmed). DB user is resolved automatically from `tsh status --format=json` — login validation is **cluster-aware** (checks both `active` and `profiles` arrays to find the matching cluster, not just that *some* session exists).
2. Frontend opens `EventSource` to `/api/inspect?params`
3. Backend lists all MySQL DBs on the cluster via `tsh db ls --format=json`
4. For each DB sequentially (one tunnel at a time to avoid port conflicts): open tunnel → PyMySQL connect → query `information_schema.COLUMNS` with `TABLE_SCHEMA IN (...)` for all requested databases → close tunnel → yield one SSE `result` event per database name
5. Frontend JS inserts rows: mismatches/errors before first `.match-row`, matches appended at bottom. Results include Database column to distinguish multi-database queries.
6. On completion: SSE `done` event with not-found section, final stats, updated history sidebar HTML

### SSE Event Types

- `result` — JSON: `row_html`, `progress_html`, `status` (match/mismatch/not_found/error)
- `done` — JSON: `not_found_html`, `progress_html`, `history_html`, summary counts
- `error` — plain text error message

### Session History

In-memory `list[InspectionSession]` in `app.py` (newest first, lost on restart). Sidebar shows past inspections with pill-badge counts; clicking loads full results via HTMX `hx-get`.
