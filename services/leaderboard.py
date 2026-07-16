"""Event top-100 leaderboard: hour-over-hour stats from the saved snapshots, and the rendered
leaderboard image (one row per player: rank + leader card + name + their figures).

A "game" is any rise in event points, counted from the first snapshot a player appears in - the
same rule the heatmap uses. We only ever see a player while they're in the top 100, so their
counts start when they first show up there.
"""

from __future__ import annotations

import datetime
import io
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

_HOUR_MS = 3_600_000

_FONT_MED = "data/assets/image_gen/rodinntlg_m.otf"
_FONT_BOLD = "data/assets/image_gen/rodinntlg_db.otf"

_BG = (17, 17, 17, 255)
_ROW_ALT = (25, 25, 30, 255)  # zebra striping so long rows stay readable
_TEXT = (242, 245, 250, 255)
_MUTED = (150, 162, 178, 255)
_UP = (120, 200, 130, 255)
_DOWN = (231, 106, 106, 255)
_YOU = (255, 205, 70, 255)  # highlight for the caller's own row


@dataclass
class LBRow:
    """one rendered leaderboard row"""

    rank: str  # "T5"
    delta_dir: int  # +1 climbed, -1 dropped, 0 unchanged
    delta_n: int  # how many places moved
    card: bytes | None  # the player's rendered leader card png
    name: str
    values: list[str]  # right-aligned figures, one per column
    is_you: bool = False


def _draw_delta(
    draw: ImageDraw.ImageDraw,
    x: float,
    mid: float,
    direction: int,
    n: int,
    font: ImageFont.FreeTypeFont,
) -> None:
    """The rank-change marker: a hand-drawn triangle plus the count. The font's arrow glyphs
    render badly at this size, so the triangle is a polygon."""
    if direction == 0:
        draw.text((x + 3, mid), "-", font=font, fill=_MUTED, anchor="lm")
        return
    color = _UP if direction > 0 else _DOWN
    w, h = 11, 9
    top = mid - h / 2
    if direction > 0:
        points = [(x, top + h), (x + w, top + h), (x + w / 2, top)]
    else:
        points = [(x, top), (x + w, top), (x + w / 2, top + h)]
    draw.polygon(points, fill=color)
    draw.text((x + w + 5, mid), str(n), font=font, fill=color, anchor="lm")


def _rows_of(ranking: dict, chapter_cid: int | None, chapter: bool) -> list[dict]:
    """the ranking rows to read: a world-link chapter's sub-ranking, or the overall top 100"""
    if not chapter:
        return ranking.get("rankings") or []
    return next(
        (
            c.get("rankings") or []
            for c in ranking.get("userWorldBloomChapterRankings", [])
            if c.get("gameCharacterId") == chapter_cid
        ),
        [],
    )


def hour_stats(
    snapshots: Iterable[dict],
    user_ids: Iterable[int],
    chapter_cid: int | None = None,
    chapter: bool = False,
) -> tuple[dict[int, int], dict[int, int], dict[int, tuple[int, int, int]], int | None]:
    """One streaming pass over an event's snapshots, for the players in `user_ids`.

    Returns (games, last_score, baseline, baseline_ts): `games` is each player's cumulative game
    count, `last_score` their score in the newest snapshot, and `baseline` maps a player to their
    (score, rank, games) an hour before the newest snapshot - what every "last hour" figure is
    diffed against. Anchoring to the newest snapshot (rather than the wall clock) keeps this
    right even when the tracker has fallen behind. Blocking; run off the event loop.
    """
    targets = set(user_ids)
    games: dict[int, int] = {}
    last_score: dict[int, int] = {}
    # (ts, {uid: (score, rank, games)}) for the last hour of snapshots; the left end is the
    # oldest snapshot still inside that hour, i.e. the baseline
    window: deque[tuple[int, dict[int, tuple[int, int, int]]]] = deque()

    for snap in snapshots:
        ranking = snap.get("ranking") or {}
        created = ranking.get("createdAt")
        if not created:
            continue
        try:
            ts = int(datetime.datetime.fromisoformat(created).timestamp() * 1000)
        except ValueError:
            continue
        rows = _rows_of(ranking, chapter_cid, chapter)

        state: dict[int, tuple[int, int, int]] = {}
        for row in rows:
            uid = row.get("userId")
            # archival saves carry cutoffs with no userId at all - without this every one of
            # those rows collapses onto a single None "player"
            if uid is None or uid not in targets:
                continue
            score = row.get("score")
            if score is None:
                continue
            prev = last_score.get(uid)
            if prev is None:
                games[uid] = 1  # first time we see them counts as a game
            elif score > prev:
                games[uid] = games.get(uid, 0) + 1
            last_score[uid] = score
            state[uid] = (score, row.get("rank") or 0, games[uid])

        while window and window[0][0] <= ts - _HOUR_MS:
            window.popleft()
        window.append((ts, state))

    if not window:
        return games, last_score, {}, None
    baseline_ts, baseline = window[0]
    return games, last_score, baseline, baseline_ts


