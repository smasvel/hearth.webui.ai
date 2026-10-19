#!/usr/bin/env python3
"""Run one admin conversion job and stream its status to the terminal."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flm_conversion


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("name")
    args = parser.parse_args()

    job = flm_conversion.start_job(args.source, args.name, True)
    job_id = job["id"]
    log_index = 0
    previous_status = None
    print(f"Started {job_id}: {job['staging_output']}", flush=True)

    try:
        while True:
            job = flm_conversion.get_job(job_id)
            if job is None:
                print("Conversion job disappeared.", file=sys.stderr, flush=True)
                return 1
            logs = job.get("logs", [])
            for line in logs[log_index:]:
                print(line, flush=True)
            log_index = len(logs)

            status = (job["state"], job.get("progress"), job.get("detail"))
            if status != previous_status:
                print(f"STATUS {status[0]} {status[1]}%: {status[2]}", flush=True)
                previous_status = status
            if job["state"] in {"complete", "failed", "cancelled"}:
                print(f"Output: {job['output']}", flush=True)
                return 0 if job["state"] == "complete" else 1
            time.sleep(2)
    except KeyboardInterrupt:
        flm_conversion.cancel_job(job_id)
        print("Cancellation requested; partial staging output was preserved.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
