from __future__ import annotations

import asyncio
import io
import math
import random
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from data.pjsk import character_display_name
from data.song_equivalents import equivalents_of, songs_equivalent
from database.queries import GUESS_LEADERBOARD_PER_PAGE
from helpers import converters, embeds, tools, unblock
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.imaging import _crop_chart, _crop_square
from helpers.views import SbugaView
from services import chart_cache, chart_clip, chart_preview, song_clip
from services.sbuga import SbugaError

if TYPE_CHECKING:
    from main import SbugaBot

GUESS_TIME = 60
MODE_TIME = {
    "character": 30,
    "character_bw": 30,
    "chart": 90,
    "chart_append": 90,
    "music": 300,
}
GUESS_PREFIX = "-"
# appended to every "not found" / "wrong guess" reply
GUESS_TIP = "\n-# Use `-hint` for a hint, `-end` to give up, or `-time` for time left!"
# music mode's variant - a hint reveals more of the song rather than a fact
MUSIC_TIP = "\n-# Use `-hint` to provide more of the song, or `-time` for time left!"
# a music hint can't fire within this many seconds of the previous one
MUSIC_HINT_COOLDOWN = 5.0
# players must let at least this fraction of the round elapse before they can give up
GIVEUP_FRACTION = 1 / 3
# a round whose build never finished (task cancelled, a fetch hung) sits with startTime=None
# forever, holding its image bytes and locking the channel - reap it once it's this stale
PENDING_ROUND_TIMEOUT = 300
_ASSET_ATTEMPTS = 5

# A bare number is matched as a song *id*, which collides with songs whose actual title is
# that number. When a guess of the plain number lands on the id-sharing song, nudge the
# guesser toward the one they probably meant. Keyed by (typed text, matched song id) ->
# (intended song's name, how to type it). Mirrors old Sbotga's hardcoded list.
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


_CHART_CLIP_ATTEMPTS = 3  # capped lower: each attempt may render a video
_BAD_FILENAME_CHARS = set('\\/:*?"<>|')


