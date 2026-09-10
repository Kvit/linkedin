"""The three client factories the rest of the service builds on.

Every LinkedIn, Firestore and Gemini call in this service goes through one of
these functions, and none of them is ever imported by name -- callers do::

    from linkedinmcp import clients
    ...
    db = clients.firestore_client()      # resolved at call time

so a test can replace the whole factory with
`monkeypatch.setattr(clients, "firestore_client", fake)`. That is the only
reason this module exists: nothing here adds behaviour, it adds a seam. A
caller that did `from linkedinmcp.clients import firestore_client` would bind a
local name the patch cannot reach, and would then reach the real database from
a unit test.

**Importing this module must stay free.** `app.py` imports it at startup and
the tests import it constantly, so nothing at module level opens a connection,
reads a credential or constructs a client. Each factory imports its own
dependency inside the function body for the same reason -- `import pipeline`
alone costs ~0.8 s because it pulls in `google.genai`, which only
`gemini_client` needs.
"""


def firestore_client():
    """A Firestore client on the `linkedin` database of project `vk-linkedin`.

    Delegates to `lib.firestore.client`, which every entry point in the repo
    uses -- the local-credentials guard and the two database literals live
    there, in one place, rather than in a copy per script.
    """
    from lib import firestore

    return firestore.client()


def unipile_client():
    """A **new** `UnipileClient` on every call. Never cache this.

    Caching it is the obvious-looking optimisation, and it is wrong here:
    `lib/unipile` keeps the send-budget counters and the transport circuit
    breaker as state on the client instance. A client shared between two
    concurrent jobs would let each spend the other's send budget, and would
    let one job's run of transport failures trip the breaker on the other --
    a job failing for a reason that has nothing to do with what it did.

    One client per unit of work; call `close()` (or use it as a context
    manager) when that work is done.
    """
    from lib.unipile import UnipileClient

    return UnipileClient.from_env()


def gemini_client():
    """A new `google.genai` client, configured by `pipeline.gemini_client`.

    Delegated rather than re-implemented so the retry policy, the 60 s
    per-request timeout and this service stay in step with the batch pipeline
    -- there is one definition of how this project talks to Gemini, and it
    lives there.

    New on every call for the reason `pipeline.gemini_client`'s own docstring
    gives: the async HTTP pool binds to the first event loop it runs on, so
    there is one client per `asyncio.run`.
    """
    import pipeline

    return pipeline.gemini_client()
