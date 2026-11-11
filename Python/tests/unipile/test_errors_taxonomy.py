"""The full error taxonomy: every documented `type` maps to an actionable class."""

import pytest

from lib.unipile import errors as E

CASES = [
    (401, "errors/disconnected_account", E.AccountDisconnected),
    (401, "errors/invalid_credentials", E.AuthenticationError),
    (403, "errors/account_restricted", E.AccountRestricted),
    (403, "errors/feature_not_subscribed", E.FeatureNotSubscribed),
    (404, "errors/resource_not_found", E.NotFound),
    (429, "errors/too_many_requests", E.RateLimited),
    (422, "errors/already_connected", E.AlreadyConnected),
    (422, "errors/already_invited_recently", E.AlreadyInvited),
    (422, "errors/no_connection_with_recipient", E.NoConnectionWithRecipient),
    (422, "errors/insufficient_credits", E.InsufficientCredits),
    (422, "errors/connection_limit_reached", E.ConnectionLimitReached),
    (422, "errors/user_unreachable", E.UserUnreachable),
    (422, "errors/cannot_resend_yet", E.CannotResendYet),
    (500, "errors/provider_error", E.ServerError),
    # 502 is retried by the transport but was absent from the status map,
    # so after retries a bare UnipileError escaped past ServerError handlers.
    (502, "errors/provider_error", E.ServerError),
    (507, "errors/unexpected_error", E.ServerError),
    (503, "errors/no_client_session", E.ServerError),
]


@pytest.mark.parametrize("status,error_type,expected", CASES)
def test_error_type_maps_to_class(status, error_type, expected):
    with pytest.raises(expected) as excinfo:
        E.raise_for_response(status, {"status": status, "type": error_type, "title": "t"})
    assert excinfo.value.type == error_type


def test_every_mapped_class_descends_from_unipile_error():
    for cls in {c for _, _, c in CASES}:
        assert issubclass(cls, E.UnipileError)
