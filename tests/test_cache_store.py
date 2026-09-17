"""The result store holds derived data; it must never deserialize code."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings
from app.resources import Resources, private_to_owner


class _Info:
    def __init__(self, uid, mode):
        self.st_uid, self.st_mode = uid, mode


@pytest.fixture
def stores(tmp_path):
    resources = Resources(replace(settings, result_cache_dir=str(tmp_path), host="127.0.0.1", api_key=None))
    resources._open_stores()
    try:
        yield resources
    finally:
        resources.cache.close()
        resources.state.close()


@pytest.mark.parametrize("store", ["cache", "state"])
def test_stores_reject_values_that_are_not_json(stores, store):
    with pytest.raises(TypeError):
        getattr(stores, store).set("key", object())


def test_stores_round_trip_response_shaped_values(stores):
    value = {"markdown": "# Optics", "warnings": ["late"], "status_code": 200, "links": None}
    stores.cache.set("results-key", value, expire=60)
    stores.state.set("rate:example.com", 1.5)
    assert stores.cache.get("results-key") == value
    assert stores.state.get("rate:example.com") == 1.5


def test_stores_live_in_a_directory_named_after_the_layout(stores, tmp_path):
    assert {path.name for path in tmp_path.iterdir()} == {"results-json-v1", "state-json-v1"}


@pytest.mark.parametrize(
    "info, expected",
    [
        (_Info(1000, 0o40700), True),
        (_Info(1000, 0o40750), False),
        (_Info(1000, 0o40707), False),
        (_Info(1001, 0o40700), False),
    ],
)
def test_private_to_owner_requires_owner_only_access(info, expected):
    assert private_to_owner(info, 1000) is expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_group_readable_cache_directory_is_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o750)
    Path(shared).chmod(0o750)
    resources = Resources(replace(settings, result_cache_dir=str(shared), host="127.0.0.1", api_key=None))
    with pytest.raises(RuntimeError, match="private"):
        resources._open_stores()
