# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""One test's backend registration must not reach the next test.

``register_backend`` records which backend it registered in a process
global, and most test files that call it never put the previous value
back. Nothing sets ``EDA_AGENT_BACKEND`` in CI or in conftest, so
``active_backend_name`` falls back to that global and a leak is not
masked. A later test can then resolve against a backend it never asked
for, and either pass for the wrong reason or fail for one that has
nothing to do with the file it lives in.

That is what happened: a pair of files enumerating all three backends
left easyeda active and broke ``tests/design/test_autonomy.py``.

An autouse fixture in conftest now restores it around every test. This
file is the reason anyone would notice if that fixture were deleted:
the first test deliberately leaks, the second checks the leak did not
arrive. Verified by disabling the fixture, at which point the second
test fails.

The pair depends on running in file order. That order is forced, not
assumed: serial runs execute in collection order, and CI's parallel
runs use pytest-xdist with loadfile distribution, which pins every
test in this file to a single worker in collection order. A guard
below fails if a random-order plugin appears or the xdist
distribution mode changes, since either would make this pair
unreliable rather than merely reordered.
"""

from __future__ import annotations

import importlib.util

from eda_agent.core import backends

_LEAKED = "easyeda"
_UNSET = object()
_PRE_LEAK_VALUE = _UNSET


def test_order_is_still_deterministic():
    """This file's two-step check is only sound in a fixed order."""
    for plugin in ("pytest_randomly", "pytest_random_order"):
        assert importlib.util.find_spec(plugin) is None, (
            f"{plugin} is installed, so test order is no longer fixed and "
            f"the leak pair below cannot be relied on. Rewrite it to force "
            f"the ordering explicitly rather than deleting it")
    # pytest-xdist is installed so CI can parallelize the suite. Its
    # distribution options are not visible inside worker processes, so
    # the loadfile requirement cannot be asserted here; it is enforced
    # where the options exist, in pytest_configure in the root
    # conftest.py, which refuses to start under any other --dist mode.


def test_step_one_registers_a_backend_and_leaves_it():
    """Deliberately does NOT restore. That is the point."""
    global _PRE_LEAK_VALUE
    # Record before the imports below: importing the server registers
    # the default backend as a side effect, which would otherwise be
    # mistaken for the value this test started from.
    _PRE_LEAK_VALUE = backends._REGISTERED
    from eda_agent.server import register_backend
    from eda_agent.tools.registry import ToolRegistry

    register_backend(ToolRegistry(), _LEAKED, "full")
    assert backends._REGISTERED == _LEAKED, (
        "registering no longer sets the global, so this file guards a "
        "mechanism that has changed")


def test_step_two_does_not_inherit_it():
    # Compare against the value step one started from, not against the
    # leaked name: on a shared xdist worker another file may already
    # have left _LEAKED behind, and the fixture faithfully restores to
    # that residue. Containment means the global is back at the exact
    # pre-leak value, whatever it was.
    assert _PRE_LEAK_VALUE is not _UNSET, (
        "test_step_one did not run before this test; the pair only "
        "means anything in file order (see the module docstring)")
    assert backends._REGISTERED == _PRE_LEAK_VALUE, (
        "the backend registered by the previous test survived into this "
        "one instead of being restored to its pre-leak value. The "
        "autouse _restore_active_backend fixture in conftest is missing "
        "or no longer autouse, and every test after a registering one "
        "now resolves against the wrong backend")
