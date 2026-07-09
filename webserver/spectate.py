from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import WebSocket

from webserver.redis_state import get_redis

# Cross-worker live spectating. Each activity client holds one websocket to
# /api/activity/ws and joins a room keyed by the Discord activity *instance id*
# (shared by everyone who launched the activity in the same channel). Because the
# server runs N uvicorn workers, room members are spread across processes, so all
# shared room state lives in Redis and live events fan out over Redis pub/sub:
#   act:room:{inst}:members       HASH  uid -> {name, avatar, active}
#   act:room:{inst}:state:{uid}   JSON  {round, typing, log, result}
#   act:room:{inst}:watch:{uid}   SET   uids currently spectating this player
#   act:pub:{inst}                pub/sub channel carrying the fan-out envelopes
# Each worker keeps only its *local* sockets in memory and forwards the events it
# receives to whichever of them are affected.

_LOG_CAP = 100
_TEXT_CAP = 200
ROOM_TTL = 3600  # safety net so a crashed worker's presence eventually clears

# instance_id -> {user_id -> LocalMember} for sockets connected to THIS worker only
_local: dict[str, dict[int, "LocalMember"]] = {}
_lock = asyncio.Lock()
_listener_task: asyncio.Task | None = None


def _empty_state() -> dict[str, Any]:
    return {"round": None, "typing": "", "log": [], "result": None}


