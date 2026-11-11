"""Error-mapping tests.

`raise_for_response` is a pure function: it takes the HTTP status and the parsed
JSON body and raises the typed exception. Keeping it free of httpx objects means
the taxonomy can be exercised exhaustively without constructing responses.
"""

import pytest

from lib.unipile.errors import AlreadyConnected, UnprocessableError, raise_for_response


def test_already_connected_maps_to_typed_exception():
    body = {
        "status": 422,
        "type": "errors/already_connected",
        "title": "Already connected",
        "detail": "You are already connected to this user.",
    }

    with pytest.raises(AlreadyConnected) as excinfo:
        raise_for_response(422, body)

    assert excinfo.value.type == "errors/already_connected"
    assert excinfo.value.status == 422
    assert excinfo.value.title == "Already connected"


def test_unknown_type_falls_back_to_status_class_and_keeps_raw_type():
    """A provider error code we have never seen must stay inspectable."""
    body = {"status": 422, "type": "errors/some_future_code", "title": "Nope"}

    with pytest.raises(UnprocessableError) as excinfo:
        raise_for_response(422, body)

    assert type(excinfo.value) is UnprocessableError
    assert excinfo.value.type == "errors/some_future_code"
