import json
import os
import re
import unicodedata

import cutlet
from korean_romanizer.romanizer import Romanizer as _KoreanRomanizer
from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from data.models import Event, Music, _build_char_name

_katsu_hepburn = cutlet.Cutlet(
    system="hepburn", use_foreign_spelling=False, ensure_ascii=False
)
_katsu_nihon = cutlet.Cutlet(
    system="nihon", use_foreign_spelling=False, ensure_ascii=False
)
_katsu_kunrei = cutlet.Cutlet(
    system="kunrei", use_foreign_spelling=False, ensure_ascii=False
)

ROMANIZERS = [
    lambda text: _katsu_hepburn.romaji(text).lower().strip(),
    lambda text: _katsu_nihon.romaji(text).lower().strip(),
    lambda text: _katsu_kunrei.romaji(text).lower().strip(),
]

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
CACHE_VERSION = 2  # bump when the cache shape changes (e.g. added alias maps)


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
    (jp/kr/zh) matches the romanized keys and vice versa."""
    query_pp = preprocess(query)
    return list(dict.fromkeys([query_pp, *_romanize(query_pp)]))


LevelKey = tuple[int, int, str]

_search_map: dict[str, set[LevelKey]] = {}
_vocal_id_map: dict[str, set[LevelKey]] = {}
_playlist_map: dict[str, set[int]] = {}
_event_map: dict[str, set[int]] = {}
# key -> ids for which that key is ONLY an alias (title/name variant), not the real
# title/name or a romanization of it. Used to prefer real-title matches on ties, so
# e.g. "aria" resolves to the song *titled* アリア over one merely aliased "aria".
_playlist_alias_map: dict[str, set[int]] = {}
_event_alias_map: dict[str, set[int]] = {}
ALIAS_PENALTY = 3.0  # slight; only flips near-ties toward the real title
_all_artists: list[str] = []
_all_captions: list[str] = []
_min_level: int = 1
_max_level: int = 40
_cached_versions: dict[str, str] = {}


def _add_keys(keys: list[str], level_keys: list[LevelKey]) -> None:
    for key in keys:
        pk = preprocess(key)
        if pk:
            _search_map.setdefault(pk, set()).update(level_keys)


def _add_playlist_keys(keys: list[str], music_ids: list[int]) -> None:
    for key in keys:
        pk = preprocess(key)
        if pk:
            _playlist_map.setdefault(pk, set()).update(music_ids)


def _save_to_disk() -> None:
    os.makedirs("cache", exist_ok=True)
    data = {
        "cache_version": CACHE_VERSION,
        "versions": _cached_versions,
        "search_map": {k: [list(lk) for lk in v] for k, v in _search_map.items()},
        "vocal_id_map": {k: [list(lk) for lk in v] for k, v in _vocal_id_map.items()},
        "playlist_map": {k: list(v) for k, v in _playlist_map.items()},
        "event_map": {k: list(v) for k, v in _event_map.items()},
        "playlist_alias_map": {k: list(v) for k, v in _playlist_alias_map.items()},
        "event_alias_map": {k: list(v) for k, v in _event_alias_map.items()},
        "all_artists": _all_artists,
        "all_captions": _all_captions,
        "min_level": _min_level,
        "max_level": _max_level,
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_from_disk() -> bool:
    global _search_map, _vocal_id_map, _playlist_map, _event_map
    global _playlist_alias_map, _event_alias_map
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


def build_search_maps(
    musics: list[Music],
    music_data: dict[str, list[Music]],
    versions: dict[str, str] | None = None,
) -> None:
    global _all_artists, _all_captions, _min_level, _max_level, _cached_versions
    _search_map.clear()
    _vocal_id_map.clear()
    _playlist_map.clear()
    _playlist_alias_map.clear()

    all_artists_set: set[str] = set()
    all_captions_set: set[str] = set()
    min_lv = 99
    max_lv = 0

    for music in musics:
        mid_list = [music.id]
        all_level_keys: list[LevelKey] = []
        for vocal in music.vocals:
            for diff in music.difficulties:
                all_level_keys.append((music.id, vocal.id, diff.difficulty))

        # real titles (title + pronunciation, all regions) vs aliases (title_variants)
        real_titles: list[str] = []
        alias_titles: list[str] = []
        for source_list in music_data.values():
            for m in source_list:
                if m.id == music.id:
                    real_titles.append(m.title)
                    if m.pronunciation:
                        real_titles.append(m.pronunciation)
                    alias_titles.extend(m.title_variants)
        alias_titles.extend(music.title_variants)
        real_titles.append(music.title)
        if music.pronunciation:
            real_titles.append(music.pronunciation)

        real_keys = list(real_titles)
        for t in list(dict.fromkeys(real_titles)):
            real_keys.extend(_romanize(t))
        alias_keys = list(alias_titles)
        for t in list(dict.fromkeys(alias_titles)):
            alias_keys.extend(_romanize(t))

        real_pp = {preprocess(k) for k in real_keys if preprocess(k)}
        alias_pp = {preprocess(k) for k in alias_keys if preprocess(k)}
        deduped_titles = list(real_pp | alias_pp)
        _add_keys(deduped_titles, all_level_keys)
        _add_playlist_keys(deduped_titles, mid_list)
        # keys that are ONLY an alias for this song (not also its real title)
        for k in alias_pp - real_pp:
            _playlist_alias_map.setdefault(k, set()).update(mid_list)

        _add_keys([str(music.id)], all_level_keys)
        _add_playlist_keys([str(music.id)], mid_list)

        if music.artist:
            artist_keys = [music.artist.name, *_romanize(music.artist.name)]
            if music.artist.pronunciation:
                artist_keys.append(music.artist.pronunciation)
                artist_keys.extend(_romanize(music.artist.pronunciation))
            _add_keys(artist_keys, all_level_keys)
            _add_playlist_keys(artist_keys, mid_list)

        for field in [music.lyricist, music.composer, music.arranger]:
            if field:
                field_keys = [field, *_romanize(field)]
                _add_keys(field_keys, all_level_keys)
                _add_playlist_keys(field_keys, mid_list)

        for vocal in music.vocals:
            vocal_level_keys = [
                (music.id, vocal.id, diff.difficulty) for diff in music.difficulties
            ]

            vocal_id_str = preprocess(str(vocal.id))
            _add_keys([vocal_id_str], vocal_level_keys)
            _vocal_id_map.setdefault(vocal_id_str, set()).update(vocal_level_keys)

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
                        _add_keys(name_keys, vocal_level_keys)
                else:
                    outside = music.outside_characters.get(c.character_id)
                    if outside:
                        all_artists_set.add(outside.name)
                        name_keys = [outside.name, *_romanize(outside.name)]
                        _add_keys(name_keys, vocal_level_keys)

        for diff in music.difficulties:
            diff_level_keys = [
                (music.id, vocal.id, diff.difficulty) for vocal in music.vocals
            ]
            _add_keys([diff.difficulty], diff_level_keys)
            if diff.play_level < min_lv:
                min_lv = diff.play_level
            if diff.play_level > max_lv:
                max_lv = diff.play_level

    _all_artists = sorted(all_artists_set)
    _all_captions = sorted(all_captions_set)
    _min_level = min_lv if min_lv < 99 else 1
    _max_level = max_lv if max_lv > 0 else 40

    if versions:
        _cached_versions = versions

    if _search_map:
        _save_to_disk()
    print(f"[Search] Search maps built: {len(_search_map)} keys")


def build_event_search_map(events: list[Event]) -> None:
    _event_map.clear()
    _event_alias_map.clear()
    for event in events:
        real = [event.name, str(event.id)]
        if event.pronunciation:
            real.append(event.pronunciation)
        for source in [event.name, event.pronunciation]:
            if source:
                real.extend(_romanize(source))
        alias = list(event.name_variants)
        for source in event.name_variants:
            if source:
                alias.extend(_romanize(source))

        real_pp = {preprocess(k) for k in real if preprocess(k)}
        alias_pp = {preprocess(k) for k in alias if preprocess(k)}
        for pk in real_pp | alias_pp:
            _event_map.setdefault(pk, set()).add(event.id)
        for pk in alias_pp - real_pp:
            _event_alias_map.setdefault(pk, set()).add(event.id)
    if _search_map:
        _save_to_disk()
    print(f"[Search] Event maps built: {len(_event_map)} keys")


def _score_key(variants: list[str], key: str) -> tuple[float, int]:
    """Best (similarity, edit_distance) of any query variant against `key`."""
    best_sim: float = -1.0
    best_dist = 10**9
    for q in variants:
        similarity: float = fuzz.token_set_ratio(q, key)
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


def _fuzzy(
    scope: dict[str, set],
    query: str,
    sensitivity: float,
    limit: int,
    alias_map: dict[str, set[int]] | None = None,
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
            aliased = alias_map.get(key, ()) if alias_map else ()
            for i in ids:
                if i in _vocal_id_map.get(key, ()):
                    penalized = similarity * 0.9
                elif i in aliased:
                    penalized = similarity - ALIAS_PENALTY
                else:
                    penalized = similarity
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
    query: str,
    sensitivity: float,
) -> int | None:
    """Single best id: token_set_ratio with an edit-distance penalty, plus a slight
    penalty when the matched key is only an *alias* for that id — so the song/event
    the key is the real title of wins a tie over one merely aliased to it. Ties are
    otherwise broken by edit distance, then lowest id (matching the old min())."""
    if not scope or not query.strip():
        return None
    variants = _query_variants(query)
    sensitivity_100 = sensitivity * 100
    best_id: int | None = None
    best_score = -1.0
    best_distance = 10**9

    for key, ids in scope.items():
        similarity: float = -1.0
        edit_distance = 10**9
        for q in variants:
            sim: float = fuzz.token_set_ratio(q, key)
            dist = Levenshtein.distance(q, key)
            if dist > 5:
                sim -= (dist - 5) * 5
            if sim > similarity or (sim == similarity and dist < edit_distance):
                similarity = sim
                edit_distance = dist
        if similarity < sensitivity_100:
            continue
        alias_ids = alias_map.get(key, ())
        for i in sorted(ids):
            score = similarity - (ALIAS_PENALTY if i in alias_ids else 0.0)
            if score > best_score or (
                score == best_score and edit_distance < best_distance
            ):
                best_id = i
                best_score = score
                best_distance = edit_distance
    return best_id


def best_song_match(query: str, sensitivity: float = 0.6) -> int | None:
    return _best_entity(_playlist_map, _playlist_alias_map, query, sensitivity)


def best_event_match(query: str, sensitivity: float = 0.6) -> int | None:
    return _best_entity(_event_map, _event_alias_map, query, sensitivity)


def fuzzy_search(
    query: str, sensitivity: float = 0.65, limit: int = 200
) -> list[LevelKey]:
    return _fuzzy(_search_map, query, sensitivity, limit)


def fuzzy_search_playlists(
    query: str, sensitivity: float = 0.65, limit: int = 200
) -> list[int]:
    return _fuzzy(_playlist_map, query, sensitivity, limit, _playlist_alias_map)


def fuzzy_search_events(
    query: str, sensitivity: float = 0.65, limit: int = 50
) -> list[int]:
    return _fuzzy(_event_map, query, sensitivity, limit, _event_alias_map)


def get_all_artists() -> list[str]:
    return _all_artists


def get_all_captions() -> list[str]:
    return _all_captions


def get_level_range() -> tuple[int, int]:
    return _min_level, _max_level
