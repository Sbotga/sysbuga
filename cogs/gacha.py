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

# the 3rd-anniversary result screen debuted with song "NEO" (music id 366), which released on
# a different date per region - banners from that date on use the 3rd-anni style, earlier ones 1st
_NEO_MUSIC_ID = 366

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
    mask_img = Image.open(f"{ASSETS}/image_gen/gacha_card_mask_1st_anni.png").convert(
        "RGBA"
    )
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


def _compose_ten_pull_1st_anni(
    cards: list[tuple[bytes, str, str | None]],
) -> io.BytesIO:
    """1st anniversary gacha screen: 2 rows of 5 tiles on the 1st-anni background.
    The bg was cropped 300px off top and bottom, so tile/cover y-offsets are shifted up 300.
    """
    pic = Image.open(f"{ASSETS}/image_gen/gacha_bg_1st_anni.png").convert("RGBA")
    cover = Image.new("RGB", (1550, 600), (255, 255, 255))
    pic.paste(cover, (314, 200))
    for i, (cutout, rarity, attr) in enumerate(cards[:10]):
        thumb = _card_thumbnail(cutout, rarity, attr).resize((263, 263))
        x = 336 + 304 * (i % 5)
        y = 220 if i < 5 else 525
        pic.paste(thumb, (x, y), thumb.split()[3])
    out = io.BytesIO()
    pic.convert("RGB").save(out, "JPEG")
    out.seek(0)
    return out


# 3rd anniversary layout: a 5x2 grid on the blurred base. The spec is a height-locked 1080p
# reference (the base is 1.75:1, so the frame is 1890x1080) scaled onto the 2520x1440 asset by
# 4/3, with screen center at the image center. Card centers are (x, y), Y-up from that center.
_ANNI3_CELL = (320, 180)  # reference-unit cell size
_ANNI3_CENTERS = [
    (-672, 122),
    (-336, 122),
    (0, 122),
    (336, 122),
    (672, 122),
    (-672, -122),
    (-336, -122),
    (0, -122),
    (336, -122),
    (672, -122),
]

