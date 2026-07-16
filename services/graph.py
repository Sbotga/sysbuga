"""Event points-over-time line graph, rendered from the saved snapshots.

Two ways to follow a tier, mirroring the heatmap's two modes:
  - by_tier: the cutoff line - whatever score sits at rank N at each poll, whoever holds it.
  - by id (default): the player who currently holds that rank, followed backwards through the
    event even while they were at other ranks.
"""

from __future__ import annotations

import datetime
import io
import math
from collections.abc import Iterable

from PIL import Image, ImageDraw, ImageFont

from services.leaderboard import _rows_of

_FONT_MED = "data/assets/image_gen/rodinntlg_m.otf"
_FONT_BOLD = "data/assets/image_gen/rodinntlg_db.otf"
_FONT_EB = "data/assets/image_gen/rodinntlg_eb.otf"  # titles only

_SCALE = 2  # supersample factor - PIL doesn't antialias, so we draw big and shrink

# a player sitting on the rank-100 boundary pops off the board for a poll or two at a time,
# which isn't a real absence - only a hole longer than this breaks the line
_FLICKER_MS = 5 * 60_000

_BG = (17, 17, 17, 255)
_TEXT = (242, 245, 250, 255)
_MUTED = (150, 162, 178, 255)
_GRID = (40, 52, 66, 255)
_LINE = (96, 176, 240, 255)  # the tracked player
_CUTOFF = (231, 106, 106, 255)  # the tier's cutoff, when comparing
_RANK_GREEN = (120, 200, 130, 255)  # "Current T.." when the player is ranked


def _snapshot_rows(snap: dict, chapter_cid: int | None, chapter: bool) -> list[dict]:
    """every ranked row in one poll: the top 100, plus the border tiers (T200 and beyond) that
    live in a sibling key - without those, any tier past 100 looks like it has no data
    """
    ranking = snap.get("ranking") or {}
    border = snap.get("border") or {}
    rows = _rows_of(ranking, chapter_cid, chapter)
    if not chapter:
        return rows + (border.get("borderRankings") or [])
    for chap in border.get("userWorldBloomChapterRankingBorders") or []:
        if chap.get("gameCharacterId") == chapter_cid:
            return rows + (chap.get("borderRankings") or [])
    return rows


