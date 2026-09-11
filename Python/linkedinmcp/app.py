"""The ASGI entry point for the `linkedin-outreach` Cloud Run service.

Run as `uvicorn --factory linkedinmcp.app:create_app` with `Python/` as the
working directory -- the same directory in the container, where it is `/app`.
That is what makes `settings.load_environment()` find the project's shared
`.env` file and `templates_dir`'s default resolve beside it.

**`create_app` is a factory, and there is no module-level `app` object.** That
is a ruling, not a preference:

- a module-level `app` would load settings while the module is imported, and
  `tests/linkedinmcp/test_app.py` imports this module at collection time with
  `Python/` as the working directory and no `OUTREACH_*` variable set. The
  whole test module would error out before a single test ran;
- the factory gives every test its own app with its own key, so no middleware
  instance leaks from one test into the next.

Importing this module opens no connection, reads no credential and makes no
network call. Calling `create_app()` with no arguments does read settings, so a
missing `OUTREACH_API_KEY` raises `ConfigError` at uvicorn startup. That is
intended: a container that refuses to start is a much better outcome than one
that starts and serves without authentication.

Beside the MCP app, two plain routes that Cloud Scheduler and the Unipile
webhook call, each behind `http_auth.require_api_key`: `POST /jobs/{job}`
runs `tick`, `sync` or `daily` through `run_jobs.run` (the same function the
command line uses), and `POST /webhooks/unipile` records that a sync is
wanted. Both are sync `def` handlers -- FastAPI runs those in its thread pool,
which is where the blocking jobs belong. A failure answers 500 with the
exception's class name only; the message stays in the service's own log.

Modules are imported as modules (`from linkedinmcp import mcp_server`), matching
the convention `linkedinmcp/settings.py` sets out.
"""

import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from linkedinmcp import clients, clock, http_auth, jobs, mcp_server, monitor, run_jobs, settings as cfg, state

logger = logging.getLogger(__name__)


