"""The project's Firestore client, built the same way everywhere.

`main.py`, `collection-tocsv.py`, `messages_sync.py`, `pipeline-classify.py` and
the outreach service all need the same thing: point
`GOOGLE_APPLICATION_CREDENTIALS` at the local service-account key when there is
one, then open the `linkedin` database of project `vk-linkedin`. That was five
near-identical copies of the same six lines, and they had already drifted --
the outreach service needed to search one directory further up and the others
did not know to.

The project and database names are literals rather than settings. One database,
named the same way in every entry point, is easier to reason about than a
configurable one that has never varied.
"""

import os
from pathlib import Path

#: The service-account key the repo keeps locally. Absent on Cloud Run, where
#: Application Default Credentials supply the runtime service account instead.
CREDENTIALS_FILE = "vk-linkedin-master-service-account.json"

#: Google Cloud project holding the database.
PROJECT = "vk-linkedin"

#: Named database within that project. Not `(default)`.
DATABASE = "linkedin"

#: Where to look for the key, in order: the working directory, then one level
#: up. The notebooks and scripts run from `Python/`, where the bare name is
#: right. Anything running from a subdirectory finds the same file one level up.
#: Building these `Path` objects touches no disk.
CREDENTIALS_SEARCH_PATH = (Path(CREDENTIALS_FILE), Path("..") / CREDENTIALS_FILE)


def use_local_credentials_if_present() -> str | None:
    """Point `GOOGLE_APPLICATION_CREDENTIALS` at the local key, if there is one.

    **The absent case is the production case, and it must never raise.** On
    Cloud Run the file does not exist and Application Default Credentials supply
    the runtime service account, which is the intended path rather than a
    fallback. A missing file therefore means "leave the environment alone and
    let ADC do its job", never an error.

    Returns the path that was set, or `None` when no key was found -- useful for
    logging and tests; no caller needs to branch on it.
    """
    for candidate in CREDENTIALS_SEARCH_PATH:
        if candidate.is_file():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(candidate)
            return str(candidate)
    return None


def client():
    """A Firestore client on the `linkedin` database of project `vk-linkedin`.

    `google.cloud.firestore` is imported inside the function on purpose. Callers
    that only want `use_local_credentials_if_present`, and tests that only want
    to monkeypatch this, should not pay the import; and a module whose import is
    free can be imported at the top of anything.
    """
    from google.cloud import firestore

    use_local_credentials_if_present()
    return firestore.Client(project=PROJECT, database=DATABASE)
