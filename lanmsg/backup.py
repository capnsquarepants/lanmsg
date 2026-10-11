"""Consistent SQLite snapshots, including committed data still in the WAL."""

import argparse
import os
import sqlite3
import time
import shutil
from contextlib import ExitStack
from pathlib import Path

from .attachments import ID_PATTERN, attachment_directory


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    companion = attachment_directory(destination)
    if companion.exists():
        raise FileExistsError(companion)
    # Reserve a new file; never overwrite a database or an earlier backup.
    with destination.open("xb"):
        pass
    copied_files = []
    companion_created = False
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
            if snapshot.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'").fetchone():
                attachment_ids = [row[0] for row in snapshot.execute("SELECT id FROM attachments")]
                if attachment_ids:
                    companion.mkdir()
                    companion_created = True
                    for attachment_id in attachment_ids:
                        if not ID_PATTERN.fullmatch(attachment_id):
                            raise ValueError("Invalid attachment ID in database")
                        if time.monotonic() > deadline:
                            raise TimeoutError("Backup did not finish within 60 seconds")
                        target = companion / attachment_id
                        with (attachment_directory(source) / attachment_id).open('rb') as original_file, target.open('xb') as copy:
                            copied_files.append(target)
                            shutil.copyfileobj(original_file, copy, length=1024 * 1024)
    except Exception:
        destination.unlink(missing_ok=True)
        for file in copied_files:
            file.unlink(missing_ok=True)
        if companion_created:
            companion.rmdir()
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
