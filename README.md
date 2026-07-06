# sbuga-bot

A Discord-only Project Sekai bot. All PJSK data comes from **sbuga.com's public API**.

## Setup

1. `pip install -r requirements.txt`
   - On Python 3.13+ this builds `mojimoji` from source (no upstream wheels yet),
     which needs a C++ compiler (`build-essential` / MSVC Build Tools).
2. Copy `config.example.yml` → `config.yml` and fill in the Discord token, owner IDs,
   sbuga API URL/token, and Postgres connection.
3. Create the schema: `python -m scripts.database_setup`
4. Upload app emojis: `python -m scripts.upload_emojis` (uploads `data/assets/emojis/*`,
   exports ids to `data/emojis.json`; re-run whenever new emoji images are added)
5. Download unidic data `python -m unidic download`
6. Run: `python main.py`

### Ubuntu (venv) 3.14+
bcz mojimoji
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev build-essential git

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m unidic download

python -m scripts.database_setup
python -m scripts.upload_emojis
python main.py
```