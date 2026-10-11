"""Consistent SQLite snapshots, including committed data still in the WAL."""

import argparse
import os
import sqlite3
import time
from contextlib import ExitStack
from pathlib import Path


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a new file; never overwrite a database or an earlier backup.
    with destination.open("xb"):
        pass
    try:
        with ExitStack() as resources:
            original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            resources.callback(original.close)
            snapshot = sqlite3.connect(destination)
            resources.callback(snapshot.close)
            deadline = time.monotonic() + 60
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError("Backup did not finish within 60 seconds")
            original.backup(snapshot, pages=256, progress=progress, sleep=.05)
            if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description="Back up a running LANmsg SQLite database")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("LANMSG_DB", "data/lanmsg.db")))
    args = parser.parse_args()
    print(backup_database(args.database, args.destination))


if __name__ == "__main__":
    main()
