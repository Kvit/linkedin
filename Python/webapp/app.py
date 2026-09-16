"""The contacts webapp: `uvicorn --factory webapp.app:create_app`.

No module-level `app`, for the reason `linkedinmcp/app.py` gives: building
one at import would read settings and break test collection. Pass
`settings`, `outreach` and `contacts` to skip the environment entirely --
the path every test takes.

Three screens: Home (`/`), Contacts (`/contacts`) and Contact
(`/contacts/{doc_id}`, in `contacts.py`). All but `/health` sit behind
`auth.IapMiddleware`.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from linkedinmcp import clients, settings as outreach_cfg
from webapp import auth, contacts as contact_screen, projection, render, settings as cfg

logger = logging.getLogger(__name__)

PER_PAGE = 100

#: The Home screen's count tables: column and heading.
COUNTED = {"industry": "Industry", "pipeline_stage": "Stage", "handling": "Handling"}


def create_app(
    settings: cfg.WebappSettings | None = None,
    *,
    outreach: outreach_cfg.OutreachSettings | None = None,
    contacts: projection.Contacts | None = None,
) -> FastAPI:
    """Build the app: the guarded screens, the open `/health`, the frame."""
    if settings is None:
        outreach_cfg.load_environment()  # `.env`, then zero pacing: before any Unipile client
        settings = cfg.get_settings()
    if outreach is None:
        outreach = outreach_cfg.get_settings()
    if contacts is None:
        contacts = projection.Contacts(clients.firestore_client)
    if settings.dev_user is not None:
        logger.warning("WEBAPP_DEV_USER is set: every request is treated as %s", settings.dev_user)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Before the port opens: startup has CPU on Cloud Run, a background
        # thread would not. A failed build fails the startup, loudly.
        if contacts.frame is None:
            await asyncio.to_thread(contacts.rebuild)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.outreach = outreach
    app.state.contacts = contacts
    app.add_middleware(
        auth.IapMiddleware, audience=settings.iap_audience, allowed_email=settings.allowed_email,
        dev_user=settings.dev_user,
    )
    app.mount("/static", StaticFiles(directory=render.STATIC_DIR), name="static")
    app.include_router(contact_screen.router)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/")
    def home(request: Request):
        frame = contacts.frame
        tallies = [(heading, projection.counts(frame, column)) for column, heading in COUNTED.items()]
        return render.templates.TemplateResponse(
            request, "home.html", render.page_context(request, total=frame.height, tallies=tallies)
        )

    @app.get("/contacts")
    def contacts_list(request: Request, q: str = "", sort: str = "activity_at", dir: str = "desc", page: int = 1):
        found, total = projection.query(
            contacts.frame, q=q, sort=sort, descending=(dir != "asc"), page=page, per_page=PER_PAGE
        )
        pages = max(1, -(-total // PER_PAGE))
        return render.templates.TemplateResponse(
            request, "contacts.html",
            render.page_context(
                request, rows=found.to_dicts(), total=total, q=q, sort=sort, dir=dir, page=page, pages=pages
            ),
        )

    @app.post("/refresh")
    def refresh(request: Request) -> RedirectResponse:
        """Rebuild inside this request (about 10 seconds on the real data),
        then return to the screen it was pressed on. A failed build keeps
        the previous frame and answers 500; the log names the cause."""
        try:
            contacts.rebuild()
        except Exception:
            logger.exception("refresh failed; the previous frame is still served")
            raise
        return RedirectResponse(render.back_to(request), status_code=303)

    return app
