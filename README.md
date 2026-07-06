# sbuga-bot

A Discord-only Project Sekai bot. All PJSK data comes from **sbuga.com's public API**
(no direct game-API access). Music/event/card data is fetched once and cached locally;
fuzzy matching and Discord autocomplete run on the bot's own side.

## Setup

1. `pip install -r requirements.txt`
2. Copy `config.example.yml` → `config.yml` and fill in the Discord token, owner IDs,
   sbuga API URL/token, and Postgres connection.
3. Create the schema: `python -m scripts.database_setup`
4. Upload app emojis: `python -m scripts.upload_emojis` (uploads `data/assets/emojis/*`,
   exports ids to `data/emojis.json`; re-run whenever new emoji images are added)
5. (Optional) Migrate from an old Sbotga DB: `python -m scripts.migrate_from_sbotga`
6. Run: `python main.py`

## Layout

```
main.py                  bot entrypoint (loads every cog in cogs/)
config.example.yml       config template (copy to config.yml, gitignored)
services/                sbuga.com API client + typed request/response models
data/                    local PJSK cache, fuzzy search, 39s constants, pydantic models, assets
helpers/                 config, logging, embeds, views, autocompletes, converters, emojis, image gen
database/                asyncpg pool + cleaned user-data layer
scripts/                 schema setup + one-shot migrations
cache/                   generated JSON caches (gitignored)
```

## Pending backend work

`events`, `character`, and `gacha` are coded but stay dormant until sbuga gains a
generic masterdata passthrough route, and `/alias add|remove` needs a service-token
auth path. See [MISSING_SBUGA_ROUTES.md](MISSING_SBUGA_ROUTES.md). The data layer
tolerates the missing endpoints (logs a warning instead of crashing).

## Notes

- English-only (no translation layer). Discord-only (no Twitch, no bundled API server).
- `/b30` and `/progress` are intentionally absent — they required a full account transfer.
  `/summary` is kept (it only reads public-profile clear counts).
