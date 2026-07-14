import asyncio
from typing import Any, Literal
from urllib.parse import quote

import aiohttp

from data.models import Music
from helpers import unblock
from services.models import (
    AddEventAliasBody,
    AddSongAliasBody,
    AliasAddResponse,
    CheckWordsBody,
    Comic,
    ComicsResponse,
    CurrentEventResponse,
    CurrentRankedResponse,
    Difficulty,
    EventAlias,
    EventAliasesResponse,
    ImageType,
    MusicSearchBody,
    MusicSearchResponse,
    MusicsResponse,
    ProfileResponse,
    Region,
    RemoveAliasBody,
    SongAlias,
    SongAliasesResponse,
    Stamp,
    StampsResponse,
    SuccessResponse,
    VersionResponse,
    WhyInappropriateResponse,
)

__all__ = [
    "SbugaClient",
    "SbugaError",
    "SbugaUnavailable",
    "SbugaNotFound",
    "Region",
    "ImageType",
    "Difficulty",
]


class SbugaError(Exception):
    def __init__(self, status: int, detail: Any = "") -> None:
        self.status = status
        # some errors (e.g. a taken alias) return a structured detail; keep `detail`
        # a plain code string so existing messages stay readable, and expose the rest
        self.data: dict[str, Any] = detail if isinstance(detail, dict) else {}
        self.detail: str = str(self.data.get("code", "")) if self.data else str(detail)
        super().__init__(f"sbuga API error {status}: {self.detail}")


class SbugaUnavailable(SbugaError):
    """Region client down (503) or the endpoint does not exist yet."""


class SbugaNotFound(SbugaError):
    """404 from the API."""


