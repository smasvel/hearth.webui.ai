"""Run tests in an isolated PostgreSQL schema, never the application's live tables."""
import argparse
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true', help='Also run both Playwright suites')
    args = parser.parse_args()
    with database.temporary_schema():
        suite = unittest.defaultTestLoader.discover(str(database.ROOT / 'tests'))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if not result.wasSuccessful():
            return 1
        if args.browser:
            for script in ('check_model_tests_ui.py', 'check_agents_ui.py', 'check_model_resource_warnings_ui.py'):
                subprocess.run([sys.executable, str(database.ROOT / 'tools' / script)], check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
