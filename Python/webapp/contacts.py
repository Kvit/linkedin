"""The Contact screen: one contact whole. Read-only in the skeleton; the
edit and compose routes of later stages join this module.

Everything shown comes from `linkedinmcp.contacts`, whose readers build
explicit whitelists -- `email*` and `phone*` never reach a page -- and
give every contact date as an ISO string in the service's time zone.
`get_conversation` imports `pipeline` (about 0.8 s, once per process) for
the transcript. `clients.firestore_client` is called through its module,
so a test can replace it.

The headline comes from the frame, not from `get_contact`: that reader
reads only `extracted.occupation`, which LinkedIn Helper documents lack
(see `projection`'s docstring), so the Contact screen shows what the list
shows.
"""

from fastapi import APIRouter, HTTPException, Request

from linkedinmcp import clients, contacts as reads
from webapp import projection, render

router = APIRouter()

FETCH_COLLECTION = "fetch_queue"


@router.get("/contacts/{doc_id}")
def contact_screen(request: Request, doc_id: str):
    db = clients.firestore_client()
    contact = reads.get_contact(db, request.app.state.outreach, doc_id, full=True)
    if contact is None:
        raise HTTPException(status_code=404, detail="No such contact.")
    listed = projection.contact_row(request.app.state.contacts.frame, doc_id)
    if listed is not None and listed["headline"]:
        contact["headline"] = listed["headline"]
    conversation = reads.get_conversation(db, doc_id)
    fetch = db.collection(FETCH_COLLECTION).document(doc_id).get()
    connected_at = (fetch.to_dict() or {}).get("connected_at") if fetch.exists else None
    return render.templates.TemplateResponse(
        request, "contact.html",
        render.page_context(request, contact=contact, conversation=conversation, connected_at=connected_at),
    )
