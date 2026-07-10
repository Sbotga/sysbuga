from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import aiohttp
from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel

from cogs.guessing import (
    GUESS_TIME,
    MODE_TIME,
    _crop_chart,
    _crop_square,
    _fetch_bytes,
)
from data.pjsk import character_display_name
from data.search import preprocess
from data.song_equivalents import songs_equivalent
from helpers import converters, unblock
from services import chart_clip, chart_preview
from webserver import redis_state, spectate

if TYPE_CHECKING:
    from data.pjsk import PJSKData
    from database.queries import UserData
    from services.sbuga import SbugaClient

DISCORD_API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"

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

# small per-worker cache for proxied avatars (they rarely change and are tiny)
_avatar_cache: "OrderedDict[str, bytes]" = OrderedDict()
_AVATAR_MAX = 256


class StartBody(BaseModel):
    mode: str


class SubmitBody(BaseModel):
    round_id: str
    guess: str


class RoundBody(BaseModel):
    round_id: str


class ThemeBody(BaseModel):
    theme: str


def _pjsk(app_state: Any) -> "PJSKData":
    pjsk = getattr(app_state, "pjsk", None)
    if pjsk is None:
        raise HTTPException(status_code=503, detail="pjsk data unavailable")
    return pjsk


def _user_data(app_state: Any) -> "UserData | None":
    return getattr(app_state, "user_data", None)


async def _resolve_user(authorization: str | None) -> int:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    cached = await redis_state.get_cached_token(token)
    if cached is not None:
        return cached
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=401, detail="invalid token")
            user = await resp.json()
    user_id = int(user["id"])
    await redis_state.cache_token(token, user_id)
    return user_id


async def _safe_fetch(url: str) -> bytes | None:
    try:
        return await _fetch_bytes(url)
    except Exception:
        return None


