"""Run Hearth model tests sequentially: load, test, unload."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app
from _side_test_helpers import as_admin, csrf, report


def main():
    from unittest.mock import patch
    import app as _app
    patcher = patch.object(_app.users_store, "get_user_by_id", side_effect=lambda uid: {"id": uid, "username": uid, "role": "admin"})
    patcher.start()
    client = as_admin()
    models = (client.get("/api/models").get_json() or {}).get("models", [])
    chat_models = [m for m in models if m.get("type") == "llm" and m.get("id")]
    print(f"Found {len(chat_models)} chat models")

    for idx, model in enumerate(chat_models, 1):
        backend = model.get("backend")
        model_id = model.get("id")
        print(f"\n[{idx}/{len(chat_models)}] {backend}/{model_id}")

        load = client.post("/api/models/load", json={"model": model_id}, headers=csrf())
        report("load", load.status_code)
        if load.status_code >= 400:
            print(load.get_json())
            continue

        started = client.post("/api/model-tests", json={"models": [{"backend": backend, "id": model_id}], "auto_repair": False, "suite": "core"}, headers=csrf())
        report("test start", started.status_code)
        if started.status_code < 400:
            job_id = started.get_json()["id"]
            for _ in range(300):
                row = client.get(f"/api/model-tests/{job_id}").get_json()
                if row.get("status") in ("completed", "cancelled", "error"):
                    break
                time.sleep(2)
            row = client.get(f"/api/model-tests/{job_id}").get_json()
            report("test status", row.get("status"))
            for result in row.get("results") or []:
                print(f"    {result.get('name')} {'PASS' if result.get('passed') else 'FAIL'} {result.get('error') or ''}")

        unload = client.post("/api/models/unload", json={"model": model_id}, headers=csrf())
        report("unload", unload.status_code)
        time.sleep(5)


if __name__ == "__main__":
    main()