class SbugaClient:
    def __init__(
        self,
        api_url: str,
        *,
        image_type: ImageType = "webp",
        bot_token: str = "",
    ) -> None:
        self.base = api_url.rstrip("/")
        self.image_type: ImageType = image_type
        self.bot_token = bot_token
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # --- low-level ---

    def _url(self, path: str) -> str:
        return f"{self.base}/api{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        session = await self._ensure_session()
        headers: dict[str, str] = {}
        if auth:
            # the backend resolves a bot token to its account row, so permissions
            # (manage_aliases) work exactly like a human account's
            headers["Authorization"] = f"Bot {self.bot_token}"
        async with session.request(
            method, self._url(path), params=_clean(params), json=json, headers=headers
        ) as resp:
            if resp.status == 404:
                raise SbugaNotFound(404, await _detail(resp))
            if resp.status == 503:
                raise SbugaUnavailable(503, await _detail(resp))
            if resp.status >= 400:
                raise SbugaError(resp.status, await _detail(resp))
            return await resp.json()

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params)

    async def _get_bytes(self, path: str, **params: Any) -> bytes:
        session = await self._ensure_session()
        async with session.get(self._url(path), params=_clean(params)) as resp:
            if resp.status == 404:
                raise SbugaNotFound(404, await _detail(resp))
            if resp.status == 503:
                raise SbugaUnavailable(503, await _detail(resp))
            if resp.status >= 400:
                raise SbugaError(resp.status, await _detail(resp))
            return await resp.read()

    # --- pjsk data (existing endpoints) ---

    async def get_version(self, region: Region) -> VersionResponse:
        return VersionResponse.model_validate(
            await self._get("/pjsk_data/version", region=region)
        )

    async def get_musics(
        self,
        region: Region,
        *,
        image_type: ImageType | None = None,
        ignore_leak: bool = False,
    ) -> list[Music]:
        data = await self._get(
            "/pjsk_data/musics",
            region=region,
            image_type=image_type or self.image_type,
            ignore_leak=str(ignore_leak).lower(),
        )
        # validating the whole musics payload (~hundreds of models) is heavy; keep it off
        # the event loop
        return await asyncio.get_running_loop().run_in_executor(
            unblock.executor, lambda: MusicsResponse.model_validate(data).musics
        )

    async def search_musics(self, body: MusicSearchBody) -> list[int]:
        data = await self._request(
            "POST", "/pjsk_data/musics/search", json=body.model_dump(exclude_none=True)
        )
        return MusicSearchResponse.model_validate(data).ids

    async def get_profile(
        self, user_id: int, region: Region, *, fresh: bool = False
    ) -> ProfileResponse:
        return ProfileResponse.model_validate(
            await self._get(
                f"/pjsk_data/profile/{user_id}",
                region=region,
                fresh="true" if fresh else None,
            )
        )

    async def get_current_event(
        self, region: Region, *, fresh: bool = False
    ) -> CurrentEventResponse:
        return CurrentEventResponse.model_validate(
            await self._get(
                "/pjsk_data/current_event",
                region=region,
                fresh="true" if fresh else None,
            )
        )

    async def get_current_ranked(self, region: Region) -> CurrentRankedResponse:
        return CurrentRankedResponse.model_validate(
            await self._get("/pjsk_data/current_ranked", region=region)
        )

    async def get_comics(
        self, region: Region, *, image_type: ImageType | None = None
    ) -> list[Comic]:
        data = await self._get(
            "/pjsk_data/comics", region=region, image_type=image_type or self.image_type
        )
        return ComicsResponse.model_validate(data).comics

    async def get_stamps(
        self, region: Region, *, image_type: ImageType | None = None
    ) -> list[Stamp]:
        data = await self._get(
            "/pjsk_data/stamps", region=region, image_type=image_type or self.image_type
        )
        return StampsResponse.model_validate(data).stamps

    # --- tools / assets (existing) ---

    async def get_chart_image(
        self,
        music_id: int,
        difficulty: Difficulty,
        region: Region,
        *,
        mirrored: bool = False,
    ) -> bytes:
        # The endpoint 302-redirects un-mirrored charts to R2 (aiohttp follows
        # it transparently) and applies the flip itself for mirrored ones.
        return await self._get_bytes(
            "/tools/chart_viewer",
            music_id=music_id,
            difficulty=difficulty,
            region=region,
            image_type=self.image_type,
            mirrored=str(mirrored).lower(),
        )

    async def get_custom_chart_image(
        self, chart_id: str, region: Region, *, mirrored: bool = False
    ) -> bytes:
        # renders + returns the custom chart png (server caches the image)
        return await self._get_bytes(
            "/tools/custom_chart",
            chart_id=chart_id,
            region=region,
            chart_image="true",
            mirrored=str(mirrored).lower(),
        )

    async def get_custom_chart_info(self, chart_id: str, region: Region) -> dict:
        # raw published-score metadata (no image)
        return await self._get(
            "/tools/custom_chart",
            chart_id=chart_id,
            region=region,
        )

    async def why_inappropriate(
        self, text: str, region: Region
    ) -> WhyInappropriateResponse:
        data = await self._request(
            "POST",
            "/tools/why_inappropriate",
            json=CheckWordsBody(text=text, region=region).model_dump(),
        )
        return WhyInappropriateResponse.model_validate(data)

    def asset_url(
        self, asset_path: str, region: Region | Literal["auto"] = "auto"
    ) -> str:
        return f"{self.base}/api/pjsk_data/assets/{quote(asset_path)}?region={region}"

    async def get_asset(
        self, asset_path: str, region: Region | Literal["auto"] = "auto"
    ) -> bytes:
        return await self._get_bytes(f"/pjsk_data/assets/{asset_path}", region=region)

    # --- raw masterdata (GET /pjsk_data/master/{file}; see data/masterdata.py) ---

    async def get_master(self, file: str, region: Region) -> Any:
        return await self._get(f"/pjsk_data/master/{file}", region=region)

    # --- aliases (reads are public; editing needs a bot token whose account has
    #     the `manage_aliases` permission) ---

    async def get_song_aliases(self) -> list[SongAlias]:
        return SongAliasesResponse.model_validate(
            await self._get("/manage/alias/song")
        ).aliases

    async def get_event_aliases(self) -> list[EventAlias]:
        return EventAliasesResponse.model_validate(
            await self._get("/manage/alias/event")
        ).aliases

    async def add_song_alias(
        self, music_id: int, alias: str, region: Region | None = None
    ) -> AliasAddResponse:
        data = await self._request(
            "POST",
            "/manage/alias/song",
            json=AddSongAliasBody(
                music_id=music_id, alias=alias, region=region
            ).model_dump(),
            auth=True,
        )
        return AliasAddResponse.model_validate(data)

    async def remove_song_alias(self, alias_id: int) -> SuccessResponse:
        data = await self._request(
            "DELETE",
            "/manage/alias/song",
            json=RemoveAliasBody(alias_id=alias_id).model_dump(),
            auth=True,
        )
        return SuccessResponse.model_validate(data)

    async def add_event_alias(
        self, event_id: int, alias: str, region: Region | None = None
    ) -> AliasAddResponse:
        data = await self._request(
            "POST",
            "/manage/alias/event",
            json=AddEventAliasBody(
                event_id=event_id, alias=alias, region=region
            ).model_dump(),
            auth=True,
        )
        return AliasAddResponse.model_validate(data)

    async def remove_event_alias(self, alias_id: int) -> SuccessResponse:
        data = await self._request(
            "DELETE",
            "/manage/alias/event",
            json=RemoveAliasBody(alias_id=alias_id).model_dump(),
            auth=True,
        )
        return SuccessResponse.model_validate(data)


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


async def _detail(resp: aiohttp.ClientResponse) -> Any:
    """The raw `detail` — a code string, or a dict for structured errors."""
    try:
        body = await resp.json()
        if isinstance(body, dict):
            return body.get("detail", body)
        return body
    except Exception:
        return ""
