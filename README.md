# sbuga-bot

A Discord-only Project Sekai bot. All PJSK data comes from **sbuga.com's public API**.

## Setup

1. `pip install -r requirements.txt`
2. Copy `config.example.yml` → `config.yml` and fill in the Discord token, owner IDs,
   sbuga API URL/token, and Postgres connection.
3. Create the schema: `python -m scripts.database_setup`
4. Upload app emojis: `python -m scripts.upload_emojis` (uploads `data/assets/emojis/*`,
   exports ids to `data/emojis.json`; re-run whenever new emoji images are added)
5. Download unidic data `python -m unidic download`
6. Run: `python main.py`