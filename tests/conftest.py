"""Shared fixtures for the client_loop tests."""

import asyncio

import pytest

from backend import client_loop


@pytest.fixture
def cl():
    return client_loop


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the file tools at a temporary workspace root."""
    monkeypatch.setattr(client_loop, "FILE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def small_cap(monkeypatch):
    """Shrink the result cap so truncation is testable with small payloads."""
    monkeypatch.setattr(client_loop, "RESULT_MAX_CHARS", 50)
    return 50


def run(coro):
    """Run an async tool call from a sync test."""
    return asyncio.run(coro)
