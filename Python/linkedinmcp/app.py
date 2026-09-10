"""The ASGI entry point for the `linkedin-outreach` Cloud Run service.

Run as `uvicorn --factory linkedinmcp.app:create_app` with `Python/` as the
working directory -- the same directory in the container, where it is `/app`.
That is what makes `load_dotenv(".env")` find the project's shared environment
file and `templates_dir`'s default resolve beside it.

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

Modules are imported as modules (`from linkedinmcp import mcp_server`), matching
the convention `linkedinmcp/settings.py` sets out.
"""

from dotenv import load_dotenv
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from linkedinmcp import mcp_server, settings as cfg
from linkedinmcp.http_auth import ApiKeyMiddleware


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


def create_app(settings: cfg.OutreachSettings | None = None) -> FastAPI:
    """Build the service: the MCP app behind an API key, plus an open
    `/health`.

    Pass `settings` to skip environment loading entirely -- that is the path
    every test takes, with
    `OutreachSettings(api_key="test-key", _env_file=None)`.
    """
    if settings is None:
        # Two files, in this order, both by explicit relative path and never
        # via a bare `load_dotenv()` -- the bare form calls `find_dotenv()`,
        # which walks up the directory tree and would pick up whatever it found
        # first.
        #
        # `.env` is the project's shared file, the same one the notebooks read.
        # It carries every credential -- `OUTREACH_API_KEY` included, alongside
        # `UNIPILE_*` and `GOOGLE_API_KEY` -- and the rate limits `SendBudget`
        # enforces. `linkedinmcp/.env` is optional and holds one thing: the
        # zero-pacing values. Pacing is the only setting whose right value
        # genuinely differs between a notebook, which sleeps between calls
        # inside one long process, and this service, where the scheduler
        # interval is the cadence and an in-process sleep would be billed
        # wall-clock time spent doing nothing. `override=True` is what makes the
        # second file win where the two name the same variable.
        #
        # The working directory is `Python/` locally and `/app` in the
        # container, which is the same directory, so both paths resolve
        # identically in both places. Nothing from either file is ever printed.
        load_dotenv(".env")
        load_dotenv("linkedinmcp/.env", override=True)
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
                ApiKeyMiddleware, api_key=settings.api_key.get_secret_value()
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

    return app
