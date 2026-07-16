from __future__ import annotations

import asyncio
import io
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont
from sekai_images import (
    DeckCardImage,
    LeaderCardImage,
    UserCardSpecialTrainingStatus,
)
from sekai_images.generators.deck_card import CardData as DeckCardData
from sekai_images.generators.leader_card import CardData as LeaderCardData
from sekai_images.util import load_image

from data.models import Card
from data.search import preprocess
from helpers import converters, embeds
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.views import SbugaView
from services import isv
from services.sbuga import SbugaError, SbugaNotFound

if TYPE_CHECKING:
    from main import SbugaBot

PJSK_REGIONS = ["en", "jp", "tw", "kr"]
_DECK_BG = (17, 17, 17, 255)  # matches heatmap _BG
_DECK_PAD = 24  # padding around and between deck cards
_DECK_TEXT = (242, 245, 250, 255)
_DECK_MUTED = (150, 162, 178, 255)
_DECK_WHITE = (255, 255, 255, 255)
_DECK_GREEN = (88, 214, 141, 255)
_DECK_RED = (231, 106, 106, 255)
_DECK_FONT_BOLD = "data/assets/image_gen/rodinntlg_eb.otf"
_DECK_FONT_MED = "data/assets/image_gen/rodinntlg_m.otf"
_UNIT_NAMES = {
    "light_sound": "Leo/need",
    "idol": "MORE MORE JUMP!",
    "street": "Vivid BAD SQUAD",
    "theme_park": "Wonderlands×Showtime",
    "school_refusal": "25-ji, Nightcord de.",
    "piapro": "VIRTUAL SINGER",
}


def _training_status(trained: bool) -> UserCardSpecialTrainingStatus:
    return (
        UserCardSpecialTrainingStatus.DONE
        if trained
        else UserCardSpecialTrainingStatus.DO_NOTHING
    )


def _render_leader_card(
    member_image: str,
    card_rarity_type: str,
    attr: str,
    level: int | None,
    mastery_rank: int,
    trained: bool,
) -> bytes:
    # LeaderCardImage composites a 128px frame without resizing the art, so normalize first
    img = load_image(member_image).resize((128, 128), Image.Resampling.LANCZOS)
    return LeaderCardImage(
        LeaderCardData(
            level=level,
            mastery_rank=mastery_rank,
            special_training_status=_training_status(trained),
            card_rarity_type=card_rarity_type,  # type: ignore[arg-type]
            attr=attr,  # type: ignore[arg-type]
            member_image=img,
        )
    ).create()


def _render_deck_cards(specs: list[dict]) -> list[bytes]:
    """Render each deck card (the network-heavy part: fetches the card art)."""
    return [
        DeckCardImage(
            DeckCardData(
                level=s["level"],
                mastery_rank=s["mastery_rank"],
                special_training_status=_training_status(s["trained"]),
                card_rarity_type=s["card_rarity_type"],
                attr=s["attr"],
                slot=s["slot"],
                member_image=s["member_image"],
            )
        ).create()
        for s in specs
    ]


