import secrets
import string
from typing import Any

import discord
from discord.ext import commands


def generate_secure_string(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def escape_md(text: str, markdown: bool = True, mentions: bool = True) -> str:
    if markdown:
        text = discord.utils.escape_markdown(text)
    if mentions:
        text = discord.utils.escape_mentions(text).replace("<#", "<#​")
    return text


def command_mention(bot: commands.Bot, name: str) -> str | None:
    """Get a command mention from its name, refreshing the bot's id cache."""
    app_commands: list[discord.app_commands.AppCommand] = bot.app_commands  # type: ignore[attr-defined]
    for cmd in app_commands:
        if cmd.guild_id is None:
            entry: Any = bot.tree._global_commands[cmd.name]
        else:
            entry = bot.tree._guild_commands[cmd.guild_id][cmd.name]
        entry.id = cmd.id
    for cmd in bot.tree.get_commands():
        resolved: Any = cmd
        if resolved.qualified_name == name:
            return f"</{resolved.qualified_name}:{resolved.id}>"
    return None
