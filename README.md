
# Day/Night Radio Discord Bot (ZIP)

A Discord bot that plays music continuously and switches playlists **by real-world time**:

- **Day playlist:** 6:00 AM → 5:59 PM (America/New_York)
- **Night playlist:** 6:00 PM → 5:59 AM
- When the time flips, the bot **finishes the current song** and switches after it ends.
- Playlists are always **shuffled** (no fixed order).
- `/radio` posts a “radio panel” embed with buttons:
  - **Request from playlists** (search by title substring; pick from up to 25 matches)
  - **Request YouTube link** (queues your link to play next)
  - **Vote Skip**:
    - If you’re alone in VC → instant skip
    - If more than one person is in VC → **everyone** must vote to skip

## Setup

### 1) Install requirements

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -U -r requirements.txt
```

### 2) Install FFmpeg

- Windows: download FFmpeg, add `ffmpeg.exe` to PATH
- macOS: `brew install ffmpeg`
- Linux: `sudo apt-get install ffmpeg`

### 3) Add your bot token

Copy the config:

```bash
cp config.example.json config.json
```

Edit `config.json` and paste your bot token.

### 4) Add songs to playlists

Edit:

- `data/day_playlist.json`
- `data/night_playlist.json`

Format is:

```json
[
  {"title": "Song Name", "url": "https://www.youtube.com/watch?v=..."},
  {"title": "Another Song", "url": "https://..."}
]
```

> Note: If you put a direct stream URL, it’ll still try to play it. YouTube links work best when `yt-dlp` is installed.

### 5) Run

```bash
python bot.py
```

Invite the bot with **bot + applications.commands** scopes, and give it:
- View Channels
- Connect
- Speak

## Commands

- `/radio` — joins your VC, starts playback, posts the control panel
- `/nowplaying` — shows current track
- `/reload_playlists` — reloads JSON from disk (requires Manage Server)

## Notes / Caveats

- This uses `yt-dlp` for extracting audio from YouTube links. Make sure you’re using it in a way that respects the sites’ terms and your local laws.
- “High quality” in Discord voice is ultimately limited by Discord’s voice encoding and server settings, but this uses the best audio stream available.
