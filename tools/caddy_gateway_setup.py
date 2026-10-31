"""Generate Hearth's authenticated Caddy model gateway.

The gateway used to be assembled by hand: three bearer tokens pasted into a
``(client_auth)`` snippet, five port blocks copied from the docs, and Caddy's local CA
trusted separately. The installers call this module so a fresh host gets a complete,
randomly keyed gateway, and the Docker installer can point the container at it instead
of showing ``SET_A_BEARER_TOKEN_IN_CADDYFILE`` in Help.

Existing tokens are preserved on every run: rerunning an installer must never invalidate
an OpenCode/Continue config that is already in use. A state file that already exists is
left byte-for-byte alone; only a missing one is generated.
"""
import argparse
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Kept in sync with app.py's _CADDY_UPSTREAM and help_data._UPSTREAM_BACKEND.
BACKENDS = (
    # (name, external HTTPS port, local upstream port)
    ("lmstudio", 9443, 1234),
    ("lemonade", 8444, 13306),
    ("vllm", 8445, 8000),
    ("kobold", 8446, 5001),
    ("fastflowlm", 8447, 52626),
)
TOKEN_LABELS = ("opencode", "vscode", "lan")

# Older releases appended the snippet under a bare marker with no end marker. Treat that
# as "already installed" and never append a second copy.
LEGACY_MARKER = "# Hearth authenticated backend proxies"
BEGIN_MARKER = "# BEGIN Hearth authenticated backend proxies"
END_MARKER = "# END Hearth authenticated backend proxies"

TOKEN_PATTERN = re.compile(r'@(\w+)\s+header\s+Authorization\s+"Bearer\s+([A-Za-z0-9]+)"')
ADDRESS_PATTERN = re.compile(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)/")
# Container/bridge interfaces are not reachable from the tailnet or LAN and would only add
# certificate names nobody uses; the primary uplink is what the Help configs point at.
IGNORED_INTERFACES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap", "tailscale")
CADDY_ADMIN_CA = "http://localhost:2019/pki/ca/local"


def new_token() -> str:
    return secrets.token_hex(32)


def parse_tokens(text: str) -> dict[str, str]:
    return {label: token for label, token in TOKEN_PATTERN.findall(text or "")}


def generate_tokens(existing: dict[str, str] | None = None) -> dict[str, str]:
    existing = existing or {}
    return {label: existing.get(label) or new_token() for label in TOKEN_LABELS}