async def _build_round(
    pjsk: "PJSKData", sbuga: "SbugaClient", mode: str
) -> dict[str, Any] | None:
    """Round payload mirroring GuessCog._build_round, minus Discord bits."""
    now_ms = int(time.time() * 1000)
    round_data: dict[str, Any] = {
        "mode": mode,
        "prompt": None,
        "image": None,
        "image_media": "image/png",
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
            clip = None
            if chart_preview.available():
                sus = await _safe_fetch(pjsk.chart_source_url(music.id, diff, region))
                if sus:
                    try:
                        clip = await chart_clip.render_clip(
                            sus.decode("utf-8", "replace")
                        )
                    except chart_clip.ChartClipError:
                        clip = None  # renderer missing/broken: fall back to the crop
            if clip:
                round_data["image"] = clip
                round_data["image_media"] = "video/mp4"
                return round_data
            try:
                png = await sbuga.get_chart_image(music.id, diff, region)  # type: ignore[arg-type]
            except Exception:
                return None
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


def _match(pjsk: "PJSKData", round_data: dict[str, Any], guess: str):
    """(id, display name, matched key). The key is the alias the query actually hit;
    it's the display name itself for non-song rounds."""
    if round_data["type"] == "song":
        hit = converters.match_song_with_key(pjsk, guess)
        return (hit[0].id, hit[0].title, hit[1]) if hit else None
    if round_data["type"] == "character":
        char = converters.match_character(pjsk, guess)
        name = character_display_name(char) if char else ""
        return (char.id, name, name) if char else None
    event = converters.match_event(pjsk, guess)
    return (event.id, event.name, event.name) if event else None


def _meta(
    round_data: dict[str, Any], user_id: int, expires_at: float
) -> dict[str, Any]:
    return {
        "mode": round_data["mode"],
        "type": round_data["type"],
        "answer_id": round_data["answer_id"],
        "answer_name": round_data["answer_name"],
        "prompt": round_data["prompt"],
        "has_image": round_data["image"] is not None,
        "image_media": round_data["image_media"],
        "has_reveal": round_data["reveal"] is not None,
        "expires_at": expires_at,
        "user_id": user_id,
        "finished": False,
    }


@router.get("/settings")
async def get_settings(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user_id = await _resolve_user(authorization)
    user_data = _user_data(request.app.state)
    theme = "dark"
    if user_data:
        theme = await user_data.get_settings(user_id, "activity_theme")
    return {"theme": theme}


@router.post("/settings")
async def save_settings(
    request: Request,
    body: ThemeBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user_id = await _resolve_user(authorization)
    user_data = _user_data(request.app.state)
    theme = body.theme if body.theme in ("dark", "light") else "dark"
    if user_data:
        await user_data.change_settings(user_id, "activity_theme", theme)
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
    state = request.app.state
    user_id = await _resolve_user(authorization)
    if body.mode not in MODES:
        raise HTTPException(status_code=400, detail="unknown mode")

    round_data = await _build_round(_pjsk(state), state.sbuga, body.mode)
    if round_data is None:
        raise HTTPException(status_code=503, detail="that mode isn't available yet")

    round_id = secrets.token_urlsafe(24)
    now = time.time()
    expires_at = now + MODE_TIME.get(body.mode, GUESS_TIME)
    meta = _meta(round_data, user_id, expires_at)
    meta["started_at"] = now
    await redis_state.save_round(
        round_id, user_id, meta, round_data["image"], round_data["reveal"]
    )

    return {
        "round_id": round_id,
        "mode": body.mode,
        "type": meta["type"],
        "prompt": meta["prompt"],
        "has_image": meta["has_image"],
        "image_media": meta["image_media"],
        "has_reveal": meta["has_reveal"],
        "expires_at": expires_at,
    }


@router.get("/guess/round/{round_id}/image")
async def round_image(round_id: str) -> Response:
    img = await redis_state.get_round_image(round_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    meta = await redis_state.get_round(round_id)
    media = (meta or {}).get("image_media", "image/png")
    return Response(content=img, media_type=media)


@router.get("/guess/round/{round_id}/reveal")
async def round_reveal(round_id: str) -> Response:
    img = await redis_state.get_round_reveal(round_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    return Response(content=img, media_type="image/png")


@router.post("/guess/hint")
async def hint(
    request: Request,
    body: RoundBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user_id = await _resolve_user(authorization)
    meta = await redis_state.get_round(body.round_id)
    if not meta or meta["user_id"] != user_id or meta.get("finished"):
        raise HTTPException(status_code=404, detail="no active round")
    name = str(meta["answer_name"])
    masked = name[0] + "".join("_" if c != " " else " " for c in name[1:])
    user_data = _user_data(request.app.state)
    if user_data:
        await user_data.add_guesses(user_id, meta["mode"], "hint")
    return {"hint": masked, "length": len(name)}


@router.post("/guess/reveal")
async def reveal_answer(
    body: RoundBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """End a round without a correct guess (timer ran out). No stat recorded,
    matching chat guessing where timeouts don't count."""
    user_id = await _resolve_user(authorization)
    meta = await redis_state.get_round(body.round_id)
    if not meta or meta["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="no active round")
    await redis_state.finish_round(body.round_id, user_id)
    return {"answer": meta["answer_name"], "has_reveal": meta["has_reveal"]}


@router.post("/guess/submit")
async def submit_guess(
    request: Request,
    body: SubmitBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    state = request.app.state
    user_id = await _resolve_user(authorization)
    meta = await redis_state.get_round(body.round_id)
    if not meta or meta["user_id"] != user_id or meta.get("finished"):
        raise HTTPException(status_code=404, detail="no active round")

    user_data = _user_data(state)
    if time.time() > meta["expires_at"]:
        await redis_state.finish_round(body.round_id, user_id)
        return {
            "result": "expired",
            "answer": meta["answer_name"],
            "has_reveal": meta["has_reveal"],
        }

    matched = _match(_pjsk(state), meta, body.guess)
    if matched is None:
        return {"result": "not_found"}
    correct = (
        songs_equivalent(matched[0], meta["answer_id"])
        if meta["type"] == "song"
        else matched[0] == meta["answer_id"]
    )
    if correct:
        await redis_state.finish_round(body.round_id, user_id)
        if user_data:
            await user_data.add_guesses(user_id, meta["mode"], "success")
        resp = {
            "result": "correct",
            "answer": meta["answer_name"],
            "has_reveal": meta["has_reveal"],
        }
        started = meta.get("started_at")
        if started is not None:
            resp["time"] = round(time.time() - started, 2)
        return resp
    if user_data:
        await user_data.add_guesses(user_id, meta["mode"], "fail")
    # matched_key is the alias the guess actually hit; omitted when it is the name
    resp: dict[str, Any] = {"result": "incorrect", "matched": matched[1]}
    if preprocess(matched[2]) != preprocess(matched[1]):
        resp["matched_key"] = matched[2]
    return resp


# --- avatar proxy (activities can't load cdn.discordapp.com directly) -------


def _avatar_path(user_id: int, avatar_hash: str | None) -> str:
    if avatar_hash:
        return f"/api/activity/avatar/{user_id}?h={avatar_hash}"
    return f"/api/activity/avatar/{user_id}"


@router.get("/avatar/{user_id}")
async def avatar(user_id: int, h: str | None = None) -> Response:
    key = f"{user_id}:{h or ''}"
    data = _avatar_cache.get(key)
    if data is None:
        default = f"{CDN}/embed/avatars/{user_id % 5}.png"
        url = f"{CDN}/avatars/{user_id}/{h}.png?size=64" if h else default
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200 and h:
                        async with session.get(default) as r2:
                            data = await r2.read()
                    elif resp.status != 200:
                        raise HTTPException(
                            status_code=404, detail="avatar unavailable"
                        )
                    else:
                        data = await resp.read()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="avatar fetch failed")
        _avatar_cache[key] = data
        _avatar_cache.move_to_end(key)
        while len(_avatar_cache) > _AVATAR_MAX:
            _avatar_cache.popitem(last=False)
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --- spectate websocket ----------------------------------------------------


@router.websocket("/ws")
async def spectate_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    member: spectate.LocalMember | None = None
    instance_id: str | None = None
    writer: asyncio.Task | None = None
    try:
        hello = json.loads(await websocket.receive_text())
        if hello.get("op") != "hello":
            await websocket.close(code=4001)
            return
        try:
            user_id = await _resolve_user(f"Bearer {hello.get('token')}")
        except HTTPException:
            await websocket.close(code=4003)
            return
        instance_id = str(hello.get("instance_id") or "").strip()
        if not instance_id:
            await websocket.close(code=4002)
            return

        name = str(hello.get("name") or "Player")[:64]
        avatar = _avatar_path(
            user_id, str(hello.get("avatar")) if hello.get("avatar") else None
        )
        member = spectate.LocalMember(user_id, name, avatar, websocket, instance_id)
        await spectate.join(instance_id, member)

        async def _drain(m: spectate.LocalMember) -> None:
            try:
                while True:
                    await websocket.send_text(await m.queue.get())
            except Exception:
                return

        writer = asyncio.create_task(_drain(member))

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if isinstance(msg, dict):
                await spectate.handle(instance_id, member, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if writer is not None:
            writer.cancel()
        if member is not None and instance_id is not None:
            await spectate.leave(instance_id, member)
