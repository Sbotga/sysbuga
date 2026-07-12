import asyncio
import json
import os
import time
from functools import partial

from data import masterdata, search
from data.models import Card, Character, CheerfulCarnivalTeam, Event, Gacha, Music
from helpers import unblock
from helpers.emojis import emojis
from services.sbuga import Region, SbugaClient, SbugaError

MUSIC_CACHE_FILE = "cache/music_data.json"
EVENT_CACHE_FILE = "cache/event_data.json"
EXTRA_CACHE_FILE = "cache/extra_data.json"
# kept current on every alias add/remove/poll (unlike manual_aliases in the music cache,
# which is only rewritten on a game-data version change) so a restart never shows a stale
# alias list before the first poll
ALIAS_CACHE_FILE = "cache/aliases.json"

ALIAS_REFRESH_INTERVAL = 120

RARITY_DISPLAY = {
    "rarity_1": "1☆",
    "rarity_2": "2☆",
    "rarity_3": "3☆",
    "rarity_4": "4☆",
    "rarity_birthday": "🎀",
}


def character_display_name(char: Character) -> str:
    if not char.first_name:
        return char.given_name.title()
    sep = "" if any("　" <= c <= "鿿" for c in char.given_name) else " "
    if char.unit == "piapro":
        return f"{char.first_name}{sep}{char.given_name}".title()
    return f"{char.given_name}{sep}{char.first_name}".title()


