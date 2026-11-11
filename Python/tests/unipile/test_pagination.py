"""Cursor pagination is hidden behind generators; callers never see a page."""

import pytest

from lib.unipile.pagination import iter_cursor


def test_yields_items_across_pages_until_the_cursor_runs_out():
    pages = {
        None: {"items": [1, 2], "cursor": "c1"},
        "c1": {"items": [3], "cursor": None},
    }
    seen_cursors = []

    def fetch(cursor):
        seen_cursors.append(cursor)
        return pages[cursor]

    assert list(iter_cursor(fetch)) == [1, 2, 3]
    assert seen_cursors == [None, "c1"]


def test_stops_when_a_page_comes_back_empty():
    pages = {None: {"items": [], "cursor": "c1"}}

    assert list(iter_cursor(lambda cursor: pages[cursor])) == []


def test_breaks_when_the_cursor_repeats():
    """A server that echoes the same cursor forever must not hang the caller."""
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return {"items": ["x"], "cursor": "same"}

    assert list(iter_cursor(fetch)) == ["x", "x"]
    assert calls == [None, "same"]


def test_parses_each_item_through_the_supplied_factory():
    pages = {None: {"items": [{"n": 1}, {"n": 2}], "cursor": None}}

    result = list(iter_cursor(lambda c: pages[c], parse=lambda d: d["n"]))

    assert result == [1, 2]


def test_missing_items_key_is_treated_as_an_empty_page():
    assert list(iter_cursor(lambda c: {"cursor": None})) == []
