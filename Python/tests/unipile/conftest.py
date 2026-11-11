"""Shared fixtures, loaded from real (redacted) Unipile responses."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def profile_body() -> dict:
    return load("profile_full.json")


@pytest.fixture
def throttled_profile_body() -> dict:
    return load("profile_throttled.json")


@pytest.fixture
def relations_body() -> dict:
    return load("relations_page.json")


@pytest.fixture
def chats_body() -> dict:
    return load("chats_page.json")


@pytest.fixture
def invitations_sent_body() -> dict:
    return load("invitations_sent.json")
