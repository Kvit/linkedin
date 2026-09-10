"""Classify LinkedIn conversations into sales-pipeline stages with Gemini.

An argparse wrapper around `pipeline.run_pipeline`. Every rule lives in
pipeline.py, so the same code classifies one contact from a webhook -- see
`pipeline.classify_contact`.

    uv run python pipeline-classify.py --dry-run --limit 30   # first look; read the CSV
    uv run python pipeline-classify.py --dry-run              # everyone, nothing written
    uv run python pipeline-classify.py                        # incremental
    uv run python pipeline-classify.py --contact some-slug    # one contact, whatever is stored
    uv run python pipeline-classify.py --reprocess-all        # after a prompt change
    uv run python pipeline-classify.py --concurrency 1        # if the run reports many 429s
    uv run python pipeline-classify.py --dry-run --reprocess-all --thinking-level medium \\
        --csv pipeline-review-medium.csv                      # a second opinion to diff

Who is classified
-----------------
Every contact with at least one inbound message goes to Gemini -- one message is
enough, since a single "no, thank you" disqualifies. That is 617 contacts, not
the 456 with `replied_total > 0`: the 161 who wrote first are read too. A
contact we have written to who has never answered is `prospect` by rule, with no
model call, because there is nothing to read. A contact with no `analysis`
document is skipped and counted, never minted.

Gemini sees the contact's `summary`, the `industry`, `function` and `seniority`
already on file as given facts, and the whole conversation, oldest first. It
returns a stage and a reason and nothing else: the three classifications are
inputs, never re-judged. Target-market fit is a query-time filter
(`TARGET_INDUSTRIES` in send-intros.ipynb), not part of this label:
`not_relevant` means the person is not a buyer at all -- a recruiter, a vendor
pitching us -- never "their employer is a hospital"; a hospital executive who
asks about the product is a `lead`. `prospect` is the default: a courtesy reply
moves nothing.

How it re-runs
--------------
`pipeline_message_id` records the newest inbound message the classification
read. When messages-sync.py stores a newer one, the next run rebuilds the whole
transcript and classifies again; the latest signal wins, so an interested
contact who later declines becomes `reject`. Our own follow-ups never trigger a
reclassification, and neither does a changed profile summary. `--contact`
forces one contact; `--reprocess-all` forces everyone, for prompt changes.

A Gemini failure -- after the SDK's five attempts -- writes nothing, so the key
stays behind and the contact is queued again next run. The silent rule never
overwrites a stage Gemini assigned: a contact whose reply has left the stored
window is left alone and counted stale. A run with nothing new writes nothing.

Fields written to `analysis`, always merged and never set -- the collection is
the only copy of some contacts' names and emails:

    pipeline_stage          prospect | lead | reject | not_relevant | unknown
    pipeline_reason         one sentence from the model, or "messaged, no reply yet"
    pipeline_classified_at  server timestamp
    pipeline_message_id     the inbound message the stage was read from; absent for the rule

Every run that classifies anything writes `pipeline-review.csv` -- on a dry run
too, which is the point of a dry run: Gemini is still called and billed, and the
CSV is what to read before writing.
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import polars as pl
from dotenv import load_dotenv
from lib import firestore

from pipeline import (
    DEFAULT_THINKING_LEVEL,
    SILENT_STAGE,
    THINKING_LEVELS,
    gemini_client,
    run_pipeline,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Classify LinkedIn conversations into sales-pipeline stages.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="call Gemini (billed) but write nothing to Firestore; review the CSV")
    parser.add_argument("--reprocess-all", action="store_true",
                        help="reclassify every contact with an inbound message, "
                             "e.g. after a prompt change")
    parser.add_argument("--contact", action="append", default=[], metavar="DOC_ID",
                        help="only this contact, whatever is stored; repeatable")
    parser.add_argument("--limit", type=int, default=None,
                        help="at most this many Gemini calls, newest conversations first")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Gemini calls in flight at once (default: 4). Many 429 "
                             "failures mean the account's tier is lower: use 1 and "
                             "check the rate limits in AI Studio")
    parser.add_argument("--thinking-level", choices=THINKING_LEVELS, default=DEFAULT_THINKING_LEVEL,
                        help="how much the model reasons per call (default: low, the docs' "
                             "recommendation for classification); recorded on every CSV row")
    parser.add_argument("--csv", default="pipeline-review.csv",
                        help="where to write what was classified (default: pipeline-review.csv)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("pipeline").setLevel(logging.INFO)

    # Credentials guard and the two database literals live in lib/firestore.py,
    # shared with every other entry point.
    db = firestore.client()
    # Built up front so a missing GOOGLE_API_KEY fails here, not after the reads.
    client = gemini_client()
    started = time.monotonic()

    print("pipeline-classify")
    if args.dry_run:
        print("  [!] dry run: Gemini is called, Firestore is not written")

    # One asyncio.run per process: the client's HTTP pool binds to this loop.
    run = asyncio.run(run_pipeline(
        db, client,
        contacts=args.contact or None,
        force=bool(args.contact),
        reprocess_all=args.reprocess_all,
        limit=args.limit,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        thinking_level=args.thinking_level,
    ))
    tally = run.tally

    print(f"  messages:      {tally['messages']:,} read")
    print(f"  contacts:      {tally['contacts']:,} with readable messages, "
          f"{tally['with_inbound']:,} with an inbound one")
    print(f"  silent:        {tally['silent']:,} marked {SILENT_STAGE} by rule")
    capped = f" of {tally['queued']:,}" if tally["called"] < tally["queued"] else ""
    print(f"  gemini:        {tally['called']:,} called{capped}, "
          f"{len(run.rows):,} classified, {run.failed:,} failed, thinking {args.thinking_level}")
    print(f"  unchanged:     {tally['unchanged']:,}")
    for count, note in (
        (tally["stale"], "contact(s) classified from a message no longer stored -- left alone"),
        (tally["missing"], "contact(s) not in 'analysis' -- skipped"),
        (tally["not_found"], "--contact id(s) with no messages"),
    ):
        if count:
            print(f"  [!] {count:,} {note}")

    # Only when there is something to review: a no-op run must not clobber
    # the last review file.
    if run.rows:
        pl.DataFrame(run.rows).write_csv(args.csv)
        print("  by stage:      " + ", ".join(f"{s} {n:,}" for s, n in run.stages.most_common()))
        print(f"  review:        {args.csv} ({len(run.rows):,} rows)")
    if run.failed:
        print("  [!] failed contacts keep their old stage and are queued again next run;"
              " if the failures were 429s, re-run with --concurrency 1")

    print(f"  elapsed: {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