def render_leaderboard(rows: list[LBRow], columns: list[str]) -> bytes:
    """The leaderboard table as a PNG: rank (+ tier change) and leader card on the left, then
    the player's name, then one right-aligned figure per column. Blocking (PIL); run off the
    event loop."""
    pad = 22
    row_h = 74
    card = 58
    rank_w = 150  # "T100" + the change triangle and count
    name_w = 460
    col_w = 190
    width = pad + rank_w + card + 12 + name_w + col_w * len(columns) + pad
    header_h = 44
    height = header_h + row_h * max(1, len(rows)) + pad

    img = Image.new("RGBA", (width, height), _BG)
    draw = ImageDraw.Draw(img)
    f_rank = ImageFont.truetype(_FONT_BOLD, 26)
    f_delta = ImageFont.truetype(_FONT_MED, 19)
    f_name = ImageFont.truetype(_FONT_BOLD, 25)
    f_val = ImageFont.truetype(_FONT_MED, 24)
    f_head = ImageFont.truetype(_FONT_BOLD, 19)

    name_x = pad + rank_w + card + 12
    col_x = name_x + name_w  # left edge of the first figure column

    # header labels: figures are right-aligned to each column's right edge
    draw.text((pad, header_h // 2), "T", font=f_head, fill=_MUTED, anchor="lm")
    draw.text((name_x, header_h // 2), "Name", font=f_head, fill=_MUTED, anchor="lm")
    for i, label in enumerate(columns):
        draw.text(
            (col_x + col_w * (i + 1) - 10, header_h // 2),
            label,
            font=f_head,
            fill=_MUTED,
            anchor="rm",
        )

    for i, row in enumerate(rows):
        y = header_h + i * row_h
        mid = y + row_h // 2
        if i % 2:
            draw.rectangle([0, y, width, y + row_h], fill=_ROW_ALT)

        rank_color = _YOU if row.is_you else _TEXT
        draw.text((pad, mid), row.rank, font=f_rank, fill=rank_color, anchor="lm")
        _draw_delta(
            draw,
            pad + f_rank.getlength(row.rank) + 10,
            mid,
            row.delta_dir,
            row.delta_n,
            f_delta,
        )

        if row.card:
            try:
                art = (
                    Image.open(io.BytesIO(row.card))
                    .convert("RGBA")
                    .resize((card, card), Image.Resampling.LANCZOS)
                )
                img.alpha_composite(art, (pad + rank_w, mid - card // 2))
            except Exception:
                pass

        name = row.name
        while name and f_name.getlength(name) > name_w - 14:
            name = name[:-1]
        draw.text((name_x, mid), name, font=f_name, fill=rank_color, anchor="lm")

        for j, value in enumerate(row.values):
            draw.text(
                (col_x + col_w * (j + 1) - 10, mid),
                value,
                font=f_val,
                fill=_TEXT if value not in ("N/A", "-") else _MUTED,
                anchor="rm",
            )

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
