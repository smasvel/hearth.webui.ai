#!/usr/bin/env python3
"""Safely stream and extract an uploaded project ZIP inside its isolated container."""
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_PROJECT_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
SKIP_PARTS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}


def normalized_entry(info):
    name = info.filename.replace('\\', '/')
    while name.startswith('./'):
        name = name[2:]
    if name in ('', '.'):
        return None
    path = PurePosixPath(name.rstrip('/'))
    if (path.is_absolute() or not path.parts or
            any(part in ('', '.', '..') for part in path.parts) or
            re.match(r'^[A-Za-z]:', name) or '\x00' in name or '\n' in name or '\r' in name or
            len(name) > 4096):
        raise ValueError('The project ZIP contains an unsafe path.')
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFLNK or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ValueError('The project ZIP may contain only regular files and folders.')
    if info.flag_bits & 0x1:
        raise ValueError('Encrypted project ZIP files are not supported.')
    return path


def main():
    target = Path(sys.argv[1]).resolve()
    options = set(sys.argv[2:])
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='hearth-project-', suffix='.zip',
                                     dir=target.parent, delete=False) as tmp:
        archive_path = Path(tmp.name)
        total = 0
        while True:
            chunk = sys.stdin.buffer.read(8 * 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError('The project ZIP exceeds the 16 GiB upload limit.')
            tmp.write(chunk)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ENTRIES:
                raise ValueError('The project ZIP is empty or contains too many entries.')
            plans = []
            seen = set()
            expanded = 0
            checkpoint_size = 0
            for info in entries:
                path = normalized_entry(info)
                if path is None:
                    continue
                key = path.as_posix()
                if key in seen:
                    raise ValueError('The project ZIP contains duplicate paths.')
                seen.add(key)
                is_dir = info.is_dir() or stat.S_IFMT((info.external_attr >> 16) & 0xFFFF) == stat.S_IFDIR
                if not is_dir:
                    if any(part in SKIP_PARTS for part in path.parts):
                        plans.append((info, path, is_dir))
                        continue
                    expanded += info.file_size
                    if expanded > MAX_EXPANDED_BYTES:
                        raise ValueError('Expanded project files exceed the 64 GiB archive limit.')
                    if info.file_size <= MAX_FILE_BYTES:
                        checkpoint_size += info.file_size
                        if checkpoint_size > MAX_PROJECT_BYTES:
                            raise ValueError('Checkpointable project files exceed the 2 GiB workspace limit.')
                plans.append((info, path, is_dir))
            if not any(not is_dir for _info, _path, is_dir in plans):
                raise ValueError('The project ZIP does not contain any files.')
            for info, relative, is_dir in plans:
                if any(part in SKIP_PARTS for part in relative.parts):
                    continue
                if '--large-only' in options and (
                        is_dir or info.file_size <= MAX_FILE_BYTES):
                    continue
                destination = target.joinpath(*relative.parts)
                if not destination.resolve().is_relative_to(target):
                    raise ValueError('The project ZIP contains a path outside the project directory.')
                if is_dir:
                    destination.mkdir(parents=True, exist_ok=True)
                    destination.chmod(0o755)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                open_mode = 'wb' if '--replace' in options else 'xb'
                with archive.open(info) as source, destination.open(open_mode) as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_EXPANDED_BYTES or written > info.file_size:
                            raise ValueError('The project ZIP expanded beyond its declared file size.')
                        output.write(chunk)
                if written != info.file_size:
                    raise ValueError('The project ZIP contains a truncated file.')
                mode = (info.external_attr >> 16) & 0x1FF
                destination.chmod(0o444 if info.file_size > MAX_FILE_BYTES else
                                  0o755 if mode & 0o111 else 0o644)
    finally:
        archive_path.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Project ZIP extraction failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
