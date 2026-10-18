"""Legacy mode logs at INFO unless HLE_OPERATOR_DEBUG asks for DEBUG."""

from __future__ import annotations

import pytest

from hle_operator import __main__ as entry


@pytest.mark.parametrize("value", ["", "0", "no", "false"])
def test_debug_logging_off_by_default(monkeypatch, value):
    monkeypatch.setenv("HLE_OPERATOR_DEBUG", value)
    assert entry._debug_logging() is False


def test_debug_logging_unset(monkeypatch):
    monkeypatch.delenv("HLE_OPERATOR_DEBUG", raising=False)
    assert entry._debug_logging() is False


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_debug_logging_on(monkeypatch, value):
    monkeypatch.setenv("HLE_OPERATOR_DEBUG", value)
    assert entry._debug_logging() is True
