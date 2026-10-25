#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
python_cmd=${PYTHON:-python3}
command -v "$python_cmd" >/dev/null || {
  echo 'Python 3.12 or newer is required. Install Python, then rerun this script.' >&2
  exit 1
}
"$python_cmd" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
  echo 'Python 3.12 or newer is required.' >&2
  exit 1
}
"$python_cmd" tools/release_installer.py "$@"
