"""FastAPI application with SSE-powered inspection endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from .inspector import inspect_databases
from .models import (
    MYSQL_DATA_TYPES,
    InspectionQuery,
    InspectionSession,
    InspectionStatus,
)
from .teleport import check_login_status, find_tsh, get_clusters, login_to_cluster

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI(title="Data Type Inspector")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# In-memory session history (newest first)
history: list[InspectionSession] = []

# Track login state
_login_process = None
_logged_in_cluster: str = ""
_logged_in_username: str = ""


def _check_cluster_login(cluster: str) -> tuple[bool, str]:
    """Check if the user is logged into *cluster*. Returns (ok, username).

    Parses ``tsh status --format=json`` and verifies the active cluster matches.
    """
    import json as _json
    import subprocess

    tsh = find_tsh()
    result = subprocess.run(
        [tsh, "status", "--format=json"],
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return False, ""

    status = _json.loads(result.stdout)

    # Check active profile first
    active = status.get("active", {})
    if active.get("cluster", "") == cluster and active.get("username", ""):
        return True, active["username"]

    # Check inactive profiles (logged in but not the current active session)
    for profile in status.get("profiles", []):
        if profile.get("cluster", "") == cluster and profile.get("username", ""):
            return True, profile["username"]

    return False, ""


def _resolve_username(cluster: str = "") -> str:
    """Return the logged-in Teleport username for *cluster*."""
    global _logged_in_username, _logged_in_cluster
    if _logged_in_username and _logged_in_cluster == cluster:
        return _logged_in_username
    try:
        if cluster:
            ok, username = _check_cluster_login(cluster)
        else:
            tsh = find_tsh()
            ok, username = check_login_status(tsh)
        if ok:
            _logged_in_username = username
            _logged_in_cluster = cluster
            return username
    except Exception:
        pass
    return ""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    clusters = get_clusters()
    username = _resolve_username()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "clusters": clusters,
            "mysql_data_types": MYSQL_DATA_TYPES,
            "history": history,
            "username": username,
        },
    )


@app.post("/api/login", response_class=HTMLResponse)
async def api_login(request: Request):
    global _login_process
    form = await request.form()
    cluster = str(form.get("cluster", ""))
    if not cluster:
        return HTMLResponse('<div class="error">No cluster selected</div>')

    try:
        tsh = find_tsh()
        _login_process = login_to_cluster(tsh, cluster)
        return templates.TemplateResponse(
            "partials/login_status.html",
            {"request": request, "status": "pending", "cluster": cluster},
        )
    except Exception as e:
        return HTMLResponse(f'<div class="error">{e}</div>')


@app.get("/api/login-status", response_class=HTMLResponse)
async def api_login_status(request: Request):
    global _logged_in_username, _logged_in_cluster
    cluster = request.query_params.get("cluster", "")
    try:
        logged_in, username = _check_cluster_login(cluster)
        if logged_in:
            _logged_in_username = username
            _logged_in_cluster = cluster
            return templates.TemplateResponse(
                "partials/login_status.html",
                {
                    "request": request,
                    "status": "success",
                    "username": username,
                    "cluster": cluster,
                },
            )
        else:
            return templates.TemplateResponse(
                "partials/login_status.html",
                {"request": request, "status": "pending", "cluster": cluster},
            )
    except Exception:
        return templates.TemplateResponse(
            "partials/login_status.html",
            {"request": request, "status": "pending", "cluster": cluster},
        )


@app.get("/api/inspect")
async def api_inspect(request: Request):
    cluster = request.query_params.get("cluster", "")
    database_name = request.query_params.get("database_name", "")
    table_name = request.query_params.get("table_name", "")
    column_name = request.query_params.get("column_name", "")
    expected_data_type = request.query_params.get("expected_data_type", "")

    if not all([cluster, database_name, table_name, column_name, expected_data_type]):
        async def error_stream():
            yield {"event": "error", "data": "All fields are required"}
        return EventSourceResponse(error_stream())

    db_user = _resolve_username(cluster)
    if not db_user:
        async def error_stream():
            yield {"event": "error", "data": f"Not logged in to {cluster}. Click Login first."}
        return EventSourceResponse(error_stream())

    query = InspectionQuery(
        cluster=cluster,
        database_name=database_name,
        table_name=table_name,
        column_name=column_name,
        expected_data_type=expected_data_type,
        db_user=db_user,
        started_at=datetime.now(),
    )

    session = InspectionSession(query=query)

    async def event_stream():
        try:
            async for result, current, total in inspect_databases(query):
                session.total_databases = total
                session.results.append(result)

                row_html = templates.get_template("partials/result_row.html").render(
                    result=result,
                    expected=expected_data_type,
                )
                progress_html = templates.get_template("partials/progress.html").render(
                    current=current,
                    total=total,
                    session=session,
                )

                yield {
                    "event": "result",
                    "data": json.dumps({
                        "row_html": row_html,
                        "progress_html": progress_html,
                        "status": result.status.value,
                    }),
                }

            # Done — build not-found section
            session.completed = True
            not_found = [r for r in session.results if r.status == InspectionStatus.NOT_FOUND]
            errors = [r for r in session.results if r.status == InspectionStatus.ERROR]

            not_found_html = ""
            if not_found or errors:
                not_found_html = templates.get_template("partials/not_found.html").render(
                    not_found=not_found,
                    errors=errors,
                )

            progress_html = templates.get_template("partials/progress.html").render(
                current=session.total_databases,
                total=session.total_databases,
                session=session,
                done=True,
            )

            history.insert(0, session)

            history_html = templates.get_template("partials/history.html").render(
                history=history,
            )

            yield {
                "event": "done",
                "data": json.dumps({
                    "not_found_html": not_found_html,
                    "progress_html": progress_html,
                    "history_html": history_html,
                    "match_count": session.match_count,
                    "mismatch_count": session.mismatch_count,
                    "not_found_count": session.not_found_count,
                    "error_count": session.error_count,
                }),
            }

        except asyncio.CancelledError:
            session.completed = True
            history.insert(0, session)
            raise

        except Exception as e:
            logger.exception("Inspection error")
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(event_stream())


@app.get("/api/history", response_class=HTMLResponse)
async def api_history(request: Request):
    return templates.TemplateResponse(
        "partials/history.html",
        {"request": request, "history": history},
    )


@app.get("/api/history/{index}", response_class=HTMLResponse)
async def api_history_detail(request: Request, index: int):
    if index < 0 or index >= len(history):
        return HTMLResponse('<div class="error">Session not found</div>')

    session = history[index]
    return templates.TemplateResponse(
        "partials/history_detail.html",
        {"request": request, "session": session},
    )
