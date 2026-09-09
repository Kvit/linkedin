"""Tests for the existing profile-flattening helpers.

`join_keys` builds the plain-text `summary` that Gemini classifies. Its output
must be deterministic: the notebook treats a changed summary as a signal to
re-classify, so unstable ordering re-bills Gemini for profiles that did not
actually change.
"""

from functions import get_member_distance, join_keys


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
