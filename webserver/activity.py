from __future__ import annotations

import random
import secrets
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from cogs.guessing import (
    GUESS_TIME,
    MODE_TIME,
    _crop_chart,
    _crop_square,
    _fetch_bytes,
)
from data.pjsk import character_display_name
from helpers import converters, unblock

if TYPE_CHECKING:
    from main import SbugaBot

DISCORD_API = "https://discord.com/api/v10"

MODES: dict[str, str] = {
    "jacket": "Jacket",
    "jacket_30px": "Jacket (30px)",
    "jacket_bw": "Jacket (grayscale)",
    "jacket_challenge": "Jacket (challenge)",
    "notes": "Note Count",
    "chart": "Chart",
    "chart_append": "Chart (Append)",
    "character": "Character",
    "character_bw": "Character (grayscale)",
    "event": "Event",
}

router = APIRouter(prefix="/api/activity")

_rounds: dict[str, dict[str, Any]] = {}  # round_id -> round
_user_rounds: dict[int, str] = {}  # user_id -> active round_id
_token_cache: dict[str, tuple[int, float]] = {}  # bearer -> (user_id, cached_at)
TOKEN_CACHE_TTL = 600


class StartBody(BaseModel):
    mode: str


class SubmitBody(BaseModel):
    round_id: str
    guess: str


class RoundBody(BaseModel):
    round_id: str


class ThemeBody(BaseModel):
    theme: str


async def _resolve_user(authorization: str | None) -> int:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    cached = _token_cache.get(token)
    if cached and cached[1] + TOKEN_CACHE_TTL > time.time():
        return cached[0]
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=401, detail="invalid token")
            user = await resp.json()
    user_id = int(user["id"])
    _token_cache[token] = (user_id, time.time())
    return user_id


def _finish(round_id: str, user_id: int) -> None:
    """End the active round but keep it briefly so the reveal image can load."""
    _user_rounds.pop(user_id, None)
    data = _rounds.get(round_id)
    if data:
        data["finished"] = True
        data["reveal_until"] = time.time() + 120


async def _safe_fetch(url: str) -> bytes | None:
    try:
        return await _fetch_bytes(url)
    except Exception:
        return None


def _prune() -> None:
    now = time.time()
    for rid, data in list(_rounds.items()):
        keep_until = data.get("reveal_until", data["expires_at"] + 300)
        if keep_until < now:
            _rounds.pop(rid, None)
    for uid in [u for u, r in _user_rounds.items() if r not in _rounds]:
        _user_rounds.pop(uid, None)


