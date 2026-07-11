import asyncio
import json
import os
import re
import unicodedata
from typing import Any

import cutlet
from korean_romanizer.romanizer import Romanizer as _KoreanRomanizer
from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from data.models import Event, Music, _build_char_name
from helpers.unblock import executor

_ROMAJI_SYSTEMS = ("hepburn", "nihon", "kunrei")

# Each system is run twice. `use_foreign_spelling` maps katakana loanwords back to
# their source spelling — アクセラレイト -> "accelerate", which is how people type it —
# but it guesses wrong on names (ロキ -> "loci"). Neither setting is a superset, so
# both are kept: every title gets a phonetic key *and* a loanword key.
_KATSU = [
    cutlet.Cutlet(system=system, use_foreign_spelling=foreign, ensure_ascii=False)
    for system in _ROMAJI_SYSTEMS
    for foreign in (False, True)
]


def _make_romanizer(katsu: "cutlet.Cutlet"):
    def romanize(text: str) -> str:
        return katsu.romaji(text).lower().strip()

    return romanize


ROMANIZERS = [_make_romanizer(k) for k in _KATSU]

_KANA_RE = re.compile(r"[぀-ヿㇰ-ㇿ]")
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_HANGUL_RE = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")

_pinyin = None  # lazy: PinyinJyutping() builds the jieba dict (~1s) on init


def _get_pinyin():
    global _pinyin
    if _pinyin is None:
        import pinyin_jyutping

        _pinyin = pinyin_jyutping.PinyinJyutping()
    return _pinyin


def _strip_diacritics(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
    )


CACHE_FILE = "cache/search_maps.json"
CACHE_VERSION = 6  # bump when the cache shape OR the generated keys change


def _is_invisible(ch: str) -> bool:
    # zero-width / control / bidi format chars (Cc, Cf) plus the Tags block (U+E0000
    # tag space some clients like 7TV inject — it's unassigned/Cn, so category alone
    # misses it) and variation selectors. Strips invisible junk that makes two
    # identical-looking strings (e.g. アリア vs アリア) miss a match.
    if unicodedata.category(ch) in ("Cc", "Cf"):
        return True
    o = ord(ch)
    return (
        0xE0000 <= o <= 0xE007F  # Tags
        or 0xFE00 <= o <= 0xFE0F  # variation selectors
        or 0xE0100 <= o <= 0xE01EF  # variation selectors supplement
    )


