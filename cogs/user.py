from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds, tools
from helpers.autocompletes import autocompletes
from helpers.views import SbugaView
from services.sbuga import SbugaError, SbugaNotFound

if TYPE_CHECKING:
    from main import SbugaBot

PJSK_REGIONS = ["en", "jp", "tw", "kr"]
SETTING_NAMES = {
    "default_region": "Default Region",
    "default_difficulty": "Default Difficulty",
    "mirror_charts_by_default": "Mirror Charts by Default",
}
SETTING_OPTIONS = {
    "default_region": ["EN", "JP", "TW", "KR"],
    "default_difficulty": ["Master", "Expert", "Hard", "Normal", "Easy"],
}
SETTING_DESCRIPTIONS = {
    "mirror_charts_by_default": "Applies to guessing as well!",
    "default_difficulty": "Does NOT include Append — not every song has an Append chart.",
}
IGNORE_SETTINGS = ["first_time_guess_end"]


def _joined_line(user: dict, region: str) -> str:
    if region not in ("en", "jp"):
        return ""
    ts = (int(format(user["userId"], "064b")[:42], 2) + 1600218000000) // 1000
    return f"**Joined:** <t:{ts}:R>\n"


class UserCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    async def _resolve_region(
        self, interaction: discord.Interaction, region: str
    ) -> str | None:
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(  # type: ignore[union-attr]
                interaction.user.id, "default_region"
            )
        if region not in PJSK_REGIONS:
            await interaction.response.send_message(
                embed=embeds.error_embed(f"Region `{region.upper()}` isn't supported."),
                ephemeral=True,
            )
            return None
        return region

    user = app_commands.Group(
        name="user",
        description="User account settings.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )
    user_pjsk = app_commands.Group(
        name="pjsk", description="PJSK account linking.", parent=user
    )

    @user_pjsk.command(name="link", description="Link your PJSK account.")
    @app_commands.autocomplete(region=autocompletes.pjsk_region(PJSK_REGIONS))
    @app_commands.describe(region="Game server region.")
    async def link(
        self, interaction: discord.Interaction, region: str = "default"
    ) -> None:
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        if await self.bot.user_data.get_pjsk_id(interaction.user.id, resolved):  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"You're already linked to a PJSK {resolved.upper()} account. Alt accounts aren't supported."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(UserIDModal(self.bot, resolved))

    @user_pjsk.command(name="unlink", description="Unlink your PJSK account.")
    @app_commands.autocomplete(region=autocompletes.pjsk_region(PJSK_REGIONS))
    @app_commands.describe(region="Game server region.")
    async def unlink(
        self, interaction: discord.Interaction, region: str = "default"
    ) -> None:
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        if not await self.bot.user_data.get_pjsk_id(interaction.user.id, resolved):  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"You aren't linked to a PJSK {resolved.upper()} account."
                ),
                ephemeral=True,
            )
            return
        await self.bot.user_data.remove_pjsk_id(interaction.user.id, resolved)  # type: ignore[union-attr]
        await interaction.response.send_message(
            embed=embeds.success_embed(
                f"Unlinked your PJSK {resolved.upper()} account.",
                title="Unlink Success",
            )
        )

    @user_pjsk.command(name="accounts", description="View your linked PJSK accounts.")
    async def accounts(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        embed = embeds.embed(
            title="Your PJSK Linked Accounts", color=discord.Color.blurple()
        )
        lines = []
        for region in ["en", "jp", "tw", "kr", "cn"]:
            pjsk_id = await self.bot.user_data.get_pjsk_id(interaction.user.id, region)  # type: ignore[union-attr]
            lines.append(
                f"**{region.upper()}:** {'`' + str(pjsk_id) + '`' if pjsk_id else 'Not Linked'}"
            )
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)

    @user.command(name="settings", description="Change your bot settings.")
    async def settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        owner_id = interaction.user.id

        def to_readable(value: Any) -> str:
            if isinstance(value, bool):
                return str(value)
            if isinstance(value, (int, float)):
                return f"{value:,}"
            return str(value).upper()

        async def generate_setting(
            key: str, value: Any
        ) -> tuple[discord.Embed, SbugaView]:
            embed = embeds.embed(
                title=f"{SETTING_NAMES[key]} Setting",
                description=f"Currently set to `{to_readable(value)}`."
                + (
                    f"\n\n{SETTING_DESCRIPTIONS[key]}"
                    if key in SETTING_DESCRIPTIONS
                    else ""
                ),
                color=discord.Color.dark_gold(),
            )
            view = SbugaView(restrict_to=owner_id)
            view.add_item(_setting_picker())
            if isinstance(value, bool):
                view.add_item(
                    ToggleButton(self.bot, key, value, owner_id, generate_setting)
                )
            else:
                view.add_item(
                    ValueSelect(
                        self.bot, key, SETTING_OPTIONS[key], owner_id, generate_setting
                    )
                )
            return embed, view

        def _setting_picker() -> "PickSettingSelect":
            settings_now = SETTING_NAMES
            options = {SETTING_NAMES[k]: k for k in settings_now if k in SETTING_NAMES}
            return PickSettingSelect(self.bot, options, owner_id, generate_setting)

        embed = embeds.embed(
            title="Changing Settings",
            description="Select the setting you'd like to change.",
            color=discord.Color.blue(),
        )
        view = SbugaView(restrict_to=owner_id)
        view.add_item(_setting_picker())
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()


