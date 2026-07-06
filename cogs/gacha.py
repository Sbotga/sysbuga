from __future__ import annotations

import asyncio
import io
import random
import time
from typing import TYPE_CHECKING

import aiohttp
import discord
from PIL import Image
from discord import app_commands
from discord.ext import commands

from data.models import Card, Gacha
from helpers import embeds, unblock
from helpers.autocompletes import autocompletes

if TYPE_CHECKING:
    from main import SbugaBot

GACHA_REGIONS = ["en", "jp", "tw", "kr"]
COOLDOWN = 20

ASSETS = "data/assets"
STAR_FILES = {
    "trained": f"{ASSETS}/emojis/rarity_star_afterTraining.png",
    "untrained": f"{ASSETS}/emojis/rarity_star_normal.png",
    "birthday": f"{ASSETS}/emojis/rarity_birthday.png",
}
STAR_POSITIONS = [21, 78, 134, 190]
RARITY_STAR_COUNT = {
    "rarity_1": 1,
    "rarity_2": 2,
    "rarity_3": 3,
    "rarity_4": 4,
    "rarity_birthday": 1,
}


def _card_thumbnail(cutout: bytes, rarity: str, attr: str | None) -> Image.Image:
    """One 338x338 gacha result tile: masked cutout + rarity frame + stars + attr."""
    pic = Image.new("RGBA", (338, 338), (0, 0, 0, 0))
    mask_img = Image.open(f"{ASSETS}/image_gen/gachacardmask.png").convert("RGBA")
    mask = mask_img.split()[3]
    art = Image.open(io.BytesIO(cutout)).convert("RGBA")
    art = art.resize(mask_img.size, Image.Resampling.LANCZOS)
    pic.paste(art, (0, 0), mask)

    frame = (
        Image.open(f"{ASSETS}/chara/cardFrame_{rarity}.png")
        .convert("RGBA")
        .resize((338, 338))
    )
    pic.paste(frame, (0, 0), frame.split()[3])

    star_file = (
        STAR_FILES["birthday"]
        if rarity == "rarity_birthday"
        else STAR_FILES["untrained"]
    )
    star = Image.open(star_file).convert("RGBA").resize((60, 60))
    star_mask = star.split()[3]
    for i in range(RARITY_STAR_COUNT.get(rarity, 1)):
        pic.paste(star, (STAR_POSITIONS[i], 256), star_mask)

    if attr:
        icon = (
            Image.open(f"{ASSETS}/emojis/icon_attribute_{attr}.png")
            .convert("RGBA")
            .resize((76, 76))
        )
        pic.paste(icon, (1, 1), icon.split()[3])
    return pic


def _compose_ten_pull(cards: list[tuple[bytes, str, str | None]]) -> io.BytesIO:
    """The old Sbotga gacha screen: 2 rows of 5 tiles on the gacha background."""
    pic = Image.open(f"{ASSETS}/image_gen/gacha.png").convert("RGBA")
    cover = Image.new("RGB", (1550, 600), (255, 255, 255))
    pic.paste(cover, (314, 500))
    for i, (cutout, rarity, attr) in enumerate(cards[:10]):
        thumb = _card_thumbnail(cutout, rarity, attr).resize((263, 263))
        x = 336 + 304 * (i % 5)
        y = 520 if i < 5 else 825
        pic.paste(thumb, (x, y), thumb.split()[3])
    out = io.BytesIO()
    pic.convert("RGB").save(out, "JPEG")
    out.seek(0)
    return out


class GachaCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot
        self.cooldowns: dict[int, float] = {}

    def _current_gacha(self, region: str) -> Gacha | None:
        gachas = self.bot.pjsk.gachas(region)  # type: ignore[union-attr]
        if not gachas:
            return None
        now = int(time.time() * 1000)
        current = [g for g in gachas if (g.start_at or 0) <= now <= (g.end_at or 0)]
        return current[0] if current else max(gachas, key=lambda g: g.start_at or 0)

    def _simulate(self, gacha: Gacha) -> list[Card]:
        rates = {r.card_rarity_type: r.rate for r in gacha.rarity_rates}
        rate4 = rates.get("rarity_4", rates.get("rarity_birthday", 3.0))
        rate3 = rates.get("rarity_3", 12.0)

        by_rarity: dict[str, list[int]] = {}
        for cid in gacha.pool_card_ids:
            card = self.bot.pjsk.get_card(cid)  # type: ignore[union-attr]
            if card:
                by_rarity.setdefault(card.card_rarity_type, []).append(cid)
        pickup = set(gacha.pickup_card_ids)

        def pick(rarity: str) -> Card | None:
            pool = by_rarity.get(rarity) or by_rarity.get("rarity_birthday")
            if not pool:
                return None
            weighted = [c for c in pool if c in pickup] * 3 + pool
            return self.bot.pjsk.get_card(random.choice(weighted))  # type: ignore[union-attr]

        results: list[Card] = []
        for i in range(1, 11):
            roll = random.uniform(0, 100)
            if i == 10 and roll >= rate4 + rate3:
                roll = random.uniform(
                    0, rate4 + rate3
                )  # 10th-pull pity: guaranteed 3★+
            if roll < rate4:
                rarity = "rarity_4" if "rarity_4" in by_rarity else "rarity_birthday"
            elif roll < rate4 + rate3:
                rarity = "rarity_3"
            else:
                rarity = "rarity_2"
            picked = pick(rarity)
            if picked:
                results.append(picked)
        return results

    async def _pull_image(self, cards: list[Card]) -> discord.File | None:
        """Compose the ten-pull screen; None if any cutout can't be fetched."""
        urls = [c.cutout_url_normal for c in cards]
        if not all(urls):
            return None
        try:
            async with aiohttp.ClientSession() as cs:

                async def fetch(url: str) -> bytes:
                    async with cs.get(url) as resp:
                        resp.raise_for_status()
                        return await resp.read()

                cutouts = await asyncio.gather(*(fetch(u) for u in urls))  # type: ignore[arg-type]
            payload = [
                (data, c.card_rarity_type, c.attr) for data, c in zip(cutouts, cards)
            ]
            buf = await unblock.to_process_with_timeout(_compose_ten_pull, payload)
            return discord.File(buf, "gacha.jpg")
        except Exception as e:
            self.bot.warn(f"gacha image failed: {e}")
            return None

    @app_commands.command(
        name="gacha", description="Simulate a ten-pull on the current banner."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(GACHA_REGIONS))
    @app_commands.describe(region="Game server region.")
    async def gacha(
        self, interaction: discord.Interaction, region: str = "default"
    ) -> None:
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(interaction.user.id, "default_region")  # type: ignore[union-attr]
        if region not in GACHA_REGIONS:
            region = "en"

        cooldown_end = self.cooldowns.get(interaction.user.id, 0) + COOLDOWN
        if cooldown_end > time.time():
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"You recently pulled. Try again <t:{int(cooldown_end)}:R>."
                ),
                ephemeral=True,
            )
            return
        self.cooldowns[interaction.user.id] = time.time()

        await interaction.response.defer(thinking=True)
        gacha = self._current_gacha(region)
        if not gacha:
            await interaction.followup.send(
                embed=embeds.error_embed("No gacha banner data is available right now.")
            )
            return

        cards = self._simulate(gacha)
        if not cards:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't simulate this banner.")
            )
            return

        pickup = set(gacha.pickup_card_ids)
        lines = [
            self.bot.pjsk.card_display_name(c, use_emojis=True)  # type: ignore[union-attr]
            + (" [pickup]" if c.id in pickup else "")
            for c in cards
        ]
        embed = embeds.embed(
            title=f"Ten Pull - {gacha.name}", description="\n".join(lines)
        )
        file = await self._pull_image(cards)
        if file:
            embed.set_image(url="attachment://gacha.jpg")
        elif gacha.banner_url:
            embed.set_image(url=gacha.banner_url)
        embed.set_footer(text=f"{region.upper()} Current Gacha")
        await interaction.followup.send(embed=embed, file=file or discord.utils.MISSING)


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(GachaCog(bot))
