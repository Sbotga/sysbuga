"""Entry point for the standalone activity server (separate from the bot).

Runs webserver/activity_app.py under uvicorn with the worker count taken from
config (api.workers, default 2). Each worker holds its own PJSK data copy and
shares round/spectate state through Redis.

    python run_activity.py
"""

import uvicorn

from helpers.config_loader import get_config, set_config_path


def main() -> None:
    set_config_path("config.yml")
    api = get_config().get("api", {})
    uvicorn.run(
        "webserver.activity_app:app",
        host=api.get("host", "0.0.0.0"),
        port=api.get("port", 8039),
        workers=int(api.get("workers", 2)),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
