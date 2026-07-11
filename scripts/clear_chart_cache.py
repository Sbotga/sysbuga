"""Delete the pre-rendered chart-clip cache so the background worker regenerates it from
scratch - e.g. after changing render settings (resolution/fps/watermark) or the clip length.

Run from the project root, ideally with the bot stopped so nothing is mid-render:

    python -m scripts.clear_chart_cache            # clear every chart type
    python -m scripts.clear_chart_cache chart      # clear only the "chart" pool

Self-contained (no service imports) so it works even while the render code is being edited.
"""

import shutil
import sys
import tempfile
from pathlib import Path

# keep in sync with services/chart_cache.py + chart_clip.py
CACHE_ROOT = Path("cache/chart_clips")
TYPES = ("chart", "chart_append")
SCRATCH = Path(tempfile.gettempdir()) / "sbuga_chart_clips"


def _count(d: Path) -> int:
    # published entries are folders whose names don't start with "_" (temp/claim markers)
    return sum(1 for e in d.iterdir() if e.is_dir() and not e.name.startswith("_"))


def main() -> None:
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    types = wanted or list(TYPES)
    unknown = [t for t in types if t not in TYPES]
    if unknown:
        raise SystemExit(f"unknown chart type(s) {unknown}; valid: {list(TYPES)}")

    total = 0
    for gtype in types:
        d = CACHE_ROOT / gtype
        n = _count(d) if d.exists() else 0
        total += n
        shutil.rmtree(d, ignore_errors=True)
        print(f"  {gtype}: deleted {n} cached clips ({d})")

    # leftover per-render scratch folders a crash/kill can strand
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)
        print(f"  cleared render scratch dir ({SCRATCH})")

    print(f"Done. Removed {total} cached clips.")
    print("The background worker refills the pools on the next bot start.")


if __name__ == "__main__":
    main()
