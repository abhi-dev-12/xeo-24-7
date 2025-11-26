#!/usr/bin/env python3
"""
XEO 24x7 Ayyappa Radio bot - production-ready for Render background worker.

Features:
- Play YouTube playlists (yt-dlp) with cookie support (base64 env var)
- Skip blocked / age-restricted videos gracefully
- Optional RADIO_URL stream mode (continuous streaming)
- Robust voice connect with backoff and handling of existing connections
- Role-protected music controller
- Simple queue + autoplay loop
"""

import os
import asyncio
import logging
import base64
import tempfile
from typing import Optional, List
import yt_dlp
import discord
from discord.ext import commands
from discord import FFmpegPCMAudio
# at top of bot.py imports
import os
import asyncio
from aiohttp import web

# health server
async def _handle_health(request):
    return web.Response(text="ok")

async def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.add_routes([web.get("/health", _handle_health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server listening on 0.0.0.0:%d /health", port)

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ayyappa_radio")

# ---------- Config via environment ----------
TOKEN = os.getenv("TOKEN")                      # required
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
VOICE_CHANNEL_ID = int(os.getenv("VC_ID")) if os.getenv("VC_ID") else None
TEXT_CHANNEL_ID = int(os.getenv("TC_ID")) if os.getenv("TC_ID") else None

MUSIC_CONTROLLER_ROLE = os.getenv("MUSIC_CONTROLLER_ROLE", "Music Controller")

YOUTUBE_PLAYLIST_URL = os.getenv("YT")         # playlist URL (optional)
HARIVARASANAM_URL = os.getenv("YT_2")          # optional single video for nightly play
RADIO_URL = os.getenv("RADIO_URL")             # optional direct stream URL

# yt-dlp cookies base64 (optional)
YTDLP_COOKIES_B64 = os.getenv("YTDLP_COOKIES_B64")  # paste base64 string into Render env var

# ffmpeg options
FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

# ---------- Prepare cookie file (if provided) ----------
COOKIES_PATH: Optional[str] = None
if YTDLP_COOKIES_B64:
    try:
        b = base64.b64decode(YTDLP_COOKIES_B64)
        tf = tempfile.NamedTemporaryFile(delete=False, prefix="ytdlp_cookies_", suffix=".txt")
        tf.write(b)
        tf.close()
        COOKIES_PATH = tf.name
        log.info("yt-dlp cookies written to %s", COOKIES_PATH)
    except Exception as e:
        log.exception("Failed to decode/write YTDLP_COOKIES_B64: %s", e)
        COOKIES_PATH = None

# ---------- Bot / intents ----------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="&", intents=intents, help_command=None)

# Shared state
song_queue: asyncio.Queue = asyncio.Queue()
voice_client: Optional[discord.VoiceClient] = None
player_task: Optional[asyncio.Task] = None
stop_radio_event = asyncio.Event()

# ---------- yt-dlp options factory ----------
def make_ydl_opts():
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        # Better extractor client for some yt-dlp SABR issues
        "extractor_args": {"youtube": {"player_client": "default"}},
        # Do not download - we stream
        "noplaylist": False,
        "skip_download": True,
    }
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    return opts

# ---------- Helper: safe extract ----------
from yt_dlp.utils import DownloadError, ExtractorError

def safe_extract_info(url: str):
    ydl_opts = make_ydl_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return info
        except (DownloadError, ExtractorError) as e:
            log.warning("yt-dlp blocked or failed to extract %s: %s", url, e)
            return None
        except Exception as e:
            log.exception("Unexpected yt-dlp error for %s: %s", url, e)
            return None

# ---------- Voice connection helper with backoff ----------
async def ensure_voice(guild: discord.Guild, target_channel_id: int, timeout: int = 30) -> discord.VoiceClient:
    global voice_client
    vc_target = guild.get_channel(target_channel_id)
    if vc_target is None or not isinstance(vc_target, discord.VoiceChannel):
        raise RuntimeError(f"Voice channel with ID {target_channel_id} not found in guild {guild.id}")

    existing = guild.voice_client

    if existing and existing.is_connected():
        # move if wrong channel
        if existing.channel.id != vc_target.id:
            try:
                await existing.move_to(vc_target)
            except Exception:
                pass
        voice_client = existing
        return existing

    # Clean up half-broken clients
    if existing and not existing.is_connected():
        try:
            await existing.disconnect(force=True)
        except Exception:
            pass

    delay = 1.0
    for attempt in range(6):
        try:
            vc = await vc_target.connect(timeout=timeout)
            voice_client = vc
            return vc
        except discord.ClientException as e:
            # Already connected somewhere else / race; try reusing
            existing = guild.voice_client
            if existing and existing.is_connected():
                voice_client = existing
                return existing
            log.warning("Voice client clientexception during connect: %s (attempt %d)", e, attempt)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
        except (asyncio.TimeoutError, discord.HTTPException) as e:
            log.warning("Voice connect attempt %d failed: %s; retrying in %.1fs", attempt, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
    raise RuntimeError("Failed to connect to voice after retries")

# ---------- Play radio stream (continuous) ----------
async def play_radio_stream(guild: discord.Guild):
    """Play RADIO_URL as continuous stream (no yt-dlp)."""
    if not RADIO_URL:
        raise RuntimeError("RADIO_URL not configured")

    vc = await ensure_voice(guild, VOICE_CHANNEL_ID)
    # stop current
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    source = FFmpegPCMAudio(RADIO_URL, before_options=FFMPEG_BEFORE_OPTS, options=FFMPEG_OPTS)
    def after(err):
        if err:
            log.exception("Radio stream playback error: %s", err)
    vc.play(source, after=after)
    log.info("Started radio stream: %s", RADIO_URL)

# ---------- Play a single url via ffmpeg (uses yt-dlp to find best direct url) ----------
async def play_url(guild: discord.Guild, url: str):
    vc = await ensure_voice(guild, VOICE_CHANNEL_ID)
    # Resolve source URL using yt-dlp
    info = safe_extract_info(url)
    if not info:
        log.warning("Could not extract %s — skipping", url)
        return False

    # If playlist entry, try to pick a video
    if "entries" in info and info["entries"]:
        # Flatten and pick first valid entry
        for entry in info["entries"]:
            if entry is None:
                continue
            if entry.get("url") or entry.get("formats"):
                info = entry
                break
        else:
            log.warning("No playable entries found in playlist %s", url)
            return False

    # Get direct audio url if available
    # yt-dlp may return 'url' or choose format
    if info.get("url") and info.get("protocol") != "m3u8_native":
        stream_url = info.get("url")
    else:
        # Fallback: build an ffmpeg input from yt-dlp using -f bestaudio
        # Use yt-dlp to return a direct format url if possible
        formats = info.get("formats") or []
        chosen = None
        for f in formats[::-1]:
            if f.get("acodec") != "none" and f.get("url"):
                chosen = f
                break
        if chosen:
            stream_url = chosen.get("url")
        else:
            log.warning("No usable format found for %s", url)
            return False

    # Stop current and play
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    source = FFmpegPCMAudio(stream_url, before_options=FFMPEG_BEFORE_OPTS, options=FFMPEG_OPTS)
    played = asyncio.get_event_loop().create_future()

    def _after(err):
        if err:
            log.exception("Playback error for %s: %s", url, err)
        if not played.done():
            played.set_result(True)

    vc.play(source, after=_after)
    log.info("Playing URL in guild %s: %s", guild.id, url)

    # Wait until playback ends
    try:
        await played
    except Exception:
        pass
    return True

# ---------- Main queue loop ----------
async def main_player_loop(guild: discord.Guild, text_channel: discord.TextChannel):
    """
    Continuously consumes song_queue and plays songs.
    If RADIO_URL is set and radio_mode flag is enabled, it plays the stream instead.
    """
    log.info("Player loop started for guild %s", guild.id)
    while True:
        try:
            # If radio mode active (RADIO_URL) and queue is empty — choose behavior you prefer.
            item = await song_queue.get()
            if item is None:
                # sentinel to stop
                break
            url = item
            log.info("Dequeued %s", url)
            ok = await play_url(guild, url)
            if not ok:
                log.warning("Failed to play %s — continuing", url)
            # small delay between tracks
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Error in main_player_loop")
            await asyncio.sleep(1)

# ---------- Bot events and commands ----------
@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    # Optionally auto-start radio on boot if desired:
    # await try_start_radio_on_boot()

@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong from {bot.user.name} ✅")

@bot.command()
async def join(ctx):
    """Join configured VC (anyone can tell bot to join)."""
    guild = ctx.guild
    try:
        await ensure_voice(guild, VOICE_CHANNEL_ID)
        await ctx.send("✅ Joined voice channel.")
    except Exception as e:
        await ctx.send(f"❌ Failed to join voice: `{e}`")

@bot.command()
@commands.has_role(MUSIC_CONTROLLER_ROLE)
async def start(ctx):
    """Start the 24x7 playlist player (load playlist and start loop)."""
    global player_task
    guild = ctx.guild
    text_ch = ctx.channel

    # Load playlist into queue (if provided)
    if YOUTUBE_PLAYLIST_URL:
        ydl_opts = make_ydl_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(YOUTUBE_PLAYLIST_URL, download=False)
            except Exception as e:
                await text_ch.send(f"❌ Failed to load playlist: `{e}`")
                return

        # Flatten and enqueue available entries
        entries = info.get("entries") if info and info.get("entries") else [info]
        count = 0
        for e in entries:
            if not e:
                continue
            # try to get video id or url
            url = e.get("webpage_url") or e.get("url")
            if not url:
                continue
            # optional: skip if duration > X
            await song_queue.put(url)
            count += 1

        await text_ch.send(f"📜 Loaded {count} Ayyappa songs into the queue.")

    # start player loop if not running
    if player_task is None or player_task.done():
        player_task = asyncio.create_task(main_player_loop(guild, ctx.channel))
        await text_ch.send("Starting Ayyappa 24x7 Malayalam Radio 🙏")
    else:
        await text_ch.send("Player already running.")

@bot.command()
@commands.has_role(MUSIC_CONTROLLER_ROLE)
async def radio(ctx):
    """Start continuous RADIO_URL stream (if configured)."""
    if not RADIO_URL:
        await ctx.send("❌ No RADIO_URL configured in environment.")
        return
    guild = ctx.guild
    try:
        await play_radio_stream(guild)
        await ctx.send("✅ Radio stream started.")
    except Exception as e:
        await ctx.send(f"❌ Failed to start radio stream: `{e}`")

@bot.command()
@commands.has_role(MUSIC_CONTROLLER_ROLE)
async def stop(ctx):
    """Stop player and disconnect."""
    global player_task
    guild = ctx.guild
    existing = guild.voice_client
    if existing and existing.is_connected():
        try:
            existing.stop()
            await existing.disconnect(force=True)
        except Exception:
            pass
    # stop player task
    if player_task and not player_task.done():
        player_task.cancel()
    # clear queue
    while not song_queue.empty():
        try:
            song_queue.get_nowait()
        except Exception:
            break
    await ctx.send("✅ Stopped radio and disconnected.")

@bot.command()
@commands.has_role(MUSIC_CONTROLLER_ROLE)
async def enqueue(ctx, *, query: str):
    """Add a YouTube URL (or query) to queue. Query must be a direct URL to play reliably."""
    if not query:
        await ctx.send("Usage: &enqueue <YouTube URL>")
        return
    # If user provided a search term instead of URL, you can add search via ytdlp -- but keep it simple:
    await song_queue.put(query)
    await ctx.send(f"✅ Enqueued: {query}")

@bot.command()
async def info(ctx):
    """Show simple status."""
    guild = ctx.guild
    vc = guild.voice_client
    qsize = song_queue.qsize()
    await ctx.send(f"Bot: {bot.user.name}\nVoice: {'connected' if vc and vc.is_connected() else 'disconnected'}\nQueue: {qsize}")

# Called when the gateway disconnects
@bot.event
async def on_disconnect():
    log.warning("Discord client disconnected from gateway.")

@bot.event
async def on_resumed():
    log.info("Discord session resumed.")

@bot.event
async def on_error(event, *args, **kwargs):
    log.exception("Exception in event %s — %s", event, args or kwargs)

@bot.event
async def on_connect():
    log.info("Discord client connected to gateway.")

@bot.event
async def on_shard_disconnect(shard_id, exception):
    log.warning("Shard %s disconnected: %s", shard_id, exception)

async def try_recover_voice(guild):
    """Attempt to rejoin voice and restart the player loop after a disconnect."""
    global player_task
    try:
        log.info("Attempting voice recovery for guild %s", guild.id)
        await ensure_voice(guild, VOICE_CHANNEL_ID)
        # restart player loop if not running and queue has items
        if (player_task is None or player_task.done()) and song_queue.qsize() > 0:
            player_task = asyncio.create_task(main_player_loop(guild, guild.get_channel(TEXT_CHANNEL_ID)))
            log.info("Restarted player loop after recovery.")
    except Exception as e:
        log.exception("Voice recovery failed: %s", e)

@bot.event
async def on_voice_state_update(member, before, after):
    # if bot was disconnected from voice, try to recover
    if member.id == bot.user.id:
        # bot's voice state changed
        guild = member.guild
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            log.warning("Bot voice_state update: bot not connected, scheduling recovery.")
            # schedule recovery after small delay to avoid thundering reconnections
            asyncio.create_task(_delayed_recover(guild, delay=2.0))

async def _delayed_recover(guild, delay=2.0):
    await asyncio.sleep(delay)
    await try_recover_voice(guild)

async def play_url_with_retries(guild, url, retries=2):
    for attempt in range(retries+1):
        try:
            ok = await play_url(guild, url)
            if ok:
                return True
        except Exception as e:
            log.exception("play_url attempt %d for %s failed: %s", attempt, url, e)
            await asyncio.sleep(1 + attempt*2)
    # failed after retries => requeue or skip based on policy
    log.warning("Failed to play %s after retries — skipping", url)
    return False


# ---------- Error handling for missing role perms ----------
@start.error
@radio.error
@stop.error
@enqueue.error
async def cmd_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send("🚫 You need the Music Controller role to use this command.")
    else:
        log.exception("Command error: %s", error)
        await ctx.send(f"❌ Command error: `{error}`")

# ---------- Run bot ----------
# just before bot.run(TOKEN)
async def _startup_tasks():
    # start health server
    asyncio.create_task(start_health_server())
    # any other startup tasks here

# schedule startup tasks in an asyncio loop callback
bot.loop.create_task(_startup_tasks())
bot.run(TOKEN)