class UserIDModal(discord.ui.Modal, title="PJSK User ID"):
    def __init__(self, bot: SbugaBot, region: str) -> None:
        super().__init__()
        self.bot = bot
        self.region = region
        self.pjsk_id: discord.ui.TextInput = discord.ui.TextInput(
            label="PJSK User ID",
            placeholder=f"Your PJSK user ID for {region.upper()}",
            required=True,
        )
        self.add_item(self.pjsk_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=embeds.embed("Please wait..."))
        msg = await interaction.original_response()
        user_id = self.pjsk_id.value.strip()
        if (
            not user_id.isdigit()
            or not 10_000_000 < int(user_id) < 10_000_000_000_000_000_000
        ):
            await msg.edit(embed=embeds.error_embed("Invalid user ID."))
            return

        existing = await self.bot.user_data.get_discord_user_id_from_pjsk_id(int(user_id), self.region)  # type: ignore[union-attr]
        if existing:
            await msg.edit(
                embed=embeds.error_embed(
                    "This PJSK account is already linked.\n-# Lost your Discord account? Contact support."
                )
            )
            return

        try:
            resp = await self.bot.sbuga.get_profile(int(user_id), self.region, fresh=True)  # type: ignore[union-attr]
        except SbugaNotFound:
            await msg.edit(
                embed=embeds.error_embed(
                    f"Couldn't find that profile. Is the account on the {self.region.upper()} server, and the ID valid?"
                )
            )
            return
        except SbugaError as e:
            await msg.edit(
                embed=embeds.error_embed(
                    f"Couldn't fetch your profile: {e.detail or e.status}"
                )
            )
            return

        data = resp.profile
        link_code = "sbuga_" + tools.generate_secure_string(7)
        embed = embeds.embed(
            title="Linking to " + data["user"]["name"],
            description=(
                f"**User ID:** `{data['user']['userId']}`\n"
                f"{_joined_line(data['user'], self.region)}"
                f"**Rank:** **`🎵 {data['user']['rank']}`**\n\n"
                f"**Bio**\n```{data['userProfile'].get('word') or 'No Bio'}```\n"
                "### ℹ️ Press the button after setting your bio. Slow wifi may need a few seconds."
            ),
            color=discord.Color.dark_magenta(),
        )
        embed.add_field(
            name="To Link",
            value=f"Set your **PJSK** bio (`Comment`) to this code and click the button within 5 minutes.\n```\n{link_code}\n```",
            inline=False,
        )
        embed.set_footer(
            text=f"{self.region.upper()} - updated {round(time.time() - resp.updated)}s ago"
        )
        view = LinkCheckView(
            self.bot, link_code, int(user_id), self.region, interaction.user.id
        )
        await msg.edit(embed=embed, view=view)
        view.message = msg