def _compose_deck(
    card_pngs: list[bytes],
    card_cids: list[int],
    skill_levels: dict[int, int],
    scores: dict[int, float],
    deck_no: int | None,
    deck_name: str,
    username: str,
    region: str,
    thumb_png: bytes | None,
    bot_name: str,
    asterisk_cids: set[int],
    asterisk_average: bool,
    cr_ranks: dict[int, int],
    annotate: bool = True,
) -> bytes:
    """Lay the pre-rendered deck cards in a row on the heatmap background with a header
    (deck no. + name on the left, username + leader thumbnail on the right), a
    "Skill Level: n" caption under each card (plus "CR n" for character-rank cards),
    and a region footer. Encore (random-average) cards get a * on their score,
    explained by a note above the footer. No network.

    `annotate=False` drops everything we letter on top of the cards - the skill level, the
    score, the CR line and the asterisk note - leaving the deck itself (/pjsk deck). The
    card art is untouched either way; that comes rendered from sekai_images."""
    imgs = [Image.open(io.BytesIO(p)).convert("RGBA") for p in card_pngs]
    cw = max(i.width for i in imgs)
    ch = max(i.height for i in imgs)
    header_h = 170
    if not annotate:
        label_h, note_h = _DECK_PAD, 0
    else:
        label_h = 158 if cr_ranks else 108
        note_h = 68 if asterisk_cids else 0
    footer_h = 56
    width = _DECK_PAD + len(imgs) * (cw + _DECK_PAD)
    height = header_h + ch + label_h + note_h + footer_h
    canvas = Image.new("RGBA", (width, height), _DECK_BG)
    draw = ImageDraw.Draw(canvas)

    font_title = ImageFont.truetype(_DECK_FONT_BOLD, 52)
    font_name = ImageFont.truetype(_DECK_FONT_BOLD, 40)
    font_sub = ImageFont.truetype(_DECK_FONT_MED, 36)
    font_sl = ImageFont.truetype(_DECK_FONT_BOLD, 34)
    font_score = ImageFont.truetype(_DECK_FONT_BOLD, 48)
    font_note = ImageFont.truetype(_DECK_FONT_MED, 46)
    font_region = ImageFont.truetype(_DECK_FONT_MED, 30)

    draw.text(
        (_DECK_PAD, _DECK_PAD),
        f"Deck {deck_no}" if deck_no else "Deck",
        font=font_title,
        fill=_DECK_TEXT,
    )
    draw.text(
        (_DECK_PAD, _DECK_PAD + 66),
        f"Name: {deck_name}",
        font=font_sub,
        fill=_DECK_MUTED,
    )

    ts = header_h - _DECK_PAD * 2
    thumb_x = width - _DECK_PAD - ts
    if thumb_png:
        thumb = (
            Image.open(io.BytesIO(thumb_png))
            .convert("RGBA")
            .resize((ts, ts), Image.Resampling.LANCZOS)
        )
        canvas.alpha_composite(thumb, (thumb_x, _DECK_PAD))
    uname_right = (thumb_x - _DECK_PAD) if thumb_png else (width - _DECK_PAD)
    draw.text(
        (uname_right, _DECK_PAD + ts // 2),
        username,
        font=font_name,
        anchor="rm",
        fill=_DECK_TEXT,
    )

    x = _DECK_PAD
    label_top = header_h + ch + 6
    for img, cid in zip(imgs, card_cids):
        canvas.alpha_composite(img, (x, header_h))
        if not annotate:
            x += cw + _DECK_PAD
            continue
        cx = x + cw // 2
        draw.text(
            (cx, label_top),
            f"Skill Level: {skill_levels.get(cid, 4)}",
            font=font_sl,
            anchor="mt",
            fill=_DECK_WHITE,
        )
        draw.text(
            (cx, label_top + 46),
            f"{scores.get(cid, 0):g}%" + ("*" if cid in asterisk_cids else ""),
            font=font_score,
            anchor="mt",
            fill=_DECK_WHITE,
        )
        if cid in cr_ranks:
            draw.text(
                (cx, label_top + 100),
                f"CR {cr_ranks[cid]}",
                font=font_sl,
                anchor="mt",
                fill=_DECK_WHITE,
            )
        x += cw + _DECK_PAD

    if asterisk_cids and annotate:
        avg = "an average" if asterisk_average else "not an average"
        draw.text(
            (_DECK_PAD, header_h + ch + label_h + (note_h - 46) // 2),
            f"*This is {avg} (see Show Math for more information)",
            font=font_note,
            fill=_DECK_WHITE,
        )
    draw.text(
        (_DECK_PAD, header_h + ch + label_h + note_h + (footer_h - 30) // 2),
        f"Region: {region.upper()} - Rendered by {bot_name}",
        font=font_region,
        fill=_DECK_MUTED,
    )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


_MARK_SIZE = 40
_MARK_ADVANCE = _MARK_SIZE + 18


def _draw_mark(draw: ImageDraw.ImageDraw, x: int, cy: float, kind: str, color) -> None:
    """A check (✓) or cross (✗) drawn from primitives (the fonts lack the glyphs)."""
    s = _MARK_SIZE
    if kind == "check":
        draw.line(
            [
                (x, cy + s * 0.05),
                (x + s * 0.38, cy + s * 0.42),
                (x + s, cy - s * 0.45),
            ],
            fill=color,
            width=6,
            joint="curve",
        )
    else:
        draw.line(
            [(x, cy - s * 0.42), (x + s * 0.72, cy + s * 0.42)], fill=color, width=6
        )
        draw.line(
            [(x + s * 0.72, cy - s * 0.42), (x, cy + s * 0.42)], fill=color, width=6
        )


def _render_isv_math(
    rows: list[tuple[bytes | None, str, tuple[int, int, int, int], str | None]],
    text_lines: list[str],
    header: str | None = None,
) -> bytes:
    """A math page: an optional header line, then a thumbnail + optional check/cross +
    colored label per card, then summary lines. On the deck background."""
    pad = 32
    thumb_sz = 84
    row_h = thumb_sz + 14
    gap = 24
    line_h = 46
    header_h = 58 if header else 0
    font_header = ImageFont.truetype(_DECK_FONT_BOLD, 40)
    font_row = ImageFont.truetype(_DECK_FONT_BOLD, 36)
    font_line = ImageFont.truetype(_DECK_FONT_MED, 32)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def content_w(text: str, mark: str | None) -> float:
        return probe.textlength(text, font=font_row) + (_MARK_ADVANCE if mark else 0)

    rows_w = (
        pad
        + thumb_sz
        + gap
        + int(max((content_w(t, m) for _, t, _, m in rows), default=0))
    )
    lines_w = int(
        max((probe.textlength(ln, font=font_line) for ln in text_lines), default=0)
    )
    header_w = int(probe.textlength(header, font=font_header)) if header else 0
    width = max(rows_w, pad + lines_w, pad + header_w) + pad

    height = (
        pad
        + header_h
        + row_h * len(rows)
        + (pad + line_h * len(text_lines) if text_lines else 0)
        + pad
    )
    canvas = Image.new("RGBA", (width, height), _DECK_BG)
    draw = ImageDraw.Draw(canvas)

    y = pad
    if header:
        draw.text((pad, y), header, font=font_header, fill=_DECK_WHITE)
        y += header_h
    for thumb, text, color, mark in rows:
        if thumb:
            t = (
                Image.open(io.BytesIO(thumb))
                .convert("RGBA")
                .resize((thumb_sz, thumb_sz), Image.Resampling.LANCZOS)
            )
            canvas.alpha_composite(t, (pad, y))
        tx = pad + thumb_sz + gap
        cy = y + thumb_sz / 2
        if mark:
            _draw_mark(draw, tx, cy, mark, color)
            tx += _MARK_ADVANCE
        draw.text((tx, cy), text, font=font_row, anchor="lm", fill=color)
        y += row_h

    if text_lines:
        y += pad
        for line in text_lines:
            draw.text((pad, y), line, font=font_line, fill=_DECK_WHITE)
            y += line_h

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class _ISVData:
    username: str
    region: str
    is_self: bool
    updated: float
    thumb_png: bytes | None
    member_thumbs: dict[int, bytes]
    card_pngs: list[bytes]
    card_cids: list[int]
    deck_no: int | None
    deck_name: str
    skills: dict[int, dict]
    ranks: dict[int, int]
    unit_counts: dict[int, int]
    leader_id: int | None
    skill_levels: dict[int, int]
    member_ids: list[int]
    member_cards: dict[int, Card]
    member_unit_sets: dict[int, set[str]]


class _ChangeSLModal(discord.ui.Modal, title="Change Skill Levels"):
    def __init__(self, view: _ISVView) -> None:
        super().__init__()
        self._view = view
        self._inputs: dict[int, discord.ui.TextInput] = {}
        for slot, cid in enumerate(view.data.member_ids, start=1):
            card = view.data.member_cards.get(cid)
            name = card.prefix if card and card.prefix else f"Card {cid}"
            field = discord.ui.TextInput(
                label=f"{slot}. {name}"[:45],
                default=str(view.data.skill_levels.get(cid, 4)),
                placeholder="1-4",
                min_length=1,
                max_length=1,
            )
            self.add_item(field)
            self._inputs[cid] = field

    async def on_submit(self, interaction: discord.Interaction) -> None:
        for cid, field in self._inputs.items():
            value = field.value.strip()
            if value.isdigit():
                self._view.data.skill_levels[cid] = max(1, min(4, int(value)))
        embed, files = await self._view.render()
        await interaction.response.edit_message(
            embed=embed, attachments=files, view=self._view
        )


class _DeckView(SbugaView):
    """/pjsk deck - just a refresh, since the deck image has nothing to drill into"""

    def __init__(
        self,
        *,
        cog: InfoCog,
        author_id: int,
        user_id: str,
        region: str,
        data: _ISVData,
    ) -> None:
        super().__init__(restrict_to=author_id, timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.region = region
        self.data = data

    @discord.ui.button(emoji="🔄", label="Refresh", style=discord.ButtonStyle.danger)
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # swap to a loading state (buttons disabled) immediately so it's clear it's working
        self._disable_all()
        await interaction.response.edit_message(
            embed=embeds.embed(
                title=f"{self.data.username} - Deck", description="🔄 Refreshing..."
            ),
            attachments=[],
            view=self,
        )
        try:
            self.data = await self.cog._isv_build(
                self.user_id, self.region, self.data.is_self, fresh=True
            )
        except SbugaNotFound:
            self._enable_all()
            await interaction.edit_original_response(
                embed=embeds.error_embed(
                    f"Couldn't find that profile in the {self.region.upper()} server."
                ),
                view=self,
            )
            return
        except SbugaError as e:
            self._enable_all()
            await interaction.edit_original_response(
                embed=embeds.error_embed(f"Couldn't refresh: {e.detail or e.status}"),
                view=self,
            )
            return
        self._enable_all()
        embed, files = await asyncio.to_thread(self.cog._deck_render, self.data)
        await interaction.edit_original_response(
            embed=embed, attachments=files, view=self
        )


class _ISVView(SbugaView):
    def __init__(
        self,
        *,
        cog: InfoCog,
        author_id: int,
        user_id: str,
        region: str,
        data: _ISVData,
    ) -> None:
        # not restrict_to: Show Math is open to everyone; the other buttons check below
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.user_id = user_id
        self.region = region
        self.data = data

    async def _not_author(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "You can't interact with this — run the command yourself.",
                ephemeral=True,
            )
            return True
        return False

    async def render(self) -> tuple[discord.Embed, list[discord.File]]:
        d = self.data
        per_card, isv_text = isv.deck_isv(
            d.skills,
            d.skill_levels,
            d.ranks,
            d.unit_counts,
            d.member_unit_sets,
            d.leader_id,
        )
        leader = per_card.get(d.leader_id, 0.0)
        total = sum(per_card.values())
        percent = leader + (total - leader) / 5
        desc = []
        if d.is_self:
            desc.append("✅ This is your PJSK account!\n")
        desc.append(f"**ISV:** `{isv_text} ({percent:g}%)`")
        desc.append("**__You must input your own skill levels.__**")
        embed = embeds.embed(
            title=f"{d.username} - ISV",
            description="\n".join(desc),
            color=discord.Color.purple(),
        )
        embed.set_footer(
            text=f"{d.region.upper()} - updated {round(time.time() - d.updated)}s ago"
        )
        files: list[discord.File] = []
        if d.thumb_png:
            embed.set_thumbnail(url="attachment://leader.png")
            files.append(discord.File(io.BytesIO(d.thumb_png), "leader.png"))
        if d.card_pngs:
            # encore (reference-rate) cards score a random average -> flag with a *,
            # unless every reference already caps (then it's guaranteed, not an average)
            _, max_scores = isv.member_scores(
                d.skills, d.skill_levels, d.ranks, d.unit_counts, d.member_unit_sets
            )
            asterisk = set()
            any_average = False
            for cid in d.card_cids:
                params = (
                    isv.reference_params(d.skills[cid], d.skill_levels.get(cid, 4))
                    if cid in d.skills
                    else None
                )
                if not params:
                    continue
                asterisk.add(cid)
                others = [max_scores[o] for o in d.skills if o != cid]
                if not isv.encore_guaranteed(params[0], params[1], others):
                    any_average = True
            # trained-BFES (character-rank) cards show their character rank
            cr_ranks = {
                cid: d.ranks.get(cid, 0)
                for cid in d.card_cids
                if cid in d.skills
                and any(
                    e.get("skillEffectType") == "score_up_character_rank"
                    for e in d.skills[cid].get("skillEffects", [])
                )
            }
            deck_png = await asyncio.to_thread(
                _compose_deck,
                d.card_pngs,
                d.card_cids,
                d.skill_levels,
                per_card,
                d.deck_no,
                d.deck_name,
                d.username,
                d.region,
                d.thumb_png,
                self.cog.bot.config["discord"].get("name") or "Sbuga",
                asterisk,
                any_average,
                cr_ranks,
            )
            embed.set_image(url="attachment://deck.png")
            files.append(discord.File(io.BytesIO(deck_png), "deck.png"))
        return embed, files

    @discord.ui.button(emoji="🔄", label="Refresh", style=discord.ButtonStyle.danger)
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._not_author(interaction):
            return
        # swap to a loading state (buttons disabled) immediately so it's clear it's working
        self._disable_all()
        await interaction.response.edit_message(
            embed=embeds.embed(
                title=f"{self.data.username} - ISV", description="🔄 Refreshing..."
            ),
            attachments=[],
            view=self,
        )
        try:
            self.data = await self.cog._isv_build(
                self.user_id, self.region, self.data.is_self, fresh=True
            )
        except SbugaNotFound:
            self._enable_all()
            await interaction.edit_original_response(
                embed=embeds.error_embed(
                    f"Couldn't find that profile in the {self.region.upper()} server."
                ),
                view=self,
            )
            return
        except SbugaError as e:
            self._enable_all()
            await interaction.edit_original_response(
                embed=embeds.error_embed(f"Couldn't refresh: {e.detail or e.status}"),
                view=self,
            )
            return
        self._enable_all()
        embed, files = await self.render()
        await interaction.edit_original_response(
            embed=embed, attachments=files, view=self
        )

    @discord.ui.button(label="Change Skill Levels", style=discord.ButtonStyle.secondary)
    async def change_sl(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._not_author(interaction):
            return
        await interaction.response.send_modal(_ChangeSLModal(self))

    def _char_first_name(self, cid: int) -> str:
        card = self.data.member_cards.get(cid)
        if not card:
            return "?"
        ch = self.cog.bot.pjsk.get_character(card.character_id)  # type: ignore[union-attr]
        return ch.given_name if ch and ch.given_name else "?"

    @discord.ui.button(label="Show Math", style=discord.ButtonStyle.secondary)
    async def show_math(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        d = self.data
        # first page is always the overall calc; extra pages are dynamic per skill kind
        pages: list[tuple[str, str, int | None]] = [("Calculations", "calc", None)]
        for cid in d.member_ids:
            skill = d.skills.get(cid)
            if not skill:
                continue
            slot = d.member_ids.index(cid) + 1
            name = self._char_first_name(cid)
            prefix = "Leader" if cid == d.leader_id else f"Slot {slot}"
            card = d.member_cards.get(cid)
            is_bfes = bool(card and card.special_training_skill_id)
            effect_types = {
                e.get("skillEffectType") for e in skill.get("skillEffects", [])
            }
            if isv.sub_unit_enhance(skill):  # unit-scorer (+% per same-unit member)
                pages.append((f"{prefix} ({name} Uscorer)", "uscorer", cid))
            elif isv.reference_params(skill, d.skill_levels.get(cid, 4)):
                pages.append((f"{prefix} ({name} Untrained BFES)", "bfes_ref", cid))
            elif "score_up_unit_count" in effect_types:
                pages.append((f"{prefix} ({name} Untrained BFES)", "bfes_unit", cid))
            elif is_bfes and "score_up_character_rank" in effect_types:
                pages.append((f"{prefix} ({name} Trained BFES)", "bfes_trained", cid))

        math_view = _MathView(self, pages, requester_id=interaction.user.id)
        embed, file = await math_view.render_page(0)
        await interaction.response.send_message(embed=embed, file=file, view=math_view)
        math_view.message = await interaction.original_response()


class _MathPageButton(discord.ui.Button):
    def __init__(self, index: int, label: str, *, disabled: bool, row: int) -> None:
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.show_page(interaction, self.index)  # type: ignore[union-attr]


class _TrashButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(emoji="🗑️", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _MathView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message(
                "Only whoever opened this can delete it.", ephemeral=True
            )
            return
        if interaction.message:
            await interaction.message.delete()


class _MathView(SbugaView):
    def __init__(
        self,
        parent: _ISVView,
        pages: list[tuple[str, str, int | None]],
        *,
        requester_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.parent = parent
        self.pages = pages
        self.requester_id = requester_id
        # page buttons flow 5 per row; the trash sits on the row below them
        trash_row = 0
        if len(pages) > 1:
            for i, (label, _, _) in enumerate(pages):
                self.add_item(_MathPageButton(i, label, disabled=(i == 0), row=i // 5))
            trash_row = (len(pages) - 1) // 5 + 1
        self.add_item(_TrashButton(row=trash_row))

    async def render_page(
        self, index: int
    ) -> tuple[discord.Embed, discord.File | None]:
        label, kind, cid = self.pages[index]
        d = self.parent.data
        per_card, isv_text = isv.deck_isv(
            d.skills,
            d.skill_levels,
            d.ranks,
            d.unit_counts,
            d.member_unit_sets,
            d.leader_id,
        )
        if cid is not None:
            if kind == "uscorer":
                return await self._uscorer_page(d, per_card, cid)
            if kind == "bfes_ref":
                return await self._bfes_ref_page(d, per_card, cid)
            if kind == "bfes_unit":
                return await self._bfes_unit_page(d, per_card, cid)
            if kind == "bfes_trained":
                return await self._bfes_trained_page(d, per_card, cid)
        return await self._calc_page(d, per_card, isv_text)

    async def _calc_page(
        self, d: _ISVData, per_card: dict[int, float], isv_text: str
    ) -> tuple[discord.Embed, discord.File]:
        leader = per_card.get(d.leader_id, 0.0)
        total = sum(per_card.values())
        percent = leader + (total - leader) / 5
        ordered = [cid for cid in d.member_ids if cid in per_card]
        # encore (reference-rate) cards score a random average -> mark with a *,
        # unless every reference caps (then it's guaranteed, not an average)
        _, max_scores = isv.member_scores(
            d.skills, d.skill_levels, d.ranks, d.unit_counts, d.member_unit_sets
        )
        encore = set()
        any_average = False
        for cid in ordered:
            params = (
                isv.reference_params(d.skills[cid], d.skill_levels.get(cid, 4))
                if cid in d.skills
                else None
            )
            if not params:
                continue
            encore.add(cid)
            others = [max_scores[o] for o in d.skills if o != cid]
            if not isv.encore_guaranteed(params[0], params[1], others):
                any_average = True
        rows = [
            (
                d.member_thumbs.get(cid),
                f"{per_card[cid]:g}%"
                + ("*" if cid in encore else "")
                + ("  (Leader)" if cid == d.leader_id else ""),
                _DECK_WHITE,
                None,
            )
            for cid in ordered
        ]
        text_lines = [
            f"Total = {' + '.join(f'{per_card[c]:g}' for c in ordered)} = {total:g}%",
            f"Leader = {leader:g}%",
            "",
            f"ISV = Leader / Total = {leader:g} / {total:g}",
            "Boost% = Leader + (Total - Leader) / 5",
            f"       = {leader:g} + ({total:g} - {leader:g}) / 5 = {percent:g}%",
        ]
        if encore:
            avg = "an average" if any_average else "not an average"
            text_lines += ["", f"*This is {avg} (see their math pages)"]
        img = await asyncio.to_thread(_render_isv_math, rows, text_lines)
        embed = embeds.embed(
            title="ISV Calculations",
            description=f"`{isv_text} ({percent:g}%)`",
            color=discord.Color.purple(),
        )
        embed.set_image(url="attachment://math.png")
        return embed, discord.File(io.BytesIO(img), "math.png")

    async def _uscorer_page(
        self, d: _ISVData, per_card: dict[int, float], cid: int
    ) -> tuple[discord.Embed, discord.File]:
        skill = d.skills.get(cid, {})
        sub = isv.sub_unit_enhance(skill)
        unit, value = sub if sub else (None, 0.0)  # unit rewarded, +% per member
        rows = []
        others = 0
        for other in d.member_ids:
            if other not in d.member_thumbs and other not in per_card:
                continue
            # a member counts if its unit set (native + support) includes that unit
            same = unit is not None and unit in d.member_unit_sets.get(other, set())
            if same and other != cid:
                others += 1
            rows.append(
                (
                    d.member_thumbs.get(other),
                    "Same Unit" if same else "Different Unit",
                    _DECK_GREEN if same else _DECK_RED,
                    "check" if same else "x",
                )
            )

        base = isv.card_score_up(skill, d.skill_levels.get(cid, 4))
        all_unit = unit is not None and all(
            unit in d.member_unit_sets.get(o, set()) for o in d.member_unit_sets
        )
        text_lines = [
            f"Base = {base:g}%",
            f"Same-unit: +{value:g}% x {others} = +{value * others:g}%",
        ]
        if all_unit:
            text_lines.append(f"All from unit: +{value:g}%")
        text_lines.append(f"Total = {per_card.get(cid, 0.0):g}%")

        unit_name = _UNIT_NAMES.get(unit, unit) if unit else "?"
        img = await asyncio.to_thread(
            _render_isv_math, rows, text_lines, f"Unit: {unit_name}"
        )
        name = self.parent._char_first_name(cid)
        embed = embeds.embed(
            title=f"{name} Uscorer",
            description=f"Score: `{per_card.get(cid, 0.0):g}%`",
            color=discord.Color.purple(),
        )
        embed.set_image(url="attachment://math.png")
        return embed, discord.File(io.BytesIO(img), "math.png")

    async def _bfes_ref_page(
        self, d: _ISVData, per_card: dict[int, float], cid: int
    ) -> tuple[discord.Embed, discord.File]:
        base_scores, max_scores = isv.member_scores(
            d.skills, d.skill_levels, d.ranks, d.unit_counts, d.member_unit_sets
        )
        rate, cap = isv.reference_params(d.skills[cid], d.skill_levels.get(cid, 4))
        others = [o for o in d.member_ids if o != cid and o in max_scores]
        rows = []
        contributions = []
        for o in others:
            contrib = min(cap, rate / 100 * max_scores[o])
            contributions.append(contrib)
            # +contribution (member's total before the 50% is taken)
            rows.append(
                (
                    d.member_thumbs.get(o),
                    f"+{contrib:g}% ({max_scores[o]:g}%)",
                    _DECK_WHITE,
                    None,
                )
            )
        base = base_scores.get(cid, 0.0)
        avg = sum(contributions) / len(contributions) if contributions else 0.0
        guaranteed = isv.encore_guaranteed(rate, cap, [max_scores[o] for o in others])
        text_lines = [
            f"Base = {base:g}%",
            f"Each = {rate:g}% of a member's skill (max {cap:g}%)",
        ]
        if guaranteed:
            text_lines.append(f"Guaranteed = {avg:g}% (every member caps)")
        else:
            text_lines.append(
                f"Average = ({' + '.join(f'{c:g}' for c in contributions)}) / "
                f"{len(contributions)} = {avg:g}%"
            )
        text_lines.append(f"Total = {base:g} + {avg:g} = {per_card.get(cid, 0.0):g}%")
        text_lines.append("")
        if guaranteed:
            threshold = cap * 100 / rate if rate else 0
            text_lines.append(
                f"*Not an average: every member is {threshold:g}%+, so each reference "
                f"maxes at {cap:g}%, meaning it is guaranteed to use "
                f"{per_card.get(cid, 0.0):g}%."
            )
        else:
            text_lines.append(
                "*Average: it references a random member each time, so this is the mean."
            )
        name = self.parent._char_first_name(cid)
        img = await asyncio.to_thread(
            _render_isv_math, rows, text_lines, "Random Untrained BFES"
        )
        embed = embeds.embed(
            title=f"{name} BFES",
            description=f"Score: `{per_card.get(cid, 0.0):g}%`",
            color=discord.Color.purple(),
        )
        embed.set_image(url="attachment://math.png")
        return embed, discord.File(io.BytesIO(img), "math.png")

    async def _bfes_unit_page(
        self, d: _ISVData, per_card: dict[int, float], cid: int
    ) -> tuple[discord.Embed, discord.File]:
        base_scores, _ = isv.member_scores(
            d.skills, d.skill_levels, d.ranks, d.unit_counts, d.member_unit_sets
        )
        own = d.member_unit_sets.get(cid, set())
        rows = []
        for other in d.member_ids:
            if other not in d.member_thumbs and other not in per_card:
                continue
            # other-group members are what boost a mixed scorer, so those are the "✓"
            same = other == cid or bool(own & d.member_unit_sets.get(other, set()))
            rows.append(
                (
                    d.member_thumbs.get(other),
                    "Same Group" if same else "Other Group",
                    _DECK_RED if same else _DECK_GREEN,
                    "x" if same else "check",
                )
            )
        base = base_scores.get(cid, 0.0)
        groups = d.unit_counts.get(cid, 0)
        bonus = per_card.get(cid, 0.0) - base
        text_lines = [
            f"Base = {base:g}%",
            f"Other groups: {groups} -> +{bonus:g}%",
            f"Total = {per_card.get(cid, 0.0):g}%",
        ]
        img = await asyncio.to_thread(
            _render_isv_math, rows, text_lines, "Anti-Uscorer Untrained BFES"
        )
        name = self.parent._char_first_name(cid)
        embed = embeds.embed(
            title=f"{name} BFES",
            description=f"Score: `{per_card.get(cid, 0.0):g}%`",
            color=discord.Color.purple(),
        )
        embed.set_image(url="attachment://math.png")
        return embed, discord.File(io.BytesIO(img), "math.png")

    async def _bfes_trained_page(
        self, d: _ISVData, per_card: dict[int, float], cid: int
    ) -> tuple[discord.Embed, discord.File]:
        skill = d.skills[cid]
        level = d.skill_levels.get(cid, 4)
        rank = d.ranks.get(cid, 0)
        base = isv.card_score_up(skill, level, character_rank=0)  # score-up, no rank
        rank_bonus = per_card.get(cid, 0.0) - base
        rows = [(d.member_thumbs.get(cid), f"Character Rank {rank}", _DECK_WHITE, None)]
        text_lines = [
            f"Base = {base:g}%",
            f"Rank bonus (+1% per 2 ranks, max +50%): +{rank_bonus:g}%",
            f"Total = {base:g} + {rank_bonus:g} = {per_card.get(cid, 0.0):g}%",
        ]
        name = self.parent._char_first_name(cid)
        img = await asyncio.to_thread(
            _render_isv_math, rows, text_lines, "Character Rank Trained BFES"
        )
        embed = embeds.embed(
            title=f"{name} BFES",
            description=f"Score: `{per_card.get(cid, 0.0):g}%`",
            color=discord.Color.purple(),
        )
        embed.set_image(url="attachment://math.png")
        return embed, discord.File(io.BytesIO(img), "math.png")

    async def show_page(self, interaction: discord.Interaction, index: int) -> None:
        for item in self.children:
            if isinstance(item, _MathPageButton):
                item.disabled = item.index == index
        embed, file = await self.render_page(index)
        await interaction.response.edit_message(
            embed=embed, attachments=[file] if file else [], view=self
        )


class InfoCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    async def _leader_thumbnail(self, data: dict) -> discord.File | None:
        png = await self._leader_thumbnail_bytes(data)
        return discord.File(io.BytesIO(png), "leader.png") if png else None

    async def _thumbnail_for(self, card: Card, uc: dict) -> bytes | None:
        """Render a card's framed square thumbnail (trained art if defaultImage picks it)."""
        if not card.attr:
            return None
        trained = uc.get("defaultImage") == "special_training" and bool(
            card.thumbnail_url_trained
        )
        member_image = (
            card.thumbnail_url_trained if trained else card.thumbnail_url_normal
        )
        if not member_image:
            return None
        try:
            return await asyncio.to_thread(
                _render_leader_card,
                member_image,
                card.card_rarity_type,
                card.attr,
                uc.get("level"),
                uc.get("masterRank", 0),
                trained,
            )
        except Exception:
            return None

    async def _leader_thumbnail_bytes(self, data: dict) -> bytes | None:
        """Render the profile's showcased-deck leader as a framed card thumbnail."""
        decks = data.get("userDecks")
        if not decks:
            single = data.get("userDeck")
            decks = [single] if single else []
        deck = decks[0] if decks else {}
        leader_id = deck.get("leader")
        if not leader_id:
            return None
        card: Card | None = self.bot.pjsk.get_card(leader_id)  # type: ignore[union-attr]
        user_card = next(
            (c for c in data.get("userCards", []) if c.get("cardId") == leader_id), None
        )
        if not card or user_card is None:
            return None
        return await self._thumbnail_for(card, user_card)

    async def _deck_cards(
        self, deck: dict, cards: dict
    ) -> tuple[list[bytes], list[int]]:
        """Render the deck members (slot 0 = member1 ... slot 4 = member5) to card PNGs,
        returned with each card's id so skill-level captions can be composed later."""
        specs: list[dict] = []
        cids: list[int] = []
        for slot in range(5):
            cid = deck.get(f"member{slot + 1}")
            if not cid:
                continue
            card: Card | None = self.bot.pjsk.get_card(cid)  # type: ignore[union-attr]
            uc = cards.get(cid)
            if not card or not card.attr or uc is None:
                continue
            # defaultImage: "special_training" -> trained art, "original" -> untrained
            trainable = bool(card.deck_cutout_url_trained)
            trained = trainable and uc.get("defaultImage") == "special_training"
            member_image = (
                card.deck_cutout_url_trained if trained else card.deck_cutout_url_normal
            )
            if not member_image:
                continue
            specs.append(
                {
                    "member_image": member_image,
                    "card_rarity_type": card.card_rarity_type,
                    "attr": card.attr,
                    "level": uc.get("level") or 1,
                    "mastery_rank": uc.get("masterRank") or 0,
                    "trained": trained,
                    "slot": slot,
                }
            )
            cids.append(cid)
        if not specs:
            return [], []
        try:
            card_pngs = await asyncio.to_thread(_render_deck_cards, specs)
        except Exception:
            return [], []
        return card_pngs, cids

    async def _isv_build(
        self, user_id: str, region: str, is_self: bool, *, fresh: bool
    ) -> _ISVData:
        """Fetch a profile and render everything the ISV embed/view needs."""
        resp = await self.bot.sbuga.get_profile(int(user_id), region, fresh=fresh)  # type: ignore[union-attr,arg-type]
        data = resp.profile

        char_ranks = {
            c["characterId"]: c["characterRank"]
            for c in data.get("userCharacters", [])
            if "characterId" in c
        }
        cards = {
            c["cardId"]: {
                "level": c.get("level"),
                "masterRank": c.get("masterRank"),
                "defaultImage": c.get("defaultImage"),
            }
            for c in data.get("userCards", [])
            if "cardId" in c
        }
        decks = data.get("userDecks")
        if not decks:
            single = data.get("userDeck")
            decks = [single] if single else []
        raw_deck = decks[0] if decks else {}
        deck = {
            key: raw_deck.get(key)
            for key in (
                "deckId",
                "name",
                "leader",
                "subLeader",
                "member1",
                "member2",
                "member3",
                "member4",
                "member5",
            )
        }
        member_ids = [
            deck[key]
            for key in ("member1", "member2", "member3", "member4", "member5")
            if deck.get(key)
        ]

        skills_raw = await self.bot.sbuga.get_master("skills", region)  # type: ignore[union-attr]
        skills_by_id = {s["id"]: s for s in skills_raw}
        member_cards: dict[int, Card] = {}
        skills: dict[int, dict] = {}
        for cid in member_ids:
            card = self.bot.pjsk.get_card(cid)  # type: ignore[union-attr]
            if not card:
                continue
            member_cards[cid] = card
            # the shown art (defaultImage, toggleable) picks the active skill: trained
            # art -> specialTrainingSkillId, else the base skillId
            trained = cards.get(cid, {}).get("defaultImage") == "special_training"
            skill_id = (
                card.special_training_skill_id
                if trained and card.special_training_skill_id
                else card.skill_id
            )
            if skill_id in skills_by_id:
                skills[cid] = skills_by_id[skill_id]

        def _units(card: Card) -> tuple[str | None, set[str]]:
            ch = self.bot.pjsk.get_character(card.character_id)  # type: ignore[union-attr]
            native = ch.unit if ch else None
            support = card.support_unit
            # primary = one unit for group counting; full = native + support, so a
            # Virtual Singer card matches either its own unit or its support unit
            return (support or native), {u for u in (native, support) if u}

        member_primary: dict[int, str | None] = {}
        member_unit_sets: dict[int, set[str]] = {}
        for cid, c in member_cards.items():
            member_primary[cid], member_unit_sets[cid] = _units(c)

        ranks = {
            cid: char_ranks.get(member_cards[cid].character_id, 0) for cid in skills
        }
        # distinct OTHER groups: members sharing no unit with this card, tallied by
        # their primary unit (so a shared support unit doesn't count as "other")
        unit_counts: dict[int, int] = {}
        for cid in skills:
            own = member_unit_sets.get(cid, set())
            others = {
                member_primary[o]
                for o in member_unit_sets
                if o != cid and member_primary[o] and not (member_unit_sets[o] & own)
            }
            unit_counts[cid] = len(others)

        async def _thumb(cid: int) -> tuple[int, bytes | None]:
            card = member_cards.get(cid)
            uc = cards.get(cid)
            if not card or uc is None:
                return cid, None
            return cid, await self._thumbnail_for(card, uc)

        thumb_results = await asyncio.gather(*[_thumb(cid) for cid in member_ids])
        member_thumbs = {cid: png for cid, png in thumb_results if png}
        card_pngs, card_cids = await self._deck_cards(deck, cards)

        return _ISVData(
            username=data["user"]["name"],
            region=region,
            is_self=is_self,
            updated=resp.updated,
            thumb_png=member_thumbs.get(deck.get("leader")),
            member_thumbs=member_thumbs,
            card_pngs=card_pngs,
            card_cids=card_cids,
            deck_no=deck.get("deckId"),
            deck_name=deck.get("name") or "",
            skills=skills,
            ranks=ranks,
            unit_counts=unit_counts,
            leader_id=deck.get("leader"),
            skill_levels={cid: 4 for cid in member_ids},
            member_ids=member_ids,
            member_cards=member_cards,
            member_unit_sets=member_unit_sets,
        )

    def _trim_cmd_log(self) -> None:
        cutoff = time.time() - 60
        log = self.bot.cache.executed_commands
        while log and log[0][1] < cutoff:
            log.popleft()

    async def _is_alias_mod(self, user_id: int) -> bool:
        """The manager roles only exist in the support server, so authority is always
        resolved there — never against the roles of whatever guild the caller ran this
        in (which may be none at all, in a DM or a user install)."""
        if user_id in (self.bot.owner_ids or set()):
            return True
        role_ids = set(self.bot.config["discord"].get("alias_manager_role_ids", []))
        support_id = self.bot.config["discord"].get("support_id")
        if not role_ids or not support_id:
            return False
        guild = self.bot.get_guild(support_id)
        if not guild:
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return False
        return bool(role_ids & {r.id for r in member.roles})

    async def _alias_error_embed(
        self, error: SbugaError, alias: str, *, kind: str = "song"
    ) -> discord.Embed:
        """aliases are unique across every song (or event), so name the one already holding it.
        the backend may not return structured info on a conflict (it can surface a plain 500),
        so fall back to looking the holder up ourselves instead of echoing a raw error
        """
        holder_id = error.data.get("music_id") or error.data.get("event_id")
        if holder_id is None and error.status in (409, 500):
            try:
                if kind == "song":
                    aliases = await self.bot.sbuga.get_song_aliases()  # type: ignore[union-attr]
                    hit = next((a for a in aliases if a.alias == alias), None)
                    holder_id = hit.music_id if hit else None
                else:
                    aliases = await self.bot.sbuga.get_event_aliases()  # type: ignore[union-attr]
                    hit = next((a for a in aliases if a.alias == alias), None)
                    holder_id = hit.event_id if hit else None
            except SbugaError:
                holder_id = None
        if holder_id is not None:
            if kind == "song":
                other = self.bot.pjsk.get_music(holder_id)  # type: ignore[union-attr]
                where = (
                    f"**{other.title}** (ID `{other.id}`)"
                    if other
                    else f"song ID `{holder_id}`"
                )
            else:
                other = self.bot.pjsk.get_event(holder_id)  # type: ignore[union-attr]
                where = (
                    f"**{other.name}** (ID `{other.id}`)"
                    if other
                    else f"event ID `{holder_id}`"
                )
            return embeds.error_embed(
                f"`{alias}` is already an alias for {where}.\n"
                "Remove it from there before adding it here.",
                title="Alias already taken",
            )
        return embeds.error_embed(f"Couldn't add alias: {error.detail or error.status}")

    async def _deny_alias(self, interaction: discord.Interaction) -> bool:
        """Reply and return True if this caller may not use the alias commands."""
        if await self._is_alias_mod(interaction.user.id):
            return False
        await interaction.response.send_message(
            embed=embeds.error_embed("You're not authorized to manage aliases."),
            ephemeral=True,
        )
        return True

    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: app_commands.Command
    ) -> None:
        self.bot.cache.executed_commands.append(
            (command.qualified_name, time.time(), interaction.user.id)
        )
        self._trim_cmd_log()

    # --- general ---

    @app_commands.command(
        name="ping", description="Check the bot's latency and recent activity."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        self._trim_cmd_log()
        log = self.bot.cache.executed_commands
        cmds_ran = len(log) + 1
        users = {interaction.user.id} | {uid for _, _, uid in log}
        counter = Counter(cmd for cmd, _, _ in log)
        counter["ping"] += 1
        popular = (
            f"`/{counter.most_common(1)[0][0]}` was the most popular command in the last minute."
            if counter
            else "No commands were ran."
        )
        embed = embeds.embed(
            title="Pong!",
            description=(
                f"**Latency:** `{round(self.bot.latency * 1000, 2)}`ms\n\n"
                f"**{cmds_ran:,}** commands ran in the last minute.\n"
                f"**{len(users)}** users ran commands in the last minute.\n{popular}"
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="help", description="Bot info and links.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.user
        discord_cfg = self.bot.config["discord"]
        links = [
            f"**Invite:** https://discord.com/oauth2/authorize?client_id={self.bot.user.id}"
        ]
        for label, key in (
            ("Support", "support_invite"),
            ("Terms of Service", "tos_url"),
            ("Privacy Policy", "privacy_url"),
        ):
            url = str(discord_cfg.get(key) or "").strip()
            if url:  # blank links are omitted rather than rendered empty
                links.append(f"**{label}:** {url}")
        embed = embeds.embed(
            title=self.bot.user.name,
            description=(
                "\n".join(links) + "\n\n"
                f"-# {self.bot.user.mention} is not affiliated with SEGA, Colorful Palette, or Project Sekai."
            ),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="donate", description="Support the bot.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def donate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = embeds.embed(
            title="Donations",
            description=(
                "Donations are strictly **optional** and help cover hosting costs.\n\n"
                "**LINK:** https://ko-fi.com/uselessyum"
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    # --- pjsk group ---

    pjsk = app_commands.Group(
        name="pjsk",
        description="PJSK information.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @pjsk.command(
        name="why_inappropriate",
        description="Check why text is blocked by PJSK's filter.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(["en", "jp"]))
    @app_commands.describe(text="Text to check.", region="Game server region (en/jp).")
    async def why_inappropriate(
        self, interaction: discord.Interaction, text: str, region: str = "default"
    ) -> None:
        if len(text) > 512:
            await interaction.response.send_message(
                embed=embeds.error_embed("Text is too long! Max 512 characters."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(
                interaction.user.id, "default_region"
            )  # type: ignore[union-attr]
        if region not in ("en", "jp"):
            region = "en"

        try:
            resp = await self.bot.sbuga.why_inappropriate(text, region)  # type: ignore[union-attr,arg-type]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't check text: {e.detail or e.status}")
            )
            return

        blocked = [text[r.start : r.end] for r in resp.indexes]
        verdict = bool(blocked)
        escaped = text.replace("`", "ˋ")
        block_section = (
            "```diff\n"
            + ("\n".join(f"- {w}" for w in blocked) if blocked else "+ None!")
            + "\n```"
        )
        embed = embeds.embed(
            title=f"PJSK {region.upper()} Text Check",
            description=(
                f"Your text is **{'inappropriate' if verdict else 'appropriate'}** for PJSK {region.upper()}!"
            ),
            color=discord.Color.red() if verdict else discord.Color.green(),
        )
        embed.add_field(
            name="Your Text", value=f"```text\n{escaped}\n```", inline=False
        )
        embed.add_field(name="Blocked Words", value=block_section, inline=False)
        await interaction.followup.send(embed=embed)

    @pjsk.command(name="profile", description="View a PJSK profile.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(PJSK_REGIONS))
    @app_commands.describe(
        user_id="PJSK user ID (omit to use your linked account).",
        region="Game server region.",
    )
    async def profile(
        self,
        interaction: discord.Interaction,
        user_id: str | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(
                interaction.user.id, "default_region"
            )  # type: ignore[union-attr]
        if region not in PJSK_REGIONS:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Region `{region.upper()}` isn't supported.")
            )
            return

        linked = await self.bot.user_data.get_pjsk_id(interaction.user.id, region)  # type: ignore[union-attr]
        if not user_id:
            if not linked:
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        f"Link your {region.upper()} PJSK account, or pass a user ID."
                    )
                )
                return
            user_id = str(linked)
        if not user_id.isdigit():
            await interaction.followup.send(
                embed=embeds.error_embed("Invalid user ID.")
            )
            return

        try:
            resp = await self.bot.sbuga.get_profile(int(user_id), region)  # type: ignore[union-attr,arg-type]
        except SbugaNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't find that profile in the {region.upper()} server."
                )
            )
            return
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch profile: {e.detail or e.status}"
                )
            )
            return

        data = resp.profile
        is_self = str(linked) == user_id
        joined = ""
        if region in ("en", "jp"):
            ts = (
                int(format(data["user"]["userId"], "064b")[:42], 2) + 1600218000000
            ) // 1000
            joined = f"**Joined:** <t:{ts}:R>\n"
        clears = data.get("userMusicDifficultyClearCount", [])

        def stat(index: int, key: str) -> str:
            return str(clears[index][key]) if len(clears) > index else "?"

        embed = embeds.embed(
            title=data["user"]["name"],
            description=(
                ("✅ This is your PJSK account!\n\n" if is_self else "")
                + f"**User ID:** `{data['user']['userId']}`\n{joined}"
                + f"**Rank:** **`🎵 {data['user']['rank']}`**\n\n"
                + f"**Bio**\n```{data['userProfile'].get('word') or 'No Bio'}```\n"
                + f"**Clears:** `{stat(3, 'liveClear')}` Expert {emojis.clear}, "
                + f"`{stat(4, 'liveClear')}` Master {emojis.clear}, "
                + f"`{stat(5, 'liveClear')}` Append {emojis.append_clear}\n"
                + f"**FCs:** `{stat(3, 'fullCombo')}` Expert {emojis.fc}, "
                + f"`{stat(4, 'fullCombo')}` Master {emojis.fc}, "
                + f"`{stat(5, 'fullCombo')}` Append {emojis.append_fc}\n"
                + f"**APs:** `{stat(3, 'allPerfect')}` Expert {emojis.ap}, "
                + f"`{stat(4, 'allPerfect')}` Master {emojis.ap}, "
                + f"`{stat(5, 'allPerfect')}` Append {emojis.append_ap}\n"
            ),
            color=discord.Color.dark_green(),
        )
        embed.set_footer(
            text=f"{region.upper()} - updated {round(time.time() - resp.updated)}s ago"
        )
        thumb = await self._leader_thumbnail(data)
        if thumb:
            embed.set_thumbnail(url="attachment://leader.png")
            await interaction.followup.send(embed=embed, files=[thumb])
        else:
            await interaction.followup.send(embed=embed)

    def _deck_render(self, d: "_ISVData") -> tuple[discord.Embed, list[discord.File]]:
        """the plain deck: the same image /pjsk isv builds, minus everything we letter on
        top of the cards, and a title-only embed"""
        embed = embeds.embed(title=f"{d.username} - Deck", color=discord.Color.purple())
        embed.set_footer(
            text=f"{d.region.upper()} - updated {round(time.time() - d.updated)}s ago"
        )
        files: list[discord.File] = []
        if d.thumb_png:
            embed.set_thumbnail(url="attachment://leader.png")
            files.append(discord.File(io.BytesIO(d.thumb_png), "leader.png"))
        if d.card_pngs:
            # no ISV math at all here - the scores, asterisks and character ranks only exist
            # to explain a number this command doesn't show
            deck_png = _compose_deck(
                d.card_pngs,
                d.card_cids,
                d.skill_levels,
                {},
                d.deck_no,
                d.deck_name,
                d.username,
                d.region,
                d.thumb_png,
                self.bot.config["discord"].get("name") or "Sbuga",
                set(),
                False,
                {},
                False,
            )
            embed.set_image(url="attachment://deck.png")
            files.append(discord.File(io.BytesIO(deck_png), "deck.png"))
        return embed, files

    async def _deck_target(
        self, interaction: discord.Interaction, user_id: str | None, region: str
    ) -> tuple[str, str, bool] | None:
        """(user id, region, is_self) for the deck commands, or None once the error is sent"""
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(
                interaction.user.id, "default_region"
            )  # type: ignore[union-attr]
        if region not in PJSK_REGIONS:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Region `{region.upper()}` isn't supported.")
            )
            return None

        linked = await self.bot.user_data.get_pjsk_id(interaction.user.id, region)  # type: ignore[union-attr]
        if not user_id:
            if not linked:
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        f"Link your {region.upper()} PJSK account, or pass a user ID."
                    )
                )
                return None
            user_id = str(linked)
        if not user_id.isdigit():
            await interaction.followup.send(
                embed=embeds.error_embed("Invalid user ID.")
            )
            return None
        return user_id, region, str(linked) == user_id

    async def _fetch_deck(
        self, interaction: discord.Interaction, user_id: str, region: str, is_self: bool
    ) -> "_ISVData | None":
        """the profile's deck, or None once the error is sent"""
        try:
            return await self._isv_build(user_id, region, is_self, fresh=False)
        except SbugaNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't find that profile in the {region.upper()} server."
                )
            )
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch profile: {e.detail or e.status}"
                )
            )
        return None

    @pjsk.command(name="deck", description="View a PJSK profile's deck.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(PJSK_REGIONS))
    @app_commands.describe(
        user_id="PJSK user ID (omit to use your linked account).",
        region="Game server region.",
    )
    async def deck(
        self,
        interaction: discord.Interaction,
        user_id: str | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        target = await self._deck_target(interaction, user_id, region)
        if target is None:
            return
        resolved_id, region, is_self = target
        d = await self._fetch_deck(interaction, resolved_id, region, is_self)
        if d is None:
            return

        embed, files = await asyncio.to_thread(self._deck_render, d)
        view = _DeckView(
            cog=self,
            author_id=interaction.user.id,
            user_id=resolved_id,
            region=region,
            data=d,
        )
        await interaction.followup.send(embed=embed, files=files, view=view)
        view.message = await interaction.original_response()

    @pjsk.command(name="isv", description="View a PJSK profile's deck ISV.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.autocomplete(region=autocompletes.pjsk_region(PJSK_REGIONS))
    @app_commands.describe(
        user_id="PJSK user ID (omit to use your linked account).",
        region="Game server region.",
    )
    async def isv(
        self,
        interaction: discord.Interaction,
        user_id: str | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        target = await self._deck_target(interaction, user_id, region)
        if target is None:
            return
        user_id, region, is_self = target
        data = await self._fetch_deck(interaction, user_id, region, is_self)
        if data is None:
            return

        view = _ISVView(
            cog=self,
            author_id=interaction.user.id,
            user_id=user_id,
            region=region,
            data=data,
        )
        embed, files = await view.render()
        view.message = await interaction.followup.send(
            embed=embed, files=files, view=view, wait=True
        )

    # --- alias group (reads are public; editing disabled until the
    #     service-token auth path ships, see MISSING_SBUGA_ROUTES.md #2) ---

    # guild-scoped: only ever registered to the support server, so it never appears
    # elsewhere. Not user-installable and not usable in DMs.
    alias = app_commands.Group(
        name="alias",
        description="Manage song and event aliases (alias managers only).",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )
    alias_music = app_commands.Group(
        name="music", description="Song aliases.", parent=alias
    )
    alias_event = app_commands.Group(
        name="event", description="Event aliases.", parent=alias
    )

    @alias_music.command(
        name="list", description="Authorized only; view a song's aliases."
    )
    @app_commands.autocomplete(song=autocompletes.pjsk_song_alias)
    @app_commands.describe(song="Song name or ID.")
    async def music_list(self, interaction: discord.Interaction, song: str) -> None:
        if await self._deny_alias(interaction):
            return
        await interaction.response.defer(thinking=True)
        music = converters.match_song(self.bot.pjsk, song)  # type: ignore[arg-type]
        if not music:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        names = sorted(self.bot.pjsk.song_aliases(music.id))  # type: ignore[union-attr]
        embed = embeds.embed(
            title=f"Aliases - {music.title}",
            description=(
                "\n".join(f"- `{n}`" for n in names) if names else "No aliases yet."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Song ID {music.id} - {len(names)} aliases")
        await interaction.followup.send(embed=embed)

    @alias_music.command(name="add", description="Authorized only; add a song alias.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song_alias)
    @app_commands.describe(song="Song name or ID.", alias="Alias to add.")
    async def music_add(
        self, interaction: discord.Interaction, song: str, alias: str
    ) -> None:
        if await self._deny_alias(interaction):
            return
        await interaction.response.defer(thinking=True)
        music = converters.match_song(self.bot.pjsk, song)  # type: ignore[arg-type]
        if not music:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        target = preprocess(alias)
        if not target:
            await interaction.followup.send(
                embed=embeds.error_embed("That alias is empty after normalisation.")
            )
            return
        try:
            await self.bot.sbuga.add_song_alias(music.id, target)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=await self._alias_error_embed(e, target, kind="song")
            )
            return
        await self.bot.pjsk.add_song_alias_local(music.id, target)  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Added alias for `{music.title}` (ID `{music.id}`)\nAlias: `{target}`",
                title="Added alias!",
            )
        )

    @alias_music.command(
        name="remove", description="Authorized only; remove a song alias."
    )
    @app_commands.autocomplete(song=autocompletes.pjsk_song_alias)
    @app_commands.describe(song="Song name or ID.", alias="Alias to remove.")
    async def music_remove(
        self, interaction: discord.Interaction, song: str, alias: str
    ) -> None:
        if await self._deny_alias(interaction):
            return
        await interaction.response.defer(thinking=True)
        music = converters.match_song(self.bot.pjsk, song)  # type: ignore[arg-type]
        if not music:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        # aliases are stored preprocessed, so normalise the input the same way
        target = preprocess(alias)
        try:
            existing = await self.bot.sbuga.get_song_aliases()  # type: ignore[union-attr]
            match = next(
                (a for a in existing if a.music_id == music.id and a.alias == target),
                None,
            )
            if not match:
                await interaction.followup.send(
                    embed=embeds.error_embed(f"No alias `{target}` on `{music.title}`.")
                )
                return
            await self.bot.sbuga.remove_song_alias(match.id)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't remove alias: {e.detail or e.status}"
                )
            )
            return
        await self.bot.pjsk.remove_song_alias_local(music.id, target)  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Removed alias for `{music.title}` (ID `{music.id}`)\nAlias: `{target}`",
                title="Removed alias!",
            )
        )

    @alias_event.command(
        name="list", description="Authorized only; view an event's aliases."
    )
    @app_commands.autocomplete(event=autocompletes.pjsk_event_alias)
    @app_commands.describe(event="Event name or ID.")
    async def event_list(self, interaction: discord.Interaction, event: str) -> None:
        if await self._deny_alias(interaction):
            return
        await interaction.response.defer(thinking=True)
        ev = converters.match_event(self.bot.pjsk, event)  # type: ignore[arg-type]
        if not ev:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find an event matching `{event}`.")
            )
            return
        names = sorted(self.bot.pjsk.event_aliases(ev.id))  # type: ignore[union-attr]
        embed = embeds.embed(
            title=f"Aliases - {ev.name}",
            description=(
                "\n".join(f"- `{n}`" for n in names) if names else "No aliases yet."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Event ID {ev.id} - {len(names)} aliases")
        await interaction.followup.send(embed=embed)

    @alias_event.command(name="add", description="Authorized only; add an event alias.")
    @app_commands.autocomplete(event=autocompletes.pjsk_event_alias)
    @app_commands.describe(event="Event name or ID.", alias="Alias to add.")
    async def event_add(
        self, interaction: discord.Interaction, event: str, alias: str
    ) -> None:
        if await self._deny_alias(interaction):
            return
        await interaction.response.defer(thinking=True)
        ev = converters.match_event(self.bot.pjsk, event)  # type: ignore[arg-type]
        if not ev:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find an event matching `{event}`.")
            )
            return
        target = preprocess(alias)
        if not target:
            await interaction.followup.send(
                embed=embeds.error_embed("That alias is empty after normalisation.")
            )
            return
        try:
            await self.bot.sbuga.add_event_alias(ev.id, target)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=await self._alias_error_embed(e, target, kind="event")
            )
            return
        await self.bot.pjsk.add_event_alias_local(ev.id, target)  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Added alias for `{ev.name}` (ID `{ev.id}`)\nAlias: `{target}`",
                title="Added alias!",
            )
        )

    @alias_event.command(
        name="remove", description="Authorized only; remove an event alias."
    )
    @app_commands.autocomplete(event=autocompletes.pjsk_event_alias)
    @app_commands.describe(event="Event name or ID.", alias="Alias to remove.")
    async def event_remove(
        self, interaction: discord.Interaction, event: str, alias: str
    ) -> None:
        if await self._deny_alias(interaction):
            return
        await interaction.response.defer(thinking=True)
        ev = converters.match_event(self.bot.pjsk, event)  # type: ignore[arg-type]
        if not ev:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find an event matching `{event}`.")
            )
            return
        target = preprocess(alias)
        try:
            existing = await self.bot.sbuga.get_event_aliases()  # type: ignore[union-attr]
            match = next(
                (a for a in existing if a.event_id == ev.id and a.alias == target),
                None,
            )
            if not match:
                await interaction.followup.send(
                    embed=embeds.error_embed(f"No alias `{target}` on `{ev.name}`.")
                )
                return
            await self.bot.sbuga.remove_event_alias(match.id)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't remove alias: {e.detail or e.status}"
                )
            )
            return
        await self.bot.pjsk.remove_event_alias_local(ev.id, target)  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Removed alias for `{ev.name}` (ID `{ev.id}`)\nAlias: `{target}`",
                title="Removed alias!",
            )
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(InfoCog(bot))
