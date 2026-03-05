
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

Use only the raw token string from the Discord Developer Portal. Do **not** include a `Bot ` prefix or extra quotes.

Optional: set `voice_channel_id` to force `/radio` to always join a specific VC.
If omitted, it joins the user who ran `/radio`.

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

If startup fails with `Improper token has been passed`, re-check `config.json`:

- Ensure the value is your **current** bot token (regenerate it in Developer Portal if needed).
- Remove any surrounding quotes, backticks, or `Bot ` prefix.
- Make sure there are no extra spaces/newlines before or after the token.
- Confirm the token has the normal Discord format: three dot-separated parts.

Invite the bot with **bot + applications.commands** scopes, and give it:
- View Channels
- Connect
- Speak

## Commands

- `/radio` — starts playback + control panel and joins configured `voice_channel_id` (or your VC if not configured)
- `/nowplaying` — shows current track
- `/reload_playlists` — reloads JSON from disk (requires Manage Server)

## Notes / Caveats

- This uses `yt-dlp` for extracting audio from YouTube links. Make sure you’re using it in a way that respects the sites’ terms and your local laws.
- “High quality” in Discord voice is ultimately limited by Discord’s voice encoding and server settings, but this uses the best audio stream available.
