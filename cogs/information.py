from __future__ import annotations

import time
from collections import Counter
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.search import preprocess
from helpers import converters, embeds
from helpers.autocompletes import autocompletes
from helpers.config_loader import get_config
from helpers.emojis import emojis
from services.sbuga import SbugaError, SbugaNotFound

# /alias is a support-server-only tool (`/song aliases` is the public read).
# Registering it as a guild command keeps it out of every other server's picker.
_SUPPORT_GUILD_ID: int | None = get_config()["discord"].get("support_id") or None

if TYPE_CHECKING:
    from main import SbugaBot

PJSK_REGIONS = ["en", "jp", "tw", "kr"]


class InfoCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    def _trim_cmd_log(self) -> None:
        cutoff = time.time() - 60
        log = self.bot.cache.executed_commands
        while log and log[0][1] < cutoff:
            log.popleft()

    def _in_support_server(self, interaction: discord.Interaction) -> bool:
        """Alias editing is confined to the support server — the manager roles only
        exist there, and the /alias group is installable anywhere."""
        support_id = self.bot.config["discord"].get("support_id")
        return bool(support_id) and interaction.guild_id == support_id

    def _is_alias_mod(self, user: discord.abc.User | discord.Member) -> bool:
        if user.id in (self.bot.owner_ids or set()):
            return True
        role_ids = set(self.bot.config["discord"].get("alias_manager_role_ids", []))
        user_roles = {r.id for r in getattr(user, "roles", [])}
        return bool(role_ids & user_roles)

    def _alias_error_embed(self, error: SbugaError, alias: str) -> discord.Embed:
        """Aliases are unique across every song, so name the one already holding it."""
        if error.detail == "alias_taken":
            music_id = error.data.get("music_id")
            other = self.bot.pjsk.get_music(music_id) if music_id else None  # type: ignore[union-attr]
            where = (
                f"**{other.title}** (ID `{other.id}`)"
                if other
                else f"song ID `{music_id}`"
            )
            return embeds.error_embed(
                f"`{alias}` is already an alias for {where}.\n"
                "Remove it from that song before adding it here.",
                title="Alias already taken",
            )
        return embeds.error_embed(f"Couldn't add alias: {error.detail or error.status}")

    async def _deny_alias_edit(self, interaction: discord.Interaction) -> bool:
        """Reply and return True if this caller may not edit aliases here."""
        if not self._in_support_server(interaction):
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    "Alias commands can only be used in the support server."
                ),
                ephemeral=True,
            )
            return True
        if not self._is_alias_mod(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error_embed("You're not authorized to manage aliases."),
                ephemeral=True,
            )
            return True
        return False

    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: app_commands.Command
    ) -> None:
        self.bot.cache.executed_commands.append(
            (command.qualified_name, time.time(), interaction.user.id)
        )
        self._trim_cmd_log()

    # --- general ---

    @app_commands.command(
        name="ping", description="Check the bot's latency and recent activity."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        self._trim_cmd_log()
        log = self.bot.cache.executed_commands
        cmds_ran = len(log) + 1
        users = {interaction.user.id} | {uid for _, _, uid in log}
        counter = Counter(cmd for cmd, _, _ in log)
        counter["ping"] += 1
        popular = (
            f"`/{counter.most_common(1)[0][0]}` was the most popular command in the last minute."
            if counter
            else "No commands were ran."
        )
        embed = embeds.embed(
            title="Pong!",
            description=(
                f"**Latency:** `{round(self.bot.latency * 1000, 2)}`ms\n\n"
                f"**{cmds_ran:,}** commands ran in the last minute.\n"
                f"**{len(users)}** users ran commands in the last minute.\n{popular}"
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="help", description="Bot info and links.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.user
        support = self.bot.config["discord"].get("support_invite", "")
        embed = embeds.embed(
            title=self.bot.user.name,
            description=(
                f"**Invite:** https://discord.com/oauth2/authorize?client_id={self.bot.user.id}\n"
                f"**Support:** {support}\n\n"
                f"-# {self.bot.user.mention} is not affiliated with SEGA, Colorful Palette, or Project Sekai."
            ),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="donate", description="Support the bot.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def donate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = embeds.embed(
            title="Donations",
            description=(
                "Donations are strictly **optional** and help cover hosting costs.\n\n"
                "**LINK:** https://ko-fi.com/uselessyum"
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    # --- pjsk group ---

    pjsk = app_commands.Group(
        name="pjsk",
        description="PJSK information.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @pjsk.command(
        name="why_inappropriate",
        description="Check why text is blocked by PJSK's filter.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(["en", "jp"]))
    @app_commands.describe(text="Text to check.", region="Game server region (en/jp).")
    async def why_inappropriate(
        self, interaction: discord.Interaction, text: str, region: str = "default"
    ) -> None:
        if len(text) > 512:
            await interaction.response.send_message(
                embed=embeds.error_embed("Text is too long! Max 512 characters."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(interaction.user.id, "default_region")  # type: ignore[union-attr]
        if region not in ("en", "jp"):
            region = "en"

        try:
            resp = await self.bot.sbuga.why_inappropriate(text, region)  # type: ignore[union-attr,arg-type]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't check text: {e.detail or e.status}")
            )
            return

        blocked = [text[r.start : r.end] for r in resp.indexes]
        verdict = bool(blocked)
        escaped = text.replace("`", "ˋ")
        block_section = (
            "```diff\n"
            + ("\n".join(f"- {w}" for w in blocked) if blocked else "+ None!")
            + "\n```"
        )
        embed = embeds.embed(
            title=f"PJSK {region.upper()} Text Check",
            description=(
                f"Your text is **{'inappropriate' if verdict else 'appropriate'}** for PJSK {region.upper()}!"
            ),
            color=discord.Color.red() if verdict else discord.Color.green(),
        )
        embed.add_field(
            name="Your Text", value=f"```text\n{escaped}\n```", inline=False
        )
        embed.add_field(name="Blocked Words", value=block_section, inline=False)
        await interaction.followup.send(embed=embed)

    @pjsk.command(name="profile", description="View a PJSK profile.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(PJSK_REGIONS))
    @app_commands.describe(
        user_id="PJSK user ID (omit to use your linked account).",
        region="Game server region.",
    )
    async def profile(
        self,
        interaction: discord.Interaction,
        user_id: str | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(interaction.user.id, "default_region")  # type: ignore[union-attr]
        if region not in PJSK_REGIONS:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Region `{region.upper()}` isn't supported.")
            )
            return

        linked = await self.bot.user_data.get_pjsk_id(interaction.user.id, region)  # type: ignore[union-attr]
        if not user_id:
            if not linked:
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        f"Link your {region.upper()} PJSK account, or pass a user ID."
                    )
                )
                return
            user_id = str(linked)
        if not user_id.isdigit():
            await interaction.followup.send(
                embed=embeds.error_embed("Invalid user ID.")
            )
            return

        try:
            resp = await self.bot.sbuga.get_profile(int(user_id), region)  # type: ignore[union-attr,arg-type]
        except SbugaNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't find that profile in the {region.upper()} server."
                )
            )
            return
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch profile: {e.detail or e.status}"
                )
            )
            return

        data = resp.profile
        is_self = str(linked) == user_id
        joined = ""
        if region in ("en", "jp"):
            ts = (
                int(format(data["user"]["userId"], "064b")[:42], 2) + 1600218000000
            ) // 1000
            joined = f"**Joined:** <t:{ts}:R>\n"
        clears = data.get("userMusicDifficultyClearCount", [])

        def stat(index: int, key: str) -> str:
            return str(clears[index][key]) if len(clears) > index else "?"

        embed = embeds.embed(
            title=data["user"]["name"],
            description=(
                ("✅ This is your PJSK account!\n\n" if is_self else "")
                + f"**User ID:** `{data['user']['userId']}`\n{joined}"
                + f"**Rank:** **`🎵 {data['user']['rank']}`**\n\n"
                + f"**Bio**\n```{data['userProfile'].get('word') or 'No Bio'}```\n"
                + f"**Clears:** `{stat(3, 'liveClear')}` Expert {emojis.clear}, "
                + f"`{stat(4, 'liveClear')}` Master {emojis.clear}, "
                + f"`{stat(5, 'liveClear')}` Append {emojis.append_clear}\n"
                + f"**FCs:** `{stat(3, 'fullCombo')}` Expert {emojis.fc}, "
                + f"`{stat(4, 'fullCombo')}` Master {emojis.fc}, "
                + f"`{stat(5, 'fullCombo')}` Append {emojis.append_fc}\n"
                + f"**APs:** `{stat(3, 'allPerfect')}` Expert {emojis.ap}, "
                + f"`{stat(4, 'allPerfect')}` Master {emojis.ap}, "
                + f"`{stat(5, 'allPerfect')}` Append {emojis.append_ap}\n"
            ),
            color=discord.Color.dark_green(),
        )
        embed.set_footer(
            text=f"{region.upper()} - updated {round(time.time() - resp.updated)}s ago"
        )
        await interaction.followup.send(embed=embed)

    # --- alias group (reads are public; editing disabled until the
    #     service-token auth path ships, see MISSING_SBUGA_ROUTES.md #2) ---

    # guild-scoped: only ever registered to the support server, so it never appears
    # elsewhere. Not user-installable and not usable in DMs.
    alias = app_commands.Group(
        name="alias",
        description="Song aliases (support server only).",
        guild_ids=[_SUPPORT_GUILD_ID] if _SUPPORT_GUILD_ID else None,
    )

    @alias.command(name="list", description="View a song's aliases.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song)
    @app_commands.describe(song="Song name or ID.")
    async def list_aliases(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        music = converters.match_song(self.bot.pjsk, song)  # type: ignore[arg-type]
        if not music:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        try:
            aliases = await self.bot.sbuga.get_song_aliases()  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch aliases: {e.detail or e.status}"
                )
            )
            return
        names = sorted(a.alias for a in aliases if a.music_id == music.id)
        embed = embeds.embed(
            title=f"Aliases - {music.title}",
            description=(
                "\n".join(f"- `{n}`" for n in names) if names else "No aliases yet."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Song ID {music.id} - {len(names)} aliases")
        await interaction.followup.send(embed=embed)

    @alias.command(name="add", description="Authorized only; add a song alias.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song)
    @app_commands.describe(song="Song name or ID.", alias="Alias to add.")
    async def add_alias(
        self, interaction: discord.Interaction, song: str, alias: str
    ) -> None:
        if await self._deny_alias_edit(interaction):
            return
        await interaction.response.defer(thinking=True)
        music = converters.match_song(self.bot.pjsk, song)  # type: ignore[arg-type]
        if not music:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        target = preprocess(alias)
        if not target:
            await interaction.followup.send(
                embed=embeds.error_embed("That alias is empty after normalisation.")
            )
            return
        try:
            await self.bot.sbuga.add_song_alias(music.id, target)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(embed=self._alias_error_embed(e, target))
            return
        await self.bot.pjsk.refresh_aliases()  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Added alias for `{music.title}` (ID `{music.id}`)\nAlias: `{target}`",
                title="Added alias!",
            )
        )

    @alias.command(name="remove", description="Authorized only; remove a song alias.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song)
    @app_commands.describe(song="Song name or ID.", alias="Alias to remove.")
    async def remove_alias(
        self, interaction: discord.Interaction, song: str, alias: str
    ) -> None:
        if await self._deny_alias_edit(interaction):
            return
        await interaction.response.defer(thinking=True)
        music = converters.match_song(self.bot.pjsk, song)  # type: ignore[arg-type]
        if not music:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        # aliases are stored preprocessed, so normalise the input the same way
        target = preprocess(alias)
        try:
            existing = await self.bot.sbuga.get_song_aliases()  # type: ignore[union-attr]
            match = next(
                (a for a in existing if a.music_id == music.id and a.alias == target),
                None,
            )
            if not match:
                await interaction.followup.send(
                    embed=embeds.error_embed(f"No alias `{target}` on `{music.title}`.")
                )
                return
            await self.bot.sbuga.remove_song_alias(match.id)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't remove alias: {e.detail or e.status}"
                )
            )
            return
        await self.bot.pjsk.refresh_aliases()  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Removed alias for `{music.title}` (ID `{music.id}`)\nAlias: `{target}`",
                title="Removed alias!",
            )
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(InfoCog(bot))
