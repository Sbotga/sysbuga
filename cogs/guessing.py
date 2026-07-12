from __future__ import annotations

import asyncio
import datetime
import io
import math
import random
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from data.pjsk import RARITY_DISPLAY, character_display_name
from data.song_equivalents import equivalents_of, songs_equivalent
from database.queries import GUESS_LEADERBOARD_PER_PAGE
from helpers import converters, embeds, periods, tools, unblock
from helpers.autocompletes import autocompletes
from helpers.config_loader import get_config
from helpers.emojis import emojis
from helpers.imaging import _crop_chart, _crop_square
from helpers.views import SbugaView
from services import chart_cache, chart_clip, chart_preview, event_story, song_clip
from services.sbuga import SbugaError

if TYPE_CHECKING:
    from main import SbugaBot

GUESS_TIME = 60
MODE_TIME = {
    "character": 30,
    "character_bw": 30,
    "chart": 90,
    "chart_append": 90,
    "chart_expert": 90,
    "music": 300,
    "event": 180,
}
# modes whose hints extend a snippet by stage (like music) rather than adding tiered facts
STAGED_MODES = {"music", "event"}
GUESS_PREFIX = "-"
# appended to every not-found or wrong-guess reply
GUESS_TIP = "\n-# Use `-hint` for a hint, `-end` to give up, or `-time` for time left!"
# music mode variant where a hint reveals more of the song rather than a fact
MUSIC_TIP = "\n-# Use `-hint` to provide more of the song, or `-time` for time left!"
# XXX: temporary
EVENT_ALIAS_PLUG = (
    "\n-# Events are new to the bot and need aliases! "
    "Help suggest aliases in support server."
)
# a hint can't fire within this many seconds of the previous one
HINT_COOLDOWN = 2.0
# non-music modes get three cumulative text hints and the last reveals this fraction of the
# name's characters at random
MAX_TEXT_HINTS = 3
SONG_REVEAL_FRACTION = 1 / 7
EVENT_REVEAL_FRACTION = 1 / 7
# the description hint shows this fraction of its characters, the rest masked as underscores
EVENT_DESC_FRACTION = 1 / 3

# songs that don't work as a /guess music round; still fine for chart and jacket guessing
MUSIC_GUESS_EXCLUDED = {674, 675, 676}

# leaderboard-eligible modes: a correct guess is worth `start` points, reduced by each pooled hint
# the round has revealed. every mode bottoms out at 250 once all its hints are shown
GUESS_POINTS: dict[str, dict[str, Any]] = {
    "chart": {"start": 5000, "deductions": (1500, 500, 2750)},
    "jacket": {"start": 2000, "deductions": (500, 250, 1000)},
    "music": {"start": 3000, "deductions": (750, 1000, 1000)},
    "event": {"start": 7000, "deductions": (1000, 1500, 2000, 2250)},
}
# prize per placement -> (paid crystals, free crystals); only listed ranks win
WEEKLY_PRIZES: dict[int, tuple[int, int]] = {1: (350, 20), 2: (110, 5), 3: (50, 0)}
MONTHLY_PRIZES: dict[int, tuple[int, int]] = {
    1: (1800, 90),
    2: (950, 50),
    3: (350, 20),
    4: (110, 5),
    **{r: (50, 0) for r in range(5, 11)},
}
# a top finish this deep is DM'd that they placed
WEEKLY_DM_TOP = 3
MONTHLY_DM_TOP = 10

# guess-attempt caps beyond which a correct guess earns 0 points. the per-round cap is shared by
# everyone guessing that round; the hourly/daily caps are per user, spanning the bot and activity
ROUND_GUESS_LIMIT = 20
HOURLY_GUESS_LIMIT = 500
DAILY_GUESS_LIMIT = 6000


def _over_guess_limit(attempts: int, hour_count: int, day_count: int) -> bool:
    """True when this guess is past the round cap or the user's hourly/daily cap (so 0 points)"""
    return (
        attempts > ROUND_GUESS_LIMIT
        or hour_count > HOURLY_GUESS_LIMIT
        or day_count > DAILY_GUESS_LIMIT
    )


def _hints_taken(mode: str, d: dict) -> int:
    """how many hints the round has revealed - music/event count a stage from 1, the tiered modes
    count hint stages from 0"""
    if mode in ("music", "event"):
        return max(0, d.get("stage", 1) - 1)
    return d.get("hint_stage", 0)


def _guess_points(mode: str, hints: int) -> "int | None":
    """points a correct guess is worth after `hints` hints, or None when the mode isn't ranked"""
    cfg = GUESS_POINTS.get(mode)
    if not cfg:
        return None
    used = min(hints, len(cfg["deductions"]))
    return cfg["start"] - sum(cfg["deductions"][:used])


def _prizes_enabled() -> bool:
    """prizes (and their button/DMs/claims) only exist when a prizes channel is configured"""
    return bool(get_config()["discord"].get("prizes_channel_id"))


# how long a winner has to claim before the prize expires
PRIZE_CLAIM_DAYS = 3


def _prize_label(prize: dict) -> str:
    """the period a prize is for, e.g. 'Week 3' or 'Month 1'"""
    kind = "Week" if prize["period_type"] == "weekly" else "Month"
    return f"{kind} {prize['period_index']}"


def _prize_reward(prize: dict) -> str:
    """the crystal reward line, e.g. '{crystal} 350 paid + 20 free crystals'"""
    parts = [f"{prize['paid_crystals']:,} paid"]
    if prize["free_crystals"]:
        parts.append(f"{prize['free_crystals']:,} free")
    return f"{emojis.crystal} {' + '.join(parts)} crystals"


def _prize_status_line(prize: dict) -> str:
    """the status shown in the /guess prize history"""
    status = prize["status"]
    if status == "unclaimed":
        if prize["expires_at"] <= datetime.datetime.now(datetime.timezone.utc):
            return "⏳ Expired (unclaimed)"
        return f"🎁 Unclaimed - expires <t:{int(prize['expires_at'].timestamp())}:R>"
    if status == "pending":
        return "🕓 Claimed - awaiting manual grant"
    if status == "complete":
        return "✅ Sent"
    if status == "denied":
        return f"❌ Denied: {prize.get('deny_reason') or 'no reason given'}"
    if status == "forfeited":
        return "🚫 Forfeited"
    return status


def _masked_name(name: str, fraction: float) -> str:
    """name with a random fraction of its non-space characters shown and the rest as
    underscores with spaces kept like __a__b__c d_ e__"""
    positions = [i for i, ch in enumerate(name) if not ch.isspace()]
    if not positions:
        return name
    count = max(1, int(len(positions) * fraction))  # floored, but always at least one
    revealed = set(random.sample(positions, min(count, len(positions))))
    return "".join(
        ch if (ch.isspace() or i in revealed) else "_" for i, ch in enumerate(name)
    )


# players must let at least this fraction of the round elapse before they can give up
# music rounds are 5 minutes so they use a smaller fraction 1/15 which is 20s
GIVEUP_FRACTION = 1 / 3
MUSIC_GIVEUP_FRACTION = 1 / 15


def _giveup_seconds(mode: str) -> float:
    frac = MUSIC_GIVEUP_FRACTION if mode == "music" else GIVEUP_FRACTION
    return MODE_TIME.get(mode, GUESS_TIME) * frac


# a round whose build never finished from a cancelled task or hung fetch sits with startTime
# None forever holding its image bytes and locking the channel so reap it once it's this stale
PENDING_ROUND_TIMEOUT = 300
_ASSET_ATTEMPTS = 5

# a bare number is matched as a song id which collides with songs whose actual title is that
# number, so when a guess of the plain number lands on the id-sharing song nudge the guesser
# toward the one they probably meant
# keyed by (typed text, matched song id) to (intended song name, how to type it)
# mirrors old sbotga's hardcoded list
_ID_COLLISION_HINTS: dict[tuple[str, int], tuple[str, str]] = {
    ("88", 88): ("88☆彡", "88s or 224"),
    ("1", 1): ("「１」", "[1] or 132"),
}


def _id_collision_hint(content: str, music: Any) -> str:
    entry = _ID_COLLISION_HINTS.get((content.strip(), music.id))
    if not entry:
        return ""
    intended_name, suggestion = entry
    return (
        f"\n-# Did you mean to guess **{intended_name}**? `{content.strip()}` is the ID for "
        f"**{music.title}**, so use `{suggestion}` to guess **{intended_name}**."
    )


def _egg_block(descriptions: list[str]) -> str:
    """the warning banner appended under the chart prompt, one bullet per triggered egg"""
    if not descriptions:
        return ""
    return "\n\n**⚠️ EASTER EGG! ⚠️**\n" + "\n".join(f"- {d}" for d in descriptions)


_CHART_CLIP_ATTEMPTS = 3  # capped lower since each attempt may render a video
_BAD_FILENAME_CHARS = set('\\/:*?"<>|')


def _safe_filename(name: str) -> str:
    """a song title trimmed to something safe for an upload filename"""
    cleaned = "".join(
        c for c in name if c not in _BAD_FILENAME_CHARS and c >= " "
    ).strip()
    return cleaned[:100] or "song"


async def _fetch_bytes(url: str) -> bytes | None:
    async with aiohttp.ClientSession() as cs:
        async with cs.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()


class GuessCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot
        self.bot.cache.guess_channels = {}
        chart_clip.cleanup_stale()
        self.check_guess_task.start()
        self.finalize_prizes_task.start()

    async def cog_load(self) -> None:
        # warm the render server then keep the clip cache topped up in the background
        async def _boot() -> None:
            chart_preview.cleanup_orphans()  # kill renderer processes a prior crash leaked
            await chart_preview.start()
            chart_cache.cleanup_invalid()  # drop entries a prior crash left half-generated
            chart_cache.start(self.bot.pjsk)

        asyncio.create_task(_boot())

    async def cog_unload(self) -> None:
        self.check_guess_task.cancel()
        self.finalize_prizes_task.cancel()
        await chart_cache.stop()
        await chart_preview.stop()

    # --- random pickers ---

    def _random_song(self, has_append: bool = False, needs_master: bool = False):
        def has(music, difficulty: str) -> bool:
            return any(d.difficulty == difficulty for d in music.difficulties)

        musics = [
            m
            for m in self.bot.pjsk.released_musics()  # type: ignore[union-attr]
            if (not has_append or has(m, "append"))
            and (not needs_master or has(m, "master"))
        ]
        return random.choice(musics) if musics else None

    def _random_card(self):
        now = int(time.time() * 1000)
        cards = [
            c
            for c in self.bot.pjsk.cards()  # type: ignore[union-attr]
            if c.card_rarity_type in ("rarity_3", "rarity_4", "rarity_birthday")
            and (c.release_at or 0) <= now
            and (c.card_url_normal or c.card_url_trained)
        ]
        return random.choice(cards) if cards else None

    def _random_event(self):
        now = int(time.time() * 1000)
        events = [
            e
            for e in self.bot.pjsk.events()  # type: ignore[union-attr]
            if (e.start_at or 0) <= now and (e.background_url or e.banner_url)
        ]
        return random.choice(events) if events else None

    # --- pickers that reroll past a missing asset ---
    # some entries are permanently unrenderable like unmirrored jackets or cards without trained art

    async def _pick_song_jacket(self):
        for _ in range(_ASSET_ATTEMPTS):
            music = self._random_song()
            if not music:
                return None
            jacket = await _fetch_bytes(music.jacket_url)
            if jacket:
                return music, jacket
        return None

    async def _pick_chart_image(self, mode: str):
        # fallback when the clip renderer isn't installed using the old cropped-chart round
        difficulty = chart_clip.DIFFICULTIES[mode]
        for _ in range(_ASSET_ATTEMPTS):
            music = chart_clip.weighted_chart_music(self.bot.pjsk.released_musics(), difficulty)  # type: ignore[union-attr]
            if not music:
                return None
            region = next((r for r in self.bot.pjsk.regions_for_music(music.id) if r in ("en", "jp")), "en")  # type: ignore[union-attr]
            try:
                png = await self.bot.sbuga.get_chart_image(music.id, difficulty, region, mirrored=False)  # type: ignore[union-attr,arg-type]
            except SbugaError:
                continue
            return music, png, difficulty
        return None

    async def _fetch_chart_sus(
        self, music_id: int, difficulty: str, region: str
    ) -> str | None:
        url = self.bot.pjsk.chart_source_url(music_id, difficulty, region)  # type: ignore[union-attr]
        raw = await _fetch_bytes(url)
        return raw.decode("utf-8", "replace") if raw else None

    async def _chart_reveal_png(self, music_id: int, difficulty: str) -> bytes | None:
        region = next((r for r in self.bot.pjsk.regions_for_music(music_id) if r in ("en", "jp")), "en")  # type: ignore[union-attr]
        try:
            return await self.bot.sbuga.get_chart_image(music_id, difficulty, region, mirrored=False)  # type: ignore[union-attr,arg-type]
        except SbugaError:
            return None

    async def _pick_chart_clip(self, mode: str):
        """returns music, clip mp4, reveal png, difficulty, answer video or none, egg descriptions
        mode is one of chart chart_append or chart_expert"""
        # grab a pre-rendered higher quality clip instantly if one is cached
        cached = chart_cache.pop(mode)
        if cached:
            clip, answer, meta = cached
            music = self.bot.pjsk.get_music(meta["music_id"])  # type: ignore[union-attr]
            if music:
                png = await self._chart_reveal_png(music.id, meta["diff"])
                return music, clip, png, meta["diff"], answer, meta.get("eggs", [])
        # nothing cached so render on the fly smaller and faster with live-priority
        # on-the-fly has no answer video since the audio must not leak during the round
        with chart_cache.live_priority():
            return await self._render_chart_clip_live(mode)

    async def _render_chart_clip_live(self, mode: str):
        difficulty = chart_clip.DIFFICULTIES[mode]
        for _ in range(_CHART_CLIP_ATTEMPTS):
            music = chart_clip.weighted_chart_music(self.bot.pjsk.released_musics(), difficulty)  # type: ignore[union-attr]
            if not music:
                return None
            region = next((r for r in self.bot.pjsk.regions_for_music(music.id) if r in ("en", "jp")), "en")  # type: ignore[union-attr]
            sus_text = await self._fetch_chart_sus(music.id, difficulty, region)
            if not sus_text:
                continue
            try:
                result = await chart_clip.render_clip(
                    sus_text,
                    height=chart_clip.LIVE_HEIGHT,
                    fps=chart_clip.LIVE_FPS,
                )
            except chart_clip.ChartClipError as exc:
                # a render failure is a renderer problem not a chart one so bail and let the
                # caller fall back to the cropped image instead of retrying identically
                self.bot.warn(
                    f"chart clip render failed ({music.id} {difficulty}): {exc}"
                )
                return None
            if result is None:
                continue  # no usable window in this chart
            clip, eggs = result
            png = await self._chart_reveal_png(music.id, difficulty)
            return music, clip, png, difficulty, None, eggs
        return None

    async def _pick_music(self):
        """returns music, full song audio, nosil url, window start, jacket, cover type or none
        rerolls past songs with no nosil audio or too short to place a window and returns none
        if nothing is usable
        the audio is only used to cut the small stage clips and isn't kept on the round
        """
        for _ in range(_ASSET_ATTEMPTS):
            music = self._random_song()
            if not music:
                return None
            if music.id in MUSIC_GUESS_EXCLUDED:
                continue
            picked = song_clip.pick_nosil(music)
            if not picked:
                continue
            url, cover_type = picked
            audio = await _fetch_bytes(url)
            if not audio:
                continue
            start = await song_clip.choose_window(audio)
            if start is None:
                continue  # too short to place a window
            jacket = await _fetch_bytes(music.jacket_url) if music.jacket_url else None
            return music, audio, url, start, jacket, cover_type
        return None

    async def _gen_music_clips(self, data: dict, audio: bytes, start: float) -> None:
        """cut the longer stage clips in the background while the player listens to stage 1
        so only a few kb of clips are held rather than the whole song"""
        for stage in range(2, song_clip.MAX_STAGE + 1):
            try:
                data["clips"][stage] = await song_clip.stage_clip(audio, start, stage)
            except song_clip.SongClipError:
                pass

    async def _pick_card_art(self):
        for _ in range(_ASSET_ATTEMPTS):
            card = self._random_card()
            if not card:
                return None
            trained = card.card_rarity_type != "rarity_birthday" and bool(
                random.randint(0, 1)
            )
            if trained and not card.card_url_trained:
                trained = (
                    False  # no trained art so don't claim it in the reveal or hint
                )
            url = card.card_url_trained if trained else card.card_url_normal
            art = await _fetch_bytes(url) if url else None
            if art:
                return card, trained, art
        return None

    async def _pick_event_background(self):
        for _ in range(_ASSET_ATTEMPTS):
            event = self._random_event()
            if not event:
                return None
            url = event.background_url or event.banner_url
            background = await _fetch_bytes(url) if url else None
            if background:
                return event, background
        return None

    async def _pick_event_story(self):
        """(event, background | None, dialogue lines) for a random english event with a story,
        rerolling past events with no usable dialogue. None if nothing is usable."""
        try:
            eligible = await event_story.eligible_event_ids(self.bot.sbuga)  # type: ignore[arg-type]
        except SbugaError:
            return None
        # only released events we can actually name and match a guess against
        ids = [
            eid
            for eid in eligible
            if self.bot.pjsk.get_event(eid)  # type: ignore[union-attr]
            and not self.bot.pjsk.is_event_leaked(eid)  # type: ignore[union-attr]
        ]
        random.shuffle(ids)
        for eid in ids[:_ASSET_ATTEMPTS]:
            lines = await event_story.pick_snippet(self.bot.sbuga, eid, random)  # type: ignore[arg-type]
            if not lines:
                continue
            event = self.bot.pjsk.get_event(eid)  # type: ignore[union-attr]
            url = event.background_url or event.banner_url
            background = await _fetch_bytes(url) if url else None
            return event, background, lines
        return None

    # --- helpers ---

    @staticmethod
    def remove_guess(bot: SbugaBot, channel_id: int) -> None:
        bot.cache.guess_channels.pop(channel_id, None)

    @staticmethod
    def guess_ended(bot: SbugaBot, data: dict) -> bool:
        return (
            bot.cache.guess_channels.get(data["channel"].id, {}).get("id") != data["id"]
        )

    async def _in_support_server(self, user_id: int) -> bool:
        guild = self.bot.get_guild(self.bot.config["discord"].get("support_id", 0))
        if not guild:
            return False
        if guild.get_member(user_id):
            return True
        try:
            await guild.fetch_member(user_id)
            return True
        except discord.HTTPException:
            return False

    async def channel_checks(
        self, interaction: discord.Interaction, already_guessing_check: bool = True
    ) -> bool:
        if not interaction.channel:
            await interaction.followup.send(
                embed=embeds.error_embed("I couldn't get this channel.")
            )
            return False
        # guesses are read from chat so the bot must actually be able to see this
        # channel, user installs can run commands in places it can't
        if interaction.guild is not None:
            # is_guild_integration() only proves the app is installed here, an
            # applications.commands-only install has no bot member so on_message
            # never fires and guild.me is none in that case and on the partial guild
            # a user install falls back to
            me = interaction.guild.me if interaction.is_guild_integration() else None
            if me is None:
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        "I need to be **in this server** to read guesses. "
                        "Add me to the server, or play in my DMs."
                    )
                )
                return False
            perms = interaction.channel.permissions_for(me)  # type: ignore[union-attr]
            missing = [
                name
                for name, allowed in (
                    ("View Channel", perms.view_channel),
                    # threads gate posting behind a different flag than plain channels
                    (
                        "Send Messages",
                        (
                            perms.send_messages_in_threads
                            if isinstance(interaction.channel, discord.Thread)
                            else perms.send_messages
                        ),
                    ),
                    ("Embed Links", perms.embed_links),
                    ("Attach Files", perms.attach_files),
                )
                if not allowed
            ]
            if missing:
                listed = "\n".join(f"- `{name}`" for name in missing)
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        "I don't have access to this channel. Check my permissions "
                        f"and try again.\n\n**Permissions Required**\n{listed}"
                    )
                )
                return False
        elif interaction.context.dm_channel:
            if not await self._in_support_server(interaction.user.id):
                support = self.bot.config["discord"].get("support_invite", "")
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        "Guessing in DMs is only available to support server "
                        + (f"members. Join here: {support}" if support else "members.")
                    )
                )
                return False
        else:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "I can't read messages here. Play in a server I'm in, or in my DMs."
                )
            )
            return False
        if (
            already_guessing_check
            and interaction.channel.id in self.bot.cache.guess_channels
        ):
            await interaction.followup.send(
                embed=embeds.error_embed("A guessing game is already happening here!")
            )
            return False
        if interaction.guild and not await self.bot.user_data.guessing_enabled(interaction.guild_id):  # type: ignore[union-attr,arg-type]
            await interaction.followup.send(
                embed=embeds.error_embed("Guessing is disabled in this server.")
            )
            return False
        return True

    async def _reveal_files(self, data: dict) -> tuple[list[discord.File], dict]:
        files: list[discord.File] = []
        kwargs: dict[str, Any] = {}
        if data["data"].get("thumbnail"):
            data["data"]["thumbnail"].seek(0)
            files.append(discord.File(data["data"]["thumbnail"], "thumb.png"))
            kwargs["thumb"] = True
        if data["answer_file_path"]:
            data["answer_file_path"].seek(0)
            files.append(discord.File(data["answer_file_path"], "image.png"))
            kwargs["image"] = True
        if data["data"].get("answer_video"):
            files.append(
                discord.File(io.BytesIO(data["data"]["answer_video"]), "answer.mp4")
            )
            kwargs["video"] = True
        return files, kwargs

    # --- timeout loop ---

    @tasks.loop(seconds=2)
    async def check_guess_task(self) -> None:
        now = time.time()
        for channel_id, data in list(self.bot.cache.guess_channels.items()):
            if not data["startTime"]:
                # never started from a cancelled build or hung fetch so reap once clearly orphaned
                if now - data.get("createdAt", now) > PENDING_ROUND_TIMEOUT:
                    self.remove_guess(self.bot, channel_id)
                continue
            max_time = MODE_TIME.get(data["guessing"], GUESS_TIME)
            if data["startTime"] + max_time >= now:
                continue
            if self.guess_ended(self.bot, data):
                continue
            self.remove_guess(self.bot, channel_id)
            try:
                embed = embeds.embed(
                    title="Failed",
                    description=f"You failed to guess the {data['guessType']}.",
                    color=discord.Color.red(),
                )
                answer = f"The correct answer was **{data['answerName']}**."
                if data["data"].get("notes"):
                    answer += f"\n\n**This song has `{data['data']['notes']}` notes on Master.**"
                if data["guessType"] == "character" and data["data"].get("card_name"):
                    answer += f"\n**Card:** {data['data']['card_name']}"
                embed.add_field(name="Answer", value=answer, inline=False)
                files, flags = await self._reveal_files(data)
                if flags.get("thumb"):
                    embed.set_thumbnail(url="attachment://thumb.png")
                if flags.get("image"):
                    embed.set_image(url="attachment://image.png")
                view = _GuessResultView(self, data)
                view.message = await data["channel"].send(
                    embed=embed, files=files, view=view
                )
            except discord.HTTPException:
                pass

    @check_guess_task.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # --- prize finalization (award + DM winners once a period ends) ---

    @tasks.loop(minutes=20)
    async def finalize_prizes_task(self) -> None:
        if not _prizes_enabled():
            return
        for period_type, current in (
            ("weekly", periods.week_index()),
            ("monthly", periods.month_index()),
        ):
            last = await self.bot.user_data.last_finalized_index(period_type)  # type: ignore[union-attr]
            # award every ended-but-unfinalized period (index >= 1 skips the pre-launch bucket)
            for index in range(max(1, last + 1), current):
                await self._finalize_period(period_type, index)

    @finalize_prizes_task.before_loop
    async def _before_finalize(self) -> None:
        await self.bot.wait_until_ready()

    async def _finalize_period(self, period_type: str, index: int) -> None:
        prize_map = WEEKLY_PRIZES if period_type == "weekly" else MONTHLY_PRIZES
        top = await self.bot.user_data.get_points_top(period_type, index, max(prize_map))  # type: ignore[union-attr]
        winners = [
            (row["discord_id"], rank, *prize_map[rank])
            for rank, row in enumerate(top, start=1)
            if rank in prize_map
        ]
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=PRIZE_CLAIM_DAYS
        )
        created = await self.bot.user_data.finalize_period(period_type, index, winners, expires_at)  # type: ignore[union-attr]
        for prize in created:
            await self._dm_prize(prize)

    async def _dm_prize(self, prize: dict) -> None:
        try:
            user = self.bot.get_user(prize["discord_id"]) or await self.bot.fetch_user(
                prize["discord_id"]
            )
        except discord.HTTPException:
            return
        embed = embeds.embed(title="🏆 You Won a Prize!", color=discord.Color.gold())
        embed.description = (
            f"You ranked **#{prize['rank']}** on **{_prize_label(prize)}**'s guessing "
            f"leaderboard!\n\n**Prize:** {_prize_reward(prize)}\n\n"
            f"Claim it on the **Global PJSK server** within **{PRIZE_CLAIM_DAYS} days** "
            f"(expires <t:{int(prize['expires_at'].timestamp())}:R>) - run `/guess prize` there."
        )
        try:
            await user.send(embed=embed, view=_PrizeClaimView(self, [prize]))
        except discord.HTTPException:
            pass  # DMs closed; still claimable via /guess prize

    # --- guess checking ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        data = self.bot.cache.guess_channels.get(message.channel.id)
        if not data or not data.get("startTime"):
            return
        content = message.content.strip()
        if not content.startswith(GUESS_PREFIX):
            return
        content = content[len(GUESS_PREFIX) :].strip()
        if not content:
            return

        # -hint -end or -time in any case or spacing run those instead of guessing
        command = "".join(content.lower().split())
        if command == "hint":
            await self._chat_hint(message, data)
            return
        if command == "end":
            await self._chat_end(message, data)
            return
        if command == "time":
            await message.reply(embed=self._time_embed(data))
            return

        # a real guess attempt so record the guesser and let them -end
        data.setdefault("guessers", set()).add(message.author.id)
        # count this attempt toward the round's shared cap and this user's rate limits
        data["data"]["attempts"] = data["data"].get("attempts", 0) + 1
        data["_rate"] = await self.bot.user_data.record_guess_attempt(message.author.id)  # type: ignore[union-attr]
        if data["guessType"] == "song":
            await self._check_song(message, data, content)
        elif data["guessType"] == "character":
            await self._check_character(message, data, content)
        elif data["guessType"] == "event":
            await self._check_event(message, data, content)

    async def _award_points(self, user_id: int, data: dict) -> "int | None":
        """award leaderboard points for a correct guess, respecting opt-out. returns the points
        earned, or None when the mode isn't ranked or the user opted out"""
        mode = data["guessing"]
        points = _guess_points(mode, _hints_taken(mode, data["data"]))
        if points is None:
            return None
        if await self.bot.user_data.get_settings(user_id, "opt_out_rolling_guess_leaderboards"):  # type: ignore[union-attr]
            return None
        hour_count, day_count = data.get("_rate", (0, 0))
        if _over_guess_limit(data["data"].get("attempts", 0), hour_count, day_count):
            return 0  # too many guesses this round/hour/day - no points
        await self.bot.user_data.add_guess_points(  # type: ignore[union-attr]
            user_id, mode, points, periods.week_index(), periods.month_index()
        )
        return points

    async def _award(self, message: discord.Message, data: dict) -> discord.Embed:
        self.remove_guess(self.bot, message.channel.id)
        started = data.get("startTime")
        timing = f" in `{time.time() - started:.2f}` seconds" if started else ""
        description = f"Successfully guessed **`{data['answerName']}`**{timing}!"
        if data["data"].get("notes"):
            description += (
                f"\n\n### This song has `{data['data']['notes']}` notes on Master."
            )
        if data["guessType"] == "character" and data["data"].get("card_name"):
            description += f"\n**Card:** {data['data']['card_name']}"
        points = None
        if data["guessing"]:
            await self.bot.user_data.add_guesses(message.author.id, data["guessing"], "success")  # type: ignore[union-attr]
            points = await self._award_points(message.author.id, data)
        if points:
            description += (
                f"\n\n**+`{points:,}`** leaderboard points!"
                "\n-# Use /guess weekly and /guess monthly to see your ranking!"
            )
        elif points == 0:
            description += "\n\n-# No leaderboard points - guess limit reached."
        embed = embeds.success_embed(title="Correct", description=description)
        files, flags = await self._reveal_files(data)
        if flags.get("thumb"):
            embed.set_thumbnail(url="attachment://thumb.png")
        if flags.get("image"):
            embed.set_image(url="attachment://image.png")
        view = _GuessResultView(self, data)
        view.message = await message.reply(embed=embed, files=files, view=view)
        return embed

    async def _record_fail(self, message: discord.Message, data: dict) -> None:
        if data["guessing"]:
            await self.bot.user_data.add_guesses(message.author.id, data["guessing"], "fail")  # type: ignore[union-attr]

    async def _check_song(
        self, message: discord.Message, data: dict, content: str
    ) -> None:
        hit = converters.match_song_with_key(self.bot.pjsk, content)  # type: ignore[arg-type]
        if self.guess_ended(self.bot, data):
            return
        tip = MUSIC_TIP if data["guessing"] == "music" else GUESS_TIP
        if not hit:
            await message.reply(
                embed=embeds.error_embed(
                    f"Couldn't find a song matching `{content}`." + tip,
                    title="Incorrect",
                )
            )
            return
        music, key = hit
        if songs_equivalent(music.id, data["answer"]):
            await self._award(message, data)
        elif self.bot.pjsk.is_music_leaked(music.id) and not data.get("allow_leaks"):
            # a leak is never the answer and we don't reveal its title where leaks are off, so
            # treat it like nothing matched, with the mikuleek emoji as a quiet tell that we
            # actually did match a leak
            await message.reply(
                embed=embeds.error_embed(
                    f"{emojis.mikuleek} Couldn't find a song matching `{content}`."
                    + tip,
                    title="Incorrect",
                )
            )
        else:
            await message.reply(
                embed=embeds.error_embed(
                    f"Incorrectly guessed {converters.describe_song_match(music.title, key)}."
                    + _id_collision_hint(content, music)
                    + tip,
                    title="Incorrect",
                )
            )
            await self._record_fail(message, data)

    async def _check_character(
        self, message: discord.Message, data: dict, content: str
    ) -> None:
        char = converters.match_character(self.bot.pjsk, content)  # type: ignore[arg-type]
        if self.guess_ended(self.bot, data):
            return
        if not char:
            await message.reply(
                embed=embeds.error_embed(
                    f"Couldn't find a character matching `{content}`." + GUESS_TIP,
                    title="Incorrect",
                )
            )
            return
        if char.id == data["answer"]:
            await self._award(message, data)
        else:
            await message.reply(
                embed=embeds.error_embed(
                    f"Incorrectly guessed **`{character_display_name(char)}`**."
                    + GUESS_TIP,
                    title="Incorrect",
                )
            )
            await self._record_fail(message, data)

    async def _check_event(
        self, message: discord.Message, data: dict, content: str
    ) -> None:
        hit = converters.match_event_with_key(self.bot.pjsk, content)  # type: ignore[arg-type]
        if self.guess_ended(self.bot, data):
            return
        if not hit:
            await message.reply(
                embed=embeds.error_embed(
                    f"Couldn't find an event matching `{content}`."
                    + GUESS_TIP
                    + EVENT_ALIAS_PLUG,
                    title="Incorrect",
                )
            )
            return
        event, key = hit
        if event.id == data["answer"]:
            await self._award(message, data)
        else:
            await message.reply(
                embed=embeds.error_embed(
                    f"Incorrectly guessed {converters.describe_event_match(event.name, key)}."
                    + GUESS_TIP
                    + EVENT_ALIAS_PLUG,
                    title="Incorrect",
                )
            )
            await self._record_fail(message, data)

    # --- game start ---

    async def handle_guess(self, interaction: discord.Interaction, mode: str) -> None:
        try:
            await interaction.response.defer(thinking=True)
        except discord.InteractionResponded:
            pass
        if not await self.channel_checks(interaction):
            return

        # cached once per round so guess checking doesn't hit the db each guess; off unless this
        # channel is a leak channel, and then a guess that resolves to a leak is hidden so we
        # never reveal an unreleased title
        allow_leaks = await self.bot.user_data.channel_leaks_allowed(interaction.channel_id)  # type: ignore[union-attr,arg-type]
        guess: dict[str, Any] = {
            "guessers": set(),  # user ids who've made a guess and are needed to give up
            "host": interaction.user.id,  # the starter can give up without guessing
            "allow_leaks": allow_leaks,
            "channel": interaction.channel,
            "id": tools.generate_secure_string(25),
            "guessType": None,
            "guessing": mode,
            "answer_file_path": None,
            "answer": None,
            "answerName": None,
            "startTime": None,
            "createdAt": time.time(),
            "data": {},
        }
        self.bot.cache.guess_channels[interaction.channel.id] = guess  # type: ignore[union-attr]
        # a chart round with no cached clip renders one on the fly over several seconds so show
        # a placeholder to visibly lock the channel then edit it in
        # startTime only begins after the edit so the timer and guessing wait for the chart
        is_chart = mode in ("chart", "chart_append", "chart_expert")
        rendering_live = is_chart and chart_cache.count(mode) == 0
        try:
            if rendering_live:
                await interaction.edit_original_response(
                    embed=embeds.embed(
                        title="Guess The Chart",
                        description="Rendering the chart clip… (~10 seconds)",
                        color=discord.Color.blue(),
                    )
                )
            embed, file = await self._build_round(interaction, mode, guess)
            if embed is None:
                self.remove_guess(self.bot, interaction.channel.id)  # type: ignore[union-attr]
                err = embeds.error_embed(
                    "That guessing mode isn't available yet (missing data)."
                )
                if is_chart:
                    await interaction.edit_original_response(embed=err, attachments=[])
                else:
                    await interaction.followup.send(embed=err)
                return
            if (
                embed.description
            ):  # note when giving up unlocks and it also needs at least one hint used
                embed.description = (
                    f"-# You can give up in `{int(_giveup_seconds(mode))}` seconds "
                    "(after using a hint).\n" + embed.description
                )
            if is_chart:
                await interaction.edit_original_response(
                    embed=embed, attachments=[file] if file else []
                )
            else:
                await interaction.followup.send(
                    embed=embed, file=file or discord.utils.MISSING
                )
            guess["startTime"] = time.time()
        except Exception:
            self.remove_guess(self.bot, interaction.channel.id)  # type: ignore[union-attr]
            if is_chart:
                try:
                    await interaction.edit_original_response(
                        embed=embeds.error_embed(
                            "Something went wrong preparing the chart."
                        ),
                        attachments=[],
                    )
                except discord.HTTPException:
                    pass
            raise

    async def _build_round(
        self, interaction: discord.Interaction, mode: str, guess: dict
    ):
        secs = MODE_TIME.get(mode, GUESS_TIME)

        if mode == "music":
            guess["guessType"] = "song"
            hit = await self._pick_music()
            if not hit:
                return None, None
            music, audio, url, start, jacket, cover_type = hit
            guess["answer"] = music.id
            guess["answerName"] = music.title
            if jacket:
                guess["answer_file_path"] = io.BytesIO(jacket)
            data = guess["data"]
            # the url isn't kept: the stage clips are pre-cut here and the reveal is just jacket
            data["start"] = start
            data["stage"] = 1
            data["last_hint"] = 0.0
            data["cover_type"] = cover_type  # revealed on the final hint
            clip1 = await song_clip.stage_clip(audio, start, 1)
            data["clips"] = {1: clip1}
            # cut the longer stages in the background while they listen to stage 1
            data["clip_task"] = asyncio.create_task(
                self._gen_music_clips(data, audio, start)
            )
            embed = embeds.embed(title="Guess The Music", color=discord.Color.blue())
            embed.description = (
                f"Guess the song from a {int(song_clip.STAGE_SECONDS[1])} second clip.\n"
                f"Use `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` to hear more "
                f"of the song, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
            )
            return embed, discord.File(io.BytesIO(clip1), song_clip.clip_filename(1))

        if mode in (
            "jacket",
            "jacket_30px",
            "jacket_bw",
            "jacket_challenge",
            "notes",
            "chart",
            "chart_append",
            "chart_expert",
        ):
            guess["guessType"] = "song"

            if mode == "notes":
                # three append-only entries carry no master chart
                music = self._random_song(needs_master=True)
                if not music:
                    return None, None
                guess["answer"] = music.id
                guess["answerName"] = music.title
                master = next(d for d in music.difficulties if d.difficulty == "master")
                guess["data"]["notes"] = master.total_note_count
                jacket = await _fetch_bytes(music.jacket_url)
                if jacket:
                    guess["data"]["thumbnail"] = io.BytesIO(jacket)
                embed = embeds.embed(title="Guess The Song", color=discord.Color.blue())
                embed.description = (
                    f"Guess the song from its Master note count.\nUse `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds.\n\n"
                    f"# This song has `{master.total_note_count}` notes on Master."
                )
                return embed, None

            if mode in ("chart", "chart_append", "chart_expert"):
                clip_hit = (
                    await self._pick_chart_clip(mode)
                    if chart_preview.available()
                    else None
                )
                if clip_hit:
                    music, clip, png, diff, answer_video, eggs = clip_hit
                    guess["answer"] = music.id
                    guess["answerName"] = music.title
                    if png:  # reveal via -end still shows the full chart
                        guess["answer_file_path"] = io.BytesIO(png)
                    if (
                        answer_video
                    ):  # cached clips also reveal a jacket and audio video
                        guess["data"]["answer_video"] = answer_video
                    embed = embeds.embed(
                        title="Guess The Chart", color=discord.Color.blue()
                    )
                    embed.description = (
                        f"Guess the song from a ~10 second {diff} chart clip.\n"
                        f"Use `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
                        + _egg_block(eggs)
                    )
                    return embed, discord.File(io.BytesIO(clip), "chart.mp4")

                # renderer missing or broken or no clip available so use the cropped chart image
                hit = await self._pick_chart_image(mode)
                if not hit:
                    return None, None
                music, png, diff = hit
                guess["answer"] = music.id
                guess["answerName"] = music.title
                guess["answer_file_path"] = io.BytesIO(png)
                cropped = await unblock.to_process_with_timeout(_crop_chart, png)
                embed = embeds.embed(
                    title="Guess The Chart", color=discord.Color.blue()
                )
                embed.set_image(url="attachment://image.png")
                embed.description = (
                    f"Guess the song from a cropped {diff} chart.\n"
                    f"Use `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
                )
                return embed, discord.File(cropped, "image.png")

            hit = await self._pick_song_jacket()
            if not hit:
                return None, None
            music, jacket = hit
            guess["answer"] = music.id
            guess["answerName"] = music.title
            guess["answer_file_path"] = io.BytesIO(jacket)
            size, bw = 140, False
            label = "Guess the song from a cropped jacket."
            if mode == "jacket_30px":
                size, label = (
                    30,
                    "**30px Jacket!** Guess the song from a cropped jacket.",
                )
            elif mode == "jacket_bw":
                bw, label = (
                    True,
                    "**Grayscale Jacket!** Guess the song from a cropped jacket.",
                )
            elif mode == "jacket_challenge":
                size, bw, label = (
                    30,
                    True,
                    "**CHALLENGE!** Cropped grayscale 30px jacket.",
                )
            cropped = await unblock.to_process_with_timeout(
                _crop_square, jacket, size, bw
            )
            embed = embeds.embed(title="Guess The Song", color=discord.Color.blue())
            embed.set_image(url="attachment://image.png")
            embed.description = f"{label}\nUse `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
            return embed, discord.File(cropped, "image.png")

        if mode in ("character", "character_bw"):
            guess["guessType"] = "character"
            hit = await self._pick_card_art()
            if not hit:
                return None, None
            card, trained, art = hit
            char = self.bot.pjsk.get_character(card.character_id)  # type: ignore[union-attr]
            if not char:
                return None, None
            guess["answer"] = char.id
            guess["answerName"] = character_display_name(char)
            guess["answer_file_path"] = io.BytesIO(art)
            guess["data"]["card_name"] = self.bot.pjsk.card_display_name(card, use_emojis=True, trained=trained)  # type: ignore[union-attr]
            guess["data"]["card_id"] = card.id
            guess["data"]["trained"] = trained
            guess["data"]["rarity"] = card.card_rarity_type
            guess["data"]["attr"] = card.attr
            cropped = await unblock.to_process_with_timeout(
                _crop_square, art, 250, mode == "character_bw"
            )
            embed = embeds.embed(
                title="Guess The Character", color=discord.Color.blue()
            )
            embed.set_image(url="attachment://image.png")
            embed.description = f"Guess the character from a cropped card.\nUse `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
            return embed, discord.File(cropped, "image.png")

        if mode == "event_background":
            guess["guessType"] = "event"
            hit = await self._pick_event_background()
            if not hit:
                return None, None
            event, bg = hit
            guess["answer"] = event.id
            guess["answerName"] = event.name
            guess["answer_file_path"] = io.BytesIO(bg)
            cropped = await unblock.to_process_with_timeout(
                _crop_square, bg, 250, False
            )
            embed = embeds.embed(title="Guess The Event", color=discord.Color.blue())
            embed.set_image(url="attachment://image.png")
            embed.description = f"Guess the event from a cropped background.\nUse `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
            return embed, discord.File(cropped, "image.png")

        if mode == "event":
            guess["guessType"] = "event"
            hit = await self._pick_event_story()
            if not hit:
                return None, None
            event, bg, lines = hit
            guess["answer"] = event.id
            guess["answerName"] = event.name
            if bg:  # shown on the reveal
                guess["answer_file_path"] = io.BytesIO(bg)
            data = guess["data"]
            data["lines"] = lines
            data["stage"] = 1
            data["last_hint"] = 0.0
            # the last hints reveal these alongside/instead of more dialogue
            data["event_type"] = event_story.type_display(event.event_type)
            data["event_attribute"] = (
                event.bonus_attribute
            )  # raw, for the attribute emoji
            data["event_unit"] = await event_story.unit_display(self.bot.sbuga, event.id)  # type: ignore[arg-type]
            desc = await event_story.event_outline(self.bot.sbuga, event.id)  # type: ignore[arg-type]
            # only a third of the description is revealed, the rest masked
            data["event_desc"] = _masked_name(desc, EVENT_DESC_FRACTION) if desc else ""
            snippet = "\n".join(lines[: event_story.STAGE_LINES[1]])
            embed = embeds.embed(
                title="Guess The Event Story", color=discord.Color.blue()
            )
            embed.description = (
                f"Which event is this dialogue from?\n\n{snippet}\n\n"
                f"Use `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for more "
                f"lines, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. "
                f"You have {secs} seconds."
            )
            return embed, None

        return None, None

    # --- commands ---

    guess = app_commands.Group(
        name="guess",
        description="PJSK guessing games.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @guess.command(name="jacket", description="Guess the song from a cropped jacket.")
    async def jacket(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "jacket")

    @guess.command(
        name="jacket_30px", description="Guess the song from a tiny cropped jacket."
    )
    async def jacket_30px(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "jacket_30px")

    @guess.command(
        name="jacket_bw", description="Guess the song from a grayscale cropped jacket."
    )
    async def jacket_bw(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "jacket_bw")

    @guess.command(
        name="jacket_challenge", description="Hardest jacket guess: grayscale 30px."
    )
    async def jacket_challenge(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "jacket_challenge")

    @guess.command(
        name="character", description="Guess the character from a cropped card."
    )
    async def character(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "character")

    @guess.command(
        name="character_bw", description="Guess the character from a grayscale card."
    )
    async def character_bw(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "character_bw")

    @guess.command(name="chart", description="Guess the song from a Master chart clip.")
    async def chart(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "chart")

    @guess.command(
        name="chart_append", description="Guess the song from an Append chart clip."
    )
    async def chart_append(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "chart_append")

    @guess.command(
        name="chart_expert", description="Guess the song from an Expert chart clip."
    )
    async def chart_expert(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "chart_expert")

    @guess.command(
        name="event_background",
        description="Guess the event from a cropped background.",
    )
    async def event_background(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "event_background")

    @guess.command(
        name="notes", description="Guess the song from its Master note count."
    )
    async def notes(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "notes")

    @guess.command(name="music", description="Guess the song from a short audio clip.")
    async def music(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "music")

    @guess.command(
        name="event",
        description="Guess the event from a snippet of its story dialogue.",
    )
    async def event(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "event")

    async def _resolve_end(
        self, channel_id: int, user_id: int
    ) -> tuple[discord.Embed, list[discord.File], "_GuessResultView | None"]:
        """build the reply for ending a round
        a non-none view means the round was actually ended so attach it, a none view means it's
        an error reply like no active guess or too soon to give up"""
        data = self.bot.cache.guess_channels.get(channel_id)
        if not data:
            return embeds.error_embed("There's no active guess here."), [], None
        # the starter can give up without guessing, everyone else needs a guess first which
        # curbs drive-by trolling
        if user_id != data.get("host") and user_id not in data.get("guessers", ()):
            return (
                embeds.error_embed(
                    "You must make at least one guess before giving up."
                ),
                [],
                None,
            )
        # can't give up until enough time has passed and at least one hint has been used
        if data["guessing"] in STAGED_MODES:
            hint_used = data["data"].get("stage", 1) >= 2
        else:
            hint_used = data["data"].get("hint_stage", 0) >= 1
        started = data.get("startTime")
        remaining = 0.0
        if started:
            remaining = started + _giveup_seconds(data["guessing"]) - time.time()
        if not hint_used and remaining > 0:
            return (
                embeds.error_embed(
                    "Cannot end the guess until you use a hint and "
                    f"`{math.ceil(remaining)}` more seconds pass."
                ),
                [],
                None,
            )
        if not hint_used:
            return (
                embeds.error_embed("Cannot end the guess until you use a hint."),
                [],
                None,
            )
        if remaining > 0:
            return (
                embeds.error_embed(
                    f"Cannot end the guess for another `{math.ceil(remaining)}` seconds."
                ),
                [],
                None,
            )
        self.remove_guess(self.bot, channel_id)
        if data["guessing"]:
            await self.bot.user_data.add_guesses(user_id, data["guessing"], "ragequit")  # type: ignore[union-attr]
        embed = embeds.embed(
            title="Guess Ended",
            description=f"The answer was **{data['answerName']}**.",
            color=discord.Color.red(),
        )
        files, flags = await self._reveal_files(data)
        if flags.get("thumb"):
            embed.set_thumbnail(url="attachment://thumb.png")
        if flags.get("image"):
            embed.set_image(url="attachment://image.png")
        return embed, files, _GuessResultView(self, data)

    @guess.command(name="end", description="End the active guess in this channel.")
    async def end(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        embed, files, view = await self._resolve_end(interaction.channel.id, interaction.user.id)  # type: ignore[union-attr]
        msg = await interaction.followup.send(
            embed=embed, files=files, view=view or discord.utils.MISSING
        )
        if view:
            view.message = msg

    async def _chat_end(self, message: discord.Message, data: dict) -> None:
        embed, files, view = await self._resolve_end(
            message.channel.id, message.author.id
        )
        msg = await message.reply(
            embed=embed, files=files, view=view or discord.utils.MISSING
        )
        if view:
            view.message = msg

    def _time_embed(self, data: dict) -> discord.Embed:
        started = data.get("startTime")
        if not started:
            return embeds.embed(
                title="Time Remaining",
                description="The round is still starting.",
                color=discord.Color.blurple(),
            )
        end_ts = int(started + MODE_TIME.get(data["guessing"], GUESS_TIME))
        return embeds.embed(
            title="Time Remaining",
            description=f"This guess ends <t:{end_ts}:R>.",
            color=discord.Color.blurple(),
        )

    @guess.command(
        name="time", description="Show how long is left in the active guess."
    )
    async def time_left(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        data = self.bot.cache.guess_channels.get(interaction.channel.id)  # type: ignore[union-attr]
        if not data:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active guess here.")
            )
            return
        await interaction.followup.send(embed=self._time_embed(data))

    def _diff_level(self, song_id: int, difficulty: str = "master") -> int | None:
        # append-only entries carry no master chart so fall back to the song they're a copy of
        for candidate in (song_id, *sorted(equivalents_of(song_id))):
            music = self.bot.pjsk.get_music(candidate)  # type: ignore[union-attr]
            if not music:
                continue
            diff = next(
                (d for d in music.difficulties if d.difficulty == difficulty), None
            )
            if diff:
                return diff.play_level
        return None

    async def _music_clip(self, d: dict, stage: int) -> bytes | None:
        """the stage clip from the pre-generated cache, else wait for the background cutter
        (the url isn't kept, so a stage that failed to cut just returns None)"""
        clip = d["clips"].get(stage)
        if clip is not None:
            return clip
        task = d.get("clip_task")
        if task is not None:
            try:
                await task
            except Exception:
                pass
        return d["clips"].get(stage)

    async def _music_hint(
        self, data: dict
    ) -> tuple[discord.Embed, list[discord.File], bool]:
        """music hints reveal a longer prefix of the clip from stage 1 to 4 and are rate-limited.
        past the last stage a hint just repeats the full clip"""
        d = data["data"]
        stage = d.get("stage", 1)
        now = time.time()
        if now - d.get("last_hint", 0.0) < HINT_COOLDOWN:
            return (
                embeds.error_embed("Please wait a moment before the next hint."),
                [],
                False,
            )
        d["last_hint"] = now  # set before the cut so a rapid second hint is rejected
        if stage >= song_clip.MAX_STAGE:
            clip = await self._music_clip(d, song_clip.MAX_STAGE)
            if clip is None:
                return embeds.error_embed("Couldn't extend the clip."), [], False
            desc = f"Here's {int(song_clip.FULL_SECONDS)} seconds of the song."
            if d.get("cover_type"):
                desc += f"\nCover type: **{d['cover_type']}**"
            embed = embeds.embed(
                title=f"Guess Hint - Stage {song_clip.MAX_STAGE}/{song_clip.MAX_STAGE}",
                description=desc,
                color=discord.Color.yellow(),
            )
            return (
                embed,
                [
                    discord.File(
                        io.BytesIO(clip), song_clip.clip_filename(song_clip.MAX_STAGE)
                    )
                ],
                False,
            )
        stage += 1
        d["stage"] = stage
        clip = await self._music_clip(d, stage)
        if clip is None:
            return embeds.error_embed("Couldn't extend the clip."), [], False
        desc = f"Here's {int(song_clip.STAGE_SECONDS[stage])} seconds of the song."
        if stage >= song_clip.MAX_STAGE and d.get("cover_type"):
            desc += f"\nCover type: **{d['cover_type']}**"
        embed = embeds.embed(
            title=f"Guess Hint - Stage {stage}/{song_clip.MAX_STAGE}",
            description=desc,
            color=discord.Color.yellow(),
        )
        return (
            embed,
            [discord.File(io.BytesIO(clip), song_clip.clip_filename(stage))],
            True,
        )

    async def _story_hint(
        self, data: dict
    ) -> tuple[discord.Embed, list[discord.File], bool]:
        """event hints show the dialogue snippet grown to 7 then 10 lines (the 10-line hint also
        names the event type), then the bonus attribute and unit, then the event description. each
        hint shows everything revealed so far and is rate-limited; past the last stage it repeats
        """
        d = data["data"]
        stage = d.get("stage", 1)
        now = time.time()
        if now - d.get("last_hint", 0.0) < HINT_COOLDOWN:
            return (
                embeds.error_embed("Please wait a moment before the next hint."),
                [],
                False,
            )
        d["last_hint"] = now
        attr = d.get("event_attribute")
        attr_emoji = emojis.attributes.get(attr, "") if attr else ""
        attr_body = f"{attr_emoji} {event_story.attribute_display(attr)}".strip()
        type_line = f"**Event type:** {d.get('event_type', 'Unknown')}"
        attr_line = f"**Event attribute:** {attr_body}"
        unit_line = f"**Event unit:** {d.get('event_unit', 'Mixed')}"
        desc = d.get("event_desc") or "*No description.*"
        advanced = stage < event_story.MAX_STAGE
        if advanced:
            stage += 1
            d["stage"] = stage
        snippet = "\n".join(d["lines"][: event_story.lines_for_stage(stage)])
        if stage >= event_story.TYPE_STAGE:  # 10-line hint also names the type
            snippet += f"\n\n{type_line}"
        if stage >= event_story.FACTS_STAGE:  # then the attribute and unit
            snippet += f"\n{attr_line}\n{unit_line}"
        if stage >= event_story.DESC_STAGE:  # the last hint adds the description
            snippet += f"\n\n**Description:** {desc}"
        embed = embeds.embed(
            title=f"Guess Hint - Stage {stage}/{event_story.MAX_STAGE}",
            description=snippet,
            color=discord.Color.yellow(),
        )
        return embed, [], advanced

    async def _tier_lines(
        self, data: dict, stage: int
    ) -> tuple[list[str], list[discord.File]]:
        """the cumulative hint content for tiers 1 to stage of a non-music round
        the masked name is computed once and stored so it stays stable across re-hints
        """
        d = data["data"]
        name = str(data["answerName"])
        lines: list[str] = []
        files: list[discord.File] = []
        if data["guessType"] == "song":
            if stage >= 1:
                # chart modes hint their own difficulty and other song modes use master
                diff = chart_clip.DIFFICULTIES.get(data["guessing"], "master")
                level = self._diff_level(data["answer"], diff)
                if level is None:
                    lines.append(f"This song doesn't have a {diff.title()} chart.")
                else:
                    lines.append(
                        f"The song is level **`{level}`** on "
                        f"{emojis.difficulty_colors[diff]} **{diff.title()}** (on JP server)."
                    )
            if stage >= 2:
                lines.append(f"The name has **`{len(name)}`** characters.")
            if stage >= 3:
                d.setdefault("masked", _masked_name(name, SONG_REVEAL_FRACTION))
                lines.append(f"Name: `{d['masked']}`")
        elif data["guessType"] == "character":
            if stage >= 1:
                state = "trained" if d.get("trained") else "not trained"
                lines.append(f"This card is **`{state}`**.")
            if stage >= 2:
                rarity = RARITY_DISPLAY.get(d.get("rarity", ""), "?")
                lines.append(f"The rarity of this card is **{rarity}**.")
            if stage >= 3:
                attr = d.get("attr")
                icon = emojis.attributes.get(attr, "") if attr else ""
                lines.append(
                    f"The attribute of this card is {icon} **{(attr or 'unknown').title()}**."
                )
        elif data["guessType"] == "event":
            if stage >= 1:
                if "hint_art" not in d:
                    event = self.bot.pjsk.get_event(data["answer"])  # type: ignore[union-attr]
                    d["hint_art"] = (
                        await _fetch_bytes(event.character_url)
                        if event and event.character_url
                        else None
                    )
                if d["hint_art"]:
                    files.append(discord.File(io.BytesIO(d["hint_art"]), "image.png"))
                lines.append("Here is a character featured in this event.")
            if stage >= 2:
                lines.append(f"The name has **`{len(name)}`** characters.")
            if stage >= 3:
                d.setdefault("masked", _masked_name(name, EVENT_REVEAL_FRACTION))
                lines.append(f"Name: `{d['masked']}`")
        return lines, files

    async def _resolve_hint(
        self, data: dict
    ) -> tuple[discord.Embed, list[discord.File], bool]:
        """build the hint reply where the bool is whether it counts as a hint used
        non-music modes give three cumulative text hints and each reply repeats every tier
        revealed so far"""
        if data["guessing"] == "music":
            return await self._music_hint(data)
        if data["guessing"] == "event":
            return await self._story_hint(data)
        if data["guessType"] not in ("song", "character", "event"):
            return (
                embeds.embed(
                    title="Unsupported Hint",
                    description="Ongoing guess does not support hints.",
                    color=discord.Color.yellow(),
                ),
                [],
                False,
            )
        d = data["data"]
        stage = d.get("hint_stage", 0)
        advanced = stage < MAX_TEXT_HINTS
        if advanced:
            now = time.time()
            if now - d.get("last_hint", 0.0) < HINT_COOLDOWN:
                return (
                    embeds.error_embed("Please wait a moment before the next hint."),
                    [],
                    False,
                )
            d["last_hint"] = now
            stage += 1
            d["hint_stage"] = stage
        lines, files = await self._tier_lines(data, stage)
        if not advanced:
            lines.append("-# All hints have been revealed.")
        embed = embeds.embed(
            title=f"Guess Hint - Stage {stage}/{MAX_TEXT_HINTS}",
            description="\n".join(lines),
            color=discord.Color.yellow(),
        )
        if any(f.filename == "image.png" for f in files):
            embed.set_image(url="attachment://image.png")
        return embed, files, advanced

    @guess.command(name="hint", description="Get a hint for the active guess.")
    async def hint(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if not await self.channel_checks(interaction, already_guessing_check=False):
            return
        data = self.bot.cache.guess_channels.get(interaction.channel.id)  # type: ignore[union-attr]
        if not data:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active guess here.")
            )
            return
        embed, files, counted = await self._resolve_hint(data)
        await interaction.followup.send(embed=embed, files=files)
        if counted and data["guessing"]:
            await self.bot.user_data.add_guesses(interaction.user.id, data["guessing"], "hint")  # type: ignore[union-attr]

    async def _chat_hint(self, message: discord.Message, data: dict) -> None:
        embed, files, counted = await self._resolve_hint(data)
        await message.reply(embed=embed, files=files)
        if counted and data["guessing"]:
            await self.bot.user_data.add_guesses(message.author.id, data["guessing"], "hint")  # type: ignore[union-attr]

    # guessing_enabled still applies (see channel_checks) but is immutable for now, so no
    # toggle command is exposed

    @guess.command(name="stats", description="View your guessing statistics.")
    @app_commands.describe(user="Whose stats to view.")
    async def stats(
        self, interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        target = user or interaction.user
        await interaction.response.defer(thinking=True)
        all_stats = await self.bot.user_data.get_guesses(target.id)  # type: ignore[union-attr]
        totals = {"success": 0, "fail": 0, "hint": 0, "ragequit": 0}
        for value in all_stats.values():
            for k in totals:
                totals[k] += value.get(k, 0)
        attempts = totals["success"] + totals["fail"]
        rate = f"{totals['success'] / attempts * 100:.1f}%" if attempts else "N/A"
        embed = embeds.embed(
            title=f"{tools.escape_md(target.name)}'s Guess Stats",
            color=discord.Color.blurple(),
        )
        embed.description = (
            f"**Correct:** `{totals['success']:,}`\n**Incorrect:** `{totals['fail']:,}`\n"
            f"**Accuracy:** `{rate}`\n**Hints used:** `{totals['hint']:,}`\n"
            f"**Guesses ended:** `{totals['ragequit']:,}`"
        )
        await interaction.followup.send(embed=embed)

    @guess.command(name="leaderboard", description="View the guessing leaderboard.")
    @app_commands.autocomplete(mode=autocompletes.pjsk_guessing_types)
    @app_commands.describe(mode="Guess mode to rank by.")
    async def leaderboard(
        self, interaction: discord.Interaction, mode: str = "jacket"
    ) -> None:
        await interaction.response.defer(thinking=True)
        per_page = GUESS_LEADERBOARD_PER_PAGE
        page_rows, _, _, total_pages = await self.bot.user_data.get_guesses_leaderboard(mode, 1, interaction.user.id)  # type: ignore[union-attr]
        if not page_rows:
            await interaction.followup.send(
                embed=embeds.error_embed("No leaderboard data for that mode yet.")
            )
            return

        async def fetch_page(page: int):
            return await self.bot.user_data.get_guesses_leaderboard(mode, page, interaction.user.id)  # type: ignore[union-attr]

        def render_rows(rows, page: int) -> discord.Embed:
            embed = embeds.embed(
                title=f"Guess Leaderboard - {mode}", color=discord.Color.gold()
            )
            lines = []
            for idx, row in enumerate(rows):
                rank = (page - 1) * per_page + idx + 1
                lines.append(f"**#{rank}** <@{row['discord_id']}> - `{row['success']}`")
            embed.description = "\n".join(lines) + f"\n\n-# Page {page}/{total_pages}"
            return embed

        view = _LeaderboardView(
            fetch_page, render_rows, total_pages, interaction.user.id
        )
        await interaction.followup.send(embed=render_rows(page_rows, 1), view=view)
        view.message = await interaction.original_response()

    # --- points leaderboards (weekly / monthly) ---

    @guess.command(
        name="weekly", description="This week's combined guessing points leaderboard."
    )
    async def weekly(self, interaction: discord.Interaction) -> None:
        await self._points_board(interaction, "weekly")

    @guess.command(
        name="monthly",
        description="This month's combined guessing points leaderboard.",
    )
    async def monthly(self, interaction: discord.Interaction) -> None:
        await self._points_board(interaction, "monthly")

    async def _points_board(
        self, interaction: discord.Interaction, period_type: str
    ) -> None:
        await interaction.response.defer(thinking=True)
        if period_type == "weekly":
            index = periods.week_index()
            reset = periods.next_week_reset()
            title = f"Weekly Leaderboard - Week {index}"
        else:
            index = periods.month_index()
            reset = periods.next_month_reset()
            title = f"Monthly Leaderboard - Month {index}"
        per_page = GUESS_LEADERBOARD_PER_PAGE
        rows, rank_row, total_pages = await self.bot.user_data.get_points_leaderboard(period_type, index, 1, interaction.user.id)  # type: ignore[union-attr]
        if not rows:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "No points on this leaderboard yet - go guess!"
                )
            )
            return
        has_prizes = _prizes_enabled() and await self.bot.user_data.has_claimable_prizes(interaction.user.id)  # type: ignore[union-attr]

        def render(page_rows, page: int) -> discord.Embed:
            embed = embeds.embed(title=title, color=discord.Color.gold())
            lines = [
                f"**#{(page - 1) * per_page + i + 1}** <@{r['discord_id']}> - `{int(r['total']):,}`"
                for i, r in enumerate(page_rows)
            ]
            desc = "\n".join(lines)
            if rank_row:
                desc += f"\n\n-# You're **#{rank_row['rank']}** with `{int(rank_row['total']):,}` points."
            desc += f"\n-# Resets <t:{int(reset.timestamp())}:R> · Page {page}/{total_pages}"
            if has_prizes:
                desc += (
                    "\n**You have prizes waiting to be claimed!** Use `/guess prize`."
                )
            embed.description = desc
            return embed

        view = _PointsBoardView(
            self, period_type, index, render, total_pages, interaction.user.id
        )
        await interaction.followup.send(embed=render(rows, 1), view=view)
        view.message = await interaction.original_response()

    async def _breakdown_embed(
        self, user_id: int, period_type: str, period_index: int
    ) -> discord.Embed:
        breakdown = await self.bot.user_data.get_points_breakdown(user_id, period_type, period_index)  # type: ignore[union-attr]
        label = "Week" if period_type == "weekly" else "Month"
        embed = embeds.embed(
            title=f"Your {label} {period_index} Points", color=discord.Color.gold()
        )
        lines = [
            f"**{mode.title()}:** `{breakdown.get(mode, 0):,}`" for mode in GUESS_POINTS
        ]
        lines.append(f"\n**Total:** `{sum(breakdown.values()):,}`")
        embed.description = "\n".join(lines)
        return embed

    def _earn_points_embed(self) -> discord.Embed:
        embed = embeds.embed(title="How to Earn Points", color=discord.Color.gold())
        modes = "\n".join(
            f"**/guess {mode}** - starts at `{cfg['start']:,}` points"
            for mode, cfg in GUESS_POINTS.items()
        )
        embed.description = (
            "Only these guessing modes earn weekly/monthly leaderboard points:\n\n"
            f"{modes}\n\n"
            "-# Each hint used lowers what a correct guess is worth (down to `250` once all "
            "hints are shown). Hints are shared - any hint used on a round lowers the points "
            "for everyone who then guesses it.\n"
            f"-# Correct guesses earn `0` past `{HOURLY_GUESS_LIMIT:,}` guesses/hour, "
            f"`{DAILY_GUESS_LIMIT:,}`/day, or `{ROUND_GUESS_LIMIT}` guesses on one round."
        )
        return embed

    def _prizes_embed(self, period_type: str) -> discord.Embed:
        prizes = WEEKLY_PRIZES if period_type == "weekly" else MONTHLY_PRIZES
        label = "Weekly" if period_type == "weekly" else "Monthly"
        crystal = emojis.crystal
        items = sorted(prizes.items())
        lines: list[str] = []
        i = 0
        while i < len(items):  # collapse a run of equal prizes into one #a-b line
            start_rank, prize = items[i]
            j = i
            while j + 1 < len(items) and items[j + 1][1] == prize:
                j += 1
            end_rank = items[j][0]
            rank_label = (
                f"#{start_rank}"
                if start_rank == end_rank
                else f"#{start_rank}-{end_rank}"
            )
            paid, free = prize
            parts = [f"{paid:,} paid"]
            if free:
                parts.append(f"{free:,} free")
            lines.append(f"**{rank_label}** - {crystal} {' + '.join(parts)} crystals")
            i = j + 1
        notice = (
            "**__Prizes can only be claimed on the official Global PJSK server.__**\n"
            "-# Prizes are not sent automatically. Claim them manually with `/guess prize` "
            "(or by keeping your DMs on), within 3 days.\n"
            "-# You can opt out of these leaderboards in `/user settings`."
        )
        tos_url = get_config()["discord"].get("tos_url")
        if tos_url:
            notice += f"\n-# By claiming a prize you agree to the [Terms of Service]({tos_url})."
        embed = embeds.embed(title=f"{label} Prizes", color=discord.Color.gold())
        embed.description = "\n".join(lines) + f"\n\n{notice}"
        return embed

    # --- prize claiming ---

    @guess.command(
        name="prize", description="View, claim, or forfeit your leaderboard prizes."
    )
    async def prize(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _prizes_enabled():
            await interaction.followup.send(
                embed=embeds.error_embed("Prizes aren't available right now."),
                ephemeral=True,
            )
            return
        all_prizes = await self.bot.user_data.get_all_prizes(interaction.user.id)  # type: ignore[union-attr]
        if not all_prizes:
            await interaction.followup.send(
                embed=embeds.embed(
                    title="Your Prizes", description="You don't have any prizes."
                ),
                ephemeral=True,
            )
            return
        claimable = await self.bot.user_data.get_claimable_prizes(interaction.user.id)  # type: ignore[union-attr]
        view = _PrizeView(self, interaction.user.id, all_prizes, claimable)
        await interaction.followup.send(embed=view.render(), view=view, ephemeral=True)

    def _prize_history_embed(
        self, prizes: list[dict], page: int, per_page: int, claimable: int
    ) -> discord.Embed:
        total_pages = max(1, (len(prizes) + per_page - 1) // per_page)
        embed = embeds.embed(title="🏆 Your Prizes", color=discord.Color.gold())
        shown = prizes[(page - 1) * per_page : page * per_page]
        lines = [
            f"**{_prize_label(p)}** (#{p['rank']}) - {_prize_reward(p)}\n-# {_prize_status_line(p)}"
            for p in shown
        ]
        desc = "\n".join(lines) if lines else "You don't have any prizes."
        if claimable:
            desc = (
                f"You have **{claimable}** prize(s) to claim - use the buttons below.\n\n"
                + desc
            )
        embed.description = desc + f"\n\n-# Page {page}/{total_pages}"
        return embed

    async def _start_claim(
        self, interaction: discord.Interaction, prize_id: int
    ) -> None:
        prize = await self.bot.user_data.get_prize(prize_id)  # type: ignore[union-attr]
        now = datetime.datetime.now(datetime.timezone.utc)
        if (
            not prize
            or prize["discord_id"] != interaction.user.id
            or prize["status"] != "unclaimed"
            or prize["expires_at"] <= now
        ):
            await interaction.response.send_message(
                embed=embeds.error_embed("This prize can no longer be claimed."),
                ephemeral=True,
            )
            return
        # the account is confirmed on every claim, so a different one can be used each time
        pjsk_id = await self.bot.user_data.get_pjsk_id(interaction.user.id, "en")  # type: ignore[union-attr]
        if pjsk_id:
            await interaction.response.defer(ephemeral=True)
            await self._show_account_confirm(interaction, prize_id, pjsk_id)
        else:
            await interaction.response.send_modal(_ClaimIDModal(self, prize_id))

    async def _show_account_confirm(
        self, interaction: discord.Interaction, prize_id: int, pjsk_id: int
    ) -> None:
        """fetch the EN profile and ask the user to confirm it's where the prize should go"""
        try:
            resp = await self.bot.sbuga.get_profile(pjsk_id, "en")  # type: ignore[union-attr]
        except SbugaError:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "Couldn't find that PJSK EN account. Double-check the ID."
                ),
                view=_ClaimIDPromptView(self, prize_id),
                ephemeral=True,
            )
            return
        profile = resp.profile
        embed = embeds.embed(title="Confirm Your Account", color=discord.Color.gold())
        embed.description = (
            "Is this the PJSK **EN** account you want this prize sent to?\n\n"
            f"**Name:** {profile['user']['name']}\n"
            f"**User ID:** `{profile['user']['userId']}`\n"
            f"**Rank:** 🎵 {profile['user']['rank']}"
        )
        await interaction.followup.send(
            embed=embed,
            view=_ClaimConfirmView(self, prize_id, pjsk_id),
            ephemeral=True,
        )

    async def _do_claim(
        self, interaction: discord.Interaction, prize_id: int, pjsk_id: int
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        claimed = await self.bot.user_data.claim_prize(prize_id, interaction.user.id, pjsk_id)  # type: ignore[union-attr]
        if not claimed:
            await interaction.followup.send(
                embed=embeds.error_embed("This prize can no longer be claimed."),
                ephemeral=True,
            )
            return
        prize = await self.bot.user_data.get_prize(prize_id)  # type: ignore[union-attr]
        if not await self._notify_prize_channel(prize, pjsk_id):
            await self.bot.user_data.unclaim_prize(prize_id)  # type: ignore[union-attr]
            await interaction.followup.send(
                embed=embeds.error_embed("Claiming failed, please file a bug report."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Claimed your **{_prize_label(prize)}** prize! It's pending a manual grant - "
                "you'll be DM'd when it's sent."
            ),
            ephemeral=True,
        )

    async def _notify_prize_channel(self, prize: dict, pjsk_id: int) -> bool:
        channel_id = get_config()["discord"].get("prizes_channel_id")
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(
                channel_id
            )
            embed = embeds.embed(title="Prize Claim", color=discord.Color.gold())
            embed.description = (
                f"<@{prize['discord_id']}> (`{prize['discord_id']}`) claimed "
                f"**{_prize_label(prize)}** rank **#{prize['rank']}**.\n\n"
                f"**Reward:** {_prize_reward(prize)}\n"
                f"**Send to PJSK EN ID:** `{pjsk_id}`\n"
                f"**Prize ID:** `{prize['id']}`"
            )
            await channel.send(embed=embed, view=_PrizeAdminView(prize["id"]))  # type: ignore[union-attr]
            return True
        except (discord.HTTPException, AttributeError):
            return False

    async def _start_forfeit(
        self, interaction: discord.Interaction, prize_id: int, label: str
    ) -> None:
        await interaction.response.send_message(
            embed=embeds.embed(
                title="⚠️ Forfeit Prize",
                description=(
                    f"Forfeit your **{label}** prize? This is permanent - the prize is gone "
                    "for good and is not transferred to anyone."
                ),
                color=discord.Color.red(),
            ),
            view=_ForfeitConfirmView(self, prize_id, label, interaction.user.id),
            ephemeral=True,
        )

    # --- admin grant (buttons on the prize-channel message; handled persistently) ---

    @commands.Cog.listener("on_interaction")
    async def _prize_admin_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("prize:"):
            return
        _, action, sid = cid.split(":")
        prize_id = int(sid)
        if action == "sent":
            await self._admin_mark(interaction, prize_id, sent=True)
        elif action == "deny":
            await interaction.response.send_modal(
                _DenyReasonModal(self, prize_id, interaction.message)
            )

    async def _admin_mark(
        self,
        interaction: discord.Interaction,
        prize_id: int,
        *,
        sent: bool,
        reason: str = "",
        message: "discord.Message | None" = None,
    ) -> None:
        prize = await self.bot.user_data.get_prize(prize_id)  # type: ignore[union-attr]
        if not prize or prize["status"] != "pending":
            await interaction.response.send_message(
                embed=embeds.error_embed("This claim was already handled."),
                ephemeral=True,
            )
            return
        if sent:
            await self.bot.user_data.complete_prize(prize_id)  # type: ignore[union-attr]
            dm_text = f"Your prize of {_prize_reward(prize)} for {_prize_label(prize)} has been sent!"
            outcome = f"✅ Marked **Sent** by {interaction.user.mention}."
        else:
            await self.bot.user_data.deny_prize(prize_id, reason)  # type: ignore[union-attr]
            dm_text = (
                f"Your prize claim has been denied with the following reason: {reason}\n"
                "Please join the support server to ask for more information and/or to appeal."
            )
            outcome = f"❌ **Denied** by {interaction.user.mention}: {reason}"
        try:  # DM the user; if it fails they still see the status in /guess prize
            user = self.bot.get_user(prize["discord_id"]) or await self.bot.fetch_user(
                prize["discord_id"]
            )
            await user.send(embed=embeds.embed(description=dm_text))
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            embed=embeds.success_embed("Done."), ephemeral=True
        )
        target = message or interaction.message
        if target:
            try:
                await target.edit(content=outcome, view=None)
            except discord.HTTPException:
                pass


class _GuessResultView(SbugaView):
    """buttons on a finished guess, play again plus type-specific info buttons
    song gets song info and song aliases, character gets view card and character info
    each info button just reuses the existing slash command's callback"""

    def __init__(self, cog: "GuessCog", data: dict) -> None:
        super().__init__(timeout=30)  # how long the result buttons stay active
        self.cog = cog
        self.data = data
        if data["guessType"] == "song":
            self._add("Song Info", self._song_info)
            self._add("Song Aliases", self._song_aliases)
        elif data["guessType"] == "character":
            if data["data"].get("card_id"):
                self._add("View Card", self._view_card)
            self._add("Character Info", self._character_info)
        elif data["guessType"] == "event":
            self._add("Event Aliases", self._event_aliases)

    def _add(self, label: str, handler) -> None:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.gray)

        async def callback(interaction: discord.Interaction, _b=button) -> None:
            await self._spend(interaction, _b)
            await handler(interaction)

        button.callback = callback
        self.add_item(button)

    async def _spend(self, interaction: discord.Interaction, item) -> None:
        """disable the clicked button via a plain message edit leaving the interaction
        unresponded so the command callback can defer normally"""
        item.disabled = True
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _invoke(
        self, interaction: discord.Interaction, cog_name: str, command: str, arg: str
    ) -> None:
        cog = self.cog.bot.get_cog(cog_name)
        if cog is None:
            await interaction.response.send_message(
                embed=embeds.error_embed("That isn't available right now."),
                ephemeral=True,
            )
            return
        await getattr(cog, command).callback(cog, interaction, arg)

    @discord.ui.button(label="Play Again", style=discord.ButtonStyle.primary)
    async def play_again(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._spend(interaction, button)
        await self.cog.handle_guess(interaction, self.data["guessing"])

    async def _song_info(self, interaction: discord.Interaction) -> None:
        await self._invoke(interaction, "SongInfo", "info", str(self.data["answer"]))

    async def _song_aliases(self, interaction: discord.Interaction) -> None:
        await self._invoke(interaction, "SongInfo", "aliases", str(self.data["answer"]))

    async def _event_aliases(self, interaction: discord.Interaction) -> None:
        await self._invoke(
            interaction, "EventsCog", "aliases", str(self.data["answer"])
        )

    async def _character_info(self, interaction: discord.Interaction) -> None:
        await self._invoke(
            interaction, "CharactersCog", "info", str(self.data["answer"])
        )

    async def _view_card(self, interaction: discord.Interaction) -> None:
        await self._invoke(
            interaction, "CharactersCog", "card", str(self.data["data"]["card_id"])
        )


class _LeaderboardView(SbugaView):
    def __init__(
        self, fetch_page, render_rows, total_pages: int, restriction_id: int
    ) -> None:
        super().__init__(restrict_to=restriction_id)
        self.fetch_page = fetch_page
        self.render_rows = render_rows
        self.total_pages = max(1, total_pages)
        self.current_page = 1
        self._update()

    def _update(self) -> None:
        self.previous_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages

    async def _go(self, interaction: discord.Interaction) -> None:
        self._update()
        rows, _, _, _ = await self.fetch_page(self.current_page)
        await interaction.response.edit_message(
            embed=self.render_rows(rows, self.current_page), view=self
        )

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        await self._go(interaction)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
        await self._go(interaction)


class _PointsBoardView(SbugaView):
    """weekly/monthly points board: pagination plus a per-user breakdown and (when prizes are
    enabled) a prizes button. left unrestricted since the extra buttons reply ephemerally
    """

    def __init__(
        self,
        cog: "GuessCog",
        period_type: str,
        period_index: int,
        render,
        total_pages: int,
        user_id: int,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.period_type = period_type
        self.period_index = period_index
        self.render = render
        self.total_pages = max(1, total_pages)
        self.current_page = 1
        self.user_id = user_id
        if not _prizes_enabled():
            self.remove_item(self.prizes)
        self._update()

    def _update(self) -> None:
        self.previous_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages

    async def _go(self, interaction: discord.Interaction) -> None:
        self._update()
        rows, _, _ = await self.cog.bot.user_data.get_points_leaderboard(  # type: ignore[union-attr]
            self.period_type, self.period_index, self.current_page, self.user_id
        )
        await interaction.response.edit_message(
            embed=self.render(rows, self.current_page), view=self
        )

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        await self._go(interaction)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
        await self._go(interaction)

    @discord.ui.button(label="Breakdown", style=discord.ButtonStyle.gray)
    async def breakdown(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        embed = await self.cog._breakdown_embed(
            interaction.user.id, self.period_type, self.period_index
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Earn Points", style=discord.ButtonStyle.gray)
    async def earn_points(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            embed=self.cog._earn_points_embed(), ephemeral=True
        )

    @discord.ui.button(label="Prizes", style=discord.ButtonStyle.gray)
    async def prizes(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            embed=self.cog._prizes_embed(self.period_type), ephemeral=True
        )


def _add_claim_forfeit_buttons(view: SbugaView, cog: "GuessCog", prize: dict) -> None:
    """attach a Claim/Forfeit pair for one prize to a view"""
    label = _prize_label(prize)
    claim = discord.ui.Button(
        label=f"Claim ({label})", style=discord.ButtonStyle.success
    )
    forfeit = discord.ui.Button(
        label=f"Forfeit ({label})", style=discord.ButtonStyle.danger
    )

    async def claim_cb(interaction: discord.Interaction, _id=prize["id"]) -> None:
        await cog._start_claim(interaction, _id)

    async def forfeit_cb(
        interaction: discord.Interaction, _id=prize["id"], _l=label
    ) -> None:
        await cog._start_forfeit(interaction, _id, _l)

    claim.callback = claim_cb
    forfeit.callback = forfeit_cb
    view.add_item(claim)
    view.add_item(forfeit)


class _PrizeClaimView(SbugaView):
    """the claim/forfeit buttons attached to a winner's DM"""

    def __init__(self, cog: "GuessCog", prizes: list[dict]) -> None:
        super().__init__(timeout=None)
        for prize in prizes:
            _add_claim_forfeit_buttons(self, cog, prize)


class _PrizeView(SbugaView):
    """/guess prize: a paginated history of all prizes plus claim/forfeit buttons for the
    currently-claimable ones"""

    def __init__(
        self,
        cog: "GuessCog",
        user_id: int,
        all_prizes: list[dict],
        claimable: list[dict],
    ) -> None:
        super().__init__(timeout=300, restrict_to=user_id)
        self.cog = cog
        self.all_prizes = all_prizes
        self.claimable_count = len(claimable)
        self.per_page = 6
        self.total_pages = max(
            1, (len(all_prizes) + self.per_page - 1) // self.per_page
        )
        self.current_page = 1
        for prize in claimable:
            _add_claim_forfeit_buttons(self, cog, prize)
        self._update()

    def render(self) -> discord.Embed:
        return self.cog._prize_history_embed(
            self.all_prizes, self.current_page, self.per_page, self.claimable_count
        )

    def _update(self) -> None:
        self.previous_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary, row=0)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        self._update()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary, row=0)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
        self._update()
        await interaction.response.edit_message(embed=self.render(), view=self)


class _ClaimConfirmView(SbugaView):
    """confirm the EN account a prize will be sent to (asked on every claim)"""

    def __init__(self, cog: "GuessCog", prize_id: int, pjsk_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.prize_id = prize_id
        self.pjsk_id = pjsk_id

    @discord.ui.button(label="Confirm & Claim", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.cog._do_claim(interaction, self.prize_id, self.pjsk_id)

    @discord.ui.button(label="Use a Different ID", style=discord.ButtonStyle.secondary)
    async def another(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(_ClaimIDModal(self.cog, self.prize_id))


class _ClaimIDPromptView(SbugaView):
    """a lone button to (re)enter a PJSK EN id when the profile lookup fails"""

    def __init__(self, cog: "GuessCog", prize_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.prize_id = prize_id

    @discord.ui.button(label="Enter a PJSK EN ID", style=discord.ButtonStyle.primary)
    async def enter(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(_ClaimIDModal(self.cog, self.prize_id))


class _ClaimIDModal(discord.ui.Modal, title="Claim on a PJSK EN Account"):
    def __init__(self, cog: "GuessCog", prize_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.prize_id = prize_id
        self.pjsk_id: discord.ui.TextInput = discord.ui.TextInput(
            label="PJSK EN User ID",
            placeholder="The EN account to receive the prize",
            required=True,
        )
        self.add_item(self.pjsk_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.pjsk_id.value.strip()
        if not raw.isdigit() or not 10_000_000 < int(raw) < 10_000_000_000_000_000_000:
            await interaction.response.send_message(
                embed=embeds.error_embed("Invalid user ID."),
                view=_ClaimIDPromptView(self.cog, self.prize_id),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog._show_account_confirm(interaction, self.prize_id, int(raw))


class _ForfeitConfirmView(SbugaView):
    def __init__(
        self, cog: "GuessCog", prize_id: int, label: str, user_id: int
    ) -> None:
        super().__init__(timeout=60, restrict_to=user_id)
        self.cog = cog
        self.prize_id = prize_id
        self.label = label

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.danger)
    async def forfeit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ok = await self.cog.bot.user_data.forfeit_prize(self.prize_id, interaction.user.id)  # type: ignore[union-attr]
        self._disable_all()
        text = (
            f"Forfeited your **{self.label}** prize."
            if ok
            else "This prize can no longer be forfeited."
        )
        await interaction.response.edit_message(
            embed=embeds.embed(description=text), view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._disable_all()
        await interaction.response.edit_message(
            embed=embeds.embed(description="Cancelled - your prize is safe."), view=self
        )


class _PrizeAdminView(discord.ui.View):
    """Sent / Denied buttons on the prize-channel message. custom-id only - handled by the
    persistent on_interaction listener so they survive restarts"""

    def __init__(self, prize_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Sent",
                style=discord.ButtonStyle.success,
                custom_id=f"prize:sent:{prize_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Denied (with reason)",
                style=discord.ButtonStyle.danger,
                custom_id=f"prize:deny:{prize_id}",
            )
        )


class _DenyReasonModal(discord.ui.Modal, title="Deny Prize Claim"):
    def __init__(
        self, cog: "GuessCog", prize_id: int, message: "discord.Message | None"
    ) -> None:
        super().__init__()
        self.cog = cog
        self.prize_id = prize_id
        self.origin = message
        self.reason: discord.ui.TextInput = discord.ui.TextInput(
            label="Reason for denial",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._admin_mark(
            interaction,
            self.prize_id,
            sent=False,
            reason=self.reason.value.strip(),
            message=self.origin,
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(GuessCog(bot))
