
import os
import json
import math
import random
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Deque, Set
from collections import deque
from zoneinfo import ZoneInfo
from datetime import datetime, time as dtime

import discord
from discord import app_commands
from discord.ext import commands, tasks

# External deps:
#   pip install -U discord.py yt-dlp
# FFmpeg must be installed and on PATH.

try:
    import yt_dlp
except Exception as e:
    yt_dlp = None

LOG = logging.getLogger("daynight_radio")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TZ = ZoneInfo("America/New_York")
DAY_START = dtime(hour=6, minute=0)   # 6:00 AM
NIGHT_START = dtime(hour=18, minute=0) # 6:00 PM

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DAY_PLAYLIST_PATH = os.path.join(DATA_DIR, "day_playlist.json")
NIGHT_PLAYLIST_PATH = os.path.join(DATA_DIR, "night_playlist.json")

YTDLP_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "auto",
}

FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-nostdin"
)
# Discord will encode to Opus anyway, but we keep it clean and stable.
FFMPEG_OPTS = (
    "-vn -loglevel warning "
)

@dataclass
class Track:
    title: str
    url: str
    requested_by: Optional[int] = None
    source: str = "playlist"  # playlist | youtube
    playlist: Optional[str] = None  # day | night | None

def load_playlist(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Expected: [{"title": "...", "url": "..."}, ...]
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and "url" in item:
            out.append({"title": str(item.get("title") or item["url"]), "url": str(item["url"])})
    return out

def current_mode(now: Optional[datetime]=None) -> str:
    now = now or datetime.now(TZ)
    t = now.time()
    # Day: 06:00 -> 17:59:59, Night: 18:00 -> 05:59:59
    if DAY_START <= t < NIGHT_START:
        return "day"
    return "night"

class Shuffler:
    """Iterates a list in random order without repeats; reshuffles when exhausted."""
    def __init__(self, items: List[Dict[str, str]]):
        self.items = list(items)
        self._bag: List[Dict[str, str]] = []
        self._refill()

    def set_items(self, items: List[Dict[str, str]]):
        self.items = list(items)
        self._refill()

    def _refill(self):
        self._bag = list(self.items)
        random.shuffle(self._bag)

    def next(self) -> Optional[Dict[str, str]]:
        if not self.items:
            return None
        if not self._bag:
            self._refill()
        return self._bag.pop()

class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.voice: Optional[discord.VoiceClient] = None

        self.day_list = load_playlist(DAY_PLAYLIST_PATH)
        self.night_list = load_playlist(NIGHT_PLAYLIST_PATH)
        self.day_shuffle = Shuffler(self.day_list)
        self.night_shuffle = Shuffler(self.night_list)

        self.mode: str = current_mode()
        self.pending_mode: Optional[str] = None  # switches after current track ends
        self.priority: Deque[Track] = deque()    # user requests play NEXT, FIFO
        self.current: Optional[Track] = None

        self.skip_votes: Set[int] = set()
        self.play_task: Optional[asyncio.Task] = None
        self.track_end = asyncio.Event()

    def reload_playlists(self):
        self.day_list = load_playlist(DAY_PLAYLIST_PATH)
        self.night_list = load_playlist(NIGHT_PLAYLIST_PATH)
        self.day_shuffle.set_items(self.day_list)
        self.night_shuffle.set_items(self.night_list)

    def get_channel_members(self) -> List[discord.Member]:
        if not self.voice or not self.voice.channel:
            return []
        members = [m for m in self.voice.channel.members if not m.bot]
        return members

    def required_votes(self) -> int:
        # User requested: if >1 person in VC, everyone must vote.
        members = self.get_channel_members()
        return len(members)

    def can_control(self, user_id: int) -> bool:
        return user_id in {m.id for m in self.get_channel_members()}

    def vote_skip(self, user_id: int) -> (bool, int, int):
        """Returns (skipped_now, votes, required)"""
        members = self.get_channel_members()
        if len(members) <= 1:
            # solo listener gets full control
            self.force_skip()
            return True, 1, 1
        if user_id not in {m.id for m in members}:
            return False, len(self.skip_votes), len(members)
        self.skip_votes.add(user_id)
        required = len(members)
        if len(self.skip_votes) >= required:
            self.force_skip()
            return True, len(self.skip_votes), required
        return False, len(self.skip_votes), required

    def reset_votes(self):
        self.skip_votes.clear()

    def request_next(self, track: Track):
        # play next: append to priority queue
        self.priority.append(track)

    def request_mode_switch(self, new_mode: str):
        if new_mode != self.mode:
            self.pending_mode = new_mode

    async def ensure_voice(self, channel: discord.VoiceChannel):
        if self.voice and self.voice.channel and self.voice.channel.id == channel.id:
            return
        if self.voice and self.voice.is_connected():
            await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect(self_deaf=True)

    def is_playing(self) -> bool:
        return self.voice is not None and self.voice.is_connected() and self.voice.is_playing()

    def force_skip(self):
        if self.voice and self.voice.is_playing():
            self.voice.stop()  # triggers after callback

    def _pick_playlist_track(self) -> Optional[Track]:
        self.reload_playlists()  # small file, keeps things updated if user edits JSON live
        if self.mode == "day":
            item = self.day_shuffle.next()
            if not item:
                return None
            return Track(title=item["title"], url=item["url"], source="playlist", playlist="day")
        else:
            item = self.night_shuffle.next()
            if not item:
                return None
            return Track(title=item["title"], url=item["url"], source="playlist", playlist="night")

    async def _resolve_audio(self, url: str) -> (str, str):
        """
        Returns (direct_url, title).
        For non-YouTube URLs you can still provide direct audio streams.
        For YouTube, uses yt-dlp to get the best audio URL and title.
        """
        # If yt-dlp missing, just return as-is.
        if yt_dlp is None:
            return url, url

        loop = asyncio.get_running_loop()

        def extract():
            with yt_dlp.YoutubeDL(YTDLP_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)
                # If it's a search result, it can be a dict with "entries"
                if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
                    info = info["entries"][0]
                direct = info.get("url") or url
                title = info.get("title") or url
                return direct, title

        return await loop.run_in_executor(None, extract)

    async def _play_track(self, track: Track):
        if not self.voice:
            return
        self.current = track
        self.reset_votes()

        try:
            direct_url, title = await self._resolve_audio(track.url)
            track.title = track.title or title

            audio = discord.FFmpegPCMAudio(
                direct_url,
                before_options=FFMPEG_BEFORE_OPTS,
                options=FFMPEG_OPTS,
            )
            source = discord.PCMVolumeTransformer(audio, volume=1.0)

            self.track_end.clear()

            def _after(err: Optional[Exception]):
                if err:
                    LOG.warning("Playback error: %s", err)
                # Let the loop continue regardless.
                try:
                    self.bot.loop.call_soon_threadsafe(self.track_end.set)
                except Exception:
                    pass

            self.voice.play(source, after=_after)
            LOG.info("[%s] Now playing: %s", self.guild_id, track.title)

            # Wait until track ends (or skip stops it)
            await self.track_end.wait()

        except Exception as e:
            LOG.exception("Failed to play track: %s", e)
            await asyncio.sleep(1)
        finally:
            self.current = None
            # If a mode switch is pending, apply it after the current track ends.
            if self.pending_mode and self.pending_mode != self.mode:
                LOG.info("[%s] Switching mode %s -> %s", self.guild_id, self.mode, self.pending_mode)
                self.mode = self.pending_mode
            self.pending_mode = None

    async def play_loop(self):
        if self.play_task and not self.play_task.done():
            return
        self.play_task = asyncio.create_task(self._loop())

    async def _loop(self):
        # Keep playing as long as we're connected.
        while self.voice and self.voice.is_connected():
            # Check time-based mode changes; only switch AFTER current track.
            desired = current_mode()
            if desired != self.mode:
                self.request_mode_switch(desired)

            next_track = None
            if self.priority:
                next_track = self.priority.popleft()
            else:
                next_track = self._pick_playlist_track()

            if next_track is None:
                # Nothing to play — wait and try again.
                await asyncio.sleep(5)
                continue

            await self._play_track(next_track)

            # Small pause to prevent tight loops on errors
            await asyncio.sleep(0.2)

class PlaylistSearchModal(discord.ui.Modal, title="Pick a song from the playlists"):
    query = discord.ui.TextInput(
        label="Search (part of the title)",
        placeholder="e.g. 'city pop' or 'lofi' or an artist name",
        required=True,
        max_length=80,
    )

    def __init__(self, player: GuildPlayer, requester: discord.Member):
        super().__init__(timeout=120)
        self.player = player
        self.requester = requester

    async def on_submit(self, interaction: discord.Interaction):
        q = str(self.query.value).strip().lower()
        self.player.reload_playlists()

        # Search across both playlists
        matches: List[Track] = []
        for pl_name, pl in (("day", self.player.day_list), ("night", self.player.night_list)):
            for item in pl:
                title = str(item.get("title") or item.get("url") or "")
                if q in title.lower():
                    matches.append(Track(title=title, url=item["url"], requested_by=self.requester.id, source="playlist", playlist=pl_name))

        if not matches:
            await interaction.response.send_message(
                f"No matches for **{discord.utils.escape_markdown(q)}** in day/night playlists.",
                ephemeral=True
            )
            return

        # Keep at most 25 for a Discord select
        matches = matches[:25]
        view = PlaylistPickView(self.player, self.requester, matches)
        await interaction.response.send_message("Pick a song:", view=view, ephemeral=True)

class PlaylistPickView(discord.ui.View):
    def __init__(self, player: GuildPlayer, requester: discord.Member, tracks: List[Track]):
        super().__init__(timeout=120)
        self.player = player
        self.requester = requester

        options = []
        for i, t in enumerate(tracks):
            desc = f"{t.playlist.upper()} playlist"
            options.append(discord.SelectOption(label=t.title[:100], description=desc[:100], value=str(i)))

        self.select = discord.ui.Select(placeholder="Choose a track…", min_values=1, max_values=1, options=options)
        self.select.callback = self.on_pick  # type: ignore
        self.tracks = tracks
        self.add_item(self.select)

    async def on_pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("This picker is for the person who opened it.", ephemeral=True)
            return
        idx = int(self.select.values[0])
        track = self.tracks[idx]
        self.player.request_next(track)
        await interaction.response.send_message(f"Queued **{discord.utils.escape_markdown(track.title)}** to play next ✅", ephemeral=True)

class YouTubeRequestModal(discord.ui.Modal, title="Request a YouTube track"):
    url = discord.ui.TextInput(
        label="YouTube link",
        placeholder="https://www.youtube.com/watch?v=…",
        required=True,
        max_length=200,
    )

    def __init__(self, player: GuildPlayer, requester: discord.Member):
        super().__init__(timeout=120)
        self.player = player
        self.requester = requester

    async def on_submit(self, interaction: discord.Interaction):
        link = str(self.url.value).strip()
        track = Track(title="YouTube request", url=link, requested_by=self.requester.id, source="youtube", playlist=None)
        self.player.request_next(track)
        await interaction.response.send_message("Queued your YouTube request to play next ✅", ephemeral=True)

class RadioView(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="Request from playlists", style=discord.ButtonStyle.primary, custom_id="radio:req_playlist")
    async def req_playlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Members only.", ephemeral=True)
            return
        if not self.player.voice or not self.player.voice.channel:
            await interaction.response.send_message("I'm not in a voice channel yet. Use /radio while you're in one.", ephemeral=True)
            return
        if not self.player.can_control(interaction.user.id):
            await interaction.response.send_message("You need to be in my voice channel to request songs.", ephemeral=True)
            return
        await interaction.response.send_modal(PlaylistSearchModal(self.player, interaction.user))

    @discord.ui.button(label="Request YouTube link", style=discord.ButtonStyle.secondary, custom_id="radio:req_youtube")
    async def req_youtube(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Members only.", ephemeral=True)
            return
        if not self.player.voice or not self.player.voice.channel:
            await interaction.response.send_message("I'm not in a voice channel yet. Use /radio while you're in one.", ephemeral=True)
            return
        if not self.player.can_control(interaction.user.id):
            await interaction.response.send_message("You need to be in my voice channel to request songs.", ephemeral=True)
            return
        await interaction.response.send_modal(YouTubeRequestModal(self.player, interaction.user))

    @discord.ui.button(label="Vote Skip", style=discord.ButtonStyle.danger, custom_id="radio:vote_skip")
    async def vote_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Members only.", ephemeral=True)
            return
        if not self.player.voice or not self.player.voice.channel:
            await interaction.response.send_message("I'm not in a voice channel yet. Use /radio while you're in one.", ephemeral=True)
            return
        if not self.player.can_control(interaction.user.id):
            await interaction.response.send_message("You need to be in my voice channel to vote.", ephemeral=True)
            return

        skipped, votes, required = self.player.vote_skip(interaction.user.id)
        if required <= 1 and skipped:
            await interaction.response.send_message("Skipped ✅ (solo listener)", ephemeral=True)
            return

        if skipped:
            await interaction.response.send_message(f"Skip passed ✅ ({votes}/{required})", ephemeral=True)
        else:
            await interaction.response.send_message(f"Skip vote counted ({votes}/{required}). Need **everyone** in VC to vote.", ephemeral=True)

class DayNightRadioBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = False
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

        self.players: Dict[int, GuildPlayer] = {}
        self.radio_view = None

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild.id)
        return self.players[guild.id]

    async def setup_hook(self):
        # Persistent views survive restarts (as long as custom_id matches).
        # We'll create per-guild view at runtime, but can also register a generic one.
        self.add_view(RadioView(GuildPlayer(self, 0)))  # dummy player for view registration
        await self.tree.sync()

bot = DayNightRadioBot()

def get_target_voice_channel(guild: discord.Guild, invoker: discord.Member) -> Optional[discord.VoiceChannel]:
    """
    Resolve which voice channel /radio should use.
    Priority:
      1) config.json -> voice_channel_id (fixed channel)
      2) invoker's current voice channel
    """
    cfg = read_config()
    configured_vc_id = cfg.get("voice_channel_id")

    if configured_vc_id is not None:
        try:
            vc_id = int(configured_vc_id)
        except (TypeError, ValueError):
            LOG.warning("Invalid voice_channel_id in config.json: %r", configured_vc_id)
            return None

        channel = guild.get_channel(vc_id)
        if isinstance(channel, discord.VoiceChannel):
            return channel
        LOG.warning("Configured voice_channel_id %s was not found as a voice channel in guild %s", vc_id, guild.id)
        return None

    if invoker.voice and isinstance(invoker.voice.channel, discord.VoiceChannel):
        return invoker.voice.channel
    return None

@bot.tree.command(name="radio", description="Start the day/night radio and post the control panel.")
async def radio_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Members only.", ephemeral=True)
        return

    # Voice connection can exceed Discord's 3s initial response window.
    await interaction.response.defer(thinking=True)

    player = bot.get_player(interaction.guild)

    channel = get_target_voice_channel(interaction.guild, interaction.user)
    if channel is None:
        await interaction.followup.send(
            "Couldn't resolve a voice channel. Set `voice_channel_id` in config.json or join a voice channel first.",
            ephemeral=True,
        )
        return

    try:
        await player.ensure_voice(channel)
    except Exception as exc:
        LOG.exception("Failed to connect to voice channel %s in guild %s", channel.id, interaction.guild.id)
        await interaction.followup.send(
            "I couldn't join that voice channel. Please verify I have **Connect/Speak** permissions and that voice dependencies are installed (`pip install -U PyNaCl`).",
            ephemeral=True,
        )
        return

    await player.play_loop()

    # Real-time mode
    player.mode = current_mode()

    embed = discord.Embed(
        title="📻 All Night Radio",
        description=(
            "This bot runs on real time (America/New_York).\n"
            f"**Current mode:** `{player.mode.upper()}`  "
            f"• switches at **6:00 AM** and **6:00 PM** (finishes the current song first)\n\n"
            "Use the buttons below to request a track or vote-skip."
        ),
        color=discord.Color.blurple()
    )
    view = RadioView(player)

    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="nowplaying", description="Show the current track.")
