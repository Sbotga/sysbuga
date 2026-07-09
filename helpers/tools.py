import secrets
import string

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


def command_mention(bot: commands.Bot, name: str) -> str:
    """A clickable command mention (`</name:id>`) for a qualified command name.

    Subcommands mention with their *root* command's id, so only the root needs
    looking up. `bot.app_commands` is only populated once the tree has been synced
    or fetched, so fall back to a plain `/name` rather than raising.
    """
    root = name.split(" ", 1)[0]
    synced: list[discord.app_commands.AppCommand] = getattr(bot, "app_commands", [])
    for cmd in synced:
        if cmd.name == root:
            return f"</{name}:{cmd.id}>"
    return f"/{name}"
