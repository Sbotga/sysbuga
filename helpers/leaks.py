"""helpers for the per-guild allow-leaks behavior

when a server allows leaks we don't block leaked content, we show it spoilered: every text
line wrapped in spoiler bars, file attachments flagged as spoilers (SPOILER_ prefix), and a
mikuleek notice on top
"""

from __future__ import annotations

from helpers.emojis import emojis


def leak_notice() -> str:
    return f"{emojis.mikuleek} **This is a leak!**"


def spoiler_text(text: str) -> str:
    """wrap each non-blank line in spoiler bars so the whole thing is hidden until clicked"""
    return "\n".join(
        f"||{line}||" if line.strip() else line for line in text.split("\n")
    )