# each tile is built at the game's native 940x530 (UIPartsCardThumbnailXL) then scaled to the cell
_ANNI3_ASSETS = f"{ASSETS}/image_gen/gacha_3rd_anni_assets"
_ANNI3_TILE = (940, 530)
_ANNI3_FRAME_SUFFIX = {
    "rarity_1": "1",
    "rarity_2": "2",
    "rarity_3": "3",
    "rarity_4": "4",
    "rarity_birthday": "bd",
}
# top-left of each 55x55 star in the 940x530 tile, bottom star first (Img1..Img4)
_ANNI3_STAR_POS = [(25, 446), (25, 398), (25, 351), (25, 303)]


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """scale `img` to fully cover w x h, then center-crop the overflow."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = max(w, round(iw * scale)), max(h, round(ih * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _build_card_tile_3rd(art: bytes, rarity: str, attr: str | None) -> Image.Image:
    """One 940x530 3rd-anni result tile: cover-fit card illustration under the landscape rarity
    frame, with the attribute icon (top-right) and rarity stars (bottom-left)."""
    w, h = _ANNI3_TILE
    tile = _cover(Image.open(io.BytesIO(art)).convert("RGBA"), w, h)

    suffix = _ANNI3_FRAME_SUFFIX.get(rarity, "4")
    frame = (
        Image.open(f"{_ANNI3_ASSETS}/cardFrame_L_{suffix}.png")
        .convert("RGBA")
        .resize((w, h), Image.Resampling.LANCZOS)
    )
    tile.alpha_composite(frame)

    if attr:
        icon = (
            Image.open(f"{ASSETS}/emojis/icon_attribute_{attr}.png")
            .convert("RGBA")
            .resize((88, 88), Image.Resampling.LANCZOS)
        )
        tile.alpha_composite(icon, (812, 0))

    # gacha pulls are always untrained; birthday cards use the birthday star
    star_file = (
        STAR_FILES["birthday"]
        if rarity == "rarity_birthday"
        else STAR_FILES["untrained"]
    )
    star = (
        Image.open(star_file).convert("RGBA").resize((55, 55), Image.Resampling.LANCZOS)
    )
    for i in range(RARITY_STAR_COUNT.get(rarity, 1)):
        tile.alpha_composite(star, _ANNI3_STAR_POS[i])
    return tile


def _compose_ten_pull_3rd_anni(
    cards: list[tuple[bytes, str, str | None]],
) -> io.BytesIO:
    """3rd anniversary gacha screen: a 5x2 grid of framed 940x530 result tiles scaled into the
    320x180 cells on the blurred 3rd-anni base."""
    pic = Image.open(f"{ASSETS}/image_gen/gacha_bg_3rd_anni_blur.png").convert("RGBA")
    w, h = pic.size
    s = h / 1080  # height-locked 1080p reference -> asset scale (4/3 at 1440)
    ccx, ccy = w / 2, h / 2  # screen center == image center
    cw, ch = round(_ANNI3_CELL[0] * s), round(_ANNI3_CELL[1] * s)
    for (art, rarity, attr), (rx, ry) in zip(cards[:10], _ANNI3_CENTERS):
        tile = _build_card_tile_3rd(art, rarity, attr).resize(
            (cw, ch), Image.Resampling.LANCZOS
        )
        px, py = ccx + rx * s, ccy - ry * s  # Y-up -> image Y-down
        pic.alpha_composite(tile, (round(px - cw / 2), round(py - ch / 2)))
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

    def _get_gacha(self, region: str, banner: str | None) -> Gacha | None:
        """A specific banner by id when given, otherwise the current one."""
        if not banner:
            return self._current_gacha(region)
        banner = banner.strip()
        if banner.isdigit():
            return self.bot.pjsk.get_gacha(int(banner), region)  # type: ignore[union-attr]
        return None

    def _simulate(self, gacha: Gacha, force_four_star: bool = False) -> list[Card]:
        rates = {r.card_rarity_type: r.rate for r in gacha.rarity_rates}

        by_rarity: dict[str, list[int]] = {}
        for cid in gacha.pool_card_ids:
            card = self.bot.pjsk.get_card(cid)  # type: ignore[union-attr]
            if card:
                by_rarity.setdefault(card.card_rarity_type, []).append(cid)
        pickup = set(gacha.pickup_card_ids)

        # the top rarity is 4★, or birthday on a birthday gacha (which has no 4★)
        top = "rarity_4" if by_rarity.get("rarity_4") else "rarity_birthday"
        rate3 = rates.get("rarity_3", 12.0)
        # force_four_star guarantees the top rarity on every pull
        top_rate = 100.0 if force_four_star else rates.get(top, 3.0)

        def pick(rarity: str) -> Card | None:
            pool = by_rarity.get(rarity) or by_rarity.get("rarity_birthday")
            if not pool:
                return None
            weighted = [c for c in pool if c in pickup] * 3 + pool
            return self.bot.pjsk.get_card(random.choice(weighted))  # type: ignore[union-attr]

        results: list[Card] = []
        for i in range(1, 11):
            roll = random.uniform(0, 100)
            if i == 10 and roll >= top_rate + rate3:
                roll = random.uniform(
                    0, top_rate + rate3
                )  # 10th-pull pity: guaranteed 3★+
            if roll < top_rate:
                rarity = top
            elif roll < top_rate + rate3:
                rarity = "rarity_3"
            else:
                rarity = "rarity_2"
            picked = pick(rarity)
            if picked:
                results.append(picked)
        return results

    def _resolve_style(self, region: str, gacha: Gacha, override: str) -> str:
        """The result-screen style, '1st' or '3rd'. `override` forces it; 'auto' picks 3rd for
        banners from NEO's (per-region) release onward, otherwise 1st."""
        if override in ("1st", "3rd"):
            return override
        neo = self.bot.pjsk.region_music(region, _NEO_MUSIC_ID)  # type: ignore[union-attr]
        if neo and (gacha.start_at or 0) >= neo.published_at:
            return "3rd"
        return "1st"

    async def _pull_image(self, cards: list[Card], style: str) -> discord.File | None:
        """Compose the ten-pull screen in `style` ('1st'/'3rd'); None if any card's art can't
        be fetched."""
        try:
            async with aiohttp.ClientSession() as cs:

                async def fetch(url: str | None) -> bytes | None:
                    if not url:
                        return None
                    try:
                        async with cs.get(url) as resp:
                            if resp.status == 200:
                                return await resp.read()
                    except aiohttp.ClientError:
                        pass
                    return None

                async def fetch_art(card: Card) -> bytes | None:
                    if style == "3rd":
                        # 3rd anni tiles show the full untrained card illustration
                        return await fetch(card.card_url_normal)
                    # 1st anni: the near-square member cutout the original Sbotga gacha used,
                    # falling back to the (also square) untrained thumbnail if it's missing.
                    return await fetch(card.cutout_url_normal) or await fetch(
                        card.thumbnail_url_normal
                    )

                arts = await asyncio.gather(*(fetch_art(c) for c in cards))
            if any(a is None for a in arts):
                return None
            composer = (
                _compose_ten_pull_3rd_anni
                if style == "3rd"
                else _compose_ten_pull_1st_anni
            )
            payload = [
                (data, c.card_rarity_type, c.attr)
                for data, c in zip(arts, cards)
                if data is not None
            ]
            buf = await unblock.to_process_with_timeout(composer, payload)
            return discord.File(buf, "gacha.jpg")
        except Exception as e:
            self.bot.warn(f"gacha image failed: {e}")
            return None

    @app_commands.command(
        name="gacha", description="Simulate a ten-pull on the current banner."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.autocomplete(
        region=autocompletes.pjsk_region(GACHA_REGIONS),
        banner=autocompletes.pjsk_gacha,
    )
    @app_commands.choices(
        style=[
            app_commands.Choice(name="Auto (by banner date)", value="auto"),
            app_commands.Choice(name="1st Anniversary", value="1st"),
            app_commands.Choice(name="3rd Anniversary", value="3rd"),
        ]
    )
    @app_commands.describe(
        region="Game server region.",
        banner="Which gacha to pull from (name or ID; defaults to the current one).",
        style="Result-screen style (default: auto, chosen by the banner's date).",
        force_four_star="Guarantee every pull is 4★ (or the birthday card on birthday gachas).",
    )
    async def gacha(
        self,
        interaction: discord.Interaction,
        region: str = "default",
        banner: str | None = None,
        style: str = "auto",
        force_four_star: bool = False,
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
        gacha = self._get_gacha(region, banner)
        if not gacha:
            msg = (
                f"No {region.upper()} gacha matches `{banner}`."
                if banner
                else "No gacha banner data is available right now."
            )
            await interaction.followup.send(embed=embeds.error_embed(msg))
            return

        cards = self._simulate(gacha, force_four_star=force_four_star)
        if not cards:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't simulate this banner.")
            )
            return

        embed = embeds.embed(title=f"Ten Pull - {gacha.name}")
        file = await self._pull_image(cards, self._resolve_style(region, gacha, style))
        if file:
            embed.set_image(url="attachment://gacha.jpg")
        elif gacha.banner_url:
            embed.set_image(url=gacha.banner_url)
        embed.set_footer(
            text=f"{region.upper()} Gacha" + (" · forced 4★" if force_four_star else "")
        )
        await interaction.followup.send(embed=embed, file=file or discord.utils.MISSING)


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(GachaCog(bot))
