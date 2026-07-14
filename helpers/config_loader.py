from typing import NotRequired, TypedDict

import yaml


class ConfigDiscord(TypedDict):
    name: str
    token: str
    client_secret: str
    client_id: NotRequired[str]
    tos_url: NotRequired[str]
    privacy_url: NotRequired[str]
    owner_ids: list[int]
    support_invite: str
    support_id: int
    alias_manager_role_ids: list[int]
    chart_clips: NotRequired[
        bool
    ]  # false skips chart-clip rendering/pre-gen (default true)


class ConfigSbuga(TypedDict):
    api_url: str
    asset_base_url: str
    bot_token: str
    image_type: str
    regions: list[str]
    refresh_interval: int


class ConfigAPI(TypedDict):
    enabled: bool
    host: str
    port: int
    url: str
    workers: NotRequired[int]


class ConfigRedis(TypedDict):
    host: str
    port: int
    db: int
    password: NotRequired[str]


class ConfigPSQL(TypedDict):
    host: str
    user: str
    database: str
    port: int
    password: str
    pool_min_size: int
    pool_max_size: int


class ConfigMigrate(TypedDict):
    old_dsn: str


class Config(TypedDict):
    discord: ConfigDiscord
    sbuga: ConfigSbuga
    api: NotRequired[ConfigAPI]
    redis: NotRequired[ConfigRedis]
    psql: ConfigPSQL
    migrate: NotRequired[ConfigMigrate]


_config: Config | None = None
_config_path: str = "config.yml"


def set_config_path(path: str) -> None:
    global _config_path, _config
    _config_path = path
    _config = None


def get_config() -> Config:
    global _config
    if _config is None:
        with open(_config_path, "r", encoding="utf-8") as f:
            _config = yaml.load(f, yaml.Loader)
    assert _config is not None
    return _config