async def _build_round(bot: SbugaBot, mode: str) -> dict[str, Any] | None:
    """Round payload mirroring GuessCog._build_round, minus Discord bits."""
    pjsk = bot.pjsk
    assert pjsk is not None
    now_ms = int(time.time() * 1000)
    round_data: dict[str, Any] = {
        "mode": mode,
        "prompt": None,
        "image": None,
        "reveal": None,
    }

    if mode in (
        "jacket",
        "jacket_30px",
        "jacket_bw",
        "jacket_challenge",
        "notes",
        "chart",
        "chart_append",
    ):
        musics = [
            m
            for m in pjsk.musics()
            if mode != "chart_append"
            or any(d.difficulty == "append" for d in m.difficulties)
        ]
        if not musics:
            return None
        music = random.choice(musics)
        round_data["type"] = "song"
        round_data["answer_id"] = music.id
        round_data["answer_name"] = music.title

        jacket = await _safe_fetch(music.jacket_url)
        round_data["reveal"] = jacket  # full jacket shown on reveal, all song modes

        if mode == "notes":
            master = next(
                (d for d in music.difficulties if d.difficulty == "master"), None
            )
            if not master:
                return None
            round_data["prompt"] = (
                f"This song has {master.total_note_count} notes on Master."
            )
            return round_data

        if mode in ("chart", "chart_append"):
            diff = "append" if mode == "chart_append" else "master"
            region = next(
                (r for r in pjsk.regions_for_music(music.id) if r in ("en", "jp")),
                "en",
            )
            assert bot.sbuga is not None
            png = await bot.sbuga.get_chart_image(music.id, diff, region)  # type: ignore[arg-type]
            round_data["image"] = (
                await unblock.to_process_with_timeout(_crop_chart, png)
            ).getvalue()
            return round_data

        if not jacket:
            return None
        size, bw = 140, False
        if mode == "jacket_30px":
            size = 30
        elif mode == "jacket_bw":
            bw = True
        elif mode == "jacket_challenge":
            size, bw = 30, True
        round_data["image"] = (
            await unblock.to_process_with_timeout(_crop_square, jacket, size, bw)
        ).getvalue()
        return round_data

    if mode in ("character", "character_bw"):
        cards = [
            c
            for c in pjsk.cards()
            if c.card_rarity_type in ("rarity_3", "rarity_4", "rarity_birthday")
            and (c.release_at or 0) <= now_ms
            and (c.card_url_normal or c.card_url_trained)
        ]
        if not cards:
            return None
        card = random.choice(cards)
        char = pjsk.get_character(card.character_id)
        if not char:
            return None
        trained = card.card_rarity_type != "rarity_birthday" and bool(
            random.randint(0, 1)
        )
        url = (
            card.card_url_trained if trained else card.card_url_normal
        ) or card.card_url_normal
        art = await _fetch_bytes(url) if url else None
        if not art:
            return None
        round_data["type"] = "character"
        round_data["answer_id"] = char.id
        round_data["answer_name"] = character_display_name(char)
        round_data["reveal"] = art
        round_data["image"] = (
            await unblock.to_process_with_timeout(
                _crop_square, art, 250, mode == "character_bw"
            )
        ).getvalue()
        return round_data

    if mode == "event":
        events = [
            e
            for e in pjsk.events()
            if (e.start_at or 0) <= now_ms and (e.background_url or e.banner_url)
        ]
        if not events:
            return None
        event = random.choice(events)
        url = event.background_url or event.banner_url
        bg = await _fetch_bytes(url) if url else None
        if not bg:
            return None
        round_data["type"] = "event"
        round_data["answer_id"] = event.id
        round_data["answer_name"] = event.name
        round_data["reveal"] = bg
        round_data["image"] = (
            await unblock.to_process_with_timeout(_crop_square, bg, 250, False)
        ).getvalue()
        return round_data

    return None


def _match(bot: SbugaBot, round_data: dict[str, Any], guess: str):
    pjsk = bot.pjsk
    assert pjsk is not None
    if round_data["type"] == "song":
        music = converters.match_song(pjsk, guess)
        return (music.id, music.title) if music else None
    if round_data["type"] == "character":
        char = converters.match_character(pjsk, guess)
        return (char.id, character_display_name(char)) if char else None
    event = converters.match_event(pjsk, guess)
    return (event.id, event.name) if event else None


