"""Run one Playwright regression script in a private PostgreSQL schema."""
from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database


def main():
    if len(sys.argv) != 2 or not (database.ROOT / 'tools' / sys.argv[1]).is_file():
        raise SystemExit('Usage: run_browser_test.py check_..._ui.py')
    with database.temporary_schema():
        runpy.run_path(str(database.ROOT / 'tools' / sys.argv[1]), run_name='__main__')


if __name__ == '__main__':
    main()
