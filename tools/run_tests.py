"""Run tests in an isolated PostgreSQL schema, never the application's live tables."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hearth.storage import database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true', help='Run all Playwright suites concurrently with unit tests')
    args = parser.parse_args()
    with database.temporary_schema():
        scripts = sorted(path.name for path in (database.ROOT / "tools").glob("check_*_ui.py"))
        browser_pool = ThreadPoolExecutor(max_workers=len(scripts)) if args.browser else None
        futures = {}
        if browser_pool:
            for script in scripts:
                futures[browser_pool.submit(
                    subprocess.run,
                    [sys.executable, str(database.ROOT / 'tools' / 'run_browser_test.py'), script],
                    capture_output=True, text=True,
                )] = script
        suite = unittest.defaultTestLoader.discover(str(database.ROOT / 'tests'))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        browser_ok = True
        suites_passed = named_passes = named_failures = 0
        for future in as_completed(futures):
            script = futures[future]
            completed = future.result()
            print(f'--- {script} (exit {completed.returncode}) ---')
            if completed.stdout:
                print(completed.stdout, end='')
            if completed.stderr:
                print(completed.stderr, end='', file=sys.stderr)
            suites_passed += completed.returncode == 0
            browser_ok &= completed.returncode == 0
            for line in completed.stdout.splitlines():
                named_passes += line.startswith("CASE PASS ")
                named_failures += line.startswith("CASE FAIL ")
        if browser_pool:
            browser_pool.shutdown(wait=True)
            print(f"Playwright: {suites_passed}/{len(scripts)} suites passed, {named_passes} named checks passed, "
                  f"{named_failures} named checks failed. Suites also run workflow assertions.")
    return 0 if result.wasSuccessful() and browser_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
