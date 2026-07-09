"""Songs that are 1:1 copies of one another (or should be considered as such)

for example, the intense voice of hatsune miku's "append" variant (388)
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).parent / "song_equivalents.json"


def _load() -> dict[int, set[int]]:
    with open(_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {int(k): set(v) for k, v in raw.items() if not k.startswith("_")}


SONG_EQUIVALENTS: dict[int, set[int]] = _load()


def songs_equivalent(a: int, b: int) -> bool:
    """
    if two song ids are equivalent
    """
    return (
        a == b or b in SONG_EQUIVALENTS.get(a, ()) or a in SONG_EQUIVALENTS.get(b, ())
    )


def equivalents_of(song_id: int) -> set[int]:
    """
    every other song id equivalent to this one, in either direction
    """
    linked = set(SONG_EQUIVALENTS.get(song_id, ()))
    linked.update(k for k, v in SONG_EQUIVALENTS.items() if song_id in v)
    linked.discard(song_id)
    return linked
