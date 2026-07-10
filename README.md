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
6. Install required libraries (Linux and Windows need FFMPEG! install in path (or put into libraries as ffmpeg.exe for Windows))
7. Run: `python main.py`

### Ubuntu (venv) 3.14+
bcz mojimoji and libraries
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev build-essential git

# chart-preview renderer: needs ffmpeg, a virtual X display, and Mesa's software GL.
# (GLFW dlopens the X11/GL libs, so they don't show up in `ldd`.)
sudo apt install -y ffmpeg xvfb libgl1 libglx-mesa0 libgl1-mesa-dri libx11-6 libxext6 libxrandr2 libxinerama1 libxcursor1 libxi6
chmod +x libraries/nxsk-chart-preview

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m unidic download

python -m scripts.database_setup
python -m scripts.upload_emojis
python main.py
```