import asyncio

import asyncpg

from helpers.config_loader import get_config

USER_COLUMNS = [
    "discord_id",
    "pjsk_id_en",
    "pjsk_id_jp",
    "pjsk_id_tw",
    "pjsk_id_kr",
    "pjsk_id_cn",
    "guess_stats",
    "settings",
    "blacklisted",
]


async def main() -> None:
    cfg = get_config()
    old_dsn = cfg.get("migrate", {}).get("old_dsn")
    if not old_dsn:
        raise SystemExit("[migrate] set migrate.old_dsn in config.yml first")

    psql = cfg["psql"]
    old = await asyncpg.connect(old_dsn)
    new = await asyncpg.create_pool(
        host=psql["host"],
        user=psql["user"],
        database=psql["database"],
        port=psql["port"],
        password=psql["password"],
    )
    assert new is not None

    try:
        # users
        cols = ", ".join(USER_COLUMNS)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(USER_COLUMNS)))
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in USER_COLUMNS if c != "discord_id"
        )
        insert_sql = (
            f"INSERT INTO users ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT (discord_id) DO UPDATE SET {updates}"
        )
        rows = await old.fetch(f"SELECT {cols} FROM users")
        async with new.acquire() as conn:
            for r in rows:
                await conn.execute(insert_sql, *[r[c] for c in USER_COLUMNS])
        print(f"[migrate] users: {len(rows)} migrated")

        # guilds
        grows = await old.fetch("SELECT guild_id, guessing_enabled FROM guilds")
        async with new.acquire() as conn:
            for r in grows:
                await conn.execute(
                    "INSERT INTO guilds (guild_id, guessing_enabled) VALUES ($1, $2) "
                    "ON CONFLICT (guild_id) DO UPDATE SET guessing_enabled = EXCLUDED.guessing_enabled",
                    r["guild_id"],
                    r["guessing_enabled"],
                )
        print(f"[migrate] guilds: {len(grows)} migrated")
        print("[migrate] done (counters/ranked skipped; Twitch columns dropped)")
    finally:
        await old.close()
        await new.close()


if __name__ == "__main__":
    asyncio.run(main())
