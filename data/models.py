from pydantic import BaseModel


# --- music (ported from sbuga-sonolus-server helpers/models/api/music.py) ---


class MusicArtist(BaseModel):
    id: int
    name: str
    pronunciation: str | None = None


class MusicDifficulty(BaseModel):
    difficulty: str
    play_level: int
    total_note_count: int
    chart_url: str = ""


class GameCharacterData(BaseModel):
    givenName: str = ""
    firstName: str = ""
    unit: str = ""


class OutsideCharacterData(BaseModel):
    name: str = ""


class VocalCharacter(BaseModel):
    character_type: str
    character_id: int
    seq: int


class VocalVariant(BaseModel):
    id: int
    seq: int
    asset_type: str
    assetbundle_name: str
    jacket_url: str | None = None
    background_v1_url: str | None = None
    background_v3_url: str | None = None


class MusicVocal(BaseModel):
    id: int
    vocal_type: str
    caption: str
    characters: list[VocalCharacter]
    assetbundle_name: str
    bgm_url: str | None = None
    bgm_nosil_url: str | None = None
    preview_url: str | None = None
    published_at: int | None = None
    variants: list[VocalVariant] = []


class Music(BaseModel):
    id: int
    title: str
    pronunciation: str | None = None
    title_variants: list[str] = []
    # the subset of title_variants a human actually added; the backend folds its own
    # generated keys into title_variants, so the two tiers can't be told apart there
    manual_aliases: list[str] = []
    lyricist: str | None = None
    composer: str | None = None
    arranger: str | None = None
    artist: MusicArtist | None = None
    categories: list[str] = []
    tags: list[str] = []
    published_at: int
    released_at: int | None = None
    is_newly_written: bool = False
    is_full_length: bool = False
    filler_sec: float = 0.0
    sec_for_music_score_maker: int | None = None
    jacket_url: str = ""
    background_v1_url: str = ""
    background_v3_url: str = ""
    collaboration: str | None = None
    collaboration_id: int | None = None
    original_video: str | None = None
    difficulties: list[MusicDifficulty] = []
    vocals: list[MusicVocal] = []
    game_characters: dict[int, GameCharacterData] = {}
    outside_characters: dict[int, OutsideCharacterData] = {}


def _is_cjk(text: str) -> bool:
    return any("　" <= c <= "鿿" or "豈" <= c <= "﫿" for c in text)


def _build_char_name(char_data: GameCharacterData) -> str:
    if not char_data.firstName:
        return char_data.givenName.title()
    sep = "" if _is_cjk(char_data.givenName) else " "
    if char_data.unit == "piapro":
        return f"{char_data.firstName}{sep}{char_data.givenName}".title()
    return f"{char_data.givenName}{sep}{char_data.firstName}".title()


def get_vocal_artist(vocal: MusicVocal, music: Music) -> str:
    if not vocal.characters:
        return vocal.caption
    characters = sorted(vocal.characters, key=lambda c: c.seq)
    names: list[str] = []
    for c in characters:
        if c.character_type == "game_character":
            char_data = music.game_characters.get(c.character_id)
            names.append(
                _build_char_name(char_data)
                if char_data
                else f"Character {c.character_id}"
            )
        else:
            outside = music.outside_characters.get(c.character_id)
            names.append(
                outside.name.title() if outside else f"Character {c.character_id}"
            )
    return " & ".join(names)


# --- new sbuga contracts (see MISSING_SBUGA_ROUTES.md) ---


class WorldBloom(BaseModel):
    chapter_no: int
    game_character_id: int | None = None  # None for finale chapters
    start_at: int
    aggregate_at: int


class Event(BaseModel):
    id: int
    name: str
    name_variants: list[str] = []
    pronunciation: str | None = None
    event_type: str
    unit: str | None = None
    asset_bundle_name: str | None = None
    start_at: int | None = None
    aggregate_at: int | None = None
    ranking_announce_at: int | None = None
    distribution_start_at: int | None = None
    closed_at: int | None = None
    bonus_attribute: str | None = None
    bonus_character_ids: list[int] = []
    logo_url: str | None = None
    banner_url: str | None = None
    background_url: str | None = None
    character_url: str | None = None
    world_blooms: list[WorldBloom] = []


class Character(BaseModel):
    id: int
    given_name: str = ""
    first_name: str = ""
    given_name_pronunciation: str | None = None
    first_name_pronunciation: str | None = None
    unit: str | None = None
    support_unit: str | None = None
    gender: str | None = None
    color: str | None = None
    height: str | None = None
    birthday: str | None = None
    school: str | None = None
    school_year: str | None = None
    hobby: str | None = None
    special_skill: str | None = None
    favorite_food: str | None = None
    hated_food: str | None = None
    weak_point: str | None = None
    introduction: str | None = None
    voice_actor: str | None = None


class CheerfulCarnivalTeam(BaseModel):
    id: int
    event_id: int
    team_name: str
    asset_bundle_name: str | None = None


class Card(BaseModel):
    id: int
    character_id: int
    card_rarity_type: str
    attr: str | None = None
    support_unit: str | None = None
    prefix: str = ""
    gacha_phrase: str | None = None
    release_at: int | None = None
    archive_published_at: int | None = None
    card_url_normal: str | None = None
    card_url_trained: str | None = None
    cutout_url_normal: str | None = None
    cutout_url_trained: str | None = None
    thumbnail_url_normal: str | None = None
    thumbnail_url_trained: str | None = None
    frame_rarity: str | None = None


class GachaRate(BaseModel):
    card_rarity_type: str
    rate: float


class Gacha(BaseModel):
    id: int
    name: str
    gacha_type: str | None = None
    start_at: int | None = None
    end_at: int | None = None
    asset_bundle_name: str | None = None
    banner_url: str | None = None
    rarity_rates: list[GachaRate] = []
    pickup_card_ids: list[int] = []
    pool_card_ids: list[int] = []
