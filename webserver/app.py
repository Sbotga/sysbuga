from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pathlib import Path

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from webserver.activity import router as activity_router

if TYPE_CHECKING:
    from main import SbugaBot

DISCORD_API = "https://discord.com/api/v10"

_server: uvicorn.Server | None = None


class TokenBody(BaseModel):
    code: str


async def _exchange_code(bot: SbugaBot, code: str) -> dict[str, Any]:
    assert bot.user is not None
    data = {
        "client_id": str(bot.user.id),
        "client_secret": bot.config["discord"]["client_secret"],
        "grant_type": "authorization_code",
        "code": code,
    }
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
            user = await resp.json()
    payload["user"] = user
    return payload


def create_app(bot: SbugaBot) -> FastAPI:
    app = FastAPI(title="sbuga-bot api", docs_url=None, redoc_url=None)
    app.state.bot = bot
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
            "client_id": str(bot.user.id) if bot.user else "",
            "name": bot.config["discord"]["name"],
        }

    @app.post("/api/oauth/token")
    async def oauth_token(body: TokenBody) -> dict[str, str]:
        payload = await _exchange_code(bot, body.code)
        user_id = int(payload["user"]["id"])
        await bot.user_data.store_oauth_token(  # type: ignore[union-attr]
            user_id,
            payload["access_token"],
            payload.get("refresh_token"),
            int(payload.get("expires_in", 0)),
            str(payload.get("scope", "")).split(),
        )
        bot.info(f"[API] OAuth token stored for {user_id}")
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


async def start_webserver(bot: SbugaBot) -> None:
    global _server
    api_cfg = bot.config.get("api")
    if not api_cfg or not api_cfg.get("enabled"):
        return
    config = uvicorn.Config(
        create_app(bot),
        host=api_cfg.get("host", "0.0.0.0"),
        port=api_cfg.get("port", 8039),
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    bot.loop.create_task(_server.serve())
    bot.info(f"[API] serving on {config.host}:{config.port}")


async def stop_webserver() -> None:
    global _server
    if _server is not None:
        _server.should_exit = True
        _server = None
