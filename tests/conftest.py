import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    with (FIXTURES / name).open() as fh:
        return json.load(fh)


@pytest.fixture
def health():
    return load("health.json")


@pytest.fixture
def devices():
    return load("devices.json")


@pytest.fixture
def clients():
    return load("clients.json")


@pytest.fixture
def devices_counters_only():
    return load("devices_counters_only.json")


@pytest.fixture
def ucg_max():
    return load("ucg_max_dual_wan.json")
