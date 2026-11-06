"""Pytest configuration for the regression test suite.

Two responsibilities, both about keeping ``pytest tests/`` green without touching
the individual test modules:

1. Make sibling test modules importable by their bare module name (e.g.
   ``from test_flm_backend_profile import write_ready_fixture``). Modern pytest
   resolves a package's test files under the ``tests.`` namespace, so importing a
   sibling as a top-level module no longer works on its own; registering the
   directory on ``sys.path`` restores that convention.

2. Keep standalone, manually-run live scripts (``run_*`` and ``verify_*``) out of
   collection. They are not pytest tests: they hit the running Hearth API and exit
   with a non-zero status when a side is unreachable, which used to abort the whole
   suite with ``SystemExit``. They remain runnable directly with ``python tests/...``.
"""
import os
import sys

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)


def pytest_ignore_collect(collection_path, config):
    name = os.path.basename(str(collection_path))
    if name.startswith(("run_", "verify_")):
        return True
    return False