class _ServeMcpWithoutSlash:
    """Route a request for exactly `/mcp` as if it had asked for `/mcp/`.

    Without this, Starlette answers `/mcp` with a 307 to `/mcp/`, and the
    Claude app's connector does not follow redirects: through v1.0.1, every
    connector configured without the slash failed with "Couldn't reach". On
    Cloud Run the redirect was doubly broken, because its `Location` came back
    as `http://` -- uvicorn does not trust the front end's `X-Forwarded-Proto`.

    The path is rewritten before routing, so both spellings reach the MCP app
    through the one `Mount` and the one `ApiKeyMiddleware` inside it. Serving
    `/mcp` any other way -- a second mount, a route aimed at FastMCP's handler
    -- risks a door that skips the key check. This class checks nothing
    itself, and must never start to.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


def _failure(error: Exception, route: str) -> JSONResponse:
    """A 500 naming only the exception's class.

    The message stays out of the response -- it can carry project ids, URLs
    or a contact's data. It goes to the service's log instead, with the
    traceback, which uvicorn would have logged had the exception not been
    caught here.
    """
    logger.exception("%s failed", route)
    return JSONResponse(status_code=500, content={"ok": False, "error": type(error).__name__})


async def _json_body(request: Request) -> Any:
    """The request body parsed as JSON, or a 400.

    A dependency rather than a body parameter: FastAPI answers malformed JSON
    in a declared body with a 422, and this route promises a 400. Declared
    after `require_api_key` on the route, so an unauthenticated caller is
    refused before its body is read.
    """
    raw = await request.body()
    try:
        return json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="The body must be JSON.") from None


def create_app(settings: cfg.OutreachSettings | None = None) -> FastAPI:
    """Build the service: the MCP app behind an API key, the key-protected
    `POST /jobs/{job}` and `POST /webhooks/unipile`, and an open `/health`.

    Pass `settings` to skip environment loading entirely -- that is the path
    every test takes, with
    `OutreachSettings(api_key="test-key", _env_file=None)`.
    """
    if settings is None:
        # `.env`, then the service's forced zero pacing, then the optional
        # `linkedinmcp/.env` -- see `settings.load_environment` for the order
        # and why the pacing is forced rather than left to that optional file.
        cfg.load_environment()
        settings = cfg.get_settings()

    mcp_app = mcp_server.mcp.http_app(
        path="/",
        stateless_http=True,
        json_response=True,
        # Auth is attached *here*, to the mounted MCP app, and not to the
        # FastAPI app. That is what leaves `/health` open, without the
        # middleware knowing any path or carrying any exemption list.
        middleware=[
            Middleware(
                http_auth.ApiKeyMiddleware, api_key=settings.api_key.get_secret_value()
            )
        ],
    )

    # Passing `mcp_app.lifespan` is required, not stylistic: the MCP session
    # manager starts there. Omit it and the server accepts connections and
    # then fails every call.
    app = FastAPI(lifespan=mcp_app.lifespan)

    # The MCP endpoint is `https://<service>/mcp/`, and `/mcp` without the
    # slash reaches the same place through `_ServeMcpWithoutSlash` rather than
    # a redirect. `ids.env`, the agent definitions and the vault keep the
    # slashed form.
    app.mount("/mcp", mcp_app)
    app.add_middleware(_ServeMcpWithoutSlash)

    @app.get("/health")
    def health() -> dict[str, bool]:
        """An unauthenticated liveness check, for a person or an uptime monitor
        that holds no key. Cloud Run itself never calls it: its default startup
        probe is a TCP check on port 8080, and `deploy.cmd` configures no HTTP
        probe.

        `/health`, not `/healthz`. Cloud Run's front end reserves some paths
        ending in `z` and answers them itself, so v1.0.0's `/healthz` returned
        Google's own 404 page in production while passing every local test.
        `test_no_route_ends_in_z` pins the rule.

        Open because of *where* the auth middleware is mounted, never because
        this path is special-cased. Do not add an app-wide auth middleware: it
        would gate this route along with everything else.
        `_ServeMcpWithoutSlash` is app-wide, but it rewrites one path and
        checks nothing.
        """
        return {"ok": True}

    @app.post("/jobs/{job}", dependencies=[Depends(http_auth.require_api_key)])
    def run_job(job: str, dry_run: bool = False) -> JSONResponse:
        """Run one job -- `tick`, `sync` or `daily` -- and answer its summary.

        Cloud Scheduler calls this. Any other job is a 404. `?dry_run=1`
        writes nothing and sends nothing, and is honoured only when the
        settings this app was built with allow it (`allow_http_dry_run`);
        otherwise it is a 400. `run_jobs.run` builds the clients, closes the
        LinkedIn client afterwards, and hands the job a `RuntimeState` on the
        real clock.
        """
        if job not in run_jobs.JOBS:
            raise HTTPException(status_code=404, detail="Not Found")
        if dry_run and not settings.allow_http_dry_run:
            raise HTTPException(status_code=400, detail="dry_run is disabled on this service.")
        try:
            summary = run_jobs.run(job, settings, dry_run=dry_run)
            return JSONResponse(jsonable_encoder(summary))
        except Exception as error:
            return _failure(error, f"POST /jobs/{job}")

    @app.post("/jobs/run/{job_id}", dependencies=[Depends(http_auth.require_api_key)])
    def run_started_job(job_id: str) -> JSONResponse:
        """Run a job a process-step tool started -- the call a Cloud Task
        makes back to this service (`monitor.submit`), so the job runs inside
        a request of its own, with its CPU for the whole job.

        `monitor.run` claims the job first, so a job delivered twice runs
        once. Every outcome of the job answers 200 -- it is recorded in the
        job itself, where `get_job` reads it -- so the queue never retries
        one; only a failure to reach Firestore at all is a 500.
        """
        try:
            return JSONResponse(jsonable_encoder(monitor.run(job_id, settings)))
        except Exception as error:
            return _failure(error, "POST /jobs/run")

    @app.post("/webhooks/unipile", dependencies=[Depends(http_auth.require_api_key)])
    def unipile_webhook(payload: Any = Depends(_json_body)) -> JSONResponse:
        """Unipile's new-message webhook: record that a sync is wanted.

        Every outcome of `jobs.handle_unipile_webhook` answers 200, an ignored
        event included -- Unipile retries anything else within 30 s, up to
        five times (https://developer.unipile.com/docs/webhooks-2). Unipile
        sends the key in the custom `x-api-key` header configured when the
        webhook is registered. A body that is not JSON is a 400; a failure
        here is a 500, which Unipile retries.
        """
        try:
            db = clients.firestore_client()
            now = clock.utcnow()
            result = jobs.handle_unipile_webhook(db, payload, now, state=state.RuntimeState(db, clock.utcnow))
            return JSONResponse(jsonable_encoder(result))
        except Exception as error:
            return _failure(error, "POST /webhooks/unipile")

    return app
