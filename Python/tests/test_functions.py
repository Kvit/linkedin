"""Tests for the existing profile-flattening helpers.

`join_keys` builds the plain-text `summary` that Gemini classifies. Its output
must be deterministic: the notebook treats a changed summary as a signal to
re-classify, so unstable ordering re-bills Gemini for profiles that did not
actually change.

`count_created_since` is what the rolling-24h budget is recounted from, so the
query it builds is worth pinning: a wrong operator would silently report zero
contacts saved and hand back a full allowance.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from functions import count_created_since, get_member_distance, join_keys


def test_join_keys_preserves_input_order():
    """Deduplication must not reorder.

    `list(set(...))` returns an arbitrary order that varies per process, because
    Python randomises string hashing. With eight distinct values, matching input
    order by luck is effectively impossible.
    """
    values = ["v-one", "v-two", "v-three", "v-four", "v-five", "v-six", "v-seven", "v-eight"]
    data = {f"k{i}": value for i, value in enumerate(values)}

    result = join_keys(data, list(data))

    assert result.split("\n") == values


def test_join_keys_still_deduplicates():
    data = {"a": "same", "b": "same", "c": "different"}

    assert join_keys(data, list(data)).split("\n") == ["same", "different"]


def test_get_member_distance_reads_every_stored_shape():
    """`extracted` holds documents from two eras, so the field has several shapes.

    Phase E of new-contacts.ipynb reads distance straight off stored documents,
    so a shape it does not understand would silently write 0 into `analysis` and
    make a first-degree connection look out-of-network.
    """
    assert get_member_distance({"memberDistance": {"memberDistance": "DISTANCE_3"}}) == 3
    assert get_member_distance({"memberDistance": "DISTANCE_1"}) == 1
    assert get_member_distance({"memberDistance": 2}) == 2
    assert get_member_distance({"memberDistance": "OUT_OF_NETWORK"}) == 0


def test_get_member_distance_defaults_to_zero_when_absent_or_unknown():
    assert get_member_distance({}) == 0
    assert get_member_distance({"memberDistance": None}) == 0
    assert get_member_distance({"memberDistance": {}}) == 0
    assert get_member_distance({"memberDistance": "SOMETHING_NEW"}) == 0


# --- count_created_since ------------------------------------------------------


class _FakeAggregation:
    def __init__(self, value: int) -> None:
        self._value = value

    def get(self):
        """Firestore returns one row of results per aggregation requested."""
        return [[SimpleNamespace(value=self._value)]]


class _FakeQuery:
    def __init__(self, matches: int) -> None:
        self._matches = matches
        self.alias = None

    def count(self, alias=None):
        self.alias = alias
        return _FakeAggregation(self._matches)


class _FakeCollection:
    """Records the filter it was handed instead of talking to Firestore."""

    def __init__(self, matches: int) -> None:
        self._matches = matches
        self.filter = None
        self.query = None

    def where(self, filter=None):
        self.filter = filter
        self.query = _FakeQuery(self._matches)
        return self.query


def test_count_created_since_unwraps_the_aggregation_result():
    assert count_created_since(_FakeCollection(7), datetime(2026, 9, 8, tzinfo=UTC)) == 7


def test_count_created_since_filters_on_the_timestamp_inclusively():
    """`>=`, not `>`: the window is closed at the cutoff, and the field is the
    server timestamp the notebook writes when it saves a contact."""
    collection = _FakeCollection(0)
    cutoff = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)

    count_created_since(collection, cutoff)

    assert collection.filter.field_path == "created_at"
    assert collection.filter.op_string == ">="
    assert collection.filter.value == cutoff


def test_count_created_since_accepts_a_different_timestamp_field():
    collection = _FakeCollection(0)

    count_created_since(collection, datetime(2026, 9, 8, tzinfo=UTC), field="sent_at")

    assert collection.filter.field_path == "sent_at"
