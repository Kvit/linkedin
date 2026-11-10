"""The Documents tab: the files of the marketing bucket, their tracked links,
and how often each was opened.

Everything comes from `linkedinmcp.documents`, which the outreach service
shares: it serves the links at `/d/{token}` and stores the requests on them.
The bucket is listed again on every page load, so a file added to it shows
on the next load, with its permanent link. The frame is read only for the
header's counts.
"""

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from linkedinmcp import clients, clock, documents
from webapp import render

logger = logging.getLogger(__name__)

router = APIRouter()

#: Longest label of a link made for a person or a place that is not a contact.
LABEL_MAX = 200

#: The columns a heading of the Documents list sorts by, and each one's value
#: of a row; then the same for the Sublinks table and a link.
DOCUMENT_SORTS = {
    "name": lambda row: row["name"].lower(),
    "type": lambda row: row["type"] or None,
    "size": lambda row: row["size"],
    "updated_at": lambda row: row["updated_at"],
    "url": lambda row: row.get("url"),
    "opens": lambda row: row["opens"],
    "links": lambda row: row["links"],
    "last_open_at": lambda row: row.get("last_open_at"),
}
DOCUMENT_DEFAULT_SORT = "updated_at"

LINK_SORTS = {
    "recipient": lambda link: (link.get("label") or link.get("doc_id") or "").lower() or None,
    "created_by": lambda link: (link.get("created_by") or "").lower() or None,
    "created_at": lambda link: link.get("created_at"),
    "opens": lambda link: link.get("opens") or 0,
    "last_open_at": lambda link: link.get("last_open_at"),
    "bot_requests": lambda link: link.get("bot_requests") or 0,
    "link": lambda link: link["id"],
}
LINK_DEFAULT_SORT = "created_at"


def _sorted(rows: list[dict], value, descending: bool) -> list[dict]:
    """`rows` by `value(row)`; a row with no value comes last, both ways."""
    filled = sorted((row for row in rows if value(row) is not None), key=value, reverse=descending)
    return filled + [row for row in rows if value(row) is None]


def _file(request: Request, name: str) -> dict:
    """The listed file `name`, or 404."""
    files = documents.list_documents(clients.storage_client(), request.app.state.outreach)
    found = next((row for row in files if row["name"] == name), None)
    if found is None:
        raise HTTPException(status_code=404, detail="No such document.")
    return found


@router.get("/documents")
def documents_list(request: Request, sort: str = DOCUMENT_DEFAULT_SORT, dir: str = "desc"):
    """Every file in the folder with its main link and counts: latest on top,
    or in the order a column heading asks for."""
    if sort not in DOCUMENT_SORTS:
        sort, dir = DOCUMENT_DEFAULT_SORT, "desc"
    settings = request.app.state.outreach
    folder = f"gs://{settings.documents_bucket}/{settings.documents_prefix}"
    problem = None
    try:
        files = documents.list_documents(clients.storage_client(), settings)
    except Exception as error:
        logger.exception("listing %s failed", folder)
        files, problem = [], f"The bucket {folder} could not be listed ({type(error).__name__})."
    rows = []
    if files:
        db = clients.firestore_client()
        permanent = documents.ensure_permanent_links(db, settings, [row["name"] for row in files], clock.utcnow())
        stats = documents.totals(documents.links(db, settings))
        for row in files:
            link_id = permanent.get(row["name"])
            rows.append({
                **row, "url": documents.link_url(settings, link_id) if link_id else None,
                **stats.get(row["name"], {"opens": 0, "links": 0, "last_open_at": None}),
            })
    return render.templates.TemplateResponse(
        request, "documents.html",
        render.page_context(
            request, rows=_sorted(rows, DOCUMENT_SORTS[sort], dir != "asc"), folder=folder, problem=problem,
            sort=sort, dir="asc" if dir == "asc" else "desc",
        ),
    )


@router.get("/documents/{name}")
def document_page(request: Request, name: str, sort: str = LINK_DEFAULT_SORT, dir: str = "desc"):
    """One file: its main link (the permanent one) with its counts, the form for a
    new sublink, and every sublink (the custom links) with its newest requests.
    The sublinks are newest first, or in the order a column heading asks for."""
    if sort not in LINK_SORTS:
        sort, dir = LINK_DEFAULT_SORT, "desc"
    settings = request.app.state.outreach
    file = _file(request, name)
    db = clients.firestore_client()
    permanent_id = documents.ensure_permanent_links(db, settings, [name], clock.utcnow()).get(name)
    found = documents.links(db, settings, name=name)
    return render.templates.TemplateResponse(
        request, "document.html",
        render.page_context(
            request, file=file, permanent=next((link for link in found if link["id"] == permanent_id), None),
            links=_sorted(
                [link for link in found if link["kind"] == documents.CUSTOM], LINK_SORTS[sort], dir != "asc"
            ),
            sort=sort, dir="asc" if dir == "asc" else "desc",
            requests=documents.requests_for(db, settings, name), label_max=LABEL_MAX,
            view_url=f"https://storage.googleapis.com/{settings.documents_bucket}/{quote(file['object'])}",
        ),
    )


@router.post("/documents/{name}/links")
def create_label_link(request: Request, name: str, label: Annotated[str, Form()] = ""):
    """A new link to the file for a label: a person who is not a contact, or a place."""
    label = label.strip()
    if not label or len(label) > LABEL_MAX:
        raise HTTPException(status_code=400, detail=f"A label has 1 to {LABEL_MAX} characters.")
    _file(request, name)
    documents.create_link(
        clients.firestore_client(), request.app.state.outreach, name, label=label,
        created_by=request.state.user, now=clock.utcnow(),
    )
    return RedirectResponse(f"/documents/{quote(name, safe='')}", status_code=303)
