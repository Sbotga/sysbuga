from __future__ import annotations

import time
from datetime import datetime, timezone
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from PIL import Image, ImageDraw, ImageFont
from discord import app_commands
from discord.ext import commands

from helpers import embeds, unblock
from helpers.autocompletes import autocompletes
from helpers.progress_generate import StrDifficultyCategory, generate_general_progress
from services.sbuga import SbugaError, SbugaNotFound

if TYPE_CHECKING:
    from main import SbugaBot

SUMMARY_REGIONS = ["en", "jp", "tw", "kr"]
COOLDOWN = 90
DIFF_ORDER = ["append", "master", "expert", "hard", "normal", "easy"]
DIFF_INDEX = {"easy": 0, "normal": 1, "hard": 2, "expert": 3, "master": 4, "append": 5}


def _build_image(
    profile: dict,
    region: str,
    private: bool,
    user_name: str,
    counts: dict[str, int],
) -> BytesIO:
    # runs in a worker process, so it takes only picklable args - counts is precomputed from
    # pjsk on the main process rather than reached through the bot here
    clears = profile["userMusicDifficultyClearCount"]
    data = [
        StrDifficultyCategory(
            difficulty=diff,
            ap_count=clears[DIFF_INDEX[diff]]["allPerfect"],
            fc_count=clears[DIFF_INDEX[diff]]["fullCombo"],
            clear_count=clears[DIFF_INDEX[diff]]["liveClear"],
            all_count=counts.get(diff, 0),
        )
        for diff in DIFF_ORDER
        if counts.get(diff)
    ]

    base = Image.open(generate_general_progress(data))
    SCALE = 2
    new_img = Image.new(
        "RGBA", (base.width, base.height + 100 * SCALE), (50, 50, 50, 255)
    )
    new_img.paste(base, (0, 100 * SCALE))
    draw = ImageDraw.Draw(new_img)
    font = ImageFont.truetype("data/assets/image_gen/rodinntlg_eb.otf", 30 * SCALE)
    font_2 = ImageFont.truetype("data/assets/image_gen/rodinntlg_m.otf", 30 * SCALE)
    font_3 = ImageFont.truetype("data/assets/image_gen/rodinntlg_m.otf", 20 * SCALE)

    now = datetime.now(tz=timezone.utc)
    draw.text(
        (10, 15 * SCALE),
        profile["user"]["name"] if not private else user_name,
        font=font,
        fill="white",
    )
    draw.text(
        (10, 60 * SCALE),
        (
            f"{region.upper()} ID: {profile['user']['userId']}"
            if not private
            else f"{region.upper()} Account"
        ),
        font=font_3,
        fill="white",
    )
    draw.text(
        (base.width - 215 * SCALE, 10 * SCALE),
        now.strftime("%Y-%m-%d"),
        font=font_2,
        fill="white",
    )
    draw.text(
        (base.width - 200 * SCALE, 50 * SCALE),
        now.strftime("%H:%M") + " UTC",
        font=font_2,
        fill="white",
    )

    out = BytesIO()
    new_img.save(out, format="PNG")
    out.seek(0)
    return out


class SummaryCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot
        self.cooldowns: dict[int, float] = {}

    @app_commands.command(
        name="summary", description="View your PJSK all-difficulty clear summary."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(SUMMARY_REGIONS))
    @app_commands.describe(
        region="Game server region.", private="Hide your in-game name and ID."
    )
    async def summary(
        self,
        interaction: discord.Interaction,
        region: str = "default",
        private: bool = False,
    ) -> None:
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(interaction.user.id, "default_region")  # type: ignore[union-attr]
        if region not in SUMMARY_REGIONS:
            await interaction.response.send_message(
                embed=embeds.error_embed(f"Region `{region.upper()}` isn't supported."),
                ephemeral=True,
            )
            return

        cooldown_end = self.cooldowns.get(interaction.user.id, 0) + COOLDOWN
        if cooldown_end > time.time():
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"You recently ran summary. Try again <t:{int(cooldown_end)}:R>."
                ),
                ephemeral=True,
            )
            return
        previous = self.cooldowns.get(interaction.user.id, 0)
        self.cooldowns[interaction.user.id] = time.time()

        try:
            await interaction.response.defer(thinking=True)
            pjsk_id = await self.bot.user_data.get_pjsk_id(interaction.user.id, region)  # type: ignore[union-attr]
            if not pjsk_id:
                self.cooldowns[interaction.user.id] = previous
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        f"You aren't linked to a PJSK {region.upper()} account."
                    ).set_footer(text="Your cooldown was reset.")
                )
                return

            await interaction.followup.send(
                embed=embeds.embed("Please wait while we generate your image...")
            )
            try:
                resp = await self.bot.sbuga.get_profile(pjsk_id, region)  # type: ignore[union-attr,arg-type]
            except SbugaNotFound:
                self.cooldowns[interaction.user.id] = previous
                await interaction.edit_original_response(
                    embed=embeds.error_embed(
                        f"Couldn't find your profile in the {region.upper()} server."
                    )
                )
                return

            counts: dict[str, int] = {}
            for music in self.bot.pjsk.musics():  # type: ignore[union-attr]
                for d in music.difficulties:
                    counts[d.difficulty] = counts.get(d.difficulty, 0) + 1

            img = await unblock.to_process_with_timeout(
                _build_image,
                resp.profile,
                region,
                private,
                interaction.user.name,
                counts,
                timeout=60,
            )
            embed = embeds.embed(
                title="Your PJSK Summary", color=discord.Color.dark_gold()
            )
            embed.set_image(url="attachment://image.png")
            embed.set_footer(text="Limited-time songs included.")
            await interaction.edit_original_response(
                embed=embed, attachments=[discord.File(img, "image.png")]
            )
        except SbugaError as e:
            self.cooldowns[interaction.user.id] = previous
            await interaction.edit_original_response(
                embed=embeds.error_embed(
                    f"Couldn't generate summary: {e.detail or e.status}"
                )
            )
        except Exception:
            self.cooldowns[interaction.user.id] = previous
            raise


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(SummaryCog(bot))
