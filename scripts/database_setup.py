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

-- channels where leaked content is shown; leaks are blocked everywhere else
CREATE TABLE IF NOT EXISTS leak_channels (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT UNIQUE NOT NULL
);

-- the old per-guild allow_leaks boolean is replaced by the per-channel whitelist above;
-- drop it so any existing value is erased (everyone starts with no leak channels)
ALTER TABLE guilds DROP COLUMN IF EXISTS allow_leaks;

CREATE TABLE IF NOT EXISTS oauth_tokens (
    discord_id BIGINT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at TIMESTAMPTZ,
    scopes TEXT[] DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- points earned per guess mode in each weekly/monthly leaderboard period; the combined board is
-- the sum across modes. every period is kept forever so old boards stay queryable
CREATE TABLE IF NOT EXISTS guess_points (
    discord_id BIGINT NOT NULL,
    period_type TEXT NOT NULL,   -- 'weekly' | 'monthly'
    period_index INT NOT NULL,   -- 1-based week/month number
    mode TEXT NOT NULL,          -- chart | jacket | music | event
    points BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (discord_id, period_type, period_index, mode)
);
CREATE INDEX IF NOT EXISTS guess_points_period_idx
    ON guess_points (period_type, period_index);

-- per-user guess-attempt counters in fixed UTC hour/day buckets, shared by the bot and activity.
-- once a user passes the hourly/daily cap their correct guesses stop earning points
CREATE TABLE IF NOT EXISTS guess_rate (
    discord_id BIGINT PRIMARY KEY,
    hour_key BIGINT NOT NULL,    -- unix hour bucket (epoch // 3600)
    hour_count INT NOT NULL DEFAULT 0,
    day_key BIGINT NOT NULL,     -- unix day bucket (epoch // 86400)
    day_count INT NOT NULL DEFAULT 0
);

-- marks a weekly/monthly period whose prizes have been awarded, so finalization runs once
CREATE TABLE IF NOT EXISTS leaderboard_runs (
    period_type TEXT NOT NULL,
    period_index INT NOT NULL,
    finalized_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_type, period_index)
);

-- one prize per user per finalized period. status: unclaimed -> pending (claimed, awaiting manual
-- grant) -> complete; or forfeited; an unclaimed prize past expires_at is treated as expired
CREATE TABLE IF NOT EXISTS prizes (
    id SERIAL PRIMARY KEY,
    discord_id BIGINT NOT NULL,
    period_type TEXT NOT NULL,
    period_index INT NOT NULL,
    rank INT NOT NULL,
    paid_crystals INT NOT NULL,
    free_crystals INT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unclaimed',  -- unclaimed|pending|complete|denied|forfeited
    pjsk_id BIGINT,              -- the EN account chosen at claim time
    deny_reason TEXT,           -- set when an admin denies the claim
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    claimed_at TIMESTAMPTZ,
    UNIQUE (discord_id, period_type, period_index)
);
CREATE INDEX IF NOT EXISTS prizes_user_idx ON prizes (discord_id, status);
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
        print(
            "[database_setup] schema created (users, guilds, leak_channels, oauth_tokens)"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
