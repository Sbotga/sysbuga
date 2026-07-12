import datetime
import json
from typing import Any

import asyncpg

SETTING_DEFAULTS: dict[str, Any] = {
    "first_time_guess_end": True,
    "default_region": "en",
    "mirror_charts_by_default": False,
    "default_difficulty": "master",
    # activity-only (not surfaced in /user settings; set from the activity UI)
    "activity_theme": "dark",
}

GUESS_DEFAULT = {"fail": 0, "success": 0, "ragequit": 0, "hint": 0}

GUESS_LEADERBOARD_PER_PAGE = 20


class UserData:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.db = pool

    async def verify_discord_user(self, user_id: int) -> None:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM users WHERE discord_id = $1", user_id
            )
            if not row:
                await conn.execute(
                    "INSERT INTO users (discord_id) VALUES ($1)", user_id
                )

    async def verify_discord_guild(self, guild_id: int) -> None:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM guilds WHERE guild_id = $1", guild_id
            )
            if not row:
                await conn.execute(
                    "INSERT INTO guilds (guild_id) VALUES ($1)", guild_id
                )

    async def update_pjsk_id(self, user_id: int, pjsk_id: int, region: str) -> None:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            await conn.execute(
                f"UPDATE users SET pjsk_id_{region} = $1 WHERE discord_id = $2",
                pjsk_id,
                user_id,
            )

    async def remove_pjsk_id(self, user_id: int, region: str) -> None:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            await conn.execute(
                f"UPDATE users SET pjsk_id_{region} = $1 WHERE discord_id = $2",
                None,
                user_id,
            )

    async def set_banned(self, user_id: int, blacklisted: bool) -> None:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            await conn.execute(
                "UPDATE users SET blacklisted = $1 WHERE discord_id = $2",
                blacklisted,
                user_id,
            )

    async def get_banned(self, user_id: int) -> bool:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT blacklisted FROM users WHERE discord_id = $1", user_id
            )
            return (
                bool(result["blacklisted"])
                if result and result["blacklisted"]
                else False
            )

    async def get_pjsk_id(self, user_id: int, region: str) -> int | None:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                f"SELECT pjsk_id_{region} FROM users WHERE discord_id = $1", user_id
            )
            key = f"pjsk_id_{region}"
            return result[key] if result and result[key] else None

    async def get_discord_user_id_from_pjsk_id(
        self, pjsk_id: int, region: str
    ) -> int | None:
        pjsk_id = int(pjsk_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                f"SELECT discord_id FROM users WHERE pjsk_id_{region} = $1", pjsk_id
            )
            return result["discord_id"] if result else None

    async def get_guesses(self, user_id: int, key: str | None = None) -> dict:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT guess_stats FROM users WHERE discord_id = $1", user_id
            )
            stuff = (
                json.loads(result["guess_stats"])
                if result and result["guess_stats"]
                else {}
            )
            return stuff.get(key, dict(GUESS_DEFAULT)) if key else stuff

    async def get_settings(self, user_id: int, key: str | None = None) -> Any:
        await self.verify_discord_user(user_id)
        assert key in SETTING_DEFAULTS or key is None
        if key:
            key = key.lower().strip()
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT settings FROM users WHERE discord_id = $1", user_id
            )
            stuff = (
                json.loads(result["settings"]) if result and result["settings"] else {}
            )
            if key:
                return stuff.get(key, SETTING_DEFAULTS[key])
            return {k: stuff.get(k, v) for k, v in SETTING_DEFAULTS.items()}

    async def change_settings(self, user_id: int, key: str, value: Any) -> dict:
        await self.verify_discord_user(user_id)
        key = key.lower().strip()
        assert key in SETTING_DEFAULTS
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT settings FROM users WHERE discord_id = $1", user_id
            )
            stuff = (
                json.loads(result["settings"]) if result and result["settings"] else {}
            )
            stuff[key] = value
            for k in list(stuff.keys()):
                if k not in SETTING_DEFAULTS:
                    stuff.pop(k, None)
            await conn.execute(
                "UPDATE users SET settings = $1 WHERE discord_id = $2",
                json.dumps(stuff),
                user_id,
            )
            return stuff

    async def add_guesses(self, user_id: int, key: str, stat: str) -> dict:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT guess_stats FROM users WHERE discord_id = $1", user_id
            )
            guess_stats = (
                json.loads(result["guess_stats"])
                if result and result["guess_stats"]
                else {}
            )
            if key not in guess_stats:
                guess_stats[key] = dict(GUESS_DEFAULT)
            if stat in guess_stats[key]:
                guess_stats[key][stat] += 1
            await conn.execute(
                "UPDATE users SET guess_stats = $1 WHERE discord_id = $2",
                json.dumps(guess_stats),
                user_id,
            )
            return guess_stats[key]

    async def reset_guesses(
        self, user_id: int, key: str, stat: str | None = None
    ) -> dict:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT guess_stats FROM users WHERE discord_id = $1", user_id
            )
            guess_stats = (
                json.loads(result["guess_stats"])
                if result and result["guess_stats"]
                else {}
            )
            if key not in guess_stats:
                guess_stats[key] = dict(GUESS_DEFAULT)
            assert stat in (None, "fail", "success", "ragequit", "hint")
            if stat:
                guess_stats[key][stat] = 0
            await conn.execute(
                "UPDATE users SET guess_stats = $1 WHERE discord_id = $2",
                json.dumps(guess_stats),
                user_id,
            )
            return guess_stats[key]

    async def get_guesses_position(
        self, guess_type: str, user_id: int
    ) -> tuple[int, int]:
        per_page = GUESS_LEADERBOARD_PER_PAGE
        query = f"""
        WITH user_stats AS (
            SELECT discord_id,
                COALESCE(CAST(guess_stats->'{guess_type}'->>'success' AS INT), 0) AS success,
                COALESCE(CAST(guess_stats->'{guess_type}'->>'fail' AS INT), 0) AS fail
            FROM users WHERE discord_id = $1
        ),
        leaderboard AS (
            SELECT discord_id,
                CAST(guess_stats->'{guess_type}'->>'success' AS INT) AS score,
                ROW_NUMBER() OVER (
                    ORDER BY CAST(guess_stats->'{guess_type}'->>'success' AS INT) DESC, id
                ) AS rank
            FROM users WHERE guess_stats ? $2
        )
        SELECT COALESCE(user_stats.success + user_stats.fail, 0) AS total_guesses,
               COALESCE(leaderboard.rank, 0) AS user_position
        FROM user_stats
        LEFT JOIN leaderboard ON leaderboard.discord_id = user_stats.discord_id
        """
        user_result = await self.db.fetchrow(query, user_id, guess_type)
        if user_result and user_result["total_guesses"] != 0:
            user_position = user_result["user_position"]
            user_page = (user_position + per_page - 1) // per_page
            return user_position, user_page
        return 0, 0

    async def get_guesses_leaderboard(self, guess_type: str, page: int, user_id: int):
        per_page = GUESS_LEADERBOARD_PER_PAGE
        total_result = await self.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE guess_stats ? $1", guess_type
        )
        total_pages = (total_result + per_page - 1) // per_page
        if page > total_pages and total_pages > 0:
            page = total_pages
        leaderboard = await self.db.fetch(
            f"""
            SELECT id, discord_id,
                guess_stats->'{guess_type}'->>'success' AS success,
                CAST(guess_stats->'{guess_type}'->>'success' AS INT) AS score
            FROM users WHERE guess_stats ? $1
            ORDER BY score DESC, id ASC
            LIMIT $2 OFFSET $3
            """,
            guess_type,
            per_page,
            (page - 1) * per_page,
        )
        user_position, user_page = await self.get_guesses_position(guess_type, user_id)
        return leaderboard, user_position, user_page, total_pages

    async def get_guesses_at_rank(self, guess_type: str, rank: int):
        return await self.db.fetchrow(
            f"""
            SELECT id, discord_id,
                guess_stats->'{guess_type}'->>'success' AS success,
                CAST(guess_stats->'{guess_type}'->>'success' AS INT) AS score
            FROM users WHERE guess_stats ? $1
            ORDER BY score DESC, id ASC
            OFFSET $2 LIMIT 1
            """,
            guess_type,
            rank - 1,
        )

    async def store_oauth_token(
        self,
        user_id: int,
        access_token: str,
        refresh_token: str | None,
        expires_in: int,
        scopes: list[str],
    ) -> None:
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=expires_in
        )
        async with self.db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO oauth_tokens (discord_id, access_token, refresh_token, expires_at, scopes, updated_at)
                VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (discord_id) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    expires_at = EXCLUDED.expires_at,
                    scopes = EXCLUDED.scopes,
                    updated_at = now()
                """,
                user_id,
                access_token,
                refresh_token,
                expires_at,
                scopes,
            )

    async def get_oauth_token(self, user_id: int) -> asyncpg.Record | None:
        async with self.db.acquire() as conn:
            return await conn.fetchrow(
                "SELECT access_token, refresh_token, expires_at, scopes FROM oauth_tokens WHERE discord_id = $1",
                user_id,
            )

    async def delete_oauth_token(self, user_id: int) -> None:
        async with self.db.acquire() as conn:
            await conn.execute(
                "DELETE FROM oauth_tokens WHERE discord_id = $1", user_id
            )

    async def toggle_guessing(self, guild_id: int, enabled: bool) -> bool:
        await self.verify_discord_guild(guild_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "UPDATE guilds SET guessing_enabled = $1 WHERE guild_id = $2 RETURNING guessing_enabled",
                enabled,
                guild_id,
            )
            return result["guessing_enabled"] if result else True

    async def guessing_enabled(self, guild_id: int) -> bool:
        await self.verify_discord_guild(guild_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT guessing_enabled FROM guilds WHERE guild_id = $1", guild_id
            )
            return result["guessing_enabled"] if result else True

    async def channel_leaks_allowed(self, channel_id: int) -> bool:
        """whether leaked content may be shown in this channel (it's on the whitelist)"""
        if not channel_id:
            return False
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM leak_channels WHERE channel_id = $1", channel_id
            )
            return row is not None

    async def add_leak_channel(self, guild_id: int, channel_id: int) -> None:
        await self.verify_discord_guild(guild_id)
        async with self.db.acquire() as conn:
            await conn.execute(
                "INSERT INTO leak_channels (guild_id, channel_id) VALUES ($1, $2) "
                "ON CONFLICT (channel_id) DO NOTHING",
                guild_id,
                channel_id,
            )

    async def remove_leak_channel(self, channel_id: int) -> bool:
        async with self.db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM leak_channels WHERE channel_id = $1", channel_id
            )
            return result.endswith("1")  # "DELETE 1" when a row was removed

    async def leak_channels(self, guild_id: int) -> list[int]:
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT channel_id FROM leak_channels WHERE guild_id = $1", guild_id
            )
            return [r["channel_id"] for r in rows]
