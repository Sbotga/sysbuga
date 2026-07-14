"""Retroactively zstd-compress past events under event_saves/ - the recovery path for a
compression that crashed midway (the bot normally compresses an event the moment the next one
starts). For each region it leaves the newest (current) event alone and archives all older ones.

Run from the project root:

    python -m scripts.compress_event_saves            # every region
    python -m scripts.compress_event_saves en jp      # only these regions

Crash-safe and idempotent: it writes the .zst fully before deleting the source .json, and skips
anything already archived, so it's safe to re-run any time. Self-contained (no service imports) so
it works even while the event code is being edited.
"""

import sys
from pathlib import Path

import os

import zstandard

# keep in sync with cogs/events.py
EVENT_SAVES_DIR = Path("event_saves")
FILES = ("snapshots.jsonl", "profiles.json")
ZSTD_LEVEL = 19


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _compress_file(path: Path) -> bool:
    """Compress path -> path.zst, then delete the source once the archive is safely written.
    Returns True if it produced (or completed) an archive."""
    if not path.exists():
        return False
    archive = path.with_name(path.name + ".zst")
    if archive.exists():
        path.unlink(
            missing_ok=True
        )  # a prior run wrote the archive but not the cleanup
        return True
    tmp = archive.with_name(archive.name + ".tmp")
    compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
    with path.open("rb") as src, tmp.open("wb") as dst:
        compressor.copy_stream(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    tmp.replace(archive)  # the archive now exists in full
    _fsync_dir(archive.parent)
    path.unlink()  # safe to drop the source
    return True


def _compress_event_dir(event_dir: Path) -> int:
    return sum(_compress_file(event_dir / name) for name in FILES)


def main() -> None:
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not EVENT_SAVES_DIR.exists():
        raise SystemExit(f"nothing to do - {EVENT_SAVES_DIR} does not exist")

    region_dirs = [
        d
        for d in EVENT_SAVES_DIR.iterdir()
        if d.is_dir() and (not wanted or d.name in wanted)
    ]
    if not region_dirs:
        raise SystemExit(f"no matching regions under {EVENT_SAVES_DIR}")

    total = 0
    for region_dir in region_dirs:
        event_ids = [
            int(child.name)
            for child in region_dir.iterdir()
            if child.is_dir() and child.name.isdigit()
        ]
        if not event_ids:
            continue
        current = max(event_ids)  # newest event id is the one still being written
        for event_id in sorted(event_ids):
            if event_id == current:
                print(f"  {region_dir.name}/{event_id}: current event, left live")
                continue
            n = _compress_event_dir(region_dir / str(event_id))
            total += n
            print(f"  {region_dir.name}/{event_id}: archived {n} file(s)")

    print(f"Done. Compressed {total} file(s).")


if __name__ == "__main__":
    main()
