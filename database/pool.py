import asyncpg

from helpers.config_loader import get_config

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    global _pool
    cfg = get_config()["psql"]
    _pool = await asyncpg.create_pool(
        host=cfg["host"],
        user=cfg["user"],
        database=cfg["database"],
        port=cfg["port"],
        password=cfg["password"],
        min_size=cfg.get("pool_min_size", 3),
        max_size=cfg.get("pool_max_size", 10),
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized; call create_pool() first.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
