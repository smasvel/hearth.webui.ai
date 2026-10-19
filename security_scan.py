"""Bounded, read-only host security checks for the admin chat.

This deliberately is not a shell interface. Every command is a fixed argv list,
has a timeout, and has capped output. The sole privileged operation is an exact
Lynis command authorized by a narrowly scoped sudoers rule; the web process never
obtains sudo credentials.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    title: str
    level: str
    detail: str


def _run(argv: list[str], timeout: int = 12, limit: int = 30_000) -> tuple[int, str]:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode, output[:limit]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _mode(path: Path) -> str:
    try:
        return stat.filemode(path.stat().st_mode)
    except OSError:
        return "unreadable"


def _listener_checks() -> tuple[list[Check], str]:
    rc, output = _run(["ss", "-lntupH"])
    if rc:
        return [Check("Network listeners", "WARN", f"`ss` failed: {output}")], output

    public_tcp: list[str] = []
    risky: list[str] = []
    for line in output.splitlines():
        if not line.startswith("tcp "):
            continue
        columns = line.split()
        if len(columns) < 5:
            continue
        address = columns[4]
        if address.startswith(("127.", "[::1]:")):
            continue
        public_tcp.append(address)
        match = re.search(r":(\d+)$", address)
        port = int(match.group(1)) if match else -1
        if 5900 <= port <= 5999 or port in {23, 111, 139, 445, 3306, 5432, 6379, 27017}:
            risky.append(address)

    checks = [
        Check(
            "Network exposure",
            "INFO" if public_tcp else "PASS",
            (
                f"{len(public_tcp)} TCP listener(s) bind beyond loopback: "
                + ", ".join(f"`{item}`" for item in public_tcp[:20])
            )
            if public_tcp
            else "No TCP listeners bind beyond loopback.",
        )
    ]
    if risky:
        checks.append(
            Check(
                "High-risk listening ports",
                "HIGH",
                "Review access controls for " + ", ".join(f"`{item}`" for item in risky),
            )
        )
    return checks, output


def _update_checks() -> list[Check]:
    rc, output = _run(["apt", "list", "--upgradable"], timeout=20)
    if rc:
        return [Check("Package updates", "WARN", f"Unable to query APT: {output}")]
    packages = [
        line for line in output.splitlines()
        if line and not line.startswith(("Listing", "WARNING"))
    ]
    security = [line for line in packages if "-security" in line]
    level = "HIGH" if security else ("MEDIUM" if packages else "PASS")
    detail = f"{len(packages)} update(s) available"
    if security:
        detail += f", including {len(security)} explicitly security-tagged update(s)"
        detail += ": " + ", ".join(line.split("/")[0] for line in security[:12])
    return [Check("Package updates", level, detail + ".")]


def _permission_checks(base_dir: Path) -> list[Check]:
    checks: list[Check] = []
    sensitive = [
        base_dir / ".env",
        base_dir / "certs" / "key.pem",
        base_dir / "chat_history.db",
        base_dir / "data" / "chat.db",
        base_dir / "data" / "chat_history.db",
        base_dir / "data" / "settings.db",
        Path.home() / ".ssh" / "id_rsa",
        Path.home() / ".ssh" / "id_ed25519",
    ]
    weak: list[str] = []
    for path in sensitive:
        if not path.exists():
            continue
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            weak.append(f"`{path}` ({_mode(path)})")
    checks.append(
        Check(
            "Sensitive file permissions",
            "HIGH" if weak else "PASS",
            "Overly broad permissions: " + ", ".join(weak)
            if weak
            else "Checked application secrets, TLS key, and user SSH private keys; no broad access found.",
        )
    )

    rc, output = _run(
        ["find", str(base_dir), "-xdev", "-type", "f", "-perm", "-0002", "-print"],
        timeout=10,
    )
    files = output.splitlines() if rc == 0 and output else []
    checks.append(
        Check(
            "World-writable application files",
            "HIGH" if files else "PASS",
            "Found: " + ", ".join(f"`{item}`" for item in files[:20])
            if files else "None found.",
        )
    )
    return checks


def _lynis_check() -> tuple[Check, bool]:
    """Run the one exact command allowed by deploy/hearth-lynis-sudoers."""
    lynis = Path("/usr/sbin/lynis")
    if not lynis.is_file():
        return Check(
            "Lynis privileged audit",
            "WARN",
            "Lynis is not installed (`sudo apt install lynis`).",
        ), False

    rc, output = _run(
        ["sudo", "-n", str(lynis), "audit", "system", "--quick", "--no-colors"],
        timeout=300,
        limit=80_000,
    )
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)
    if rc != 0:
        if any(
            marker in clean.lower()
            for marker in ("password", "not allowed", "authentication is required")
        ):
            detail = (
                "The narrow sudoers permission is not active. Install "
                "`deploy/hearth-lynis-sudoers` as `/etc/sudoers.d/hearth-lynis`."
            )
        elif "timed out" in clean.lower():
            detail = "The Lynis audit exceeded the five-minute safety timeout."
        else:
            detail = "Lynis failed: " + (clean[-500:].strip() or f"exit status {rc}")
        return Check("Lynis privileged audit", "WARN", detail), False

    hardening = re.search(r"Hardening index\s*:\s*(\d+)", clean, re.IGNORECASE)
    warnings = re.search(r"Warnings\s*\(\s*(\d+)\s*\)", clean, re.IGNORECASE)
    suggestions = re.search(r"Suggestions\s*\(\s*(\d+)\s*\)", clean, re.IGNORECASE)
    warning_count = int(warnings.group(1)) if warnings else 0
    parts = ["completed successfully as root"]
    if hardening:
        parts.append(f"hardening index {hardening.group(1)}")
    if warnings:
        parts.append(f"{warning_count} warning(s)")
    if suggestions:
        parts.append(f"{suggestions.group(1)} suggestion(s)")
    return Check(
        "Lynis privileged audit",
        "HIGH" if warning_count else "PASS",
        ", ".join(parts).capitalize() + ". Full details: `/var/log/lynis.log`.",
    ), True


def run_scan(base_dir: str | Path) -> str:
    """Return a Markdown security report. No mutation and no arbitrary commands."""
    base_dir = Path(base_dir).resolve()
    checks: list[Check] = []

    lynis_check, lynis_ran = _lynis_check()
    checks.append(
        Check(
            "Scan privilege",
            "INFO",
            (
                "The root-level Lynis audit completed through the exact-command "
                "sudoers exception; no general passwordless sudo access is available."
            )
            if lynis_ran
            else (
                "The Lynis sudo exception was unavailable. Other checks continue "
                "unprivileged; root-only coverage is incomplete."
            ),
        )
    )
    checks.append(lynis_check)

    checks.extend(_update_checks())
    listener_checks, _ = _listener_checks()
    checks.extend(listener_checks)

    rc, output = _run(["systemctl", "--failed", "--no-legend", "--no-pager"])
    failed = [line for line in output.splitlines() if line.strip()] if rc == 0 else []
    checks.append(
        Check(
            "Failed services",
            "MEDIUM" if failed else "PASS",
            f"{len(failed)} failed service(s): " + "; ".join(failed[:10])
            if failed else "No failed system services reported.",
        )
    )

    _, unattended = _run(["systemctl", "is-active", "unattended-upgrades.service"])
    checks.append(
        Check(
            "Automatic security updates",
            "PASS" if unattended.strip() == "active" else "MEDIUM",
            f"`unattended-upgrades.service` is {unattended.strip() or 'unknown'}.",
        )
    )
    checks.extend(_permission_checks(base_dir))

    audit_tools = []
    for tool in ("lynis", "debsecan", "rkhunter", "chkrootkit", "pip-audit"):
        if shutil.which(tool):
            audit_tools.append(tool)
    checks.append(
        Check(
            "Specialized audit tools",
            "INFO" if audit_tools else "WARN",
            "Available: " + ", ".join(f"`{tool}`" for tool in audit_tools)
            if audit_tools
            else "No Lynis, debsecan, rootkit scanner, or pip-audit installation detected.",
        )
    )

    ranks = {"HIGH": 4, "MEDIUM": 3, "WARN": 2, "INFO": 1, "PASS": 0}
    counts = {level: sum(c.level == level for c in checks) for level in ranks}
    highest = max(checks, key=lambda item: ranks[item.level]).level
    lines = [
        "# Host security scan",
        "",
        f"Run: {_dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Overall triage: **{highest}**",
        (
            f"Findings: {counts['HIGH']} high, {counts['MEDIUM']} medium, "
            f"{counts['WARN']} warning, {counts['PASS']} passed."
        ),
        "",
    ]
    for check in checks:
        lines.extend([f"- **[{check.level}] {check.title}:** {check.detail}", ""])
    lines.extend([
        "This is a defensive configuration audit, not a guarantee that the host is "
        "unhackable. A clean scan cannot prove absence of compromise.",
    ])
    return "\n".join(lines)