def _safe_filename(name: str) -> str:
    """A song title trimmed to something safe for an upload filename."""
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

    async def cog_load(self) -> None:
        # warm the render server, then keep the clip cache topped up in the background
        async def _boot() -> None:
            chart_preview.cleanup_orphans()  # kill renderer processes a prior crash leaked
            await chart_preview.start()
            chart_cache.cleanup_invalid()  # drop entries a prior crash left half-generated
            chart_cache.start(self.bot.pjsk)

        asyncio.create_task(_boot())

    async def cog_unload(self) -> None:
        self.check_guess_task.cancel()
        await chart_cache.stop()
        await chart_preview.stop()

    # --- random pickers ---

    def _random_song(self, has_append: bool = False, needs_master: bool = False):
        def has(music, difficulty: str) -> bool:
            return any(d.difficulty == difficulty for d in music.difficulties)

        musics = [
            m
            for m in self.bot.pjsk.musics()  # type: ignore[union-attr]
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
    # some entries are permanently unrenderable (unmirrored jackets, cards without trained art)

    async def _pick_song_jacket(self):
        for _ in range(_ASSET_ATTEMPTS):
            music = self._random_song()
            if not music:
                return None
            jacket = await _fetch_bytes(music.jacket_url)
            if jacket:
                return music, jacket
        return None

    async def _pick_chart_image(self, has_append: bool):
        # fallback when the clip renderer isn't installed: the old cropped-chart round
        for _ in range(_ASSET_ATTEMPTS):
            music = self._random_song(
                has_append=has_append, needs_master=not has_append
            )
            if not music:
                return None
            difficulty = "append" if has_append else "master"
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

    async def _pick_chart_clip(self, has_append: bool):
        """(music, clip mp4, reveal png, difficulty, answer video | None)."""
        gtype = "chart_append" if has_append else "chart"
        # grab a pre-rendered (higher quality) clip instantly if one is cached
        cached = chart_cache.pop(gtype)
        if cached:
            clip, answer, meta = cached
            music = self.bot.pjsk.get_music(meta["music_id"])  # type: ignore[union-attr]
            if music:
                png = await self._chart_reveal_png(music.id, meta["diff"])
                return music, clip, png, meta["diff"], answer
        # nothing cached: render on the fly, smaller/faster, live-priority.
        # on-the-fly has no answer video (the audio must not leak during the round).
        with chart_cache.live_priority():
            return await self._render_chart_clip_live(has_append)

    async def _render_chart_clip_live(self, has_append: bool):
        for _ in range(_CHART_CLIP_ATTEMPTS):
            music = self._random_song(
                has_append=has_append, needs_master=not has_append
            )
            if not music:
                return None
            difficulty = "append" if has_append else "master"
            region = next((r for r in self.bot.pjsk.regions_for_music(music.id) if r in ("en", "jp")), "en")  # type: ignore[union-attr]
            sus_text = await self._fetch_chart_sus(music.id, difficulty, region)
            if not sus_text:
                continue
            try:
                clip = await chart_clip.render_clip(
                    sus_text,
                    height=chart_clip.LIVE_HEIGHT,
                    fps=chart_clip.LIVE_FPS,
                )
            except chart_clip.ChartClipError as exc:
                # a render failure is a renderer problem, not a chart one - bail so the
                # caller falls back to the cropped image instead of retrying identically
                self.bot.warn(
                    f"chart clip render failed ({music.id} {difficulty}): {exc}"
                )
                return None
            if not clip:
                continue
            png = await self._chart_reveal_png(music.id, difficulty)
            return music, clip, png, difficulty, None
        return None

    async def _pick_music(self):
        """(music, full song audio, nosil url, window start seconds, jacket | None). Rerolls
        past songs with no nosil audio or too short to place a window. None if nothing usable.
        The audio is only used to cut the small stage clips; it isn't kept on the round.
        """
        for _ in range(_ASSET_ATTEMPTS):
            music = self._random_song()
            if not music:
                return None
            url = song_clip.pick_nosil_url(music)
            if not url:
                continue
            audio = await _fetch_bytes(url)
            if not audio:
                continue
            start = await song_clip.choose_window(audio)
            if start is None:
                continue  # too short to place a window
            jacket = await _fetch_bytes(music.jacket_url) if music.jacket_url else None
            return music, audio, url, start, jacket
        return None

    async def _gen_music_clips(self, data: dict, audio: bytes, start: float) -> None:
        """Cut the longer stage clips in the background while the player listens to stage 1,
        so only a few KB of clips are held (not the whole song)."""
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
                trained = False  # no trained art; don't claim it in the reveal or hint
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
        # Guesses are read from chat, so the bot must actually be able to see
        # this channel - user installs can run commands in places it can't.
        if interaction.guild is not None:
            # is_guild_integration() only proves the app is installed here; an
            # applications.commands-only install has no bot member, so on_message
            # never fires. guild.me is None in that case (and on the partial guild
            # a user install falls back to).
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
        if data["guessing"] == "music" and data["data"].get("audio_url"):
            # reveal the full song alongside the jacket (re-fetched, not held)
            audio = await _fetch_bytes(data["data"]["audio_url"])
            if audio:
                name = _safe_filename(str(data["answerName"]))
                files.append(discord.File(io.BytesIO(audio), f"{name}.mp3"))
                kwargs["audio"] = True
        return files, kwargs

    # --- timeout loop ---

    @tasks.loop(seconds=2)
    async def check_guess_task(self) -> None:
        now = time.time()
        for channel_id, data in list(self.bot.cache.guess_channels.items()):
            if not data["startTime"]:
                # never started (build cancelled / a fetch hung): reap once clearly orphaned
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

        # "-hint" / "-end" / "-time" (any case, any spacing) run those instead of guessing
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

        if data["guessType"] == "song":
            await self._check_song(message, data, content)
        elif data["guessType"] == "character":
            await self._check_character(message, data, content)
        elif data["guessType"] == "event":
            await self._check_event(message, data, content)

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
        embed = embeds.success_embed(title="Correct", description=description)
        files, flags = await self._reveal_files(data)
        if flags.get("thumb"):
            embed.set_thumbnail(url="attachment://thumb.png")
        if flags.get("image"):
            embed.set_image(url="attachment://image.png")
        view = _GuessResultView(self, data)
        view.message = await message.reply(embed=embed, files=files, view=view)
        if data["guessing"]:
            await self.bot.user_data.add_guesses(message.author.id, data["guessing"], "success")  # type: ignore[union-attr]
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
        event = converters.match_event(self.bot.pjsk, content)  # type: ignore[arg-type]
        if self.guess_ended(self.bot, data):
            return
        if not event:
            await message.reply(
                embed=embeds.error_embed(
                    f"Couldn't find an event matching `{content}`." + GUESS_TIP,
                    title="Incorrect",
                )
            )
            return
        if event.id == data["answer"]:
            await self._award(message, data)
        else:
            await message.reply(
                embed=embeds.error_embed(
                    f"Incorrectly guessed **`{event.name}`**." + GUESS_TIP,
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

        guess: dict[str, Any] = {
            "guessed": [],
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
        # a chart round with no cached clip renders one on the fly (several seconds); show a
        # placeholder so the channel is visibly locked, then edit it in. startTime only
        # begins after the edit, so the timer and guessing wait for the chart to appear.
        is_chart = mode in ("chart", "chart_append")
        rendering_live = is_chart and chart_cache.count(mode) == 0
        try:
            if rendering_live:
                await interaction.edit_original_response(
                    embed=embeds.embed(
                        title="Guess The Chart",
                        description="Rendering the chart clip… (~10 seconds)",
                        color=discord.Color.dark_gold(),
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
            music, audio, url, start, jacket = hit
            guess["answer"] = music.id
            guess["answerName"] = music.title
            if jacket:
                guess["answer_file_path"] = io.BytesIO(jacket)
            data = guess["data"]
            data["audio_url"] = url  # re-fetched only for the reveal, never held
            data["start"] = start
            data["stage"] = 1
            data["last_hint"] = 0.0
            clip1 = await song_clip.stage_clip(audio, start, 1)
            data["clips"] = {1: clip1}
            # cut the longer stages in the background while they listen to stage 1
            data["clip_task"] = asyncio.create_task(
                self._gen_music_clips(data, audio, start)
            )
            embed = embeds.embed(
                title="Guess The Music", color=discord.Color.dark_gold()
            )
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
        ):
            guess["guessType"] = "song"
            has_append = mode == "chart_append"

            if mode == "notes":
                # three append-only entries carry no master chart at all
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
                embed = embeds.embed(
                    title="Guess The Song", color=discord.Color.dark_gold()
                )
                embed.description = (
                    f"Guess the song from its Master note count.\nUse `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds.\n\n"
                    f"# This song has `{master.total_note_count}` notes on Master."
                )
                return embed, None

            if mode in ("chart", "chart_append"):
                clip_hit = (
                    await self._pick_chart_clip(has_append)
                    if chart_preview.available()
                    else None
                )
                if clip_hit:
                    music, clip, png, diff, answer_video = clip_hit
                    guess["answer"] = music.id
                    guess["answerName"] = music.title
                    if png:  # reveal (/guess end) still shows the full chart
                        guess["answer_file_path"] = io.BytesIO(png)
                    if answer_video:  # cached clips also reveal a jacket+audio video
                        guess["data"]["answer_video"] = answer_video
                    embed = embeds.embed(
                        title="Guess The Chart", color=discord.Color.dark_gold()
                    )
                    embed.description = (
                        f"Guess the song from a ~10 second {diff} chart clip.\n"
                        f"Use `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
                    )
                    return embed, discord.File(io.BytesIO(clip), "chart.mp4")

                # renderer missing/broken or no clip available: cropped chart image
                hit = await self._pick_chart_image(has_append)
                if not hit:
                    return None, None
                music, png, diff = hit
                guess["answer"] = music.id
                guess["answerName"] = music.title
                guess["answer_file_path"] = io.BytesIO(png)
                cropped = await unblock.to_process_with_timeout(_crop_chart, png)
                embed = embeds.embed(
                    title="Guess The Chart", color=discord.Color.dark_gold()
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
            embed = embeds.embed(
                title="Guess The Song", color=discord.Color.dark_gold()
            )
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
            cropped = await unblock.to_process_with_timeout(
                _crop_square, art, 250, mode == "character_bw"
            )
            embed = embeds.embed(
                title="Guess The Character", color=discord.Color.dark_gold()
            )
            embed.set_image(url="attachment://image.png")
            embed.description = f"Guess the character from a cropped card.\nUse `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
            return embed, discord.File(cropped, "image.png")

        if mode == "event":
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
            embed = embeds.embed(
                title="Guess The Event", color=discord.Color.dark_gold()
            )
            embed.set_image(url="attachment://image.png")
            embed.description = f"Guess the event from a cropped background.\nUse `{GUESS_PREFIX}your guess` to guess, `{GUESS_PREFIX}hint` for a hint, `{GUESS_PREFIX}end` to give up, or `{GUESS_PREFIX}time` for time left. You have {secs} seconds."
            return embed, discord.File(cropped, "image.png")

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
        name="event", description="Guess the event from a cropped background."
    )
    async def event(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "event")

    @guess.command(
        name="notes", description="Guess the song from its Master note count."
    )
    async def notes(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "notes")

    @guess.command(name="music", description="Guess the song from a short audio clip.")
    async def music(self, interaction: discord.Interaction) -> None:
        await self.handle_guess(interaction, "music")

    async def _resolve_end(
        self, channel_id: int
    ) -> tuple[discord.Embed, list[discord.File], "_GuessResultView | None"]:
        """Build the reply for ending a round. A non-None view means the round was actually
        ended (attach it); a None view means it's an error reply (no active guess / too soon
        to give up)."""
        data = self.bot.cache.guess_channels.get(channel_id)
        if not data:
            return embeds.error_embed("There's no active guess here."), [], None
        if data["guessing"] == "music":
            # can't give up until the whole song has been revealed (stage 4)
            if data["data"].get("stage", 1) < song_clip.MAX_STAGE:
                return (
                    embeds.error_embed(
                        "You can't give up yet - use `-hint` until you've heard the "
                        "full song."
                    ),
                    [],
                    None,
                )
        else:
            started = data.get("startTime")
            if started:
                max_time = MODE_TIME.get(data["guessing"], GUESS_TIME)
                remaining = started + max_time * GIVEUP_FRACTION - time.time()
                if remaining > 0:
                    return (
                        embeds.error_embed(
                            f"You can't give up yet - wait `{math.ceil(remaining)}` "
                            "more seconds."
                        ),
                        [],
                        None,
                    )
        self.remove_guess(self.bot, channel_id)
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
        embed, files, view = await self._resolve_end(interaction.channel.id)  # type: ignore[union-attr]
        msg = await interaction.followup.send(
            embed=embed, files=files, view=view or discord.utils.MISSING
        )
        if view:
            view.message = msg

    async def _chat_end(self, message: discord.Message, data: dict) -> None:
        embed, files, view = await self._resolve_end(message.channel.id)
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

    def _master_level(self, song_id: int) -> int | None:
        # append-only entries (388) carry no master chart, so fall back to the
        # song they're a copy of
        for candidate in (song_id, *sorted(equivalents_of(song_id))):
            music = self.bot.pjsk.get_music(candidate)  # type: ignore[union-attr]
            if not music:
                continue
            master = next(
                (d for d in music.difficulties if d.difficulty == "master"), None
            )
            if master:
                return master.play_level
        return None

    async def _music_clip(self, d: dict, stage: int) -> bytes | None:
        """The stage clip: from the pre-generated cache, else wait for the background cutter,
        else re-fetch the song and cut this one."""
        clip = d["clips"].get(stage)
        if clip is not None:
            return clip
        task = d.get("clip_task")
        if task is not None:
            try:
                await task
            except Exception:
                pass
            clip = d["clips"].get(stage)
            if clip is not None:
                return clip
        audio = await _fetch_bytes(
            d["audio_url"]
        )  # background cut failed; do it once now
        if not audio:
            return None
        try:
            clip = await song_clip.stage_clip(audio, d["start"], stage)
        except song_clip.SongClipError:
            return None
        d["clips"][stage] = clip
        return clip

    async def _music_hint(
        self, data: dict
    ) -> tuple[discord.Embed, list[discord.File], bool]:
        """Music hints reveal a longer prefix of the clip (stage 1→4), rate-limited."""
        d = data["data"]
        stage = d.get("stage", 1)
        if stage >= song_clip.MAX_STAGE:
            return (
                embeds.embed(
                    title=f"Guess Hint - Stage {song_clip.MAX_STAGE}/{song_clip.MAX_STAGE}",
                    description=(
                        f"The {int(song_clip.FULL_SECONDS)} second song is already provided."
                    ),
                    color=discord.Color.red(),
                ),
                [],
                False,
            )
        now = time.time()
        if now - d.get("last_hint", 0.0) < MUSIC_HINT_COOLDOWN:
            return (
                embeds.error_embed("Please wait a moment before the next hint."),
                [],
                False,
            )
        d["last_hint"] = now  # set before the cut so a rapid second hint is rejected
        stage += 1
        d["stage"] = stage
        clip = await self._music_clip(d, stage)
        if clip is None:
            return embeds.error_embed("Couldn't extend the clip."), [], False
        embed = embeds.embed(
            title=f"Guess Hint - Stage {stage}/{song_clip.MAX_STAGE}",
            description=(
                f"Here's {int(song_clip.STAGE_SECONDS[stage])} seconds of the song."
            ),
            color=discord.Color.red(),
        )
        return (
            embed,
            [discord.File(io.BytesIO(clip), song_clip.clip_filename(stage))],
            True,
        )

    async def _resolve_hint(
        self, data: dict
    ) -> tuple[discord.Embed, list[discord.File], bool]:
        """Build the hint reply. The bool is whether it counts as a hint used (a real hint,
        not an error/unsupported reply)."""
        if data["guessing"] == "music":
            return await self._music_hint(data)
        files: list[discord.File] = []
        if data["guessType"] == "song":
            level = self._master_level(data["answer"])
            if level is None:
                # append-only songs have no Master to give a level for
                return (
                    embeds.embed(
                        title="Guess Hint",
                        description="This song doesn't have a Master chart.",
                        color=discord.Color.red(),
                    ),
                    files,
                    True,
                )
            return (
                embeds.embed(
                    title="Guess Hint",
                    description=(
                        f"The song is level **`{level}`** (after any rerates) on "
                        f"{emojis.difficulty_colors['master']} **Master**."
                    ),
                    color=discord.Color.red(),
                ),
                files,
                True,
            )
        if data["guessType"] == "character":
            trained = "trained" if data["data"]["trained"] else "not trained"
            return (
                embeds.embed(
                    title="Guess Hint",
                    description=f"The character card is **`{trained}`**.",
                    color=discord.Color.red(),
                ),
                files,
                True,
            )
        if data["guessType"] == "event":
            event = self.bot.pjsk.get_event(data["answer"])  # type: ignore[union-attr]
            art = (
                await _fetch_bytes(event.character_url)
                if event and event.character_url
                else None
            )
            if not art:
                return (
                    embeds.error_embed("I couldn't load a hint for this event."),
                    files,
                    False,
                )
            embed = embeds.embed(
                title="Guess Hint",
                description="Here is a character featured in this event.",
                color=discord.Color.red(),
            )
            embed.set_image(url="attachment://image.png")
            files.append(discord.File(io.BytesIO(art), "image.png"))
            return embed, files, True
        return (
            embeds.embed(
                title="Unsupported Hint",
                description="Ongoing guess does not support hints.",
                color=discord.Color.red(),
            ),
            files,
            False,
        )

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

    @guess.command(
        name="toggle", description="Enable or disable guessing in this server."
    )
    @app_commands.guild_only()
    @app_commands.describe(on="Whether guessing should be enabled.")
    async def toggle(self, interaction: discord.Interaction, on: bool) -> None:
        await interaction.response.defer()
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.manage_guild
        ):
            await interaction.followup.send(
                embed=embeds.error_embed("You need the `Manage Server` permission.")
            )
            return
        state = await self.bot.user_data.toggle_guessing(interaction.guild_id, on)  # type: ignore[union-attr,arg-type]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Guessing is now **{'ON' if state else 'OFF'}**!"
            )
        )

    @guess.command(name="stats", description="View your guessing statistics.")
    @app_commands.describe(user="Whose stats to view.")
    async def stats(
        self, interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        target = user or interaction.user
        await interaction.response.defer(thinking=True)
        all_stats = await self.bot.user_data.get_guesses(target.id)  # type: ignore[union-attr]
        totals = {"success": 0, "fail": 0, "hint": 0}
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
            f"**Accuracy:** `{rate}`\n**Hints used:** `{totals['hint']:,}`"
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


class _GuessResultView(SbugaView):
    """Buttons on a finished guess: Play Again, plus type-specific info buttons
    (song -> Song Info/Song Aliases, character -> View Card/Character Info).
    Each info button just reuses the existing slash command's callback."""

    def __init__(self, cog: "GuessCog", data: dict) -> None:
        super().__init__(timeout=15)  # matches the original bot's button timeout
        self.cog = cog
        self.data = data
        if data["guessType"] == "song":
            self._add("Song Info", self._song_info)
            self._add("Song Aliases", self._song_aliases)
        elif data["guessType"] == "character":
            if data["data"].get("card_id"):
                self._add("View Card", self._view_card)
            self._add("Character Info", self._character_info)

    def _add(self, label: str, handler) -> None:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.gray)

        async def callback(interaction: discord.Interaction, _b=button) -> None:
            await self._spend(interaction, _b)
            await handler(interaction)

        button.callback = callback
        self.add_item(button)

    async def _spend(self, interaction: discord.Interaction, item) -> None:
        """Disable the clicked button via a plain message edit, leaving the
        interaction unresponded so the command callback can defer normally."""
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


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(GuessCog(bot))