def detect_addresses() -> list[str]:
    """LAN IPv4 addresses, then Tailscale, then loopback — the order the app shows them."""
    addresses: list[str] = []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        for line in out.splitlines():
            match = ADDRESS_PATTERN.match(line.strip())
            if match and not match.group(1).startswith(IGNORED_INTERFACES):
                addresses.append(match.group(2))
    except (OSError, subprocess.SubprocessError):
        pass
    if shutil.which("tailscale"):
        try:
            out = subprocess.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True,
                timeout=5, check=False,
            ).stdout
            addresses += [line.strip() for line in out.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        addresses += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    addresses += ["localhost", "127.0.0.1"]
    unique: list[str] = []
    for address in addresses:
        if address.startswith("127.") and address != "127.0.0.1":
            continue
        if (re.fullmatch(r"\d+\.\d+\.\d+\.\d+", address) or address == "localhost") \
                and address not in unique:
            unique.append(address)
    return unique or ["localhost", "127.0.0.1"]


def render_caddyfile(addresses: list[str], tokens: dict[str, str], backends=BACKENDS) -> str:
    lines = ["(client_auth) {"]
    for label in TOKEN_LABELS:
        lines.append(f'\t@{label} header Authorization "Bearer {tokens[label]}"')
    lines.append("}")
    for _name, port, upstream in backends:
        listeners = ", ".join(f"{address}:{port}" for address in addresses)
        lines += [
            "",
            f"{listeners} {{",
            "\ttls internal",
            "\tlog {",
            "\t\toutput file /var/log/caddy/access.jsonl",
            "\t\tformat json",
            "\t}",
            "\troute {",
            "\t\timport client_auth",
        ]
        for label in TOKEN_LABELS:
            lines.append(f"\t\treverse_proxy @{label} localhost:{upstream}")
        lines += ["\t\trespond 401", "\t}", "}"]
    return "\n".join(lines) + "\n"


def managed_section(text: str) -> str:
    return f"{BEGIN_MARKER}\n{text.rstrip()}\n{END_MARKER}\n"


def install_section(existing: str, section: str) -> str:
    """Add or replace the Hearth block in a system Caddyfile, preserving everything else.

    Returns the text unchanged when an older release's marker is present, because that
    layout has no end marker and rewriting it could silently drop administrator edits.
    """
    if BEGIN_MARKER in existing:
        pattern = re.compile(
            re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?",
            re.DOTALL,
        )
        return pattern.sub(lambda _m: managed_section(section), existing, count=1)
    if LEGACY_MARKER in existing:
        return existing
    prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
    return prefix + "\n" + managed_section(section)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def _chown(path: Path, owner: str) -> None:
    if os.geteuid() != 0 or not owner:
        return
    try:
        import pwd
        record = pwd.getpwnam(owner)
    except KeyError:
        return
    os.chown(path, record.pw_uid, record.pw_gid)


def _validate_caddy(system_file: Path) -> bool:
    if not shutil.which("caddy"):
        print("Caddy is not installed; gateway configuration was generated but not loaded.")
        return False
    check = subprocess.run(
        ["caddy", "validate", "--config", str(system_file), "--adapter", "caddyfile"],
        capture_output=True, text=True, check=False,
    )
    if check.returncode:
        print("Caddy rejected the generated configuration:", file=sys.stderr)
        print((check.stderr or check.stdout).strip(), file=sys.stderr)
        return False
    return True


def _reload_caddy(system_file: Path) -> bool:
    if shutil.which("systemctl"):
        reload = subprocess.run(["systemctl", "reload", "caddy"], capture_output=True, text=True, check=False)
        if reload.returncode:
            subprocess.run(["systemctl", "restart", "caddy"], check=False)
        active = subprocess.run(["systemctl", "is-active", "--quiet", "caddy"], check=False)
        if active.returncode:
            print("Caddy did not stay active after loading the gateway configuration.", file=sys.stderr)
            return False
        return True
    reload = subprocess.run(["caddy", "reload", "--config", str(system_file)], capture_output=True, text=True, check=False)
    if reload.returncode:
        print("Caddy reload failed:", file=sys.stderr)
        print((reload.stderr or reload.stdout).strip(), file=sys.stderr)
        return False
    return True


def _ensure_log_dir() -> None:
    log_dir = Path("/var/log/caddy")
    if os.geteuid() != 0:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        import grp
        import pwd
        caddy = pwd.getpwnam("caddy")
        os.chown(log_dir, caddy.pw_uid, grp.getgrnam("caddy").gr_gid)
    except (KeyError, OSError):
        pass


def _prime_certificates() -> None:
    """Make Caddy issue each leaf certificate now, instead of on the first client request."""
    context = ssl._create_unverified_context()  # noqa: S323 - local priming request only
    for _name, port, _upstream in BACKENDS:
        request = urllib.request.Request(f"https://127.0.0.1:{port}/")
        try:
            urllib.request.urlopen(request, timeout=5, context=context)  # noqa: S310
        except (urllib.error.URLError, OSError, ValueError):
            pass  # 401/502 still completes the TLS handshake, which is what issues the cert.


def export_ca(destination: Path) -> bool:
    """Save Caddy's local CA root so clients can be pointed at a stable file path."""
    for attempt in range(10):
        try:
            with urllib.request.urlopen(CADDY_ADMIN_CA, timeout=4) as response:  # noqa: S310
                result = response.read().decode()
            match = re.search(r'"root_certificate"\s*:\s*"(.*?)"', result, re.S)
            certificate = (match.group(1) if match else "").replace("\\n", "\n").strip()
            if certificate:
                _write(destination, certificate + ("" if certificate.endswith("\n") else "\n"))
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5 * (attempt + 1))
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Hearth's Caddy gateway tokens, configuration, and local CA.")
    parser.add_argument("--state-file", type=Path,
                        help="Gateway config kept in the user's data dir "
                             "(default: ~/.local/share/hearth/Caddyfile).")
    parser.add_argument("--owner", help="chown the generated files to this account (root only).")
    parser.add_argument("--system-file", type=Path,
                        help="Install into this Caddyfile (typically /etc/caddy/Caddyfile).")
    parser.add_argument("--print", action="store_true", help="Print the gateway config and exit.")
    parser.add_argument("--no-reload", action="store_true",
                        help="Write the system Caddyfile but skip reloading, cert priming, and CA export.")
    args = parser.parse_args(argv)

    state_file = args.state_file or Path.home() / ".local/share/hearth/Caddyfile"
    existing = state_file.read_text() if state_file.exists() else ""
    if existing:
        # Never silently rotate keys that are already in a client configuration.
        text = existing
    else:
        text = render_caddyfile(detect_addresses(), generate_tokens(parse_tokens(existing)))
        _write(state_file, text)
        _chown(state_file, args.owner)
        print(f"Generated gateway tokens and configuration: {state_file}")

    if args.print:
        print(text, end="")
        return 0

    if not args.system_file:
        return 0

    if not shutil.which("caddy"):
        print("Caddy is not installed; skipping system gateway setup.")
        return 0
    system_file = args.system_file
    try:
        current = system_file.read_text()
    except FileNotFoundError:
        current = ""
    except OSError as exc:
        print(f"Cannot read {system_file}: {exc}", file=sys.stderr)
        return 1
    updated = install_section(current, text)
    if updated != current:
        if current.strip():
            backup = system_file.with_name(system_file.name + ".before-hearth")
            if not backup.exists():
                _write(backup, current)
        _write(system_file, updated)
        print(f"Installed the Hearth gateway block in {system_file}")
    elif LEGACY_MARKER in current and BEGIN_MARKER not in current:
        print(f"{system_file} already has the Hearth gateway block; leaving it untouched.")
    _ensure_log_dir()
    if not _validate_caddy(system_file):
        return 1
    if args.no_reload:
        return 0
    if not _reload_caddy(system_file):
        return 1

    _prime_certificates()
    ca_file = state_file.with_name("hearth-ca.crt")
    if export_ca(ca_file):
        _chown(ca_file, args.owner)
        print(f"Caddy local CA exported: {ca_file}")
    else:
        print("Caddy is running, but its local CA root was not available yet; Help can fetch it later.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
