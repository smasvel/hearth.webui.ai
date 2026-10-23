#!/usr/bin/env python3
"""Inspect a converted Qwen3.8-27B package without loading it or touching the NPU."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flm_backend_profile as profile


def flm_json(flm_bin, flm_dir, args):
    env = {**os.environ, "LD_LIBRARY_PATH": str(flm_dir / "lib")}
    result = subprocess.run(
        [str(flm_bin), *args], capture_output=True, text=True, env=env, timeout=30, check=False,
    )
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("conversion", type=Path)
    parser.add_argument("--flm-dir", type=Path, default=Path.home() / "fastflowlm")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()

    flm_bin = args.flm_dir / "flm"
    version_output = flm_json(flm_bin, args.flm_dir, ["--version"])
    version = profile.parse_version(version_output)
    version_text = ".".join(map(str, version)) if version else None
    try:
        installed = json.loads(flm_json(
            flm_bin, args.flm_dir, ["list", "--filter", "installed", "--json"],
        ))
    except json.JSONDecodeError:
        installed = {}
    tags = {
        str(item.get("model") or item.get("name"))
        for item in installed.get("models", [])
        if item.get("model") or item.get("name")
    }
    report = profile.assess_conversion(
        args.conversion, args.flm_dir / "xclbins", tags, version_text,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{report['display_name']}: {'READY' if report['ready'] else 'BLOCKED'}")
        print(f"Artifact: {report['directory']}")
        print(f"Runtime: FLM {version_text or 'unknown'} -> {report['runtime_tag']}")
        if report["warnings"]:
            print("Warnings:")
            for warning in report["warnings"]:
                print(f"  - {warning}")
        if report["blockers"]:
            print("Blockers:")
            for blocker in report["blockers"]:
                print(f"  - [{blocker['code']}] {blocker['message']}")
    raise SystemExit(0 if report["ready"] or args.allow_incomplete else 2)


if __name__ == "__main__":
    main()
