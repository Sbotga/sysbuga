"""Event games-per-hour heatmap (rows = days, columns = the 24 hours of the day).

Two modes: "cutoff" tracks a tier (rank position), "user" tracks a specific player. Each in-event,
already-elapsed hour shows how many "games" were played - a game being any rise in event points
(even +1), attributed to the hour we first see the higher score (RoboNene's rule). Cells:
  - a gradient-colored games count (white-clamped at 32) for an hour with good coverage,
  - a light-blue "+" (user mode): they were off the top 100 part of the hour, so may have more
    games we couldn't see - the count is a floor ("N+"),
  - a yellow "*": partial data (PD) - a real fetch gap, so the count may be a little off,
  - "MD" in red: missing data - our fetches failed for most of the hour,
  - "ND" (user mode, soft color): no data - we fetched all hour but they weren't on the top 100,
  - blank for an in-event hour still in the future,
  - a red X for an hour outside the event (before it starts or after it ends).

Coverage is gap-based (not per-minute), so normal poll drift near 60s doesn't get flagged; only a
real gap of more than ~2 minutes between consecutive fetches counts as missing.

Hours/days are bucketed in a chosen timezone (default Eastern), shown in the corner of the image.
"""

from __future__ import annotations

import datetime
import functools
import io
import zoneinfo
from collections import defaultdict
from collections.abc import Iterable
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

# a curated set of common zones (short label -> IANA name) surfaced first in autocomplete. any IANA
# name is accepted too - see resolve_tz / is_valid_tz.
TIMEZONES: dict[str, str] = {
    "ET": "America/New_York",
    "CT": "America/Chicago",
    "MT": "America/Denver",
    "PT": "America/Los_Angeles",
    "UTC": "UTC",
    "UK": "Europe/London",
    "CET": "Europe/Paris",
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
    "IST": "Asia/Kolkata",
    "AEST": "Australia/Sydney",
    "BRT": "America/Sao_Paulo",
}
DEFAULT_TIMEZONE = "ET"


@functools.lru_cache(maxsize=1)
def _iana_lower() -> dict[str, str]:
    """lowercased IANA name -> canonical IANA name, so lookups are case-insensitive"""
    return {name.lower(): name for name in zoneinfo.available_timezones()}


def iana_timezones() -> list[str]:
    """every IANA timezone name, sorted"""
    return sorted(_iana_lower().values())


def is_valid_tz(name: str | None) -> bool:
    """True if `name` is a common label or any IANA timezone (case-insensitive)"""
    raw = (name or "").strip()
    return bool(raw) and (raw.upper() in TIMEZONES or raw.lower() in _iana_lower())


def resolve_tz(name: str | None) -> tuple[datetime.tzinfo, str]:
    """map a stored/typed timezone (common label or any IANA name, case-insensitive) to
    (tzinfo, label), falling back to the default (Eastern)"""
    raw = (name or "").strip()
    if raw.upper() in TIMEZONES:
        label = raw.upper()
        return ZoneInfo(TIMEZONES[label]), label
    canonical = _iana_lower().get(raw.lower())
    if canonical:
        return ZoneInfo(canonical), canonical
    return ZoneInfo(TIMEZONES[DEFAULT_TIMEZONE]), DEFAULT_TIMEZONE


def timezone_suggestions(query: str) -> list[str]:
    """autocomplete choices: just the common labels until they type, then a search across all
    IANA names too (capped at 25)"""
    query = (query or "").strip().lower()
    if not query:
        return list(TIMEZONES)
    matches = [tz for tz in TIMEZONES if query in tz.lower()]
    matches += [name for name in iana_timezones() if query in name.lower()]
    return matches[:25]


_HOUR_MS = 3_600_000
_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

_FONT_PATH = "data/assets/image_gen/rodinntlg_m.otf"
_FONT_BOLD = "data/assets/image_gen/rodinntlg_db.otf"

_BG = (17, 17, 17, 255)
_TEXT = (242, 245, 250, 255)
_MUTED = (150, 162, 178, 255)
_GRID = (40, 52, 66, 255)
_X_FILL = (48, 24, 27, 255)  # an hour outside the event's window
_X_LINE = (214, 68, 78, 255)

