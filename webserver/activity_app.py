from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import Response
from starlette.types import Scope

from data.pjsk import PJSKData
from database.pool import close_pool, create_pool
from database.queries import UserData
from helpers.config_loader import Config, get_config, set_config_path
from services.sbuga import SbugaClient
from webserver import redis_state, spectate
from webserver.activity import router as activity_router

DISCORD_API = "https://discord.com/api/v10"


class TokenBody(BaseModel):
    code: str


# our own hand-edited files change on every deploy and aren't filename-hashed, so
# they must revalidate (no-cache) or a stale app.js gets served; everything else
# (the vendored SDK, images) is stable and can edge-cache normally.
_REVALIDATE = {"app.js", "style.css", "sbuga.js"}


class NoCacheStatic(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        content_type = response.headers.get("content-type", "")
        name = path.rsplit("/", 1)[-1]
        if content_type.startswith("text/html") or name in _REVALIDATE:
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response


def _client_id(config: Config) -> str:
    """OAuth2 client id. Configured value wins; otherwise derive it from the bot
    token (a bot's user id == its application id, and it is the token's first
    base64 segment)."""
    explicit = config["discord"].get("client_id")
    if explicit:
        return str(explicit)
    token = config["discord"]["token"]
    first = token.split(".", 1)[0]
    pad = "=" * (-len(first) % 4)
    return base64.b64decode(first + pad).decode("ascii")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    config = get_config()
    scfg = config["sbuga"]

    db = await create_pool()
    sbuga = SbugaClient(
        scfg["api_url"],
        image_type=scfg["image_type"],  # type: ignore[arg-type]
        alias_token=scfg["alias_token"],
    )
    pjsk = PJSKData(
        sbuga,
        scfg["regions"],
        refresh_interval=scfg.get("refresh_interval", 300),
        asset_base_url=scfg.get("asset_base_url", ""),
    )
    await pjsk.start()
    await redis_state.init_redis(dict(config.get("redis") or {}))
    await spectate.start_hub()

    app.state.config = config
    app.state.client_id = _client_id(config)
    app.state.db = db
    app.state.user_data = UserData(db)
    app.state.sbuga = sbuga
    app.state.pjsk = pjsk
    try:
        yield
    finally:
        await spectate.stop_hub()
        await redis_state.close_redis()
        await pjsk.stop()
        await sbuga.close()
        await close_pool()


async def _exchange_code(config: Config, client_id: str, code: str) -> dict[str, Any]:
    data = {
        "client_id": client_id,
        "client_secret": config["discord"]["client_secret"],
        "grant_type": "authorization_code",
        "code": code,
    }
    redirect_uri = config.get("api", {}).get("url", "")
    if redirect_uri:
        data["redirect_uri"] = redirect_uri
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DISCORD_API}/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise HTTPException(
                    status_code=400,
                    detail=payload.get("error_description", "token exchange failed"),
                )
        async with session.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="user lookup failed")
            payload["user"] = await resp.json()
    return payload


def create_app() -> FastAPI:
    set_config_path("config.yml")
    app = FastAPI(
        title="sbuga activity", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # activities load from https://<app_id>.discordsays.com
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(activity_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    async def api_config() -> dict[str, str]:
        return {
            "client_id": app.state.client_id,
            "name": app.state.config["discord"]["name"],
        }

    @app.post("/api/oauth/token")
    async def oauth_token(body: TokenBody) -> dict[str, str]:
        payload = await _exchange_code(app.state.config, app.state.client_id, body.code)
        user_id = int(payload["user"]["id"])
        await app.state.user_data.store_oauth_token(
            user_id,
            payload["access_token"],
            payload.get("refresh_token"),
            int(payload.get("expires_in", 0)),
            str(payload.get("scope", "")).split(),
        )
        return {"access_token": payload["access_token"]}

    font_dir = Path("data/assets/image_gen")
    font_files = {"rodinntlg_eb.otf", "rodinntlg_db.otf", "rodinntlg_m.otf"}

    @app.get("/fonts/{name}")
    async def font(name: str) -> FileResponse:
        if name not in font_files:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(font_dir / name, media_type="font/otf")

    static_dir = Path(__file__).parent / "static"

    # Fingerprint the two entry assets index.html references. A content change makes
    # a new filename, so the hashed files cache immutably (full CDN/browser caching)
    # while index.html stays no-cache and points at the current names — instant
    # deploys with no purging. Assets are read + hashed once at startup, so restart
    # the service after deploying. The combined hash is injected as the build id.
    entry_media = {"app.js": "text/javascript", "style.css": "text/css"}
    hashed_name: dict[str, str] = {}
    asset_body: dict[str, tuple[bytes, str]] = {}
    combined = hashlib.sha1()
    for original, media in entry_media.items():
        data = (static_dir / original).read_bytes()
        combined.update(data)
        stem, _, ext = original.rpartition(".")
        name = f"{stem}_{hashlib.sha1(data).hexdigest()[:8]}.{ext}"
        hashed_name[original] = name
        asset_body[name] = (data, media)
    build_id = combined.hexdigest()[:8]

    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    index_html = index_html.replace('src="app.js"', f'src="{hashed_name["app.js"]}"')
    index_html = index_html.replace(
        'href="style.css"', f'href="{hashed_name["style.css"]}"'
    )
    index_html = index_html.replace(
        "</head>", f'  <script>window.__BUILD="{build_id}";</script>\n</head>'
    )

    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    app.add_api_route("/", index, include_in_schema=False)
    app.add_api_route("/index.html", index, include_in_schema=False)

    def _asset_endpoint(data: bytes, media: str):
        async def endpoint() -> Response:
            return Response(
                content=data,
                media_type=media,
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )

        return endpoint

    for name, (data, media) in asset_body.items():
        app.add_api_route(
            f"/{name}", _asset_endpoint(data, media), include_in_schema=False
        )

    # everything else (the vendored SDK, images, sbuga.js) via static; sbuga.js is
    # imported by app.js under its real name and isn't fingerprinted, so it stays
    # no-cache (see NoCacheStatic), the rest edge-caches.
    app.mount("/", NoCacheStatic(directory=static_dir, html=True), name="static")
    return app


app = create_app()