def preprocess(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if not _is_invisible(c))
    text = text.lower().strip()
    STAR_LIKE = (
        r"[☀-⛿"
        r"\U0001F300-\U0001F5FF"
        r"\U0001F600-\U0001F64F"
        r"\U0001F680-\U0001F6FF]"
    )
    text = re.sub(STAR_LIKE, " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_APOSTROPHES = "'’‘`´"


def _fold(text: str) -> str:
    """Drop punctuation so it can't hide a word from the tokenizer. token_set_ratio
    splits on whitespace only, so "call!!" is one token that shares nothing with the
    query "call" (80) while "call boy" contains it as a whole token (a flat 100) — the
    wrong song wins. Folding gives "call!!" the key "call". Apostrophes close up
    ("don't" -> "dont"); everything else becomes a space.

    Only used for search keys and queries; `preprocess` still defines an alias's
    stored identity."""
    chars: list[str] = []
    for ch in text:
        if ch in _APOSTROPHES:
            continue
        category = unicodedata.category(ch)
        chars.append(" " if category[0] in ("P", "S") else ch)
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _with_folded(keys: set[str]) -> set[str]:
    return keys | {f for f in map(_fold, keys) if f}


_BRACKETS_RE = re.compile(r"[\[\(【（〔].*?[\]\)】）〕]")


def _strip_brackets(text: str) -> str:
    """Title minus any bracketed [version]/(subtitle) segment, so a query of the base title
    matches "Mikumiku Ni Shiteageru [Shiteyanyo]" without the part nobody types."""
    return re.sub(r"\s+", " ", _BRACKETS_RE.sub(" ", text)).strip()


def _romanize(text: str) -> list[str]:
    """Romanizations of `text`: Japanese via cutlet, Korean via
    korean-romanizer, Chinese via pinyin (with and without tone marks)."""
    keys: list[str] = []
    pp = preprocess(text)

    def add(value: str) -> None:
        v = preprocess(value)
        if v and v != pp:
            keys.append(v)

    if _KANA_RE.search(text) or _CJK_RE.search(text):
        for fn in ROMANIZERS:
            try:
                add(fn(text))
            except Exception:
                continue
    if _HANGUL_RE.search(text):
        try:
            add(_KoreanRomanizer(text).romanize())
        except Exception:
            pass
    if _CJK_RE.search(text):
        try:
            pinyin = _get_pinyin().pinyin(text, spaces=True)
            add(pinyin)
            add(_strip_diacritics(pinyin))
        except Exception:
            pass
    return list(dict.fromkeys(keys))


def _query_variants(query: str) -> list[str]:
    """The query itself plus its romanizations, so native-script input
    (jp/kr/zh) matches the romanized keys and vice versa, plus punctuation-folded
    forms so a typed "hello/how are you?" reaches the folded key."""
    query_pp = preprocess(query)
    variants = [query_pp, *_romanize(query_pp)]
    variants.extend(f for f in map(_fold, list(variants)) if f)
    # "slow downer" -> "slowdowner" matches the unspaced title key
    variants.extend(v.replace(" ", "") for v in list(variants))
    return list(dict.fromkeys(v for v in variants if v))


LevelKey = tuple[int, int, str]

_search_map: dict[str, set[LevelKey]] = {}
_vocal_id_map: dict[str, set[LevelKey]] = {}
_playlist_map: dict[str, set[int]] = {}
_event_map: dict[str, set[int]] = {}
# key -> ids for which that key is ONLY a hand-written alias, never the real title/name.
_playlist_alias_map: dict[str, set[int]] = {}
_event_alias_map: dict[str, set[int]] = {}
# key -> ids for which that key is ONLY machine-generated (a romanization of the title
# or of an alias). Neither map contains an id whose real title the key is.
_playlist_auto_map: dict[str, set[int]] = {}
_event_auto_map: dict[str, set[int]] = {}
# Ranked: real title > hand-written alias > generated key. Curated aliases are trusted
# nearly as much as a real title; romanizations are noisier and give way to either, so
# "aria" resolves to Eternal Aria (which is aliased that) over アリア (which only
# romanizes to it). Both penalties are just big enough to break an exact tie — any
# larger and a strong generated match would lose to a weaker title match.
MANUAL_ALIAS_PENALTY = 0.1
AUTO_KEY_PENALTY = 0.5

# Autocomplete ranking only (_fuzzy), where recall beats precision: token_set_ratio
# returns a flat 100 for any token subset, so a partial phrase still surfaces the song,
# blended with the length-aware ratio so extra words cost something. The single-answer
# matcher (_best_entity) deliberately does NOT use this — see its docstring.
TOKEN_SET_WEIGHT = 0.8
DEFAULT_SENSITIVITY = 0.67
_all_artists: list[str] = []
_all_captions: list[str] = []
_min_level: int = 1
_max_level: int = 40
_cached_versions: dict[str, str] = {}


def _add_keys(
    target: dict[str, set[LevelKey]], keys: list[str], level_keys: list[LevelKey]
) -> None:
    for key in keys:
        pk = preprocess(key)
        if pk:
            target.setdefault(pk, set()).update(level_keys)


def _add_playlist_keys(
    target: dict[str, set[int]], keys: list[str], music_ids: list[int]
) -> None:
    for key in keys:
        pk = preprocess(key)
        if pk:
            target.setdefault(pk, set()).update(music_ids)


def _snapshot() -> dict:
    """A detached, JSON-ready copy of the live maps. Built synchronously (no await) so an
    in-place reindex can't mutate a map mid-copy; the result is safe to json.dump off-loop.
    """
    return {
        "cache_version": CACHE_VERSION,
        "versions": _cached_versions,
        "search_map": {k: [list(lk) for lk in v] for k, v in _search_map.items()},
        "vocal_id_map": {k: [list(lk) for lk in v] for k, v in _vocal_id_map.items()},
        "playlist_map": {k: list(v) for k, v in _playlist_map.items()},
        "event_map": {k: list(v) for k, v in _event_map.items()},
        "playlist_alias_map": {k: list(v) for k, v in _playlist_alias_map.items()},
        "event_alias_map": {k: list(v) for k, v in _event_alias_map.items()},
        "playlist_auto_map": {k: list(v) for k, v in _playlist_auto_map.items()},
        "event_auto_map": {k: list(v) for k, v in _event_auto_map.items()},
        "all_artists": _all_artists,
        "all_captions": _all_captions,
        "min_level": _min_level,
        "max_level": _max_level,
    }


def _write_snapshot(data: dict) -> None:
    os.makedirs("cache", exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _save_to_disk() -> None:
    _write_snapshot(_snapshot())


def _load_from_disk() -> bool:
    global _search_map, _vocal_id_map, _playlist_map, _event_map
    global _playlist_alias_map, _event_alias_map, _playlist_auto_map, _event_auto_map
    global _all_artists, _all_captions, _min_level, _max_level, _cached_versions
    if not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("cache_version") != CACHE_VERSION:
            return False  # stale format -> force a rebuild
        _cached_versions = data.get("versions", {})
        _search_map = {
            k: {tuple(lk) for lk in v} for k, v in data.get("search_map", {}).items()
        }
        _vocal_id_map = {
            k: {tuple(lk) for lk in v} for k, v in data.get("vocal_id_map", {}).items()
        }
        _playlist_map = {k: set(v) for k, v in data.get("playlist_map", {}).items()}
        _event_map = {k: set(v) for k, v in data.get("event_map", {}).items()}
        _playlist_alias_map = {
            k: set(v) for k, v in data.get("playlist_alias_map", {}).items()
        }
        _event_alias_map = {
            k: set(v) for k, v in data.get("event_alias_map", {}).items()
        }
        _playlist_auto_map = {
            k: set(v) for k, v in data.get("playlist_auto_map", {}).items()
        }
        _event_auto_map = {k: set(v) for k, v in data.get("event_auto_map", {}).items()}
        _all_artists = data.get("all_artists", [])
        _all_captions = data.get("all_captions", [])
        _min_level = data.get("min_level", 1)
        _max_level = data.get("max_level", 40)
        return True
    except Exception as e:
        print(f"[Search] Failed to load search cache: {e}")
        return False


def get_cached_versions() -> dict[str, str]:
    return _cached_versions


def needs_rebuild(versions: dict[str, str]) -> bool:
    if not _search_map:
        if _load_from_disk() and versions == _cached_versions:
            return False
        return True
    return versions != _cached_versions


def _index_music(
    music: Music,
    music_data: dict[str, list[Music]],
    search_map: dict[str, set[LevelKey]],
    vocal_id_map: dict[str, set[LevelKey]],
    playlist_map: dict[str, set[int]],
    playlist_alias_map: dict[str, set[int]],
    playlist_auto_map: dict[str, set[int]],
    all_artists_set: set[str],
    all_captions_set: set[str],
) -> tuple[int, int]:
    """Add one song's keys to the given maps. Returns its (min, max) play level so the
    caller can fold the aggregate. Factored out so a single alias edit can re-index just
    this song instead of rebuilding all ~9k keys (see reindex_song)."""
    min_lv = 99
    max_lv = 0
    mid_list = [music.id]
    all_level_keys: list[LevelKey] = []
    for vocal in music.vocals:
        for diff in music.difficulties:
            all_level_keys.append((music.id, vocal.id, diff.difficulty))

    # three tiers: the real title, hand-written aliases, and our romanizations of
    # both. Music.title_variants is deliberately ignored — the backend folds the
    # aliases into it, so a removed alias would linger there as a "generated" key
    # until the next full /musics fetch. Generating locally keeps the alias table
    # authoritative, so an add/remove takes effect at once.
    real_titles: list[str] = []
    manual_titles: list[str] = []
    for source_list in music_data.values():
        for m in source_list:
            if m.id == music.id:
                real_titles.append(m.title)
                if m.pronunciation:
                    real_titles.append(m.pronunciation)
                manual_titles.extend(m.manual_aliases)
    manual_titles.extend(music.manual_aliases)
    real_titles.append(music.title)
    if music.pronunciation:
        real_titles.append(music.pronunciation)
    # also index each title without its bracketed suffix (e.g. "... [Shiteyanyo]")
    for t in list(real_titles):
        base = _strip_brackets(t)
        if base and preprocess(base) != preprocess(t):
            real_titles.append(base)

    generated_keys: list[str] = []
    for t in list(dict.fromkeys([*real_titles, *manual_titles])):
        generated_keys.extend(_romanize(t))

    # a folded key keeps the tier of whatever it was folded from, so "call" is a
    # *real title* key for Call!! and beats Call Boy's token-subset match
    real_pp = _with_folded({preprocess(k) for k in real_titles if preprocess(k)})
    manual_pp = (
        _with_folded({preprocess(k) for k in manual_titles if preprocess(k)}) - real_pp
    )
    auto_pp = (
        _with_folded({preprocess(k) for k in generated_keys if preprocess(k)})
        - real_pp
        - manual_pp
    )

    deduped_titles = list(real_pp | manual_pp | auto_pp)
    _add_keys(search_map, deduped_titles, all_level_keys)
    _add_playlist_keys(playlist_map, deduped_titles, mid_list)
    for k in manual_pp:
        playlist_alias_map.setdefault(k, set()).update(mid_list)
    for k in auto_pp:
        playlist_auto_map.setdefault(k, set()).update(mid_list)

    _add_keys(search_map, [str(music.id)], all_level_keys)
    _add_playlist_keys(playlist_map, [str(music.id)], mid_list)

    # People (artist/lyricist/composer/arranger) only go in the *search* map.
    # The playlist map resolves a name to one song, and a person's name is not a
    # song name — "accelerate" used to resolve to Wonder Style purely because its
    # composer is "colate".
    if music.artist:
        artist_keys = [music.artist.name, *_romanize(music.artist.name)]
        if music.artist.pronunciation:
            artist_keys.append(music.artist.pronunciation)
            artist_keys.extend(_romanize(music.artist.pronunciation))
        _add_keys(search_map, artist_keys, all_level_keys)

    for field in [music.lyricist, music.composer, music.arranger]:
        if field:
            field_keys = [field, *_romanize(field)]
            _add_keys(search_map, field_keys, all_level_keys)

    for vocal in music.vocals:
        vocal_level_keys = [
            (music.id, vocal.id, diff.difficulty) for diff in music.difficulties
        ]

        vocal_id_str = preprocess(str(vocal.id))
        _add_keys(search_map, [vocal_id_str], vocal_level_keys)
        vocal_id_map.setdefault(vocal_id_str, set()).update(vocal_level_keys)

        all_captions_set.add(vocal.caption)

        chars = sorted(vocal.characters, key=lambda c: c.seq)
        for c in chars:
            if c.character_type == "game_character":
                char_data = music.game_characters.get(c.character_id)
                if char_data:
                    name = _build_char_name(char_data)
                    all_artists_set.add(name)
                    name_keys = [name, *_romanize(name)]
                    if char_data.givenName:
                        name_keys.append(char_data.givenName)
                        name_keys.extend(_romanize(char_data.givenName))
                    if char_data.firstName:
                        name_keys.append(char_data.firstName)
                        name_keys.extend(_romanize(char_data.firstName))
                    _add_keys(search_map, name_keys, vocal_level_keys)
            else:
                outside = music.outside_characters.get(c.character_id)
                if outside:
                    all_artists_set.add(outside.name)
                    name_keys = [outside.name, *_romanize(outside.name)]
                    _add_keys(search_map, name_keys, vocal_level_keys)

    for diff in music.difficulties:
        diff_level_keys = [
            (music.id, vocal.id, diff.difficulty) for vocal in music.vocals
        ]
        _add_keys(search_map, [diff.difficulty], diff_level_keys)
        if diff.play_level < min_lv:
            min_lv = diff.play_level
        if diff.play_level > max_lv:
            max_lv = diff.play_level

    return min_lv, max_lv


def _compute_search_maps(
    musics: list[Music], music_data: dict[str, list[Music]]
) -> dict[str, Any]:
    """The expensive half of build_search_maps (romanizing every title, ~1.5s).
    Builds into locals so the live maps are never observed half-populated."""
    search_map: dict[str, set[LevelKey]] = {}
    vocal_id_map: dict[str, set[LevelKey]] = {}
    playlist_map: dict[str, set[int]] = {}
    playlist_alias_map: dict[str, set[int]] = {}
    playlist_auto_map: dict[str, set[int]] = {}

    all_artists_set: set[str] = set()
    all_captions_set: set[str] = set()
    min_lv = 99
    max_lv = 0

    for music in musics:
        m_min, m_max = _index_music(
            music,
            music_data,
            search_map,
            vocal_id_map,
            playlist_map,
            playlist_alias_map,
            playlist_auto_map,
            all_artists_set,
            all_captions_set,
        )
        min_lv = min(min_lv, m_min)
        max_lv = max(max_lv, m_max)

    return {
        "search_map": search_map,
        "vocal_id_map": vocal_id_map,
        "playlist_map": playlist_map,
        "playlist_alias_map": playlist_alias_map,
        "playlist_auto_map": playlist_auto_map,
        "all_artists": sorted(all_artists_set),
        "all_captions": sorted(all_captions_set),
        "min_level": min_lv if min_lv < 99 else 1,
        "max_level": max_lv if max_lv > 0 else 40,
    }


async def _off_loop(func, *args):
    return await asyncio.get_running_loop().run_in_executor(executor, func, *args)


async def build_search_maps(
    musics: list[Music],
    music_data: dict[str, list[Music]],
    versions: dict[str, str] | None = None,
) -> None:
    global _search_map, _vocal_id_map, _playlist_map
    global _playlist_alias_map, _playlist_auto_map
    global _all_artists, _all_captions, _min_level, _max_level, _cached_versions

    built = await _off_loop(_compute_search_maps, musics, music_data)

    # single synchronous swap: no await between these, so a lookup either sees the
    # whole old map set or the whole new one
    _search_map = built["search_map"]
    _vocal_id_map = built["vocal_id_map"]
    _playlist_map = built["playlist_map"]
    _playlist_alias_map = built["playlist_alias_map"]
    _playlist_auto_map = built["playlist_auto_map"]
    _all_artists = built["all_artists"]
    _all_captions = built["all_captions"]
    _min_level = built["min_level"]
    _max_level = built["max_level"]
    if versions:
        _cached_versions = versions

    if _search_map:
        await _off_loop(_save_to_disk)
    print(f"[Search] Search maps built: {len(_search_map)} keys")


def reindex_song(music: Music, music_data: dict[str, list[Music]]) -> None:
    """Re-index a single song in the live maps in place, for a lone alias add/remove.
    Rebuilding all ~9k keys romanizes every title (~1.5s of GIL-held work that stalls the
    event loop); this touches just one song. Runs with no await, so a concurrent lookup
    never observes the song half-updated. Aggregates (all_artists/level range) are
    alias-invariant, so they're left as-is."""
    m_levels = {
        (music.id, v.id, d.difficulty) for v in music.vocals for d in music.difficulties
    }
    for lk_map in (_search_map, _vocal_id_map):
        for key in list(lk_map.keys()):
            bucket = lk_map[key]
            if bucket & m_levels:
                bucket -= m_levels
                if not bucket:
                    del lk_map[key]
    for id_map in (_playlist_map, _playlist_alias_map, _playlist_auto_map):
        for key in list(id_map.keys()):
            bucket = id_map[key]
            if music.id in bucket:
                bucket.discard(music.id)
                if not bucket:
                    del id_map[key]
    _index_music(
        music,
        music_data,
        _search_map,
        _vocal_id_map,
        _playlist_map,
        _playlist_alias_map,
        _playlist_auto_map,
        set(),
        set(),
    )


async def save_maps() -> None:
    """Persist the live maps to disk. The snapshot is taken on-loop (so a reindex can't
    corrupt it), then written off the event loop."""
    if not _search_map:
        return
    data = _snapshot()
    await _off_loop(_write_snapshot, data)


def _index_event(
    event: Event,
    event_map: dict[str, set[int]],
    event_alias_map: dict[str, set[int]],
    event_auto_map: dict[str, set[int]],
) -> None:
    real = [event.name, str(event.id)]
    if event.pronunciation:
        real.append(event.pronunciation)
    for name in (event.name, event.pronunciation):
        base = _strip_brackets(name) if name else ""
        if base and preprocess(base) != preprocess(name):
            real.append(base)
    generated: list[str] = []
    for source in [event.name, event.pronunciation, *event.name_variants]:
        if source:
            generated.extend(_romanize(source))

    real_pp = _with_folded({preprocess(k) for k in real if preprocess(k)})
    manual_pp = (
        _with_folded({preprocess(k) for k in event.name_variants if preprocess(k)})
        - real_pp
    )
    auto_pp = (
        _with_folded({preprocess(k) for k in generated if preprocess(k)})
        - real_pp
        - manual_pp
    )

    for pk in real_pp | manual_pp | auto_pp:
        event_map.setdefault(pk, set()).add(event.id)
    for pk in manual_pp:
        event_alias_map.setdefault(pk, set()).add(event.id)
    for pk in auto_pp:
        event_auto_map.setdefault(pk, set()).add(event.id)


def _compute_event_maps(
    events: list[Event],
) -> tuple[dict[str, set[int]], dict[str, set[int]], dict[str, set[int]]]:
    event_map: dict[str, set[int]] = {}
    event_alias_map: dict[str, set[int]] = {}
    event_auto_map: dict[str, set[int]] = {}
    for event in events:
        _index_event(event, event_map, event_alias_map, event_auto_map)
    return event_map, event_alias_map, event_auto_map


def reindex_event(events: list[Event]) -> None:
    """Re-index one event id in the live event maps in place (see reindex_song). Takes all
    of that id's region copies, since the event maps index each region's localized name.
    """
    if not events:
        return
    event_id = events[0].id
    for id_map in (_event_map, _event_alias_map, _event_auto_map):
        for key in list(id_map.keys()):
            bucket = id_map[key]
            if event_id in bucket:
                bucket.discard(event_id)
                if not bucket:
                    del id_map[key]
    for event in events:
        _index_event(event, _event_map, _event_alias_map, _event_auto_map)


async def build_event_search_map(events: list[Event]) -> None:
    global _event_map, _event_alias_map, _event_auto_map
    _event_map, _event_alias_map, _event_auto_map = await _off_loop(
        _compute_event_maps, events
    )
    if _search_map:
        await _off_loop(_save_to_disk)
    print(f"[Search] Event maps built: {len(_event_map)} keys")


def _similarity(query: str, key: str) -> float:
    return TOKEN_SET_WEIGHT * fuzz.token_set_ratio(query, key) + (
        1 - TOKEN_SET_WEIGHT
    ) * fuzz.ratio(query, key)


def _score_key(variants: list[str], key: str) -> tuple[float, int]:
    """Best (similarity, edit_distance) of any query variant against `key`."""
    best_sim: float = -1.0
    best_dist = 10**9
    for q in variants:
        similarity: float = _similarity(q, key)
        edit_distance = Levenshtein.distance(q, key)
        if edit_distance > 5:
            excess = abs(len(key) - len(q))
            real_edits = max(0, edit_distance - excess)
            excess_penalty = max(0, excess - 5) * 2
            edit_penalty = max(0, real_edits - 5) * 5
            similarity -= excess_penalty + edit_penalty
        if similarity > best_sim or (
            similarity == best_sim and edit_distance < best_dist
        ):
            best_sim = similarity
            best_dist = edit_distance
    return best_sim, best_dist


def _key_penalty(
    key: str,
    entity_id: int,
    alias_map: dict[str, set[int]] | None,
    auto_map: dict[str, set[int]] | None,
) -> float:
    """How much to dock a match because of *how* the key relates to the id."""
    if alias_map and entity_id in alias_map.get(key, ()):
        return MANUAL_ALIAS_PENALTY
    if auto_map and entity_id in auto_map.get(key, ()):
        return AUTO_KEY_PENALTY
    return 0.0


def _fuzzy(
    scope: dict[str, set],
    query: str,
    sensitivity: float,
    limit: int,
    alias_map: dict[str, set[int]] | None = None,
    auto_map: dict[str, set[int]] | None = None,
):
    if not scope or not query.strip():
        return []

    sensitivity_100 = sensitivity * 100
    variants = _query_variants(query)
    scores: dict = {}
    distances: dict = {}

    for key, ids in scope.items():
        similarity, edit_distance = _score_key(variants, key)

        if similarity >= sensitivity_100:
            for i in ids:
                if i in _vocal_id_map.get(key, ()):
                    penalized = similarity * 0.9
                else:
                    penalized = similarity - _key_penalty(key, i, alias_map, auto_map)
                if (
                    i not in scores
                    or penalized > scores[i]
                    or (penalized == scores[i] and edit_distance < distances[i])
                ):
                    scores[i] = penalized
                    distances[i] = edit_distance

    # token_set_ratio gives 100 to token-subset matches ("meru" vs "meru to"),
    # so ties are broken by edit distance: exact/closest keys win.
    return sorted(scores.keys(), key=lambda i: (-scores[i], distances[i]))[:limit]


def _best_entity(
    scope: dict[str, set],
    alias_map: dict[str, set[int]],
    auto_map: dict[str, set[int]],
    query: str,
    sensitivity: float,
) -> tuple[int, str] | None:
    """Single best (id, matched key), the way the old bot did it: an exact key wins
    outright, and only if nothing matches exactly does a fuzzy pass run.

    The fuzzy pass scores on `fuzz.ratio` alone, not token_set_ratio. token_set_ratio
    returns a flat 100 for any token subset, so it can't tell "world" apart from "the
    world" (100) and "a word" (72.7) — it prefers the longer key. ratio is length-aware,
    so the closer key wins. That costs partial-phrase recall ("hatsune miku" no longer
    reaches the full title), which is why exact-first matters: every title, alias,
    romanisation and punctuation-folded form is already a key, so the fuzzy pass only
    ever sees typos and genuine near-misses.

    Ties break on the tier of the key (real title > hand-written alias > generated),
    then edit distance, then lowest id."""
    if not scope or not query.strip():
        return None
    variants = _query_variants(query)

    # Exact-first. Variants are ordered typed-form, romanisations, folded forms, so the
    # key the user actually typed is reported back rather than an equivalent spelling.
    for variant in variants:
        ids = scope.get(variant)
        if ids:
            best_id = min(
                sorted(ids),
                key=lambda i: (_key_penalty(variant, i, alias_map, auto_map), i),
            )
            return best_id, variant

    sensitivity_100 = sensitivity * 100
    best_id_opt: int | None = None
    best_key: str = ""
    best: tuple[float, int] | None = None

    for key, ids in scope.items():
        similarity: float = -1.0
        edit_distance = 10**9
        for q in variants:
            sim: float = fuzz.ratio(q, key)
            dist = Levenshtein.distance(q, key)
            if dist > 5:
                sim -= (dist - 5) * 5
            if sim > similarity or (sim == similarity and dist < edit_distance):
                similarity = sim
                edit_distance = dist
        if similarity < sensitivity_100:
            continue
        for i in sorted(ids):
            score = similarity - _key_penalty(key, i, alias_map, auto_map)
            candidate = (score, -edit_distance)
            if best is None or candidate > best:
                best = candidate
                best_id_opt = i
                best_key = key
    return None if best_id_opt is None else (best_id_opt, best_key)


def song_keys(music_id: int) -> list[str]:
    """Every key the song matcher accepts for this song — title, aliases, and all
    generated romanizations. Already preprocessed."""
    return sorted(k for k, ids in _playlist_map.items() if music_id in ids)


def best_song_match_key(
    query: str, sensitivity: float = DEFAULT_SENSITIVITY
) -> tuple[int, str] | None:
    """The matched song id along with the key (title or alias) that matched it."""
    return _best_entity(
        _playlist_map, _playlist_alias_map, _playlist_auto_map, query, sensitivity
    )


def best_song_match(query: str, sensitivity: float = DEFAULT_SENSITIVITY) -> int | None:
    hit = best_song_match_key(query, sensitivity)
    return hit[0] if hit else None


def best_event_match(
    query: str, sensitivity: float = DEFAULT_SENSITIVITY
) -> int | None:
    hit = _best_entity(
        _event_map, _event_alias_map, _event_auto_map, query, sensitivity
    )
    return hit[0] if hit else None


def fuzzy_search(
    query: str, sensitivity: float = 0.65, limit: int = 200
) -> list[LevelKey]:
    return _fuzzy(_search_map, query, sensitivity, limit)


def fuzzy_search_playlists(
    query: str, sensitivity: float = 0.65, limit: int = 200
) -> list[int]:
    return _fuzzy(
        _playlist_map,
        query,
        sensitivity,
        limit,
        _playlist_alias_map,
        _playlist_auto_map,
    )


def fuzzy_search_events(
    query: str, sensitivity: float = 0.65, limit: int = 50
) -> list[int]:
    return _fuzzy(
        _event_map, query, sensitivity, limit, _event_alias_map, _event_auto_map
    )


def get_all_artists() -> list[str]:
    return _all_artists


def get_all_captions() -> list[str]:
    return _all_captions


def get_level_range() -> tuple[int, int]:
    return _min_level, _max_level
