from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands, tasks

if TYPE_CHECKING:
    from main import SbugaBot


class Tasks(commands.Cog):
    """Slimmed background tasks: periodic 39s constants refresh.

    Master-data version polling lives in data/pjsk.py's own loop.
    """

    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot
        self.constants_refresh.start()

    async def cog_unload(self) -> None:
        self.constants_refresh.cancel()

    @tasks.loop(hours=1)
    async def constants_refresh(self) -> None:
        if self.bot.constants:
            try:
                await self.bot.constants.update()
            except Exception as e:
                self.bot.warn(f"Constants refresh failed: {e}")

    @constants_refresh.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(Tasks(bot))