_SCALE = 2  # supersample, then downscale for crisp text/lines

# RoboNene's default "standard" heatmap gradient, low -> high (its palette after reversescale)
_GRADIENT = [
    (48, 25, 52),  # #301934
    (121, 83, 169),  # #7953A9
    (139, 116, 189),  # #8B74BD
    (54, 144, 192),  # #3690C0
    (103, 169, 207),  # #67A9CF
    (166, 189, 219),  # #A6BDDB
    (208, 209, 230),  # #D0D1E6
    (236, 226, 240),  # #ECE2F0
    (252, 212, 220),  # #FCD4DC
]
# the gradient spans 0 (dark) to this count (white); higher counts clamp to the white end
_WHITE_POINT = 32
# red + yellow are reserved for OUR fault (a failed fetch): MD (missing) and PD (partial)
_MD_FILL = (48, 24, 27, 255)  # "MD" cell - our fetches failed for most of the hour
_MD_TEXT = (214, 68, 78, 255)
_FLAG_YELLOW = (255, 205, 70, 255)  # PD asterisk + its footer note
# not our fault: N+ (off-lb part of the hour) light blue, ND (off-lb all hour) a soft color
_NPLUS_BLUE = (96, 176, 240, 255)
_ND_CELL = (28, 31, 40, 255)
_ND_TEXT = (150, 162, 200, 255)
_RANK_GREEN = (120, 200, 130, 255)  # "Currently T.." subtitle when the player is ranked

# coverage is gap-based: each fetch "covers" +-_COVER_MS, so consecutive fetches up to ~2x that
# apart stay contiguous (normal poll drift is fine). only larger gaps count as missing time.
_COVER_MS = 60_000
_PD_MISSING_MS = 120_000  # >2 min of genuine gap in an hour -> partial data (PD)


def _gradient_at(t: float) -> tuple[int, int, int, int]:
    """the gradient color at position t in [0, 1]"""
    t = max(0.0, min(1.0, t))
    pos = t * (len(_GRADIENT) - 1)
    i = int(pos)
    if i >= len(_GRADIENT) - 1:
        return (*_GRADIENT[-1], 255)
    frac = pos - i
    a, b = _GRADIENT[i], _GRADIENT[i + 1]
    return tuple(int(a[c] + (b[c] - a[c]) * frac) for c in range(3)) + (255,)  # type: ignore[return-value]


def _color_for(value: int, maxval: int) -> tuple[int, int, int, int]:
    """a cell color interpolated along the gradient by value/maxval"""
    return _gradient_at(value / maxval if maxval else 0.0)


