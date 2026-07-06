import discord

from helpers.config_loader import get_config


class SbugaEmbed(discord.Embed):
    def set_footer(self, *, text: str | None = None, icon_url: str | None = None):
        name = get_config()["discord"]["name"]
        return super().set_footer(text=f"{name} " + (text or ""), icon_url=icon_url)


def embed(*args, **kwargs) -> SbugaEmbed:
    if len(args) == 1:
        kwargs["description"] = args[0]
        args = ()
    em = SbugaEmbed(*args, **kwargs)
    em.timestamp = discord.utils.utcnow()
    em.set_footer(text="")
    return em


def error_embed(
    description: str, title: str | None = None, color: discord.Color | None = None
) -> SbugaEmbed:
    return embed(
        title="❌ Error" if not title else f"❌ {title}",
        description=description,
        color=color or discord.Color.red(),
    )


def success_embed(
    description: str, title: str | None = None, color: discord.Color | None = None
) -> SbugaEmbed:
    return embed(
        title="✅ Success" if not title else f"✅ {title}",
        description=description,
        color=color or discord.Color.green(),
    )


def warn_embed(
    description: str, title: str | None = None, color: discord.Color | None = None
) -> SbugaEmbed:
    return embed(
        title="⚠️ Warning" if not title else f"⚠️ {title}",
        description=description,
        color=color or discord.Color.orange(),
    )
