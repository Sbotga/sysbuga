import glob
import os
from collections import deque

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from data.constants import Constants
from data.pjsk import PJSKData
from database.pool import close_pool, create_pool
from database.queries import UserData
from helpers import embeds
from helpers.autocompletes import Autocompletes, autocompletes
from helpers.cache import CACHE
from helpers.config_loader import Config, get_config, set_config_path
from helpers.emojis import emojis
from helpers.logging import LOGGING
from services.sbuga import SbugaClient

COGS_DIR = "cogs"


class SbugaBot(commands.Bot):
    def __init__(self, config: Config, **kwargs) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.restarting = False

        self.COLORS = LOGGING.COLORS
        self.print = LOGGING.print
        self.info = LOGGING.infoprint
        self.warn = LOGGING.warnprint
        self.error = LOGGING.errorprint
        self.success = LOGGING.successprint
        self.traceback = LOGGING.tracebackprint
        self.cache = CACHE
        self.cache.discord_bans = {}
        self.cache.executed_commands = deque()

        self.db: asyncpg.Pool | None = None
        self.user_data: UserData | None = None
        self.sbuga: SbugaClient | None = None
        self.pjsk: PJSKData | None = None
        self.constants: Constants | None = None
        self.autocompletes: Autocompletes = autocompletes
        self.app_commands: list[discord.app_commands.AppCommand] = []

    async def setup_hook(self) -> None:
        self.db = await create_pool()
        self.user_data = UserData(self.db)

        scfg = self.config["sbuga"]
        self.sbuga = SbugaClient(
            scfg["api_url"],
            image_type=scfg["image_type"],  # type: ignore[arg-type]
            bot_token=scfg["bot_token"],
        )
        self.pjsk = PJSKData(
            self.sbuga,
            scfg["regions"],
            refresh_interval=scfg.get("refresh_interval", 300),
            asset_base_url=scfg.get("asset_base_url", ""),
        )
        self.constants = Constants(self.pjsk)
        self.autocompletes.pjsk = self.pjsk
        await self.pjsk.start()
        try:
            await self.constants.update()
        except Exception as e:
            self.warn(f"Constants initial fetch failed: {e}")

        try:
            await emojis.load(self)
        except Exception as e:
            self.warn(f"Emoji load failed: {e}")

        await self._load_cogs()

    async def _load_cogs(self) -> None:
        for path in sorted(glob.glob(os.path.join(COGS_DIR, "*.py"))):
            name = os.path.splitext(os.path.basename(path))[0]
            if name == "__init__":
                continue
            try:
                await self.load_extension(f"{COGS_DIR}.{name}")
                self.print(
                    f"{self.COLORS.cog_logs}[COGS] {self.COLORS.normal_message}"
                    f"Loaded cog {self.COLORS.item_name}{name}"
                )
            except Exception as e:
                self.traceback(e)

    async def on_ready(self) -> None:
        assert self.user is not None
        self.print(f"Discord | Logged in as {self.user} ({self.user.id})")

    async def close(self) -> None:
        if self.pjsk:
            await self.pjsk.stop()
        if self.sbuga:
            await self.sbuga.close()
        await close_pool()
        await super().close()


async def _on_tree_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, (app_commands.CommandNotFound, discord.errors.NotFound)):
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"Command is on cooldown! Try again in **{error.retry_after:.2f}**s.",
            ephemeral=True,
        )
        return
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You're missing permissions to use that.", ephemeral=True
        )
        return
    em = embeds.error_embed(f"Something went wrong!\n```{error}```")
    try:
        await interaction.edit_original_response(embed=em)
    except discord.HTTPException:
        try:
            await interaction.followup.send(embed=em, ephemeral=True)
        except discord.HTTPException:
            pass
    raise error


def build_bot(config: Config) -> SbugaBot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = SbugaBot(
        config,
        command_prefix=commands.when_mentioned,
        help_command=None,
        intents=intents,
        owner_ids=set(config["discord"]["owner_ids"]),
    )

    async def _ban_check(interaction: discord.Interaction) -> bool:
        if bot.user_data is None:
            return True
        uid = interaction.user.id
        if uid in bot.cache.discord_bans:
            banned = bot.cache.discord_bans[uid]
        else:
            banned = await bot.user_data.get_banned(uid)
            bot.cache.discord_bans[uid] = banned
        if banned:
            try:
                await interaction.response.send_message(
                    embed=embeds.error_embed(
                        "You're banned from the bot. Join the support server to appeal."
                    ),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        return not banned

    bot.tree.interaction_check = _ban_check
    bot.tree.on_error = _on_tree_error
    return bot


def main() -> None:
    set_config_path("config.yml")
    config = get_config()
    bot = build_bot(config)
    bot.run(config["discord"]["token"])


if __name__ == "__main__":
    main()
