"""The message-sync helpers must be importable, not just runnable.

The file was named `messages-sync.py`; a hyphen is not a valid module name, so
the outreach service could only have run it as a subprocess. These five names
are what the service imports.
"""

import inspect


def test_helpers_are_importable():
    import messages_sync

    for name in (
        "_watermarks",
        "_ids_at",
        "ContactResolver",
        "forward_pass",
        "refresh_contact_stats",
    ):
        assert hasattr(messages_sync, name), f"messages_sync.{name} is missing"


def test_main_takes_an_argv_list():
    """The shim calls `main()` with no arguments; it must still read sys.argv."""
    import messages_sync

    signature = inspect.signature(messages_sync.main)
    assert "argv" in signature.parameters
    assert signature.parameters["argv"].default is None
