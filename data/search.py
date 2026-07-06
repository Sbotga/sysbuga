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


def preprocess(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
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
        "versions": _cached_versions,
        "search_map": {k: [list(lk) for lk in v] for k, v in _search_map.items()},
        "vocal_id_map": {k: [list(lk) for lk in v] for k, v in _vocal_id_map.items()},
        "playlist_map": {k: list(v) for k, v in _playlist_map.items()},
        "event_map": {k: list(v) for k, v in _event_map.items()},
        "all_artists": _all_artists,
        "all_captions": _all_captions,
        "min_level": _min_level,
        "max_level": _max_level,
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_from_disk() -> bool:
    global _search_map, _vocal_id_map, _playlist_map, _event_map
    global _all_artists, _all_captions, _min_level, _max_level, _cached_versions
    if not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cached_versions = data.get("versions", {})
        _search_map = {
            k: {tuple(lk) for lk in v} for k, v in data.get("search_map", {}).items()
        }
        _vocal_id_map = {
            k: {tuple(lk) for lk in v} for k, v in data.get("vocal_id_map", {}).items()
        }
        _playlist_map = {k: set(v) for k, v in data.get("playlist_map", {}).items()}
        _event_map = {k: set(v) for k, v in data.get("event_map", {}).items()}
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

        all_title_keys: list[str] = []
        for source_list in music_data.values():
            for m in source_list:
                if m.id == music.id:
                    all_title_keys.append(m.title)
                    if m.pronunciation:
                        all_title_keys.append(m.pronunciation)
                    all_title_keys.extend(m.title_variants)
        all_title_keys.extend(music.title_variants)
        all_title_keys.append(music.title)
        if music.pronunciation:
            all_title_keys.append(music.pronunciation)
        for title in list(dict.fromkeys(all_title_keys)):
            all_title_keys.extend(_romanize(title))
        deduped_titles = list(dict.fromkeys(all_title_keys))
        _add_keys(deduped_titles, all_level_keys)
        _add_playlist_keys(deduped_titles, mid_list)

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
    for event in events:
        keys = [event.name, *event.name_variants, str(event.id)]
        if event.pronunciation:
            keys.append(event.pronunciation)
        for source in [event.name, event.pronunciation, *event.name_variants]:
            if source:
                keys.extend(_romanize(source))
        for key in keys:
            pk = preprocess(key)
            if pk:
                _event_map.setdefault(pk, set()).add(event.id)
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


def _fuzzy(scope: dict[str, set], query: str, sensitivity: float, limit: int):
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
                penalized = (
                    similarity * 0.9 if i in _vocal_id_map.get(key, ()) else similarity
                )
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


def _best_key(scope: dict[str, set], query: str, sensitivity: float) -> str | None:
    """Sbotga-style single-best matcher: one winning key by token_set_ratio
    with an edit-distance penalty, ties broken by edit distance."""
    if not scope or not query.strip():
        return None
    variants = _query_variants(query)
    sensitivity_100 = sensitivity * 100
    best_key: str | None = None
    best_score: float = 0.0
    best_distance = 10**9

    for key in scope:
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
        if similarity > best_score or (
            similarity == best_score and edit_distance < best_distance
        ):
            best_key = key
            best_score = similarity
            best_distance = edit_distance
    return best_key


def best_song_match(query: str, sensitivity: float = 0.6) -> int | None:
    key = _best_key(_playlist_map, query, sensitivity)
    if key is None:
        return None
    return min(_playlist_map[key])


def best_event_match(query: str, sensitivity: float = 0.6) -> int | None:
    key = _best_key(_event_map, query, sensitivity)
    if key is None:
        return None
    return min(_event_map[key])


def fuzzy_search(
    query: str, sensitivity: float = 0.65, limit: int = 200
) -> list[LevelKey]:
    return _fuzzy(_search_map, query, sensitivity, limit)


def fuzzy_search_playlists(
    query: str, sensitivity: float = 0.65, limit: int = 200
) -> list[int]:
    return _fuzzy(_playlist_map, query, sensitivity, limit)


def fuzzy_search_events(
    query: str, sensitivity: float = 0.65, limit: int = 50
) -> list[int]:
    return _fuzzy(_event_map, query, sensitivity, limit)


def get_all_artists() -> list[str]:
    return _all_artists


def get_all_captions() -> list[str]:
    return _all_captions


def get_level_range() -> tuple[int, int]:
    return _min_level, _max_level
