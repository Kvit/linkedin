"""Compatibility shim. The code moved to `messages_sync.py`.

A hyphen is not a valid Python module name, so the outreach service could not
import the sync helpers from here. This file stays because `README.md` and
`send-intros.ipynb` Phase A0 both invoke it by name as a subprocess.

Run `uv run python messages_sync.py` for new work; the flags are identical.
"""

import sys

from messages_sync import main

if __name__ == "__main__":
    sys.exit(main())