def cutoff_series(
    snapshots: Iterable[dict],
    *,
    tier: int | None = None,
    user_id: int | None = None,
    chapter_cid: int | None = None,
    chapter: bool = False,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(points, cutoff points), both oldest first.

    Pass `user_id` to follow one player, `tier` to follow the cutoff at that rank, or both to
    follow the player and pull the tier's cutoff alongside for comparison - one pass either
    way, since decoding the snapshots is what costs. Polls where the player wasn't in the top
    100 simply leave a hole in their points; the renderer reads those off the timestamps.
    Blocking; run off the event loop.
    """
    out: list[tuple[int, int]] = []
    cutoff: list[tuple[int, int]] = []
    for snap in snapshots:
        ranking = snap.get("ranking") or {}
        created = ranking.get("createdAt")
        if not created:
            continue
        try:
            ts = int(datetime.datetime.fromisoformat(created).timestamp() * 1000)
        except ValueError:
            continue
        rows = _snapshot_rows(snap, chapter_cid, chapter)
        if user_id is not None:
            score = next(
                (r.get("score") for r in rows if r.get("userId") == user_id), None
            )
            if score is not None:
                out.append((ts, score))
        if tier is not None:
            score = next((r.get("score") for r in rows if r.get("rank") == tier), None)
            if score is not None:
                (cutoff if user_id is not None else out).append((ts, score))
    out.sort()
    cutoff.sort()
    return out, cutoff


def _human(n: float) -> str:
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}{suffix}"
    return f"{n:.0f}"


def _nice_step(span: float, target: int) -> float:
    """a round-ish gridline step covering `span` in about `target` steps"""
    if span <= 0:
        return 1.0
    rough = span / max(1, target)
    magnitude = 10 ** int(f"{rough:e}".split("e")[1])
    for mult in (1, 2, 2.5, 5, 10):
        if magnitude * mult >= rough:
            return magnitude * mult
    return magnitude * 10


def _dotted_line(
    draw: ImageDraw.ImageDraw,
    p0: tuple[float, float],
    p1: tuple[float, float],
    color: tuple[int, int, int, int],
    width: int,
) -> None:
    """a dashed straight line between two points - bridges a stretch we have no data for,
    so it reads as a guess rather than a measurement"""
    (x0, y0), (x1, y1) = p0, p1
    span = math.hypot(x1 - x0, y1 - y0)
    if span <= 0:
        return
    dash, gap = 7 * _SCALE, 7 * _SCALE
    pos = 0.0
    while pos < span:
        end = min(pos + dash, span)
        draw.line(
            [
                (x0 + (x1 - x0) * pos / span, y0 + (y1 - y0) * pos / span),
                (x0 + (x1 - x0) * end / span, y0 + (y1 - y0) * end / span),
            ],
            fill=color,
            width=width,
        )
        pos += dash + gap


def _draw_legend(
    draw: ImageDraw.ImageDraw,
    entries: list[tuple[str, tuple[int, int, int, int]]],
    cx: float,
    cy: float,
    font: ImageFont.FreeTypeFont,
) -> None:
    """the color key, centered on `cx`: a line swatch plus its label per entry"""
    sw, sh, gap, item_gap = 26 * _SCALE, 5 * _SCALE, 10 * _SCALE, 34 * _SCALE
    widths = [sw + gap + font.getlength(text) for text, _ in entries]
    x = cx - (sum(widths) + item_gap * (len(entries) - 1)) / 2
    for (text, color), width in zip(entries, widths):
        draw.rounded_rectangle(
            [x, cy - sh / 2, x + sw, cy + sh / 2], radius=sh / 2, fill=color
        )
        draw.text((x + sw + gap, cy), text, font=font, fill=_TEXT, anchor="lm")
        x += width + item_gap


def render_graph(
    series: list[tuple[int, int]],
    title: str,
    tz: datetime.tzinfo,
    tz_label: str,
    *,
    current_rank: int | None = None,
    username: str | None = None,
    thumb_png: bytes | None = None,
    section: str | None = None,
    by_tier: bool = False,
    cutoff: list[tuple[int, int]] | None = None,
    series_label: str = "Player",
    cutoff_label: str = "Cutoff",
    start_at: int | None = None,
) -> bytes:
    """The points-over-time line chart as a PNG. Blocking (PIL); run off the event loop.

    The header mirrors the heatmap's: `title`, then `section` (world-link only - the chapter
    name or "Overall"), then a "Current T.." subtitle, with the player name and their
    leader-card thumbnail pinned top-right. `by_tier` follows the cutoff rather than a player,
    so it has no subtitle or panel. Stretches with no readings - the player off the top 100,
    or the tracker down - are drawn dashed rather than lined straight across.

    `cutoff` draws the tier's cutoff as a second (red) line to compare against, and adds a
    color key along the bottom.
    """
    # drawn at _SCALE then downsampled - PIL has no antialiasing, so this is what keeps the
    # curve from stairstepping
    pad = 22 * _SCALE
    heading_h = 42 * _SCALE
    section_h = 42 * _SCALE if section else 0
    subtitle: str | None = None
    subtitle_color = _MUTED
    if not by_tier:
        if current_rank:
            subtitle = f"Current T{current_rank}"
            subtitle_color = _RANK_GREEN
        else:
            subtitle = "Not currently in leaderboards"
    subtitle_h = 28 * _SCALE if subtitle else 0
    header_h = heading_h + section_h + subtitle_h

    f_head = ImageFont.truetype(_FONT_EB, 24 * _SCALE)
    f_sub = ImageFont.truetype(_FONT_BOLD, 15 * _SCALE)
    f_name = ImageFont.truetype(_FONT_BOLD, 18 * _SCALE)
    f_axis = ImageFont.truetype(_FONT_MED, 15 * _SCALE)

    thumb_sz = header_h  # square, spans the whole title block
    name_gap = 12 * _SCALE
    show_panel = not by_tier and bool(username or thumb_png)
    panel_w = 0
    if show_panel:
        if thumb_png:
            panel_w += thumb_sz
        if username:
            panel_w += int(f_name.getlength(username)) + name_gap

    pad_l, pad_r = 120 * _SCALE, 34 * _SCALE
    pad_b = (86 if cutoff else 58) * _SCALE  # the color key needs its own line
    pad_t = pad + header_h + 20 * _SCALE
    plot_h = 460 * _SCALE

    title_w = int(f_head.getlength(title))
    if section:
        title_w = max(title_w, int(f_head.getlength(section)))
    header_w = pad + title_w + (pad + panel_w if panel_w else 0) + pad
    w = max(1200 * _SCALE, header_w)
    h = pad_t + plot_h + pad_b
    plot_w = w - pad_l - pad_r

    img = Image.new("RGBA", (w, h), _BG)
    draw = ImageDraw.Draw(img)

    draw.text((pad, pad), title, font=f_head, fill=_TEXT, anchor="la")
    if section:
        draw.text((pad, pad + heading_h), section, font=f_head, fill=_TEXT, anchor="la")
    if subtitle:
        draw.text(
            (pad, pad + heading_h + section_h),
            subtitle,
            font=f_sub,
            fill=subtitle_color,
            anchor="la",
        )
    if show_panel:
        x = w - pad
        if thumb_png:
            try:
                thumb = (
                    Image.open(io.BytesIO(thumb_png))
                    .convert("RGBA")
                    .resize((thumb_sz, thumb_sz), Image.Resampling.LANCZOS)
                )
                img.alpha_composite(thumb, (x - thumb_sz, pad))
                x -= thumb_sz + name_gap
            except Exception:
                pass
        if username:
            draw.text(
                (x, pad + header_h / 2), username, font=f_name, fill=_TEXT, anchor="rm"
            )

    def _finish(image: Image.Image) -> bytes:
        out = io.BytesIO()
        image.resize((w // _SCALE, h // _SCALE), Image.Resampling.LANCZOS).convert(
            "RGB"
        ).save(out, "PNG")
        return out.getvalue()

    if not series:
        draw.text(
            (w / 2, pad_t + plot_h / 2),
            "No data for this tier",
            font=f_sub,
            fill=_MUTED,
            anchor="mm",
        )
        return _finish(img)

    # both lines share one set of axes, so the ranges have to span whichever runs longer or
    # higher - the player's series starts late if they entered the top 100 mid-event, and the
    # axis reaches back to the event opening so that lead-in has somewhere to be drawn
    spanning = series + (cutoff or [])
    t0 = min(ts for ts, _ in spanning)
    if start_at is not None:
        t0 = min(t0, start_at)
    t1 = max(ts for ts, _ in spanning)
    y1 = max(s for _, s in spanning)
    tspan = max(1, t1 - t0)
    ystep = _nice_step(y1, 6)
    ytop = max(ystep, (int(y1 / ystep) + 1) * ystep)

    def px(ts: int) -> float:
        return pad_l + (ts - t0) / tspan * plot_w

    def py(score: float) -> float:
        return pad_t + plot_h - (score / ytop) * plot_h

    # y gridlines + labels
    steps = int(ytop / ystep)
    for i in range(steps + 1):
        value = ystep * i
        y = py(value)
        draw.line([(pad_l, y), (pad_l + plot_w, y)], fill=_GRID, width=_SCALE)
        draw.text(
            (pad_l - 12 * _SCALE, y),
            _human(value),
            font=f_axis,
            fill=_MUTED,
            anchor="rm",
        )

    # x gridlines every few hours, labelled by event day + hour in the chosen timezone.
    # days are numbered from the event/chapter start, the same way the heatmap numbers its
    # rows, so "Day 1" means the same thing on both
    hours = tspan / 3_600_000
    hstep = max(3, int(_nice_step(hours, 8)))
    day_one = datetime.datetime.fromtimestamp((start_at or t0) / 1000, tz).date()
    start = datetime.datetime.fromtimestamp(t0 / 1000, tz)
    tick = start.replace(minute=0, second=0, microsecond=0)
    while True:
        ts = int(tick.timestamp() * 1000)
        if ts > t1:
            break
        if ts >= t0:
            x = px(ts)
            draw.line([(x, pad_t), (x, pad_t + plot_h)], fill=_GRID, width=_SCALE)
            draw.text(
                (x, pad_t + plot_h + 8 * _SCALE),
                f"Day {(tick.date() - day_one).days + 1} {tick:%H}h",
                font=f_axis,
                fill=_MUTED,
                anchor="ma",
            )
        tick += datetime.timedelta(hours=hstep)

    # A hole is any hop between readings longer than the flicker threshold, whichever way we
    # lost them: off the board for a stretch, or the tracker missing a beat. Both mean we have
    # nothing to say about that time, so neither gets a solid line drawn across it. Short hops
    # are the rank-100 boundary bouncing and stay continuous.
    def _draw_series(
        pts: list[tuple[int, int]], color: tuple[int, int, int, int]
    ) -> None:
        def flush(seg: list[tuple[float, float]]) -> None:
            if len(seg) > 1:
                draw.line(seg, fill=color, width=3 * _SCALE, joint="curve")
            elif seg:
                # a lone reading draws nothing as a line - a player who only just reached
                # the top 100 has exactly one, so it gets a dot or the graph looks empty
                r = 3.5 * _SCALE
                (x, y) = seg[0]
                draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

        # everyone is on zero when the event opens, so a series we only pick up later isn't
        # unknown at both ends - it gets the same dashed bridge back to the origin rather than
        # appearing out of thin air partway across
        if pts and start_at is not None and pts[0][0] - start_at > _FLICKER_MS:
            _dotted_line(
                draw,
                (px(start_at), py(0)),
                (px(pts[0][0]), py(pts[0][1])),
                color,
                3 * _SCALE,
            )

        seg: list[tuple[float, float]] = []
        for i, (ts, score) in enumerate(pts):
            seg.append((px(ts), py(score)))
            if i + 1 >= len(pts) or pts[i + 1][0] - ts <= _FLICKER_MS:
                continue
            flush(seg)
            nxt = pts[i + 1]
            _dotted_line(draw, seg[-1], (px(nxt[0]), py(nxt[1])), color, 3 * _SCALE)
            seg = []
        flush(seg)

    # cutoff first so the player's line paints over it wherever the two run together
    if cutoff:
        _draw_series(cutoff, _CUTOFF)
    _draw_series(series, _LINE)

    draw.rectangle(
        [pad_l, pad_t, pad_l + plot_w, pad_t + plot_h], outline=_GRID, width=_SCALE
    )
    if cutoff:
        _draw_legend(
            draw,
            [(series_label, _LINE), (cutoff_label, _CUTOFF)],
            w / 2,
            pad_t + plot_h + 44 * _SCALE,
            f_axis,
        )
    draw.text(
        (pad_l + plot_w, h - 16 * _SCALE),
        f"Times in {tz_label} - final {_human(series[-1][1])}",
        font=f_axis,
        fill=_MUTED,
        anchor="rd",
    )
    return _finish(img)
