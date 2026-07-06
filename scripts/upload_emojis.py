"""Upload data/assets/emojis/* as application emojis and export their ids.

    python -m scripts.upload_emojis

Emoji name = file stem (e.g. normal_ap.png -> :normal_ap:). Files already
uploaded (matched by name) are skipped. All application emojis are then
exported to data/emojis.json, which helpers/emojis.py reads at startup.
Re-run after adding new images to data/assets/emojis/.
"""

import asyncio
import json
from pathlib import Path

import discord

from helpers.config_loader import get_config

EMOJI_DIR = Path("data/assets/emojis")
EXPORT_PATH = Path("data/emojis.json")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif")


async def main() -> None:
    token: str = get_config()["discord"]["token"]
    client = discord.Client(intents=discord.Intents.none())
    await client.login(token)
    try:
        existing: dict[str, discord.Emoji] = {
            e.name: e for e in await client.fetch_application_emojis()
        }
        uploaded = 0
        skipped = 0
        failed = 0
        for path in sorted(EMOJI_DIR.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            name = path.stem
            if name in existing:
                skipped += 1
                continue
            try:
                emoji = await client.create_application_emoji(
                    name=name, image=path.read_bytes()
                )
                existing[name] = emoji
                uploaded += 1
                print(f"[upload_emojis] uploaded {name} -> {emoji.id}")
            except discord.HTTPException as e:
                failed += 1
                print(f"[upload_emojis] {name} failed: {e} (max 256KB, 2-32 char name)")

        export = {
            name: {
                "id": str(emoji.id),
                "animated": emoji.animated,
                "mention": f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>",
            }
            for name, emoji in sorted(existing.items())
        }
        EXPORT_PATH.write_text(json.dumps(export, indent=2) + "\n", encoding="utf-8")
        print(
            f"[upload_emojis] uploaded={uploaded} skipped={skipped} failed={failed} "
            f"exported={len(export)} -> {EXPORT_PATH}"
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
