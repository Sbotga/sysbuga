"""Event games-per-hour heatmap for a tier (rows = days, columns = the 24 hours of the day).

Inspired by RoboNene's /heatmap. Each in-event, already-elapsed hour shows how many "games" the
tier played that hour - a game being any rise in its event points (even +1), attributed to the
hour we first see the higher score (RoboNene's rule). Cells:
  - a gradient-colored games count (white-clamped at 32) for an hour with good data coverage,
  - the same plus a yellow star when a few fetches were missed (possibly a little off),
  - "ND" in red when over half the hour's fetches were missed (not trustworthy),
  - blank for an in-event hour still in the future,
  - a red X for an hour outside the event (before it starts or after it ends).

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
_ND_FILL = (38, 30, 32, 255)  # a cell with too little data to trust ("ND")
_FLAG_YELLOW = (255, 205, 70, 255)  # the footer asterisk + warning text color


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


def _tier_series_and_coverage(
    snapshots: Iterable[dict], tier: int, tz: datetime.tzinfo, day_one: datetime.date
) -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], set[int]]]:
    """from the saved snapshots, build:
    games[(day, hour)]   = how many times rank `tier`'s event points rose in that hour. any rise
                           (even +1) is a game; a game is attributed to the hour we first observe
                           the higher score (RoboNene's rule - the hour it finishes/ticks up)
    covered[(day, hour)] = which minutes we actually have a fetch for (data coverage)
    """
    covered: dict[tuple[int, int], set[int]] = defaultdict(set)
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
        score = next(
            (
                row.get("score")
                for row in ranking.get("rankings", [])
                if row.get("rank") == tier
            ),
            None,
        )
        if score is None:
            continue  # fetched, but no data for this tier - counts as a missed minute
        local = dt.astimezone(tz)
        covered[((local.date() - day_one).days, local.hour)].add(local.minute)
        series.append((int(dt.timestamp() * 1000), score))

    series.sort()
    games: dict[tuple[int, int], int] = defaultdict(int)
    last: int | None = None
    for ts_ms, score in series:
        if last is not None and score > last:
            local = datetime.datetime.fromtimestamp(ts_ms / 1000, tz)
            games[((local.date() - day_one).days, local.hour)] += 1
        last = score
    return games, covered


def _missed_stats(m_lo: int, m_hi: int, covered: set[int]) -> tuple[int, int]:
    """(minutes missed, longest run of consecutive missed minutes) over the [m_lo, m_hi) range"""
    missed = 0
    run = 0
    longest = 0
    for m in range(m_lo, m_hi):
        if m in covered:
            run = 0
        else:
            missed += 1
            run += 1
            longest = max(longest, run)
    return missed, longest


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
    tier: int,
) -> bytes:
    """render the games-per-hour heatmap for a tier (start_at/end_at/now are epoch ms). PNG bytes.
    `tz_overridden` is True when the user forced the timezone via the command option
    """
    start_dt = datetime.datetime.fromtimestamp(start_at / 1000, tz)
    end_dt = datetime.datetime.fromtimestamp(end_at / 1000, tz)
    day_one = start_dt.date()
    # XXX: on the 2 DST-transition days a calendar day is really 23/25h but we always draw 24 hour
    # cells - a cosmetic off-by-one twice a year. leaving it for now; revisit later.
    num_days = max(1, (end_dt.date() - day_one).days + 1)

    games, covered = _tier_series_and_coverage(snapshots, tier, tz, day_one)

    # classify every cell up front (so we know whether to reserve footer legend lines)
    cells: dict[tuple[int, int], tuple] = {}
    has_flag = False
    has_nd = False
    for row in range(num_days):
        for hour in range(24):
            cell_dt = datetime.datetime.combine(
                day_one + datetime.timedelta(days=row),
                datetime.time(hour=hour),
                tzinfo=tz,
            )
            cs = int(cell_dt.timestamp() * 1000)
            ce = cs + _HOUR_MS
            if ce <= start_at or cs >= end_at:
                cells[(row, hour)] = ("outside",)
                continue
            if cs > now:
                cells[(row, hour)] = ("future",)
                continue
            # how many of this hour's in-event, already-elapsed minutes we should have fetched
            elapsed_end = min(ce, end_at, now)
            m_lo = max(0, (max(cs, start_at) - cs) // 60000)
            m_hi = min(60, -(-(elapsed_end - cs) // 60000))  # ceil-divide
            missed, longest = _missed_stats(m_lo, m_hi, covered.get((row, hour), set()))
            expected = m_hi - m_lo
            count = games.get((row, hour), 0)
            if expected > 0 and missed > expected / 2:
                cells[(row, hour)] = ("nd",)  # over half missing - not trustworthy
                has_nd = True
            elif missed > 3 or longest >= 2:
                cells[(row, hour)] = ("flag", count)  # some gaps - mark maybe-off
                has_flag = True
            else:
                cells[(row, hour)] = ("count", count)

    cell = 34 * _SCALE
    day_axis_w = 30 * _SCALE  # rotated "Day" label on the far left
    left = 168 * _SCALE  # day-label column
    heading_h = 42 * _SCALE  # the overall title
    axis_h = 24 * _SCALE  # the "Hour" axis title
    hours_h = 22 * _SCALE  # the 0-24 hour numbers
    footer_gap = 20 * _SCALE  # padding between the grid and the footer
    footer_h = 20 * _SCALE  # one footer line
    pad = 16 * _SCALE
    bar_gap = 26 * _SCALE  # gap between grid and colorbar
    bar_w = 20 * _SCALE  # colorbar width
    bar_label_w = 46 * _SCALE  # colorbar tick numbers

    grid_left = pad + day_axis_w + left
    grid_top = pad + heading_h + axis_h + hours_h
    grid_h = num_days * cell
    grid_bottom = grid_top + grid_h
    grid_right = grid_left + 24 * cell

    label_font = ImageFont.truetype(_FONT_PATH, 15 * _SCALE)
    zero_font = ImageFont.truetype(_FONT_PATH, 13 * _SCALE)
    flag_font = ImageFont.truetype(_FONT_BOLD, 20 * _SCALE)
    footer_font = ImageFont.truetype(_FONT_PATH, 13 * _SCALE)
    axis_font = ImageFont.truetype(_FONT_BOLD, 17 * _SCALE)
    heading_font = ImageFont.truetype(_FONT_BOLD, 24 * _SCALE)

    right_edge = grid_right + bar_gap + bar_w + bar_label_w + pad
    width = max(right_edge, int(pad + heading_font.getlength(title) + pad))
    footer_lines = 1 + int(has_flag) + int(has_nd)
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
            elif kind[0] == "nd":  # too much data missing
                draw.rectangle(inner, fill=_ND_FILL)
                draw.text(
                    (x0 + cell / 2, y0 + cell / 2),
                    "ND",
                    font=zero_font,
                    fill=_X_LINE,
                    anchor="mm",
                )
            else:  # "count" or "flag" - a games figure, colored by the gradient
                value = kind[1]
                fill = _color_for(value, _WHITE_POINT)
                text_color = _text_on(fill)
                draw.rectangle(inner, fill=fill)
                draw.text(
                    (x0 + cell / 2, y0 + cell / 2),
                    str(value),
                    font=zero_font,
                    fill=text_color,
                    anchor="mm",
                )
                if kind[0] == "flag":
                    # asterisk top-right, in the cell's own text color so it stays readable
                    draw.text(
                        (x0 + cell - 4 * _SCALE, y0 + 2 * _SCALE),
                        "*",
                        font=flag_font,
                        fill=text_color,
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

    # "Hour" axis title, centered over the columns
    draw.text(
        ((grid_left + grid_right) / 2, pad + heading_h),
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
    if has_flag:
        footer_y += footer_h
        draw.text(
            (pad, footer_y),
            "* Possibly inaccurate due to failed data fetch.",
            font=footer_font,
            fill=_FLAG_YELLOW,
            anchor="la",
        )
    if has_nd:
        footer_y += footer_h
        draw.text(
            (pad, footer_y),
            "ND* No or extremely inaccurate data. Most of our data does not exist.",
            font=footer_font,
            fill=_X_LINE,
            anchor="la",
        )

    out = img.resize((width // _SCALE, height // _SCALE), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()
