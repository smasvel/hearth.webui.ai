#!/usr/bin/env python3
"""Block pushes containing credentials or private infrastructure identifiers.

Run with --all-history for an audit, or as a pre-push hook (refs on stdin).
Findings intentionally contain only paths, line numbers and rule names.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZERO = '0' * 40
PATTERNS = {
    'private-key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----'),
    'provider-token': re.compile(rb'\b(?:sk-(?:proj-|live-)?[A-Za-z0-9_-]{20,}|gh[psour]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,}|hf_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16})\b'),
    'credential-assignment': re.compile(rb'''(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|secret[_-]?key|bearer[_-]?token|client[_-]?secret)[\w.-]*['"]?\s*(?:=|:)\s*['"]([^'"\r\n]{8,})['"]'''),
    'credential-env': re.compile(rb'\b(?:PASSWORD|PASSWD|API_KEY|ACCESS_TOKEN|SECRET_KEY|CLIENT_SECRET|POSTGRES_PASSWORD)=([^\s\'"$;]+)'),
    'credential-url': re.compile(rb'(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/]+:[^\s/@]+@'),
    'bearer-token': re.compile(rb'(?i)\bBearer\s+[A-Za-z0-9._-]{24,}\b'),
    'tailnet-name': re.compile(rb'(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b'),
    'private-hostname': re.compile(rb'(?i)https?://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|internal)\b'),
    'private-ip': re.compile(rb'\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3})\b'),
}
PLACEHOLDER = re.compile(rb'(?i)(?:example|placeholder|your[-_ ]|changeme|dummy|fake|test[-_ ]|current-key|%s|hostname\.ts\.net|homeassistant\.local|localhost|127\.0\.0\.1|0\.0\.0\.0|\$\{|\{[a-z_]+\}|<[^>]+>)')
SENSITIVE_NAME = re.compile(r'(?i)(?:^|/)(?:\.env(?:\..+)?|id_(?:rsa|ed25519)|[^/]*\.(?:pem|p12|pfx|key)|[^/]*credentials?)$')
# Location-bearing datasets and diagnostics should stay out of credential review
# output and should not be considered push candidates by this scanner.
EXCLUDED_LOCATION_PATH = re.compile(
    r'(?i)(?:^|/)[^/]*(?:location|geolocation|gps|coordinates?|weather|addresses?)[^/]*(?:$)'
)
# Reviewed synthetic IP literals in certificate and network-help test fixtures.
# Hashes avoid repeating private-range literals in the scanner itself.
FIXTURE_IP_HASHES = {
    'tests/test_help_data.py': {'f5047344122f0dee9974ba6761e61c6b8649e1f3968d13a635ebbf7be53a3a0d'},
    'tests/test_local_tls.py': {'552354592821b7e6b76f87d10c0cb4ff512ffed049dde0e40769a0bcf9f50c8d'},
}


def git(*args: str, input_data: bytes | None = None) -> bytes:
    result = subprocess.run(['git', *args], cwd=ROOT, input=input_data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f'git {args[0]} failed ({result.returncode})')
    return result.stdout


def refs_from_push() -> list[tuple[str, str]]:
    refs = []
    for line in sys.stdin:
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError('Invalid pre-push input; refusing to push.')
        _, local_sha, _, remote_sha = fields
        if local_sha != ZERO:
            refs.append((local_sha, remote_sha))
    return refs


def objects_for_refs(refs: list[tuple[str, str]]) -> dict[str, set[str]]:
    objects: dict[str, set[str]] = {}
    for local_sha, remote_sha in refs:
        args = ['rev-list', '--objects', local_sha]
        if remote_sha != ZERO:
            args.append('^' + remote_sha)
        for raw in git(*args).splitlines():
            sha, _, path = raw.partition(b' ')
            objects.setdefault(sha.decode(), set()).add(path.decode('utf-8', 'replace'))
    return objects


def scan_content(data: bytes, path: str) -> list[str]:
    findings = []
    for number, line in enumerate(data.splitlines(), 1):
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                if label == 'private-ip' and path in FIXTURE_IP_HASHES:
                    digest = hashlib.sha256(match.group(0)).hexdigest()
                    if digest in FIXTURE_IP_HASHES[path]:
                        continue
                # For assignments, inspect only the captured value. A variable
                # named example_api_key must not exempt a real credential.
                candidate = match.group(1) if label in {'credential-assignment', 'credential-env'} else match.group(0)
                if label in {'provider-token', 'private-key', 'bearer-token', 'private-ip'} or not PLACEHOLDER.search(candidate):
                    findings.append(f'{path}:{number}: {label}')
    return findings


def scan_objects(objects: dict[str, set[str]]) -> list[str]:
    findings = []
    for sha, paths in objects.items():
        if git('cat-file', '-t', sha).strip() != b'blob':
            continue
        data = git('cat-file', 'blob', sha)
        for path in paths:
            if not path:
                continue
            if EXCLUDED_LOCATION_PATH.search(path):
                continue
            if SENSITIVE_NAME.search(path) and path != '.env.example':
                findings.append(f'{path} @ {sha[:12]}: sensitive filename')
            findings.extend(f'{item} @ {sha[:12]}' for item in scan_content(data, path))
    return findings


def working_files() -> list[str]:
    tracked = git('ls-files', '-z').split(b'\0')
    untracked = git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')
    return sorted({item.decode('utf-8', 'replace') for item in tracked + untracked if item})


def scan_working_tree() -> list[str]:
    findings = []
    for path in working_files():
        if EXCLUDED_LOCATION_PATH.search(path):
            continue
        file = ROOT / path
        if not file.is_file() or file.is_symlink():
            continue
        if SENSITIVE_NAME.search(path) and path != '.env.example':
            findings.append(f'{path}: sensitive filename')
        findings.extend(scan_content(file.read_bytes(), path))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all-history', action='store_true')
    parser.add_argument('--working-tree', action='store_true')
    args = parser.parse_args()
    try:
        if args.all_history:
            heads = [sha.decode() for sha in git('for-each-ref', '--format=%(objectname)',
                                                'refs/heads', 'refs/tags', 'refs/remotes').splitlines()]
            heads.append(git('rev-parse', 'HEAD').strip().decode())
            refs = [(sha, ZERO) for sha in set(heads)]
        elif args.working_tree:
            refs = []
        else:
            refs = refs_from_push()
        findings = scan_objects(objects_for_refs(refs)) if refs else []
        if args.working_tree:
            findings.extend(scan_working_tree())
    except (OSError, RuntimeError) as exc:
        print(f'Secret check could not complete: {exc}', file=sys.stderr)
        return 2
    for finding in sorted(set(findings)):
        print(finding)
    print(f'Secret check: {len(set(findings))} finding(s).')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
