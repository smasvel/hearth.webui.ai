"""Kick the Muse model test through Hearth's own API so Live logs show it."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
from _side_test_helpers import as_admin, csrf, report  # noqa: E402


MODEL = {"backend": "lemonade", "id": "Muse-Glimmer-30B-GGUF"}
print("== Hearth model test ==")
client = as_admin()
started = client.post("/api/model-tests", json={"models": [MODEL], "auto_repair": False, "suite": "all"}, headers=csrf())
report("start", started.status_code)
if started.status_code >= 400:
    print(started.get_json())
    raise SystemExit(started.status_code)
job_id = started.get_json()["id"]
for _ in range(240):
    row = client.get(f"/api/model-tests/{job_id}").get_json()
    if row.get("status") in ("completed", "cancelled", "error"):
        break
    print(row.get("status"), row.get("progress") or {}, flush=True)
    time.sleep(2)
row = client.get(f"/api/model-tests/{job_id}").get_json()
report("final status", row.get("status"))
report("results", len(row.get("results") or []))
for result in row.get("results") or []:
    print(f"    {result.get('name')} {'PASS' if result.get('passed') else 'FAIL'} -> {result.get('error') or ''}")