def _text_on(rgb: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """dark text on a light cell, light text on a dark one"""
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (20, 20, 26, 255) if luminance > 150 else _TEXT


def _series_and_coverage(
    snapshots: Iterable[dict],
    mode: str,
    key: int,
    tz: datetime.tzinfo,
    day_one: datetime.date,
    chapter_cid: int | None = None,
    chapter: bool = False,
) -> tuple[
    dict[tuple[int, int], int],
    list[int],
    dict[tuple[int, int], int],
    dict[tuple[int, int], int],
]:
    """from the saved snapshots, build:
    games[cell]   = how many times the tracked score rose in that hour (a game). mode "cutoff"
                    tracks rank `key`'s points; mode "user" tracks user id `key`'s points.
    fetch_times   = sorted ms timestamps of every snapshot (a snapshot = a successful fetch),
                    used for gap-based coverage regardless of whether the target was present.
    present[cell] = # of fetched snapshots the target appeared in (on the top 100 that minute)
    absent[cell]  = # of fetched snapshots the target was NOT in (off the top 100 that minute)
    """
    fetch_times: list[int] = []
    present: dict[tuple[int, int], int] = defaultdict(int)
    absent: dict[tuple[int, int], int] = defaultdict(int)
    series: list[tuple[int, int]] = []
    for snap in snapshots:
        ranking = snap.get("ranking") or {}
        created = ranking.get("createdAt")
        if not created:
            continue
        try:
            dt = datetime.datetime.fromisoformat(created)
        except ValueError:
            continue
        ts_ms = int(dt.timestamp() * 1000)
        fetch_times.append(ts_ms)
        # a world-link chapter tracks its own sub-ranking (matched by focus character);
        # otherwise the overall event top 100
        if chapter:
            rows = next(
                (
                    c.get("rankings") or []
                    for c in ranking.get("userWorldBloomChapterRankings", [])
                    if c.get("gameCharacterId") == chapter_cid
                ),
                [],
            )
        else:
            rows = ranking.get("rankings", [])
        field = "rank" if mode == "cutoff" else "userId"
        score = next(
            (row.get("score") for row in rows if row.get(field) == key),
            None,
        )
        local = dt.astimezone(tz)
        cell = ((local.date() - day_one).days, local.hour)
        if score is None:
            absent[cell] += 1
        else:
            present[cell] += 1
            series.append((ts_ms, score))

    fetch_times.sort()
    series.sort()
    games: dict[tuple[int, int], int] = defaultdict(int)
    last: int | None = None
    for ts_ms, score in series:
        if last is not None and score > last:
            local = datetime.datetime.fromtimestamp(ts_ms / 1000, tz)
            games[((local.date() - day_one).days, local.hour)] += 1
        last = score
    return games, fetch_times, present, absent


def _covered_intervals(fetch_times: list[int]) -> list[tuple[int, int]]:
    """merge each fetch's +-_COVER_MS window into disjoint covered intervals (sorted input)."""
    merged: list[tuple[int, int]] = []
    for t in fetch_times:
        s, e = t - _COVER_MS, t + _COVER_MS
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _missing_ms(merged: list[tuple[int, int]], lo: int, hi: int) -> int:
    """ms of [lo, hi) not inside any covered interval (a real gap between fetches)."""
    covered = 0
    for s, e in merged:
        a, b = max(s, lo), min(e, hi)
        if b > a:
            covered += b - a
    return max(0, (hi - lo) - covered)


def _rotated_text(text: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    """`text` rendered and rotated 90 degrees (reads bottom-to-top), tightly cropped"""
    bbox = font.getbbox(text)
    tmp = Image.new("RGBA", (bbox[2] - bbox[0], bbox[3] - bbox[1]), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((-bbox[0], -bbox[1]), text, font=font, fill=_TEXT)
    return tmp.rotate(90, expand=True)


def render_heatmap(
    start_at: int,
    end_at: int,
    now: int,
    title: str,
    tz: datetime.tzinfo,
    tz_label: str,
    tz_overridden: bool,
    snapshots: Iterable[dict],
    mode: str,
    key: int,
    current_rank: int | None = None,
    username: str | None = None,
    thumb_png: bytes | None = None,
    section: str | None = None,
    chapter_cid: int | None = None,
    chapter: bool = False,
) -> bytes:
    """render the games-per-hour heatmap (start_at/end_at/now are epoch ms). PNG bytes.
    mode "cutoff" tracks rank `key`; mode "user" tracks user id `key` (and can show ND/N+).
    `tz_overridden` is True when the user forced the timezone via the command option.
    In user mode, `current_rank`/`username`/`thumb_png` drive the "Currently T.." subtitle
    and the player name + leader-card thumbnail shown in the top-right header.
    `section` (world-link only) is drawn as a second title line - the chapter name or
    "Overall". When `chapter` is set, a world-link chapter's sub-ranking is tracked (matched
    by focus character `chapter_cid`) over the chapter's own start/end window.
    """
    start_dt = datetime.datetime.fromtimestamp(start_at / 1000, tz)
    end_dt = datetime.datetime.fromtimestamp(end_at / 1000, tz)
    day_one = start_dt.date()
    # XXX: on the 2 DST-transition days a calendar day is really 23/25h but we always draw 24 hour
    # cells - a cosmetic off-by-one twice a year. leaving it for now; revisit later.
    num_days = max(1, (end_dt.date() - day_one).days + 1)

    games, fetch_times, present, absent = _series_and_coverage(
        snapshots, mode, key, tz, day_one, chapter_cid, chapter
    )
    merged = _covered_intervals(fetch_times)
    is_user = mode == "user"

    # user mode: a subtitle under the title showing where they sit on the live leaderboard
    subtitle: str | None = None
    subtitle_color = _MUTED
    if is_user:
        if current_rank:
            subtitle = f"Currently T{current_rank}"
            subtitle_color = _RANK_GREEN
        else:
            subtitle = "Not currently in leaderboards"

    # classify every cell up front (so we know whether to reserve footer legend lines).
    # markers: MD/PD are our fault (fetch gaps); ND/N+ are the user being off the top 100.
    cells: dict[tuple[int, int], tuple] = {}
    has_pd = has_md = has_nd = has_nplus = False
    for row in range(num_days):
        for hour in range(24):
            cell = (row, hour)
            cell_dt = datetime.datetime.combine(
                day_one + datetime.timedelta(days=row),
                datetime.time(hour=hour),
                tzinfo=tz,
            )
            cs = int(cell_dt.timestamp() * 1000)
            ce = cs + _HOUR_MS
            if ce <= start_at or cs >= end_at:
                cells[cell] = ("outside",)
                continue
            if cs > now:
                cells[cell] = ("future",)
                continue
            # elapsed in-event window of this hour, and how much of it had no fetch coverage
            lo = max(cs, start_at)
            hi = min(ce, end_at, now)
            window = max(1, hi - lo)
            missing = _missing_ms(merged, lo, hi)
            count = games.get(cell, 0)
            if missing > window / 2:  # our fetches failed for most of the hour
                cells[cell] = ("md",)
                has_md = True
                continue
            if (
                is_user and present.get(cell, 0) == 0
            ):  # fetched all hour, never on the top 100
                cells[cell] = ("nd",)
                has_nd = True
                continue
            plus = (
                is_user and absent.get(cell, 0) > 0
            )  # off the top 100 part of the hour
            pd = missing > _PD_MISSING_MS  # a real gap of a couple minutes
            has_nplus = has_nplus or plus
            has_pd = has_pd or pd
            cells[cell] = ("count", count, plus, pd)

    cell = 34 * _SCALE
    day_axis_w = 30 * _SCALE  # rotated "Day" label on the far left
    left = 168 * _SCALE  # day-label column
    heading_h = 42 * _SCALE  # the overall title
    section_h = 42 * _SCALE if section else 0  # the chapter/"Overall" line (world link)
    subtitle_h = 28 * _SCALE if subtitle else 0  # the "Currently T.." line (user mode)
    header_h = heading_h + section_h + subtitle_h  # the whole title block
    axis_h = 24 * _SCALE  # the "Hour" axis title
    hours_h = 22 * _SCALE  # the 0-24 hour numbers
    footer_gap = 20 * _SCALE  # padding between the grid and the footer
    footer_h = 20 * _SCALE  # one footer line
    pad = 16 * _SCALE
    bar_gap = 26 * _SCALE  # gap between grid and colorbar
    bar_w = 20 * _SCALE  # colorbar width
    bar_label_w = 46 * _SCALE  # colorbar tick numbers

    grid_left = pad + day_axis_w + left
    grid_top = pad + header_h + axis_h + hours_h
    grid_h = num_days * cell
    grid_bottom = grid_top + grid_h
    grid_right = grid_left + 24 * cell

    label_font = ImageFont.truetype(_FONT_PATH, 15 * _SCALE)
    zero_font = ImageFont.truetype(_FONT_PATH, 13 * _SCALE)
    flag_font = ImageFont.truetype(_FONT_BOLD, 20 * _SCALE)
    footer_font = ImageFont.truetype(_FONT_PATH, 13 * _SCALE)
    axis_font = ImageFont.truetype(_FONT_BOLD, 17 * _SCALE)
    heading_font = ImageFont.truetype(_FONT_BOLD, 24 * _SCALE)
    subtitle_font = ImageFont.truetype(_FONT_BOLD, 15 * _SCALE)
    name_font = ImageFont.truetype(_FONT_BOLD, 18 * _SCALE)

    # user mode: a leader-card thumbnail + player name pinned to the top-right header band
    thumb_sz = header_h  # square, spans the whole title block
    name_gap = 12 * _SCALE
    show_panel = is_user and bool(username or thumb_png)
    panel_w = 0
    if show_panel:
        if thumb_png:
            panel_w += thumb_sz
        if username:
            panel_w += int(name_font.getlength(username)) + name_gap

    title_w = int(heading_font.getlength(title))
    if section:
        title_w = max(title_w, int(heading_font.getlength(section)))
    right_edge = grid_right + bar_gap + bar_w + bar_label_w + pad
    header_w = pad + title_w + (pad + panel_w if panel_w else 0) + pad
    width = max(right_edge, header_w)
    footer_lines = 1 + has_md + has_pd + has_nd + has_nplus
    height = grid_bottom + footer_gap + footer_h * footer_lines

    img = Image.new("RGBA", (width, height), _BG)
    draw = ImageDraw.Draw(img)

    # cells
    for row in range(num_days):
        for hour in range(24):
            kind = cells[(row, hour)]
            if kind[0] == "future":
                continue
            x0 = grid_left + hour * cell
            y0 = grid_top + row * cell
            inner = [x0 + _SCALE, y0 + _SCALE, x0 + cell - _SCALE, y0 + cell - _SCALE]
            if kind[0] == "outside":  # not part of the event - red X
                draw.rectangle(inner, fill=_X_FILL)
                inset = 3 * _SCALE
                draw.line(
                    [(x0 + inset, y0 + inset), (x0 + cell - inset, y0 + cell - inset)],
                    fill=_X_LINE,
                    width=2 * _SCALE,
                )
                draw.line(
                    [(x0 + inset, y0 + cell - inset), (x0 + cell - inset, y0 + inset)],
                    fill=_X_LINE,
                    width=2 * _SCALE,
                )
            elif kind[0] == "md":  # our fetches failed most of the hour
                draw.rectangle(inner, fill=_MD_FILL)
                draw.text(
                    (x0 + cell / 2, y0 + cell / 2),
                    "MD",
                    font=zero_font,
                    fill=_MD_TEXT,
                    anchor="mm",
                )
            elif kind[0] == "nd":  # user, fetched all hour but never on the top 100
                draw.rectangle(inner, fill=_ND_CELL)
                draw.text(
                    (x0 + cell / 2, y0 + cell / 2),
                    "ND",
                    font=zero_font,
                    fill=_ND_TEXT,
                    anchor="mm",
                )
            else:  # ("count", value, plus, pd) - a games figure, colored by the gradient
                _, value, plus, pd = kind
                fill = _color_for(value, _WHITE_POINT)
                text_color = _text_on(fill)
                draw.rectangle(inner, fill=fill)
                draw.text(
                    (x0 + cell / 2, y0 + cell / 2),
                    f"{value}+" if plus else str(value),
                    font=zero_font,
                    fill=_NPLUS_BLUE if plus else text_color,
                    anchor="mm",
                )
                if pd:  # partial data - yellow asterisk, top-right
                    draw.text(
                        (x0 + cell - 4 * _SCALE, y0 + 2 * _SCALE),
                        "*",
                        font=flag_font,
                        fill=_FLAG_YELLOW,
                        anchor="ra",
                    )

    # grid lines - the hours sit on the vertical lines, days between the horizontal ones
    for hour in range(25):
        x = grid_left + hour * cell
        draw.line([(x, grid_top), (x, grid_bottom)], fill=_GRID, width=_SCALE)
    for row in range(num_days + 1):
        y = grid_top + row * cell
        draw.line([(grid_left, y), (grid_right, y)], fill=_GRID, width=_SCALE)

    # overall title, top-left and larger
    draw.text((pad, pad), title, font=heading_font, fill=_TEXT, anchor="la")

    # world link: the chapter name / "Overall" as a second title line (same font + size)
    if section:
        draw.text(
            (pad, pad + heading_h), section, font=heading_font, fill=_TEXT, anchor="la"
        )

    # user mode: the leaderboard-standing subtitle under the title, and the player
    # name + leader-card thumbnail pinned to the top-right (like the ISV deck header)
    if subtitle:
        draw.text(
            (pad, pad + heading_h + section_h),
            subtitle,
            font=subtitle_font,
            fill=subtitle_color,
            anchor="la",
        )
    if show_panel:
        band_mid = pad + header_h / 2
        x = width - pad
        if thumb_png:
            try:
                thumb = (
                    Image.open(io.BytesIO(thumb_png))
                    .convert("RGBA")
                    .resize((thumb_sz, thumb_sz), Image.LANCZOS)
                )
                img.alpha_composite(thumb, (x - thumb_sz, pad))
                x -= thumb_sz + name_gap
            except Exception:
                pass
        if username:
            draw.text((x, band_mid), username, font=name_font, fill=_TEXT, anchor="rm")

    # "Hour" axis title, centered over the columns
    draw.text(
        ((grid_left + grid_right) / 2, pad + header_h),
        "Hour",
        font=axis_font,
        fill=_TEXT,
        anchor="ma",
    )
    # "Day" axis title, rotated on the far left and centered over the rows
    day_img = _rotated_text("Day", axis_font)
    img.alpha_composite(
        day_img,
        (
            pad + (day_axis_w - day_img.width) // 2,
            grid_top + (grid_h - day_img.height) // 2,
        ),
    )

    # hour numbers 0-24 on the vertical lines
    for hour in range(25):
        x = grid_left + hour * cell
        draw.text(
            (x, grid_top - 6 * _SCALE),
            str(hour),
            font=label_font,
            fill=_TEXT,
            anchor="ms",
        )

    # day labels down the left, Day 1 on top
    for row in range(num_days):
        weekday = _WEEKDAYS[(day_one + datetime.timedelta(days=row)).weekday()]
        y = grid_top + row * cell + cell / 2
        draw.text(
            (grid_left - 8 * _SCALE, y),
            f"({weekday}) Day {row + 1}",
            font=label_font,
            fill=_TEXT,
            anchor="rm",
        )

    # colorbar on the right, low (dark) at the bottom to high (light) at the top
    bar_x = grid_right + bar_gap
    for py in range(grid_h):
        draw.line(
            [(bar_x, grid_top + py), (bar_x + bar_w, grid_top + py)],
            fill=_gradient_at(1 - py / grid_h),
        )
    draw.rectangle(
        [bar_x, grid_top, bar_x + bar_w, grid_bottom], outline=_GRID, width=_SCALE
    )
    for value in range(0, _WHITE_POINT + 1, 8):
        ty = grid_bottom - (value / _WHITE_POINT) * grid_h
        draw.text(
            (bar_x + bar_w + 5 * _SCALE, ty),
            str(value),
            font=label_font,
            fill=_MUTED,
            anchor="lm",
        )

    # footer legend: the timezone note, then a line for each marker that actually appears
    note = f"Times shown in {tz_label}"
    if not tz_overridden:
        note += " (configurable in user settings)"
    footer_y = grid_bottom + footer_gap
    draw.text((pad, footer_y), note, font=footer_font, fill=_MUTED, anchor="la")

    def footer_line(text: str, color: tuple[int, int, int, int]) -> None:
        nonlocal footer_y
        footer_y += footer_h
        draw.text((pad, footer_y), text, font=footer_font, fill=color, anchor="la")

    if has_md:
        footer_line(
            "MD - Missing data. Our fetches failed for most of this hour.", _MD_TEXT
        )
    if has_pd:
        footer_line(
            "* - Partial data. Some fetches failed, so this hour's count may be off.",
            _FLAG_YELLOW,
        )
    if has_nplus:
        footer_line(
            "N+ - At least this many; they were not in the top 100 for part of this hour.",
            _NPLUS_BLUE,
        )
    if has_nd:
        footer_line(
            "ND - No data. They were not in the top 100 for this hour.",
            _ND_TEXT,
        )

    out = img.resize((width // _SCALE, height // _SCALE), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()
