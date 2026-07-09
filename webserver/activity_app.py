from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
