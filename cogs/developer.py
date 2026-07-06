from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from helpers import embeds

if TYPE_CHECKING:
    from main import SbugaBot

REGIONS = ["en", "jp", "tw", "kr", "cn"]


class DevCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.author.id in (self.bot.owner_ids or set())

    @commands.command()
    async def eval(self, ctx: commands.Context) -> None:
        cmd = ctx.message.content.split("\n")
        del cmd[0]
        if cmd and cmd[-1] == "```":
            del cmd[-1]
        if cmd:
            del cmd[0]
        code = "\n".join(cmd)
        try:
            indented = "".join(f"\n    {line}" for line in code.split("\n")).strip()
            exec(f"async def __ex(ctx):\n    {indented}")
            await locals()["__ex"](ctx)
        except Exception as e:
            result = "".join(traceback.format_exception(e)).replace("`", "\\`")
            await ctx.reply(f"**Eval failed:**\n```python\n{result}\n```")
            await ctx.message.add_reaction("❌")
        else:
            await ctx.message.add_reaction("✅")

    @commands.command()
    async def prepare_restart(self, ctx: commands.Context) -> None:
        self.bot.restarting = True
        await ctx.reply(
            embed=embeds.embed("Ok. (give it a moment before the actual restart)")
        )

    @commands.command()
    async def cancel_restart(self, ctx: commands.Context) -> None:
        self.bot.restarting = False
        await ctx.reply(embed=embeds.embed("Ok.."))

    @commands.command()
    async def sync(self, ctx: commands.Context, guild: int | None = None) -> None:
        msg = await ctx.reply("Hold on...")
        if guild:
            cmds = await self.bot.tree.sync(guild=discord.Object(id=guild))
            self.bot.app_commands.extend(cmds)
            await msg.edit(content=f"Synced {len(cmds)} commands to guild {guild}.")
        else:
            self.bot.app_commands = await self.bot.tree.sync()
            await msg.edit(
                content=f"Synced {len(self.bot.app_commands)} global commands."
            )

    @commands.command()
    async def reload(self, ctx: commands.Context, cog: str) -> None:
        await self._extension_action(ctx, self.bot.reload_extension, cog, "Reload")

    @commands.command()
    async def load(self, ctx: commands.Context, cog: str) -> None:
        await self._extension_action(ctx, self.bot.load_extension, cog, "Load")

    @commands.command()
    async def unload(self, ctx: commands.Context, cog: str) -> None:
        await self._extension_action(ctx, self.bot.unload_extension, cog, "Unload")

    async def _extension_action(
        self, ctx: commands.Context, action, cog: str, verb: str
    ) -> None:
        try:
            await action(f"cogs.{cog}")
            await ctx.reply(embed=embeds.success_embed(f"{verb}ed `{cog}`!"))
        except Exception as e:
            await ctx.reply(
                embed=embeds.error_embed(f"{verb} failed for `{cog}`.\n```{e}```")
            )

    @commands.command()
    async def refresh(self, ctx: commands.Context) -> None:
        await ctx.reply("Refreshing PJSK data + constants from sbuga...")
        try:
            assert self.bot.pjsk and self.bot.constants
            await self.bot.pjsk.refresh(force=True)
            await self.bot.constants.update()
            await ctx.reply(
                embed=embeds.success_embed(
                    "Refreshed data!", title="Refresh Successful"
                )
            )
        except Exception as e:
            await ctx.reply(
                embed=embeds.error_embed(
                    f"Failed to refresh.\n```{e}```", title="Refresh Failed"
                )
            )

    @commands.command()
    async def ban(self, ctx: commands.Context, user: discord.User) -> None:
        await self.bot.user_data.set_banned(user.id, True)  # type: ignore[union-attr]
        self.bot.cache.discord_bans.pop(user.id, None)
        await ctx.reply(embed=embeds.embed(f"Banned {user.display_name}."))

    @commands.command()
    async def unban(self, ctx: commands.Context, user: discord.User) -> None:
        await self.bot.user_data.set_banned(user.id, False)  # type: ignore[union-attr]
        self.bot.cache.discord_bans.pop(user.id, None)
        await ctx.reply(embed=embeds.embed(f"Unbanned {user.display_name}."))

    @commands.command(name="accounts")
    async def accounts_dev(self, ctx: commands.Context, user_id: int) -> None:
        embed = discord.Embed(
            title="PJSK Linked Accounts", color=discord.Color.blurple()
        )
        lines = []
        for region in REGIONS:
            pjsk_id = await self.bot.user_data.get_pjsk_id(user_id, region)  # type: ignore[union-attr]
            lines.append(
                f"**{region.upper()}:** {'`' + str(pjsk_id) + '`' if pjsk_id else 'Not Linked'}"
            )
        embed.description = "\n".join(lines)
        await ctx.reply(embed=embed)

    @commands.command()
    async def dev_link(
        self, ctx: commands.Context, user: discord.User, user_id: int, region: str
    ) -> None:
        await self.bot.user_data.update_pjsk_id(user.id, int(user_id), region.lower())  # type: ignore[union-attr]
        await ctx.reply(
            embed=embeds.success_embed(
                f"Linked {user.display_name} → `{user_id}` ({region.upper()})."
            )
        )

    @commands.command()
    async def guess_reset(
        self, ctx: commands.Context, user: discord.User, key: str, stat: str
    ) -> None:
        try:
            await self.bot.user_data.reset_guesses(user.id, key, None if stat == "all" else stat)  # type: ignore[union-attr]
            await ctx.reply(
                embed=embeds.success_embed(f"Reset guess stats. Key: `{stat}`")
            )
        except Exception as e:
            await ctx.reply(embed=embeds.error_embed(f"Failed to reset.\n```{e}```"))


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(DevCog(bot))
