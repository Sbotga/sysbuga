import datetime
import json
from typing import Any

import asyncpg

SETTING_DEFAULTS: dict[str, Any] = {
    "first_time_guess_end": True,
    "default_region": "en",
    "mirror_charts_by_default": False,
    "default_difficulty": "master",
    "opt_out_rolling_guess_leaderboards": False,
    "timezone": "et",
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

    # --- leaderboard points (weekly/monthly) ---

    async def add_guess_points(
        self, user_id: int, mode: str, points: int, week_index: int, month_index: int
    ) -> None:
        """add points to both the current weekly and monthly board for one mode"""
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO guess_points (discord_id, period_type, period_index, mode, points)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (discord_id, period_type, period_index, mode)
                DO UPDATE SET points = guess_points.points + EXCLUDED.points
                """,
                [
                    (user_id, "weekly", week_index, mode, points),
                    (user_id, "monthly", month_index, mode, points),
                ],
            )

    async def clear_period_points(
        self, user_id: int, week_index: int, month_index: int
    ) -> None:
        """drop a user's points for the current week and month (used when they opt out)"""
        await self.db.execute(
            """
            DELETE FROM guess_points
            WHERE discord_id = $1
              AND ((period_type = 'weekly' AND period_index = $2)
                OR (period_type = 'monthly' AND period_index = $3))
            """,
            user_id,
            week_index,
            month_index,
        )

    async def get_points_leaderboard(
        self, period_type: str, period_index: int, page: int, user_id: int
    ):
        """one page of the combined (summed across modes) board plus the caller's rank/total"""
        per_page = GUESS_LEADERBOARD_PER_PAGE
        total_people = await self.db.fetchval(
            "SELECT COUNT(DISTINCT discord_id) FROM guess_points "
            "WHERE period_type = $1 AND period_index = $2",
            period_type,
            period_index,
        )
        total_pages = max(1, (total_people + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        rows = await self.db.fetch(
            """
            SELECT discord_id, SUM(points) AS total
            FROM guess_points
            WHERE period_type = $1 AND period_index = $2
            GROUP BY discord_id
            ORDER BY total DESC, discord_id ASC
            LIMIT $3 OFFSET $4
            """,
            period_type,
            period_index,
            per_page,
            (page - 1) * per_page,
        )
        rank_row = await self.db.fetchrow(
            """
            WITH totals AS (
                SELECT discord_id, SUM(points) AS total,
                    ROW_NUMBER() OVER (ORDER BY SUM(points) DESC, discord_id ASC) AS rank
                FROM guess_points
                WHERE period_type = $1 AND period_index = $2
                GROUP BY discord_id
            )
            SELECT rank, total FROM totals WHERE discord_id = $3
            """,
            period_type,
            period_index,
            user_id,
        )
        return rows, rank_row, total_pages

    async def get_points_breakdown(
        self, user_id: int, period_type: str, period_index: int
    ) -> dict[str, int]:
        """a user's points per mode for one period"""
        rows = await self.db.fetch(
            "SELECT mode, points FROM guess_points "
            "WHERE discord_id = $1 AND period_type = $2 AND period_index = $3",
            user_id,
            period_type,
            period_index,
        )
        return {r["mode"]: r["points"] for r in rows}

    async def get_points_top(
        self, period_type: str, period_index: int, limit: int
    ) -> list[dict]:
        """the top N of a period's combined board (for prize ranking)"""
        rows = await self.db.fetch(
            """
            SELECT discord_id, SUM(points) AS total
            FROM guess_points
            WHERE period_type = $1 AND period_index = $2
            GROUP BY discord_id
            ORDER BY total DESC, discord_id ASC
            LIMIT $3
            """,
            period_type,
            period_index,
            limit,
        )
        return [dict(r) for r in rows]

    async def record_guess_attempt(self, user_id: int) -> tuple[int, int]:
        """count one guess attempt toward this user's hourly and daily buckets (fixed UTC hours/
        days), returning the new (hour_count, day_count). shared by the bot and activity
        """
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        hour_key, day_key = now // 3600, now // 86400
        row = await self.db.fetchrow(
            """
            INSERT INTO guess_rate (discord_id, hour_key, hour_count, day_key, day_count)
            VALUES ($1, $2, 1, $3, 1)
            ON CONFLICT (discord_id) DO UPDATE SET
                hour_count = CASE WHEN guess_rate.hour_key = $2
                    THEN guess_rate.hour_count + 1 ELSE 1 END,
                hour_key = $2,
                day_count = CASE WHEN guess_rate.day_key = $3
                    THEN guess_rate.day_count + 1 ELSE 1 END,
                day_key = $3
            RETURNING hour_count, day_count
            """,
            user_id,
            hour_key,
            day_key,
        )
        return row["hour_count"], row["day_count"]

    # --- prizes ---

    async def last_finalized_index(self, period_type: str) -> int:
        """the highest period index whose prizes have already been awarded (0 if none)"""
        return (
            await self.db.fetchval(
                "SELECT COALESCE(MAX(period_index), 0) FROM leaderboard_runs "
                "WHERE period_type = $1",
                period_type,
            )
            or 0
        )

    async def finalize_period(
        self,
        period_type: str,
        period_index: int,
        winners: list[tuple[int, int, int, int]],
        expires_at: "datetime.datetime",
    ) -> list[dict]:
        """award a finished period's prizes once and mark it finalized. winners are
        (discord_id, rank, paid_crystals, free_crystals). returns the created prize rows
        """
        created: list[dict] = []
        async with self.db.acquire() as conn:
            async with conn.transaction():
                for discord_id, rank, paid, free in winners:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO prizes (discord_id, period_type, period_index, rank,
                            paid_crystals, free_crystals, expires_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (discord_id, period_type, period_index) DO NOTHING
                        RETURNING *
                        """,
                        discord_id,
                        period_type,
                        period_index,
                        rank,
                        paid,
                        free,
                        expires_at,
                    )
                    if row:
                        created.append(dict(row))
                await conn.execute(
                    "INSERT INTO leaderboard_runs (period_type, period_index) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    period_type,
                    period_index,
                )
        return created

    async def get_claimable_prizes(self, user_id: int) -> list[dict]:
        """unclaimed prizes that haven't expired, oldest first"""
        rows = await self.db.fetch(
            "SELECT * FROM prizes WHERE discord_id = $1 AND status = 'unclaimed' "
            "AND expires_at > now() ORDER BY period_type, period_index",
            user_id,
        )
        return [dict(r) for r in rows]

    async def get_pending_prizes(self, user_id: int) -> list[dict]:
        """prizes the user has claimed and are awaiting a manual grant"""
        rows = await self.db.fetch(
            "SELECT * FROM prizes WHERE discord_id = $1 AND status = 'pending' "
            "ORDER BY period_type, period_index",
            user_id,
        )
        return [dict(r) for r in rows]

    async def has_claimable_prizes(self, user_id: int) -> bool:
        return bool(
            await self.db.fetchval(
                "SELECT 1 FROM prizes WHERE discord_id = $1 AND status = 'unclaimed' "
                "AND expires_at > now() LIMIT 1",
                user_id,
            )
        )

    async def get_prize(self, prize_id: int) -> "dict | None":
        row = await self.db.fetchrow("SELECT * FROM prizes WHERE id = $1", prize_id)
        return dict(row) if row else None

    async def claim_prize(self, prize_id: int, user_id: int, pjsk_id: int) -> bool:
        """mark an unclaimed, unexpired prize as pending (claimed). False if it wasn't claimable"""
        result = await self.db.execute(
            "UPDATE prizes SET status = 'pending', pjsk_id = $3, claimed_at = now() "
            "WHERE id = $1 AND discord_id = $2 AND status = 'unclaimed' AND expires_at > now()",
            prize_id,
            user_id,
            pjsk_id,
        )
        return result.split()[-1] == "1"

    async def forfeit_prize(self, prize_id: int, user_id: int) -> bool:
        """forfeit an unclaimed prize (gone for good). False if it wasn't forfeitable"""
        result = await self.db.execute(
            "UPDATE prizes SET status = 'forfeited' "
            "WHERE id = $1 AND discord_id = $2 AND status = 'unclaimed'",
            prize_id,
            user_id,
        )
        return result.split()[-1] == "1"

    async def get_all_prizes(self, user_id: int) -> list[dict]:
        """every prize the user has ever had, newest first (the /guess prize history/log)"""
        rows = await self.db.fetch(
            "SELECT * FROM prizes WHERE discord_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        return [dict(r) for r in rows]

    async def complete_prize(self, prize_id: int) -> bool:
        """mark a pending prize granted (admin pressed Sent). False if it wasn't pending"""
        result = await self.db.execute(
            "UPDATE prizes SET status = 'complete' WHERE id = $1 AND status = 'pending'",
            prize_id,
        )
        return result.split()[-1] == "1"

    async def deny_prize(self, prize_id: int, reason: str) -> bool:
        """mark a pending prize denied with a reason. False if it wasn't pending"""
        result = await self.db.execute(
            "UPDATE prizes SET status = 'denied', deny_reason = $2 "
            "WHERE id = $1 AND status = 'pending'",
            prize_id,
            reason,
        )
        return result.split()[-1] == "1"

    async def unclaim_prize(self, prize_id: int) -> None:
        """revert a pending prize to unclaimed (used when the claim notification fails to send)"""
        await self.db.execute(
            "UPDATE prizes SET status = 'unclaimed', pjsk_id = NULL, claimed_at = NULL "
            "WHERE id = $1 AND status = 'pending'",
            prize_id,
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