async def nowplaying_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.get_player(interaction.guild)
    if player.current:
        t = player.current
        who = f"<@{t.requested_by}>" if t.requested_by else "playlist"
        await interaction.response.send_message(
            f"Now playing: **{discord.utils.escape_markdown(t.title)}**  _(from {t.source}, requested by {who})_",
            ephemeral=True
        )
    else:
        await interaction.response.send_message("Nothing playing right now.", ephemeral=True)

@bot.tree.command(name="reload_playlists", description="Reload day/night playlists from disk (admin).")
@app_commands.checks.has_permissions(manage_guild=True)
async def reload_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.get_player(interaction.guild)
    player.reload_playlists()
    await interaction.response.send_message("Reloaded playlists ✅", ephemeral=True)

@reload_cmd.error
async def reload_cmd_error(interaction: discord.Interaction, error):
    await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)

def read_config():
    path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(path):
        raise FileNotFoundError("Missing config.json. Copy config.example.json -> config.json and fill in your token.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_token(raw_token: object) -> str:
    token = str(raw_token).strip()

    # Common copy/paste issues from hosting panels, markdown, and docs.
    if token.lower().startswith("bot "):
        token = token[4:].strip()

    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'", "`"}:
        token = token[1:-1].strip()

    return token


def validate_token(token: str) -> None:
    placeholder_markers = {
        "paste_your_discord_bot_token_here",
        "your_bot_token_here",
        "replace_me",
        "changeme",
    }

    lowered = token.lower()
    if lowered in placeholder_markers:
        raise ValueError(
            "config.json still contains a placeholder token. Paste the real bot token from "
            "Discord Developer Portal > Bot > Token."
        )

    # Discord bot tokens are usually 50+ chars and include two dots.
    if len(token) < 50 or token.count(".") != 2:
        raise ValueError(
            "Token format in config.json looks invalid. Paste only the raw bot token (three "
            "dot-separated parts, no 'Bot ' prefix, no extra quotes, no spaces/newlines)."
        )

def main():
    cfg = read_config()
    token = cfg.get("token")
    if not token:
        raise ValueError("config.json is missing 'token'.")

    token = normalize_token(token)
    validate_token(token)

    try:
        bot.run(token)
    except discord.errors.LoginFailure as exc:
        raise ValueError(
            "Discord rejected the bot token in config.json. Paste only the raw bot token "
            "(no 'Bot ' prefix, no extra quotes, and no spaces/newlines)."
        ) from exc

if __name__ == "__main__":
    main()