class LinkCheckView(SbugaView):
    def __init__(
        self, bot: SbugaBot, link_code: str, pjsk_id: int, region: str, owner_id: int
    ) -> None:
        super().__init__(timeout=300, restrict_to=owner_id)
        self.bot = bot
        self.link_code = link_code
        self.pjsk_id = pjsk_id
        self.region = region

    @discord.ui.button(label="Link Account", style=discord.ButtonStyle.success)
    async def link(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        button.disabled = True
        try:
            resp = await self.bot.sbuga.get_profile(self.pjsk_id, self.region, fresh=True)  # type: ignore[union-attr]
        except SbugaError:
            await interaction.followup.edit_message(
                self.message.id if self.message else interaction.message.id,  # type: ignore[union-attr]
                embed=embeds.error_embed(
                    "Couldn't fetch your profile; please try again."
                ),
                view=self,
            )
            return

        data = resp.profile
        bio = data["userProfile"].get("word")
        if bio == self.link_code:
            await self.bot.user_data.update_pjsk_id(interaction.user.id, self.pjsk_id, self.region)  # type: ignore[union-attr]
            embed = embeds.success_embed(
                title="Link Success",
                description=(
                    f"Linked your PJSK {self.region.upper()} account!\n\n"
                    f"**Name:** {data['user']['name']}\n"
                    f"**User ID:** `{data['user']['userId']}`\n"
                    f"{_joined_line(data['user'], self.region)}"
                    f"**Rank:** **`🎵 {data['user']['rank']}`**\n\n"
                    f"**Bio**\n```{bio or 'No Bio'}```"
                ),
            )
        else:
            embed = embeds.error_embed(
                title="Link Failed",
                description=(
                    f"**{data['user']['name']}**'s bio isn't `{self.link_code}`. Try again.\n\n"
                    f"**Current Bio**\n```\n{bio or 'No Bio'}\n```"
                ),
            )
        await interaction.followup.edit_message(
            self.message.id if self.message else interaction.message.id,  # type: ignore[union-attr]
            embed=embed,
            view=self,
        )


class PickSettingSelect(discord.ui.Select):
    def __init__(
        self, bot: SbugaBot, options: dict[str, str], owner_id: int, generate
    ) -> None:
        super().__init__(
            placeholder="Select a setting...",
            options=[
                discord.SelectOption(label=name, value=key)
                for name, key in options.items()
            ],
        )
        self.bot = bot
        self.owner_id = owner_id
        self.generate = generate

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        key = self.values[0]
        settings = await self.bot.user_data.get_settings(interaction.user.id)  # type: ignore[union-attr]
        embed, view = await self.generate(key, settings[key])
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=view)  # type: ignore[union-attr]
        view.message = interaction.message


class ToggleButton(discord.ui.Button):
    def __init__(
        self, bot: SbugaBot, key: str, value: bool, owner_id: int, generate
    ) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary, label=f"Change to {not value}"
        )
        self.bot = bot
        self.key = key
        self.value = value
        self.generate = generate

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        settings = await self.bot.user_data.change_settings(interaction.user.id, self.key, not self.value)  # type: ignore[union-attr]
        embed, view = await self.generate(self.key, settings[self.key])
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=view)  # type: ignore[union-attr]
        view.message = interaction.message
        await interaction.followup.send(
            embed=embeds.success_embed("Setting changed."), ephemeral=True
        )


class ValueSelect(discord.ui.Select):
    def __init__(
        self, bot: SbugaBot, key: str, options: list[str], owner_id: int, generate
    ) -> None:
        super().__init__(
            placeholder="Choose a value...",
            options=[discord.SelectOption(label=o) for o in options],
        )
        self.bot = bot
        self.key = key
        self.generate = generate

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        settings = await self.bot.user_data.change_settings(  # type: ignore[union-attr]
            interaction.user.id, self.key, self.values[0].lower()
        )
        embed, view = await self.generate(self.key, settings[self.key])
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=view)  # type: ignore[union-attr]
        view.message = interaction.message
        await interaction.followup.send(
            embed=embeds.success_embed("Setting changed."), ephemeral=True
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(UserCog(bot))
