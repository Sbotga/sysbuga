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
    if arg is None:
        return None
    q = str(arg).strip()
    if q.isdigit() and q != "39":
        music = pjsk.get_music(int(q))
        if music:
            return music
    mid = pjsk.best_song_id(q)
    return pjsk.get_music(mid) if mid is not None else None


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
