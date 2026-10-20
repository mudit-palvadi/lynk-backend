"""Shared fixtures for the backend test suite.

The rate limiter and the transfer/receiver services all talk to the global
``redis_service`` singleton, so any test module that does not stub it would
otherwise reach for a real (absent) Redis and see 503s.

Historically every module patched that global itself, at import time, which
made the suite depend on the order in which pytest happened to import the
modules: ``test_security_and_limits.py`` only passed because
``test_transfers.py`` had already replaced the global. This fixture installs a
harmless double for any module that has not patched it yet, which keeps the
existing per-module behaviour intact while making every module runnable on its
own.
"""

from unittest.mock import AsyncMock

import pytest

from app.services.redis_service import redis_service


def _allow_everything_redis() -> AsyncMock:
    """A Redis double that answers every command and never rate-limits."""
    fake = AsyncMock()
    fake.incr = AsyncMock(return_value=1)
    fake.expire = AsyncMock(return_value=True)
    return fake


@pytest.fixture(autouse=True)
def default_redis_double():
    """Install a Redis double unless the test module already installed one.

    Modules that patch the singleton themselves (``test_transfers.py``,
    ``test_receiver.py``) and modules that install their own double per test
    (``test_integration_cloud_p2p.py`` with fakeredis) keep winning; this
    fixture only fills the gap.
    """
    original = redis_service.get_client
    if not isinstance(original, AsyncMock):
        redis_service.get_client = AsyncMock(return_value=_allow_everything_redis())
    try:
        yield
    finally:
        redis_service.get_client = original