class PJSKData:
    def __init__(
        self,
        client: SbugaClient,
        regions: list[str],
        *,
        refresh_interval: int = 300,
        asset_base_url: str = "",
    ) -> None:
        self.client = client
        self.regions: list[Region] = [r for r in regions]  # type: ignore[list-item]
        self.refresh_interval = refresh_interval
        self.asset_base_url = asset_base_url.rstrip("/")
        self._music_cache: dict[str, list[Music]] = {}
        self._event_cache: dict[str, list[Event]] = {}
        self._characters: list[Character] = []
        self._cc_teams: list[CheerfulCarnivalTeam] = []
        self._cards: list[Card] = []
        self._gacha_cache: dict[str, list[Gacha]] = {}
        self._versions: dict[str, str] = {}
        # last known aliases, kept so a failed fetch doesn't silently drop them from
        # the freshly-fetched Music/Event objects (which arrive without any)
        self._song_aliases: dict[int, list[str]] = {}
        self._event_aliases: dict[int, list[str]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._alias_task: asyncio.Task | None = None

    # tw/kr assets are not extracted/uploaded (subset of jp, same bundle
    # names) — point their asset URLs at the jp tree instead.
    ASSET_REGION = {"tw": "jp", "kr": "jp"}

    def _asset_url(self, region: str, path: str) -> str:
        """R2 URL when configured, otherwise the API's /assets passthrough."""
        region = self.ASSET_REGION.get(region, region)
        full = f"{path}.{self.client.image_type}"
        if self.asset_base_url:
            return f"{self.asset_base_url}/pjsk_data/{region}/{full}"
        return self.client.asset_url(full, region)  # type: ignore[arg-type]

    def chart_source_url(self, music_id: int, difficulty: str, region: str) -> str:
        """The raw SUS (.txt) for a chart, alongside the rendered chart pngs on R2."""
        region = self.ASSET_REGION.get(region, region)
        path = f"music/music_score/{str(music_id).zfill(4)}_01/{difficulty}.txt"
        if self.asset_base_url:
            return f"{self.asset_base_url}/pjsk_data/{region}/{path}"
        return self.client.asset_url(path, region)  # type: ignore[arg-type]

    # --- disk ---

    @staticmethod
    async def _to_thread(fn, *args):
        """Run blocking CPU/IO (pydantic dump/validate, JSON to disk) off the event loop.
        These read/build model objects but never mutate shared state that a concurrent
        lookup writes, so a worker thread is safe."""
        return await asyncio.get_running_loop().run_in_executor(
            unblock.executor, fn, *args
        )

    @staticmethod
    def _ensure_dirs() -> None:
        os.makedirs("cache", exist_ok=True)

    def _load_from_disk(self) -> None:
        self._ensure_dirs()
        if os.path.exists(MUSIC_CACHE_FILE):
            try:
                with open(MUSIC_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._versions = data.get("versions", {})
                self._music_cache = {
                    r: [Music.model_validate(m) for m in data.get(r, [])]
                    for r in self.regions
                }
                print(
                    "[PJSKData] loaded music from disk "
                    + " ".join(
                        f"{r}={len(self._music_cache.get(r, []))}" for r in self.regions
                    )
                )
            except Exception as e:
                print(f"[PJSKData] failed to load music from disk: {e}")
        if os.path.exists(EVENT_CACHE_FILE):
            try:
                with open(EVENT_CACHE_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._event_cache = {
                    r: [Event.model_validate(e) for e in raw.get(r, [])]
                    for r in self.regions
                }
            except Exception as e:
                print(f"[PJSKData] failed to load events from disk: {e}")
        if os.path.exists(EXTRA_CACHE_FILE):
            try:
                with open(EXTRA_CACHE_FILE, "r", encoding="utf-8") as f:
                    extra = json.load(f)
                self._characters = [
                    Character.model_validate(c) for c in extra.get("characters", [])
                ]
                self._cc_teams = [
                    CheerfulCarnivalTeam.model_validate(t)
                    for t in extra.get("cc_teams", [])
                ]
                self._cards = [Card.model_validate(c) for c in extra.get("cards", [])]
                self._gacha_cache = {
                    r: [Gacha.model_validate(g) for g in region_gachas]
                    for r, region_gachas in extra.get("gachas", {}).items()
                }
            except Exception as e:
                print(f"[PJSKData] failed to load extras from disk: {e}")

    def _save_music(self) -> None:
        self._ensure_dirs()
        payload: dict = {"versions": self._versions}
        for r in self.regions:
            payload[r] = [m.model_dump() for m in self._music_cache.get(r, [])]
        with open(MUSIC_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def _save_events(self) -> None:
        self._ensure_dirs()
        payload = {
            r: [e.model_dump() for e in self._event_cache.get(r, [])]
            for r in self.regions
        }
        with open(EVENT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def _save_aliases(self) -> None:
        """Persist the current alias maps (small; a few KB). JSON keys must be strings."""
        self._ensure_dirs()
        payload = {
            "song": {str(k): v for k, v in self._song_aliases.items()},
            "event": {str(k): v for k, v in self._event_aliases.items()},
        }
        with open(ALIAS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def _load_aliases(self) -> bool:
        """Load the persisted alias maps into memory. True if the file existed and applied."""
        if not os.path.exists(ALIAS_CACHE_FILE):
            return False
        try:
            with open(ALIAS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._song_aliases = {int(k): v for k, v in data.get("song", {}).items()}
            self._event_aliases = {int(k): v for k, v in data.get("event", {}).items()}
            return True
        except Exception as e:
            print(f"[PJSKData] failed to load aliases from disk: {e}")
            return False

    def _save_extras(self) -> None:
        self._ensure_dirs()
        payload = {
            "characters": [c.model_dump() for c in self._characters],
            "cc_teams": [t.model_dump() for t in self._cc_teams],
            "cards": [c.model_dump() for c in self._cards],
            "gachas": {
                r: [g.model_dump() for g in region_gachas]
                for r, region_gachas in self._gacha_cache.items()
            },
        }
        with open(EXTRA_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    # --- merge helpers (first region in priority order wins) ---

    def _merged_music(self) -> dict[int, Music]:
        merged: dict[int, Music] = {}
        for r in reversed(self.regions):
            for m in self._music_cache.get(r, []):
                merged[m.id] = m
        return merged

    def _merged_events(self) -> dict[int, Event]:
        merged: dict[int, Event] = {}
        for r in reversed(self.regions):
            for e in self._event_cache.get(r, []):
                merged[e.id] = e
        return merged

    def _all_region_events(self) -> list[Event]:
        """Every region's event objects (same ids repeat) so the search map
        gets every region's localized names, not just the merge winner's."""
        return [e for events in self._event_cache.values() for e in events]

    # --- public accessors ---

    def musics(self) -> list[Music]:
        return list(self._merged_music().values())

    def get_music(self, music_id: int) -> Music | None:
        return self._merged_music().get(music_id)

    def released_musics(self) -> list[Music]:
        """merged musics already out in-game, leaks (published_at still in the future) excluded.
        we fetch leaks so the data version keeps up, then filter them here (see get_musics)
        """
        now = int(time.time() * 1000)
        return [m for m in self.musics() if m.published_at <= now]

    def is_music_leaked(self, music_id: int) -> bool:
        """true if the song exists in our data but isn't out in-game yet"""
        music = self.get_music(music_id)
        return bool(music and music.published_at > int(time.time() * 1000))

    def regions_for_music(self, music_id: int) -> list[str]:
        return [
            r
            for r in self.regions
            if any(m.id == music_id for m in self._music_cache.get(r, []))
        ]

    def events(self) -> list[Event]:
        return list(self._merged_events().values())

    def get_event(self, event_id: int) -> Event | None:
        return self._merged_events().get(event_id)

    def released_events(self) -> list[Event]:
        """merged events already started in-game, leaks (start_at in the future) excluded"""
        now = int(time.time() * 1000)
        return [e for e in self.events() if (e.start_at or 0) <= now]

    def is_event_leaked(self, event_id: int) -> bool:
        """true if the event exists in our data but hasn't started in-game yet"""
        event = self.get_event(event_id)
        return bool(event and (event.start_at or 0) > int(time.time() * 1000))

    def characters(self) -> list[Character]:
        return self._characters

    def get_character(self, character_id: int) -> Character | None:
        return next((c for c in self._characters if c.id == character_id), None)

    def cc_teams(self) -> list[CheerfulCarnivalTeam]:
        return self._cc_teams

    def cards(self) -> list[Card]:
        return self._cards

    def get_card(self, card_id: int) -> Card | None:
        return next((c for c in self._cards if c.id == card_id), None)

    def gachas(self, region: str | None = None) -> list[Gacha]:
        """Gacha ids are per-region sequences, so no cross-region merge."""
        if region is not None:
            return self._gacha_cache.get(region, [])
        for r in self.regions:
            if self._gacha_cache.get(r):
                return self._gacha_cache[r]
        return []

    def get_gacha(self, gacha_id: int, region: str | None = None) -> Gacha | None:
        return next((g for g in self.gachas(region) if g.id == gacha_id), None)

    def card_display_name(
        self, card: Card, *, use_emojis: bool = False, trained: bool = False
    ) -> str:
        char = self.get_character(card.character_id)
        name = (
            character_display_name(char) if char else f"Character {card.character_id}"
        )
        if use_emojis:
            rare = card.card_rarity_type.split("_")[-1]
            if rare.isdigit():
                star = emojis.rarities["trained" if trained else "untrained"]
                rarity = star * int(rare)
            else:
                rarity = emojis.rarities["birthday"]
            attr = (
                emojis.attributes.get(card.attr, f"[{card.attr}]") if card.attr else ""
            )
        else:
            rarity = RARITY_DISPLAY.get(card.card_rarity_type, card.card_rarity_type)
            attr = f"[{card.attr}]" if card.attr else ""
        return f"{name} - {rarity} {attr} {card.prefix}".replace("  ", " ").strip()

    def search_songs(self, query: str, limit: int = 200) -> list[int]:
        return search.fuzzy_search_playlists(query, limit=limit)

    def search_song_levelkeys(
        self, query: str, limit: int = 200
    ) -> list[search.LevelKey]:
        return search.fuzzy_search(query, limit=limit)

    def search_events(self, query: str, limit: int = 50) -> list[int]:
        return search.fuzzy_search_events(query, limit=limit)

    def best_song_id(self, query: str) -> int | None:
        return search.best_song_match(query)

    def best_song_id_key(self, query: str) -> tuple[int, str] | None:
        """The matched song id plus the key (title or alias) that matched it."""
        return search.best_song_match_key(query)

    def song_keys(self, music_id: int) -> list[str]:
        """Every key the matcher accepts for this song (title, aliases, romanizations)."""
        return search.song_keys(music_id)

    def song_aliases(self, music_id: int) -> list[str]:
        """Manually-added aliases from cache (refreshed every 120s and on each edit)."""
        return list(self._song_aliases.get(music_id, ()))

    def event_keys(self, event_id: int) -> list[str]:
        """every key the matcher accepts for this event (name, aliases, romanizations)"""
        return search.event_keys(event_id)

    def event_aliases(self, event_id: int) -> list[str]:
        """manually-added event aliases from cache (refreshed every 120s and on each edit)"""
        return list(self._event_aliases.get(event_id, ()))

    def best_event_id(self, query: str) -> int | None:
        return search.best_event_match(query)

    def get_play_level(self, music_id: int, difficulty: str) -> int | None:
        music = self.get_music(music_id)
        if not music:
            return None
        for d in music.difficulties:
            if d.difficulty == difficulty:
                return d.play_level
        return None

    # --- refresh / polling ---

    async def _fetch_versions(self) -> dict[str, str]:
        # the version is the game's master data version (a static dataVersion file), so it
        # bumps whenever master data changes - including a new leak being added
        results = await asyncio.gather(
            *[self.client.get_version(r) for r in self.regions],
            return_exceptions=True,
        )
        versions: dict[str, str] = {}
        for r, res in zip(self.regions, results):
            versions[r] = (
                res.data_version or "" if not isinstance(res, BaseException) else ""
            )
        return versions

    async def refresh(self, force: bool = False) -> bool:
        async with self._lock:
            versions = await self._fetch_versions()

            changed = force or not self._music_cache
            for r in self.regions:
                if versions[r] and versions[r] != self._versions.get(r):
                    changed = True
                if versions[r] and not self._music_cache.get(r):
                    changed = True  # region never fetched (or a past fetch failed)
            if not changed:
                return False

            print("[PJSKData] data version changed, fetching...")
            # ignore_leak=True includes unreleased (leaked) songs; we keep them in the data and
            # filter app-side (released_musics / is_music_leaked / the allow_leaks setting)
            music_results = await asyncio.gather(
                *[self.client.get_musics(r, ignore_leak=True) for r in self.regions],
                return_exceptions=True,
            )
            new_music: dict[str, list[Music]] = {}
            for r, res in zip(self.regions, music_results):
                if isinstance(res, list) and res:
                    new_music[r] = res
                else:
                    # keep old data and old version so the region retries next poll
                    new_music[r] = self._music_cache.get(r, [])
                    versions[r] = self._versions.get(r, "")
                    reason = (
                        repr(res)[:300]
                        if isinstance(res, BaseException)
                        else "empty response"
                    )
                    print(
                        f"[PJSKData] /musics failed for {r} — keeping cached data ({reason})"
                    )
            if not any(new_music.values()):
                print("[PJSKData] no music received, skipping")
                return False

            self._music_cache = new_music
            self._versions = {r: versions[r] for r in self.regions}

            await self._fetch_song_aliases()
            self._apply_song_aliases()
            merged = list(self._merged_music().values())
            await search.build_search_maps(merged, new_music, versions=self._versions)
            await self._to_thread(self._save_music)

            await self._refresh_events()
            await self._refresh_extras()
            await self._to_thread(self._save_aliases)
            return True

    async def _get_masters(self, files: tuple[str, ...], region: Region) -> list:
        return await asyncio.gather(*[self.client.get_master(f, region) for f in files])

    async def _refresh_events(self) -> None:
        new_events: dict[str, list[Event]] = {}
        unavailable = False
        for r in self.regions:
            try:
                events_raw, bonuses, blooms, units = await self._get_masters(
                    masterdata.EVENT_FILES, r
                )
                new_events[r] = await self._to_thread(
                    masterdata.build_events,
                    events_raw,
                    bonuses,
                    blooms,
                    units,
                    partial(self._asset_url, r),
                )
            except Exception:
                unavailable = True
                new_events[r] = self._event_cache.get(r, [])
        if unavailable:
            print(
                "[PJSKData] /master event files not available yet — event features "
                "dormant (see MISSING_SBUGA_ROUTES.md #1)"
            )
        self._event_cache = new_events
        await self._fetch_event_aliases()
        self._apply_event_aliases()
        await search.build_event_search_map(self._all_region_events())
        await self._to_thread(self._save_events)

    async def _fetch_song_aliases(self) -> set[int]:
        """Refresh the cached song aliases. Returns the ids whose alias lists changed."""
        try:
            aliases = await self.client.get_song_aliases()
        except Exception:
            return set()
        by_music: dict[int, list[str]] = {}
        for a in aliases:
            by_music.setdefault(a.music_id, []).append(a.alias)
        for values in by_music.values():
            values.sort()
        changed = {
            mid
            for mid in set(by_music) | set(self._song_aliases)
            if by_music.get(mid, []) != self._song_aliases.get(mid, [])
        }
        if changed:
            self._song_aliases = by_music
        return changed

    async def _fetch_event_aliases(self) -> set[int]:
        """Refresh the cached event aliases. Returns the ids whose alias lists changed."""
        try:
            aliases = await self.client.get_event_aliases()
        except Exception:
            return set()
        by_event: dict[int, list[str]] = {}
        for a in aliases:
            by_event.setdefault(a.event_id, []).append(a.alias)
        for values in by_event.values():
            values.sort()
        changed = {
            eid
            for eid in set(by_event) | set(self._event_aliases)
            if by_event.get(eid, []) != self._event_aliases.get(eid, [])
        }
        if changed:
            self._event_aliases = by_event
        return changed

    def _apply_song_aliases(self) -> None:
        for musics in self._music_cache.values():
            for m in musics:
                m.manual_aliases = self._song_aliases.get(m.id, [])

    def _apply_event_aliases(self) -> None:
        for events in self._event_cache.values():
            for e in events:
                e.name_variants = self._event_aliases.get(e.id, [])

    async def refresh_aliases(self, force: bool = False) -> bool:
        """Pull the alias lists and update the affected search maps if they moved. When only
        a handful of ids changed (the usual case) each is re-indexed in place — the full
        rebuild romanizes every title (~1.5s of GIL-held work that stalls the event loop),
        so we only pay it on `force`. Cheap when nothing changed: two API calls, no rebuild.
        """
        songs_changed = await self._fetch_song_aliases()
        events_changed = await self._fetch_event_aliases()
        if not (force or songs_changed or events_changed):
            return False
        async with self._lock:
            if (force or songs_changed) and self._music_cache:
                self._apply_song_aliases()
                if force:
                    merged = list(self._merged_music().values())
                    await search.build_search_maps(
                        merged, self._music_cache, versions=self._versions
                    )
                else:
                    merged = self._merged_music()
                    for mid in songs_changed:
                        m = merged.get(mid)
                        if m:
                            search.reindex_song(m, self._music_cache)
                    await search.save_maps()
            if (force or events_changed) and self._event_cache:
                self._apply_event_aliases()
                if force:
                    await search.build_event_search_map(self._all_region_events())
                else:
                    by_id: dict[int, list[Event]] = {}
                    for e in self._all_region_events():
                        by_id.setdefault(e.id, []).append(e)
                    for eid in events_changed:
                        search.reindex_event(by_id.get(eid, []))
                    await search.save_maps()
        await self._to_thread(self._save_aliases)
        return True

    async def _reindex_one_song(self, music_id: int) -> None:
        """Re-index a single song in the live maps after its alias list changed, then persist.
        No full rebuild, so it doesn't stall the loop (see refresh_aliases)."""
        async with self._lock:
            self._apply_song_aliases()
            merged = self._merged_music().get(music_id)
            if merged:
                search.reindex_song(merged, self._music_cache)
        await search.save_maps()
        await self._to_thread(self._save_aliases)

    async def add_song_alias_local(self, music_id: int, alias: str) -> None:
        """Record an alias just added through the API and re-index that song at once, so it's
        searchable immediately without a restart or full rebuild. `alias` is preprocessed.
        """
        values = self._song_aliases.setdefault(music_id, [])
        if alias not in values:
            values.append(alias)
            values.sort()
        await self._reindex_one_song(music_id)

    async def remove_song_alias_local(self, music_id: int, alias: str) -> None:
        """Drop an alias just removed through the API and re-index that song at once."""
        values = self._song_aliases.get(music_id)
        if values and alias in values:
            values.remove(alias)
            if not values:
                del self._song_aliases[music_id]
        await self._reindex_one_song(music_id)

    async def _reindex_one_event(self, event_id: int) -> None:
        """re-index a single event in the live maps after its alias list changed, then persist"""
        async with self._lock:
            self._apply_event_aliases()
            copies = [e for e in self._all_region_events() if e.id == event_id]
            if copies:
                search.reindex_event(copies)
        await search.save_maps()
        await self._to_thread(self._save_aliases)

    async def add_event_alias_local(self, event_id: int, alias: str) -> None:
        """record an alias just added through the api and re-index that event at once so it's
        searchable immediately. alias is preprocessed"""
        values = self._event_aliases.setdefault(event_id, [])
        if alias not in values:
            values.append(alias)
            values.sort()
        await self._reindex_one_event(event_id)

    async def remove_event_alias_local(self, event_id: int, alias: str) -> None:
        """drop an alias just removed through the api and re-index that event at once"""
        values = self._event_aliases.get(event_id)
        if values and alias in values:
            values.remove(alias)
            if not values:
                del self._event_aliases[event_id]
        await self._reindex_one_event(event_id)

    async def _refresh_extras(self) -> None:
        chars: dict[int, Character] = {}
        teams: dict[int, CheerfulCarnivalTeam] = {}
        cards: dict[int, Card] = {}
        new_gachas: dict[str, list[Gacha]] = {}
        available = False
        for r in reversed(self.regions):  # first region in the list wins merges
            try:
                game_chars, profiles, cc_teams = await self._get_masters(
                    masterdata.CHARACTER_FILES, r
                )
                cards_raw = await self.client.get_master("cards", r)
                gachas_raw = await self.client.get_master("gachas", r)
            except Exception:
                new_gachas[r] = self._gacha_cache.get(r, [])
                continue
            available = True
            for c in await self._to_thread(
                masterdata.build_characters, game_chars, profiles
            ):
                chars[c.id] = c
            for t in await self._to_thread(masterdata.build_teams, cc_teams):
                teams[t.id] = t
            for card in await self._to_thread(
                masterdata.build_cards, cards_raw, partial(self._asset_url, r)
            ):
                cards[card.id] = card
            new_gachas[r] = await self._to_thread(
                masterdata.build_gachas, gachas_raw, partial(self._asset_url, r)
            )

        if available:
            self._characters = list(chars.values())
            self._cc_teams = list(teams.values())
            self._cards = list(cards.values())
        else:
            print(
                "[PJSKData] /master character/card/gacha files not available yet — "
                "character/gacha features dormant (see MISSING_SBUGA_ROUTES.md #1)"
            )
        self._gacha_cache = new_gachas
        await self._to_thread(self._save_extras)

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_interval)
            try:
                await self.refresh()
            except Exception as e:
                print(f"[PJSKData] refresh error: {e}")

    async def _poll_aliases(self) -> None:
        # aliases change independently of the game data version, so they get their own
        # (much shorter) loop; it also lets the activity workers pick up an alias a
        # moderator added through the bot in another process
        while True:
            await asyncio.sleep(ALIAS_REFRESH_INTERVAL)
            try:
                if await self.refresh_aliases():
                    print("[PJSKData] aliases changed, search maps rebuilt")
            except Exception as e:
                print(f"[PJSKData] alias refresh error: {e}")

    def _seed_alias_cache(self) -> None:
        """Recover the alias maps from disk so the first poll doesn't rebuild just because
        the in-memory cache started out empty. Prefer the dedicated alias cache (kept current
        on every edit); fall back to the manual_aliases baked into the music cache, which is
        only refreshed on a game-data version change (so it can miss recent edits)."""
        if self._load_aliases():
            return
        for musics in self._music_cache.values():
            for m in musics:
                if m.manual_aliases:
                    self._song_aliases[m.id] = sorted(m.manual_aliases)
        for events in self._event_cache.values():
            for e in events:
                if e.name_variants:
                    self._event_aliases[e.id] = sorted(e.name_variants)

    async def start(self) -> None:
        await self._to_thread(self._load_from_disk)
        self._seed_alias_cache()
        # push the seeded aliases onto the models before building, since the search maps read
        # them from manual_aliases/name_variants, not from self._song_aliases directly
        self._apply_song_aliases()
        self._apply_event_aliases()
        if self._music_cache:
            merged = list(self._merged_music().values())
            await search.build_search_maps(
                merged, self._music_cache, versions=self._versions
            )
            await search.build_event_search_map(self._all_region_events())
        try:
            await self.refresh()
        except SbugaError as e:
            print(f"[PJSKData] initial refresh failed: {e}")
        self._task = asyncio.create_task(self._poll())
        self._alias_task = asyncio.create_task(self._poll_aliases())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._alias_task:
            self._alias_task.cancel()
