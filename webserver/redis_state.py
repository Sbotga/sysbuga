from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

# Shared cross-worker state for the standalone activity server. Every uvicorn worker
# is its own process, so a request can land on any of them; round state, the OAuth
# token cache, and (see spectate.py) the spectate presence graph all live in Redis
# instead of process memory. Keys are TTL'd so nothing needs manual pruning.

_KEY = "act"
ROUND_TTL = 900  # covers a round plus its post-answer reveal window
TOKEN_TTL = 600

_redis: aioredis.Redis | None = None


async def init_redis(cfg: dict[str, Any]) -> aioredis.Redis:
    global _redis
    password = cfg.get("password") or None
    _redis = aioredis.Redis(
        host=cfg.get("host", "127.0.0.1"),
        port=int(cfg.get("port", 6379)),
        db=int(cfg.get("db", 0)),
        password=password,
        decode_responses=False,  # round/reveal are raw PNG bytes
    )
    await _redis.ping()  # type: ignore[misc]  # redis.asyncio stub types ping() as sync
    return _redis


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("redis not initialised; call init_redis() first")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# --- round state -----------------------------------------------------------


def _rk(round_id: str) -> str:
    return f"{_KEY}:round:{round_id}"


async def save_round(
    round_id: str,
    user_id: int,
    meta: dict[str, Any],
    image: bytes | None,
    reveal: bytes | None,
) -> None:
    """Store a round's metadata (+ optional image/reveal bytes) and mark it as the user's
    active round, replacing any previous one."""
    r = get_redis()
    prev = await r.getdel(f"{_KEY}:uround:{user_id}")
    if prev:
        await _del_round(r, prev.decode())

    async with r.pipeline(transaction=True) as pipe:
        pipe.set(_rk(round_id), json.dumps(meta).encode(), ex=ROUND_TTL)
        if image is not None:
            pipe.set(f"{_rk(round_id)}:img", image, ex=ROUND_TTL)
        if reveal is not None:
            pipe.set(f"{_rk(round_id)}:rev", reveal, ex=ROUND_TTL)
        pipe.set(f"{_KEY}:uround:{user_id}", round_id.encode(), ex=ROUND_TTL)
        await pipe.execute()


async def set_round_image(round_id: str, image: bytes) -> None:
    """Replace the served image/audio blob (a music hint reveals a longer clip), keeping the
    round's remaining ttl."""
    r = get_redis()
    ttl = await r.ttl(f"{_rk(round_id)}:img")
    await r.set(f"{_rk(round_id)}:img", image, ex=ttl if ttl and ttl > 0 else ROUND_TTL)


async def set_round_stage(round_id: str, stage: int, clip: bytes) -> None:
    """Stash a small pre-generated music stage clip so a hint can swap it in instantly."""
    r = get_redis()
    ttl = await r.ttl(_rk(round_id))
    await r.set(
        f"{_rk(round_id)}:s{stage}", clip, ex=ttl if ttl and ttl > 0 else ROUND_TTL
    )


async def get_round_stage(round_id: str, stage: int) -> bytes | None:
    return await get_redis().get(f"{_rk(round_id)}:s{stage}")


async def get_round(round_id: str) -> dict[str, Any] | None:
    raw = await get_redis().get(_rk(round_id))
    return json.loads(raw) if raw else None


async def update_round(round_id: str, meta: dict[str, Any]) -> None:
    # preserve the remaining ttl so a finished round still expires on schedule
    r = get_redis()
    ttl = await r.ttl(_rk(round_id))
    await r.set(
        _rk(round_id),
        json.dumps(meta).encode(),
        ex=ttl if ttl and ttl > 0 else ROUND_TTL,
    )


async def get_round_image(round_id: str) -> bytes | None:
    return await get_redis().get(f"{_rk(round_id)}:img")


async def get_round_reveal(round_id: str) -> bytes | None:
    return await get_redis().get(f"{_rk(round_id)}:rev")


async def _del_round(r: aioredis.Redis, round_id: str) -> None:
    await r.delete(
        _rk(round_id),
        f"{_rk(round_id)}:img",
        f"{_rk(round_id)}:rev",
        f"{_rk(round_id)}:s2",
        f"{_rk(round_id)}:s3",
        f"{_rk(round_id)}:s4",
    )


async def finish_round(round_id: str, user_id: int) -> None:
    """Mark a round finished and free the user's active slot, but keep the round
    itself (its ttl) so the reveal image can still be fetched briefly."""
    r = get_redis()
    meta = await get_round(round_id)
    if meta:
        meta["finished"] = True
        await update_round(round_id, meta)
    cur = await r.get(f"{_KEY}:uround:{user_id}")
    if cur and cur.decode() == round_id:
        await r.delete(f"{_KEY}:uround:{user_id}")


# --- token cache -----------------------------------------------------------


async def cache_token(token: str, user_id: int) -> None:
    await get_redis().set(f"{_KEY}:token:{token}", str(user_id).encode(), ex=TOKEN_TTL)


async def get_cached_token(token: str) -> int | None:
    raw = await get_redis().get(f"{_KEY}:token:{token}")
    return int(raw) if raw else None