class LocalMember:
    def __init__(
        self, user_id: int, name: str, avatar: str, ws: WebSocket, instance: str
    ) -> None:
        self.user_id = user_id
        self.name = name
        self.avatar = avatar  # ready-to-use proxied path (client prefixes API)
        self.ws = ws
        self.instance = instance
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self.watching: int | None = None

    def send(self, msg: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(json.dumps(msg))
        except asyncio.QueueFull:
            pass


# --- redis key helpers -----------------------------------------------------


def _members_key(inst: str) -> str:
    return f"act:room:{inst}:members"


def _state_key(inst: str, uid: int) -> str:
    return f"act:room:{inst}:state:{uid}"


def _watch_key(inst: str, uid: int) -> str:
    return f"act:room:{inst}:watch:{uid}"


def _chan(inst: str) -> str:
    return f"act:pub:{inst}"


async def _publish(inst: str, msg: dict[str, Any]) -> None:
    # redis.asyncio's command stubs resolve to the sync overloads under pyright, so
    # command calls go through an Any-typed handle to keep the awaits honest.
    r: Any = get_redis()
    await r.publish(_chan(inst), json.dumps(msg))


async def _members_list(inst: str) -> list[dict[str, Any]]:
    r: Any = get_redis()
    raw = await r.hgetall(_members_key(inst))
    out: list[dict[str, Any]] = []
    for uid, val in raw.items():
        try:
            info = json.loads(val)
        except Exception:
            continue
        uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
        # ids are 64-bit snowflakes -> serialise as strings so JS keeps precision
        out.append({"id": uid_s, **info})
    out.sort(key=lambda m: m["name"].lower())
    return out


async def _set_presence(inst: str, member: "LocalMember", active: bool) -> None:
    r: Any = get_redis()
    await r.hset(
        _members_key(inst),
        str(member.user_id),
        json.dumps({"name": member.name, "avatar": member.avatar, "active": active}),
    )
    await r.expire(_members_key(inst), ROOM_TTL)


async def _get_state(inst: str, uid: int) -> dict[str, Any]:
    r: Any = get_redis()
    raw = await r.get(_state_key(inst, uid))
    if not raw:
        return _empty_state()
    try:
        return json.loads(raw)
    except Exception:
        return _empty_state()


async def _set_state(inst: str, uid: int, state: dict[str, Any]) -> None:
    r: Any = get_redis()
    await r.set(_state_key(inst, uid), json.dumps(state), ex=ROOM_TTL)


async def _watchers_public(inst: str, target: int) -> list[dict[str, Any]]:
    r: Any = get_redis()
    ids = await r.smembers(_watch_key(inst, target))
    if not ids:
        return []
    fields = [i if isinstance(i, str) else i.decode() for i in ids]
    vals = await r.hmget(_members_key(inst), fields)
    out: list[dict[str, Any]] = []
    for uid, val in zip(fields, vals):
        if not val:
            continue
        try:
            info = json.loads(val)
        except Exception:
            continue
        out.append({"id": uid, "name": info.get("name"), "avatar": info.get("avatar")})
    return out


# --- payload sanitisers (clients supply these; keep Redis entries bounded) --


def _clean_round(r: Any) -> dict[str, Any] | None:
    if not isinstance(r, dict):
        return None
    return {
        "round_id": str(r.get("round_id") or "")[:64],
        "mode": str(r.get("mode") or "")[:64],
        "mode_label": str(r.get("mode_label") or "")[:64],
        "prompt": (str(r.get("prompt")) if r.get("prompt") is not None else None),
        "has_image": bool(r.get("has_image")),
        "has_reveal": bool(r.get("has_reveal")),
        "expires_at": float(r.get("expires_at") or 0),
    }


def _clean_entry(e: Any) -> dict[str, Any] | None:
    if not isinstance(e, dict):
        return None
    return {
        "marker": str(e.get("marker") or "")[:8],
        "text": str(e.get("text") or "")[:_TEXT_CAP],
        "cls": str(e.get("cls") or "")[:16],
    }


def _clean_result(r: Any) -> dict[str, Any] | None:
    if not isinstance(r, dict):
        return None
    return {
        "text": str(r.get("text") or "")[:_TEXT_CAP],
        "cls": str(r.get("cls") or "")[:16],
        "round_id": str(r.get("round_id") or "")[:64],
        "has_reveal": bool(r.get("has_reveal")),
    }


# --- membership ------------------------------------------------------------


async def join(inst: str, member: LocalMember) -> None:
    async with _lock:
        room = _local.setdefault(inst, {})
        old = room.get(member.user_id)
        room[member.user_id] = member
    if old is not None and old is not member:
        old.send({"op": "replaced"})
        asyncio.create_task(_safe_close(old.ws))

    await _set_state(inst, member.user_id, _empty_state())
    await _set_presence(inst, member, active=False)
    member.send({"op": "ready", "you": str(member.user_id)})
    members = await _members_list(inst)
    member.send({"op": "members", "members": members})
    await _publish(inst, {"k": "members", "members": members})


async def leave(inst: str, member: LocalMember) -> None:
    async with _lock:
        room = _local.get(inst)
        if room is None or room.get(member.user_id) is not member:
            return
        del room[member.user_id]
        if not room:
            _local.pop(inst, None)

    r: Any = get_redis()
    await r.hdel(_members_key(inst), str(member.user_id))
    await r.delete(_state_key(inst, member.user_id))
    await r.delete(_watch_key(inst, member.user_id))
    # anyone watching this player loses their target
    await _publish(inst, {"k": "gone", "target": member.user_id})
    # this player may have been watching someone -> refresh that target's badges
    if member.watching is not None:
        await r.srem(_watch_key(inst, member.watching), str(member.user_id))
        await _publish(
            inst,
            {
                "k": "watchers",
                "target": member.watching,
                "watchers": await _watchers_public(inst, member.watching),
            },
        )
    await _publish(inst, {"k": "members", "members": await _members_list(inst)})


# --- inbound message handling ----------------------------------------------


async def handle(inst: str, member: LocalMember, msg: dict[str, Any]) -> None:
    op = msg.get("op")
    uid = member.user_id

    if op == "typing":
        text = str(msg.get("text") or "")[:_TEXT_CAP]
        state = await _get_state(inst, uid)
        state["typing"] = text
        await _set_state(inst, uid, state)
        await _publish(
            inst, {"k": "event", "from": uid, "op": "typing", "p": {"text": text}}
        )

    elif op == "round":
        rnd = _clean_round(msg.get("round"))
        state = _empty_state()
        state["round"] = rnd
        await _set_state(inst, uid, state)
        await _set_presence(inst, member, active=rnd is not None)
        await _publish(
            inst, {"k": "event", "from": uid, "op": "round", "p": {"round": rnd}}
        )
        await _publish(inst, {"k": "members", "members": await _members_list(inst)})

    elif op == "log":
        entry = _clean_entry(msg.get("entry"))
        if entry is not None:
            state = await _get_state(inst, uid)
            log = state.setdefault("log", [])
            log.append(entry)
            del log[:-_LOG_CAP]
            await _set_state(inst, uid, state)
            await _publish(
                inst, {"k": "event", "from": uid, "op": "log", "p": {"entry": entry}}
            )

    elif op == "result":
        res = _clean_result(msg.get("result"))
        state = await _get_state(inst, uid)
        state["result"] = res
        state["typing"] = ""
        await _set_state(inst, uid, state)
        await _publish(
            inst, {"k": "event", "from": uid, "op": "result", "p": {"result": res}}
        )

    elif op == "clear":
        await _set_state(inst, uid, _empty_state())
        await _set_presence(inst, member, active=False)
        await _publish(inst, {"k": "event", "from": uid, "op": "clear", "p": {}})
        await _publish(inst, {"k": "members", "members": await _members_list(inst)})

    elif op == "watch":
        target = msg.get("target")
        target_id = (
            int(target)
            if isinstance(target, (int, str)) and str(target).lstrip("-").isdigit()
            else None
        )
        await _set_watch(inst, member, target_id)


async def _set_watch(inst: str, member: LocalMember, target_id: int | None) -> None:
    if target_id == member.user_id:
        target_id = None
    old = member.watching
    member.watching = target_id
    r: Any = get_redis()

    if old is not None and old != target_id:
        await r.srem(_watch_key(inst, old), str(member.user_id))
        await _publish(
            inst,
            {
                "k": "watchers",
                "target": old,
                "watchers": await _watchers_public(inst, old),
            },
        )

    if target_id is None:
        return

    if not await r.hexists(_members_key(inst), str(target_id)):
        member.watching = None
        member.send({"op": "watch_target_gone", "target": str(target_id)})
        return

    await r.sadd(_watch_key(inst, target_id), str(member.user_id))
    await r.expire(_watch_key(inst, target_id), ROOM_TTL)
    member.send(
        {
            "op": "snapshot",
            "target": str(target_id),
            "state": await _get_state(inst, target_id),
        }
    )
    await _publish(
        inst,
        {
            "k": "watchers",
            "target": target_id,
            "watchers": await _watchers_public(inst, target_id),
        },
    )


# --- pub/sub fan-out (one listener per worker) -----------------------------


async def _dispatch(inst: str, data: dict[str, Any]) -> None:
    async with _lock:
        room = _local.get(inst)
        members = list(room.values()) if room else []
    if not members:
        return
    k = data.get("k")

    if k == "members":
        for m in members:
            m.send({"op": "members", "members": data.get("members", [])})
    elif k == "event":
        frm = data.get("from")
        op = data.get("op")
        payload = data.get("p") or {}
        for m in members:
            if m.watching == frm:
                m.send({"op": op, "from": str(frm), **payload})
    elif k == "watchers":
        target = data.get("target")
        for m in members:
            if m.user_id == target:
                m.send({"op": "watchers", "watchers": data.get("watchers", [])})
    elif k == "gone":
        target = data.get("target")
        for m in members:
            if m.watching == target:
                m.watching = None
                m.send({"op": "watch_target_gone", "target": str(target)})


async def _listen() -> None:
    pubsub = get_redis().pubsub()
    await pubsub.psubscribe("act:pub:*")
    try:
        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                continue
            channel = message["channel"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            inst = channel[len("act:pub:") :]
            try:
                data = json.loads(message["data"])
            except Exception:
                continue
            await _dispatch(inst, data)
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await pubsub.aclose()


async def start_hub() -> None:
    global _listener_task
    if _listener_task is None:
        _listener_task = asyncio.create_task(_listen())


async def stop_hub() -> None:
    global _listener_task
    if _listener_task is not None:
        _listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _listener_task
        _listener_task = None


async def _safe_close(ws: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await ws.close()
