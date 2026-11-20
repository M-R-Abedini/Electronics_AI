# SPDX-License-Identifier: Apache-2.0
"""Session-level guards for the EDA regression suite.

This file lives at the invocation root so pytest loads it before
configuration finishes (a conftest under tests/ loads too late for
pytest_configure to see the controller's options).
"""

import pytest


def pytest_configure(config):
    """Refuse xdist distribution modes that break in-file test order.

    The leak-containment pair in tests/test_backend_leak_is_contained.py
    is only sound when that file's tests run in collection order in one
    process. pytest-xdist with loadfile distribution preserves that by
    pinning each file to a single worker; any other mode can split the
    pair across workers. Worker processes cannot see the distribution
    options, so the check belongs here on the controller: fail the run
    at startup instead of letting the pair go flaky.
    """
    if getattr(config, "workerinput", None) is not None:
        return  # xdist worker process; the controller already enforced this
    try:
        workers = config.getoption("numprocesses", default=None)
        dist = config.getoption("dist", default="load")
    except (ValueError, AttributeError):
        return  # pytest-xdist is not installed; nothing to enforce
    if workers in (None, 0, "0"):
        return  # not distributing; collection order holds
    if dist != "loadfile":
        raise pytest.UsageError(
            f"pytest-xdist --dist {dist} can split ordered test pairs "
            "across workers (see tests/test_backend_leak_is_contained.py). "
            "Run with --dist loadfile, which pins each test file to one "
            "worker in collection order.")
