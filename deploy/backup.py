"""Create an owner-only, consistent SQLite snapshot, including committed WAL data."""

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path


def backup(source: Path, destination: Path):
    if not source.is_file() or source.resolve() == destination.resolve():
        raise ValueError("Source must exist and destination must be a different file")
    os.umask(0o077)
    handle, temporary = tempfile.mkstemp(prefix=".vpn-bot-backup-", dir=destination.parent)
    os.close(handle)
    try:
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(temporary)
        try:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Snapshot integrity check failed")
        finally:
            dst.close()
            src.close()
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup(args.source, args.destination)
