"""Run the LinkedIn activity crawl from the command line, inside the dev container.

A thin wrapper around `lib.get_activity.get_contact_activity`. Every limit and
the whole pace come from `.env` (`UNIPILE_MAX_ACTIVITY_CHECKS_PER_DAY`,
`CRAWLER_*`); nothing is overridden here.

    uv run python crawl-activity.py --limit 100

The scheduled routine `linkedin-activity-crawl` starts this detached and reads
its log afterwards, so the output is a contract:

- every request is logged at INFO, one stream, in order;
- the run's result is one JSON line starting with `{"audience"`;
- a line starting with `FAILED:` means the run could not be completed: an
  exception, or a crawl LinkedIn stopped (429, restriction, 5xx, lockout).

Exit code 0 when the crawl completed, 1 when it failed or was stopped, 2 when
started outside the dev container.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Before any project import: the host's own venv is not the one this code is
# written against, and must never reach LinkedIn.
if not Path("/.dockerenv").exists():
    print("FAILED: run this inside the dev container, not on the host "
          "(docker exec -u vscode <id> bash -lc 'cd /workspaces/linkedin/Python && uv run python crawl-activity.py')",
          flush=True)
    sys.exit(2)

from lib import firestore  # noqa: E402
from lib.get_activity import CrawlerSettings, get_contact_activity  # noqa: E402
from lib.unipile.client import UnipileClient  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Crawl the recent LinkedIn activity of the next contacts.")
    parser.add_argument("--limit", type=int, default=100,
                        help="contacts to check, within today's allowance (default: 100)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        with UnipileClient.from_env() as client:
            pace = CrawlerSettings.from_env()
            print(f"limit={args.limit} cap={client.settings.max_activity_checks_per_day} batch={pace.batch_size} "
                  f"pause={pace.batch_pause_min_seconds:g}-{pace.batch_pause_max_seconds:g}s "
                  f"delay={pace.min_delay_seconds:g}-{pace.max_delay_seconds:g}s", flush=True)
            result = get_contact_activity(firestore.client(), client, limit=args.limit)
    except Exception as error:
        logging.exception("the crawl raised")
        print(f"FAILED: {type(error).__name__}: {error}", flush=True)
        return 1

    print(json.dumps({key: value for key, value in result.items() if key != "contacts"}, default=str), flush=True)
    if result["stopped"]:
        print(f"FAILED: the crawl was stopped after {result['checked']} contacts: {result['stopped']}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
