import asyncio

import asyncpg

from helpers.config_loader import get_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    discord_id BIGINT UNIQUE,
    pjsk_id_en BIGINT,
    pjsk_id_jp BIGINT,
    pjsk_id_tw BIGINT,
    pjsk_id_kr BIGINT,
    pjsk_id_cn BIGINT,
    guess_stats JSONB,
    settings JSONB,
    blacklisted BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS guilds (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT UNIQUE NOT NULL,
    guessing_enabled BOOLEAN DEFAULT true
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    discord_id BIGINT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at TIMESTAMPTZ,
    scopes TEXT[] DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""


async def main() -> None:
    cfg = get_config()["psql"]
    conn = await asyncpg.connect(
        host=cfg["host"],
        user=cfg["user"],
        database=cfg["database"],
        port=cfg["port"],
        password=cfg["password"],
    )
    try:
        await conn.execute(SCHEMA)
        print("[database_setup] schema created (users, guilds, oauth_tokens)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
