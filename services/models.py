from typing import Any, Literal

from pydantic import BaseModel

from data.models import Music

Region = Literal["en", "jp", "tw", "kr", "cn"]
ImageType = Literal["webp", "png"]
Difficulty = Literal["easy", "normal", "hard", "expert", "master", "append"]


# --- request bodies ---


class MusicSearchBody(BaseModel):
    query: str
    region: Region | None = None
    difficulties: list[Difficulty] | None = None


class AddSongAliasBody(BaseModel):
    music_id: int
    alias: str
    region: Region | None = None


class AddEventAliasBody(BaseModel):
    event_id: int
    alias: str
    region: Region | None = None


class RemoveAliasBody(BaseModel):
    alias_id: int


class CheckWordsBody(BaseModel):
    text: str
    region: Region


# --- pjsk data responses ---


class VersionResponse(BaseModel):
    data_version: str | None = None
    asset_version: str | None = None


class MusicSimple(BaseModel):
    id: int
    title: str
    difficulties: list[str] = []
    jacket_url: str = ""


class MusicsSimpleResponse(BaseModel):
    musics: list[MusicSimple] = []


class MusicsResponse(BaseModel):
    musics: list[Music] = []


class MusicSearchResponse(BaseModel):
    ids: list[int] = []


class ProfileResponse(BaseModel):
    updated: float
    next_available_update: float
    profile: dict[str, Any] = {}


class CurrentEventResponse(BaseModel):
    updated: float
    next_available_update: float
    event_id: int | None = None
    event_status: str | None = None
    top_100: dict[str, Any] | None = None
    border: dict[str, Any] | None = None


class CurrentRankedResponse(BaseModel):
    updated: float
    next_available_update: float
    season_id: int | None = None
    season_status: str | None = None
    season_name: str | None = None
    top_100: dict[str, Any] | None = None
    cheaters: list[str] = []


class Comic(BaseModel):
    title: str
    image_url: str
    from_user_rank: int | None = None
    to_user_rank: int | None = None


class ComicsResponse(BaseModel):
    comics: list[Comic] = []


class Stamp(BaseModel):
    stamp_type: str | None = None
    name: str
    character_ids: list[int] = []
    game_character_unit_id: int | None = None
    description: str | None = None
    image_url: str = ""
    balloon_url: str = ""


class StampsResponse(BaseModel):
    stamps: list[Stamp] = []


# --- tools ---


class InappropriateRange(BaseModel):
    start: int
    end: int


class WhyInappropriateResponse(BaseModel):
    indexes: list[InappropriateRange] = []


# --- new contracts (see MISSING_SBUGA_ROUTES.md) ---


# --- alias management ---


class SongAlias(BaseModel):
    id: int
    alias: str
    music_id: int
    region: Region | None = None
    created_at: str | None = None
    created_by: int | None = None


class SongAliasesResponse(BaseModel):
    aliases: list[SongAlias] = []


class EventAlias(BaseModel):
    id: int
    alias: str
    event_id: int
    region: Region | None = None
    created_at: str | None = None
    created_by: int | None = None


class EventAliasesResponse(BaseModel):
    aliases: list[EventAlias] = []


class AliasAddResponse(BaseModel):
    success: bool
    id: int


class SuccessResponse(BaseModel):
    success: bool
