from data.models import Character, Event, Music
from data.pjsk import PJSKData, character_display_name

DIFFICULTY_ALIASES = {
    "append": "append",
    "master": "master",
    "expert": "expert",
    "hard": "hard",
    "normal": "normal",
    "easy": "easy",
    "apd": "append",
    "mas": "master",
    "exp": "expert",
    "ex": "expert",
    "norm": "normal",
    "ez": "easy",
}


def match_difficulty(arg: str | None) -> str | None:
    if arg is None:
        return None
    return DIFFICULTY_ALIASES.get(str(arg).lower().strip())


def match_song(pjsk: PJSKData, arg: str | int | None) -> Music | None:
    hit = match_song_with_key(pjsk, arg)
    return hit[0] if hit else None


def match_song_with_key(
    pjsk: PJSKData, arg: str | int | None
) -> tuple[Music, str] | None:
    """The matched song plus the key (title or alias) the query actually hit."""
    if arg is None:
        return None
    q = str(arg).strip()
    if q.isdigit() and q != "39":
        music = pjsk.get_music(int(q))
        if music:
            return music, q
    hit = pjsk.best_song_id_key(q)
    if hit is None:
        return None
    mid, key = hit
    music = pjsk.get_music(mid)
    return (music, key) if music else None


def describe_song_match(title: str, key: str) -> str:
    """`Title`, plus the alias that matched — omitted when it *is* the title."""
    from data.search import preprocess

    if preprocess(key) == preprocess(title):
        return f"**`{title}`**"
    return f"**`{title}`** (`{key}`)"


def match_event(pjsk: PJSKData, arg: str | int | None) -> Event | None:
    if arg is None:
        return None
    q = str(arg).strip()
    if q.isdigit():
        event = pjsk.get_event(int(q))
        if event:
            return event
    eid = pjsk.best_event_id(q)
    return pjsk.get_event(eid) if eid is not None else None


def match_character(pjsk: PJSKData, arg: str | int | None) -> Character | None:
    if arg is None:
        return None
    q = str(arg).strip().lower().replace(" ", "")
    if not q:
        return None
    if q.isdigit():
        char = pjsk.get_character(int(q))
        if char:
            return char
    for char in pjsk.characters():
        names = [
            character_display_name(char),
            char.given_name,
            char.first_name,
            f"{char.given_name}{char.first_name}",
            f"{char.first_name}{char.given_name}",
        ]
        if any(q == n.lower().replace(" ", "") for n in names if n):
            return char
    return None