@router.get("/settings")
async def get_settings(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    bot: SbugaBot = request.app.state.bot
    user_id = await _resolve_user(authorization)
    theme = "dark"
    if bot.user_data:
        theme = await bot.user_data.get_settings(user_id, "activity_theme")
    return {"theme": theme}


@router.post("/settings")
async def save_settings(
    request: Request,
    body: ThemeBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    bot: SbugaBot = request.app.state.bot
    user_id = await _resolve_user(authorization)
    theme = body.theme if body.theme in ("dark", "light") else "dark"
    if bot.user_data:
        await bot.user_data.change_settings(user_id, "activity_theme", theme)
    return {"theme": theme}


@router.get("/modes")
async def modes() -> list[dict[str, Any]]:
    return [
        {"value": value, "label": label, "seconds": MODE_TIME.get(value, GUESS_TIME)}
        for value, label in MODES.items()
    ]


@router.post("/guess/start")
async def start_round(
    request: Request,
    body: StartBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    bot: SbugaBot = request.app.state.bot
    user_id = await _resolve_user(authorization)
    if body.mode not in MODES:
        raise HTTPException(status_code=400, detail="unknown mode")
    _prune()

    round_data = await _build_round(bot, body.mode)
    if round_data is None:
        raise HTTPException(status_code=503, detail="that mode isn't available yet")

    round_id = secrets.token_urlsafe(24)
    round_data["user_id"] = user_id
    round_data["expires_at"] = time.time() + MODE_TIME.get(body.mode, GUESS_TIME)
    old = _user_rounds.pop(user_id, None)
    if old:
        _rounds.pop(old, None)
    _rounds[round_id] = round_data
    _user_rounds[user_id] = round_id

    return {
        "round_id": round_id,
        "mode": body.mode,
        "type": round_data["type"],
        "prompt": round_data["prompt"],
        "has_image": round_data["image"] is not None,
        "has_reveal": round_data["reveal"] is not None,
        "expires_at": round_data["expires_at"],
    }


@router.get("/guess/round/{round_id}/image")
async def round_image(round_id: str) -> Response:
    round_data = _rounds.get(round_id)
    if not round_data or not round_data["image"]:
        raise HTTPException(status_code=404, detail="not found")
    return Response(content=round_data["image"], media_type="image/png")


@router.get("/guess/round/{round_id}/reveal")
async def round_reveal(round_id: str) -> Response:
    round_data = _rounds.get(round_id)
    if not round_data or not round_data.get("reveal"):
        raise HTTPException(status_code=404, detail="not found")
    return Response(content=round_data["reveal"], media_type="image/png")


@router.post("/guess/hint")
async def hint(
    request: Request,
    body: RoundBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    bot: SbugaBot = request.app.state.bot
    user_id = await _resolve_user(authorization)
    round_data = _rounds.get(body.round_id)
    if not round_data or round_data["user_id"] != user_id or round_data.get("finished"):
        raise HTTPException(status_code=404, detail="no active round")
    name = str(round_data["answer_name"])
    masked = name[0] + "".join("_" if c != " " else " " for c in name[1:])
    if bot.user_data:
        await bot.user_data.add_guesses(user_id, round_data["mode"], "hint")
    return {"hint": masked, "length": len(name)}


@router.post("/guess/reveal")
async def reveal_answer(
    body: RoundBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """End a round without a correct guess (timer ran out). No stat recorded,
    matching chat guessing where timeouts don't count."""
    user_id = await _resolve_user(authorization)
    round_data = _rounds.get(body.round_id)
    if not round_data or round_data["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="no active round")
    _finish(body.round_id, user_id)
    return {
        "answer": round_data["answer_name"],
        "has_reveal": round_data["reveal"] is not None,
    }


@router.post("/guess/submit")
async def submit_guess(
    request: Request,
    body: SubmitBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    bot: SbugaBot = request.app.state.bot
    user_id = await _resolve_user(authorization)
    round_data = _rounds.get(body.round_id)
    if not round_data or round_data["user_id"] != user_id or round_data.get("finished"):
        raise HTTPException(status_code=404, detail="no active round")

    if time.time() > round_data["expires_at"]:
        _finish(body.round_id, user_id)
        return {
            "result": "expired",
            "answer": round_data["answer_name"],
            "has_reveal": round_data["reveal"] is not None,
        }

    matched = _match(bot, round_data, body.guess)
    if matched is None:
        return {"result": "not_found"}
    if matched[0] == round_data["answer_id"]:
        _finish(body.round_id, user_id)
        if bot.user_data:
            await bot.user_data.add_guesses(user_id, round_data["mode"], "success")
        return {
            "result": "correct",
            "answer": round_data["answer_name"],
            "has_reveal": round_data["reveal"] is not None,
        }
    if bot.user_data:
        await bot.user_data.add_guesses(user_id, round_data["mode"], "fail")
    return {"result": "incorrect", "matched": matched[1]}
