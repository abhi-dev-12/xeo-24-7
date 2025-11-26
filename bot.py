#!/usr/bin/env python3
"""
XEO 24x7 Ayyappa Radio - Render-ready with:
- health HTTP server (aiohttp) to satisfy host probing / debugging
- robust voice connect with backoff & recovery
- yt-dlp cookie support (base64 env var)
- safe extraction and skip of blocked videos
- playback retries and auto-restart of player loop
"""

import os
import asyncio
import logging
import base64
import tempfile
import time
from typing import Optional

import yt_dlp
import discord
from discord.ext import commands
from discord import FFmpegPCMAudio

# aiohttp for health server
from aiohttp import web

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ayyappa_radio")

# ---------- Config via environment ----------
TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
VOICE_CHANNEL_ID = int(os.getenv("VC_ID")) if os.getenv("VC_ID") else None
TEXT_CHANNEL_ID = int(os.getenv("TC_ID")) if os.getenv("TC_ID") else None

MUSIC_CONTROLLER_ROLE = os.getenv("MUSIC_CONTROLLER_ROLE", "Music Controller")

YOUTUBE_PLAYLIST_URL = os.getenv("YT")
HARIVARASANAM_URL = os.getenv("YT_2")
RADIO_URL = os.getenv("RADIO_URL")

YTDLP_COOKIES_B64 = os.getenv("YTDLP_COOKIES_B64")  # optional base64 cookies

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

# ---------- yt-dlp opts ----------
def make_ydl_opts():
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": "default"}},
        "noplaylist": False,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 2,
    }
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    return opts

from yt_dlp.utils import DownloadError, ExtractorError

def safe_extract_info(url: str):
    ydl_opts = make_ydl_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return info
        except (DownloadError, ExtractorError) as e:
            log.warning("yt-dlp blocked/failed to extract %s: %s", url, e)
            return None
        except Exception as e:
            log.exception("Unexpected yt-dlp error for %s: %s", url, e)
            return None

# ---------- Health server (aiohttp) ----------
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

# ---------- Voice connection helper with backoff ----------
async def ensure_voice(guild: discord.Guild, target_channel_id: int, timeout: int = 30) -> discord.VoiceClient:
    global voice_client
    vc_target = guild.get_channel(target_channel_id)
    if vc_target is None or not isinstance(vc_target, discord.VoiceChannel):
        raise RuntimeError(f"Voice channel with ID {target_channel_id} not found")

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
            log.info("Connected to voice channel %s (attempt %d)", target_channel_id, attempt + 1)
            return vc
        except discord.ClientException as e:
            existing = guild.voice_client
            if existing and existing.is_connected():
                voice_client = existing
                return existing
            log.warning("Voice ClientException on connect attempt %d: %s", attempt + 1, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
        except (asyncio.TimeoutError, discord.HTTPException) as e:
            log.warning("Voice connect attempt %d failed: %s; sleeping %.1f", attempt + 1, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
    raise RuntimeError("Failed to connect to voice after retries")

# ---------- Play radio stream ----------
async def play_radio_stream(guild: discord.Guild):
    if not RADIO_URL:
        raise RuntimeError("RADIO_URL not configured")
    vc = await ensure_voice(guild, VOICE_CHANNEL_ID)
    # Stop current
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    source = FFmpegPCMAudio(RADIO_URL, before_options=FFMPEG_BEFORE_OPTS, options=FFMPEG_OPTS)
    def after(err):
        if err:
            log.exception("Radio stream playback error: %s", err)
    vc.play(source, after=after)
    log.info("Started radio stream: %s", RADIO_URL)

# ---------- Play a single URL via yt-dlp resolved stream_url ----------
async def play_url(guild: discord.Guild, url: str):
    vc = await ensure_voice(guild, VOICE_CHANNEL_ID)
    info = safe_extract_info(url)
    if not info:
        log.warning("Could not extract %s — skipping", url)
        return False

    # If playlist, pick first playable entry
    if "entries" in info and info["entries"]:
        found = None
        for e in info["entries"]:
            if not e:
                continue
            if e.get("url") or e.get("formats") or e.get("webpage_url"):
                found = e
                break
        if found:
            info = found
        else:
            log.warning("No playable entries in playlist %s", url)
            return False

    # Prefer direct url if provided
    stream_url = None
    if info.get("url") and info.get("protocol") != "m3u8_native":
        stream_url = info.get("url")
    else:
        formats = info.get("formats") or []
        chosen = None
        for f in reversed(formats):
            if f.get("acodec") and f.get("url"):
                chosen = f
                break
        if chosen:
            stream_url = chosen.get("url")

    if not stream_url:
        log.warning("No usable stream URL for %s", url)
        return False

    # stop current
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    fut = asyncio.get_event_loop().create_future()
    def _after(err):
        if err:
            log.exception("Playback error for %s: %s", url, err)
        if not fut.done():
            fut.set_result(True)

    source = FFmpegPCMAudio(stream_url, before_options=FFMPEG_BEFORE_OPTS, options=FFMPEG_OPTS)
    vc.play(source, after=_after)
    log.info("Playing %s in guild %s", url, guild.id)
    try:
        await fut
    except Exception:
        pass
    return True

# ---------- Play with retries ----------
async def play_url_with_retries(guild: discord.Guild, url: str, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            ok = await play_url(guild, url)
            if ok:
                return True
            else:
                log.warning("play_url returned False for %s (attempt %d)", url, attempt + 1)
        except Exception as e:
            log.exception("play_url attempt %d for %s failed: %s", attempt + 1, url, e)
        await asyncio.sleep(1 + attempt * 2)
    log.warning("Failed to play %s after %d retries, skipping", url, retries)
    return False

# ---------- Player loop ----------
async def main_player_loop(guild: discord.Guild, text_channel: discord.TextChannel):
    log.info("Player loop started for guild %s", guild.id)
    while True:
        try:
            item = await song_queue.get()
            if item is None:
                break
            url = item
            log.info("Dequeued %s", url)
            await play_url_with_retries(guild, url, retries=2)
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Error in main_player_loop")
            await asyncio.sleep(1)

# ---------- Recovery helpers ----------
async def try_recover_voice(guild: discord.Guild):
    global player_task
    try:
        log.info("Attempting voice recovery for guild %s", guild.id)
        await ensure_voice(guild, VOICE_CHANNEL_ID)
        if (player_task is None or player_task.done()) and song_queue.qsize() > 0:
            player_task = asyncio.create_task(main_player_loop(guild, guild.get_channel(TEXT_CHANNEL_ID)))
            log.info("Restarted player loop after recovery.")
    except Exception as e:
        log.exception("Voice recovery failed: %s", e)

async def _delayed_recover(guild, delay=2.0):
    await asyncio.sleep(delay)
    await try_recover_voice(guild)

# ---------- Events ----------
@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

@bot.event
async def on_connect():
    log.info("Gateway connected")

@bot.event
async def on_disconnect():
    log.warning("Gateway disconnected")

@bot.event
async def on_resumed():
    log.info("Gateway resumed")

@bot.event
async def on_error(event, *args, **kwargs):
    log.exception("Exception in event %s: %s %s", event, args, kwargs)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id:
        guild = member.guild
        vc = guild.voice_client
        # If bot became disconnected, schedule recovery
        if vc is None or not vc.is_connected():
            log.warning("Bot voice state indicates not connected; scheduling recovery")
            asyncio.create_task(_delayed_recover(guild, delay=2.0))

# ---------- Commands ----------
@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong from {bot.user.name} ✅")

@bot.command()
async def join(ctx):
    guild = ctx.guild
    try:
        await ensure_voice(guild, VOICE_CHANNEL_ID)
        await ctx.send("✅ Joined voice channel.")
    except Exception as e:
        await ctx.send(f"❌ Failed to join voice: `{e}`")

@bot.command()
@commands.has_role(MUSIC_CONTROLLER_ROLE)
async def start(ctx):
    global player_task
    guild = ctx.guild
    text_ch = ctx.channel

    # load playlist
    if YOUTUBE_PLAYLIST_URL:
        ydl_opts = make_ydl_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(YOUTUBE_PLAYLIST_URL, download=False)
            except Exception as e:
                await text_ch.send(f"❌ Failed to load playlist: `{e}`")
                return

        entries = info.get("entries") if info and info.get("entries") else [info]
        count = 0
        for e in entries:
            if not e:
                continue
            url = e.get("webpage_url") or e.get("url")
            if not url:
                continue
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
    if not RADIO_URL:
        await ctx.send("❌ No RADIO_URL configured.")
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
    global player_task
    guild = ctx.guild
    existing = guild.voice_client
    if existing and existing.is_connected():
        try:
            existing.stop()
            await existing.disconnect(force=True)
        except Exception:
            pass
    if player_task and not player_task.done():
        player_task.cancel()
    while not song_queue.empty():
        try:
            song_queue.get_nowait()
        except Exception:
            break
    await ctx.send("✅ Stopped radio and disconnected.")

@bot.command()
@commands.has_role(MUSIC_CONTROLLER_ROLE)
async def enqueue(ctx, *, query: str):
    if not query:
        await ctx.send("Usage: &enqueue <YouTube URL>")
        return
    await song_queue.put(query)
    await ctx.send(f"✅ Enqueued: {query}")

@bot.command()
async def info(ctx):
    guild = ctx.guild
    vc = guild.voice_client
    qsize = song_queue.qsize()
    await ctx.send(f"Bot: {bot.user.name}\nVoice: {'connected' if vc and vc.is_connected() else 'disconnected'}\nQueue: {qsize}")

# command error handler
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

# ---------- Startup tasks (health server, optional auto-start) ----------
async def _startup_tasks():
    # start health server (bind to PORT if set)
    try:
        asyncio.create_task(start_health_server())
    except Exception:
        log.exception("Failed to start health server")
    # optionally auto-join and auto-start if you want immediate start on boot.
    # Uncomment if desired (be careful - ensure envs exist)
    # await auto_join_and_start()

import asyncio

async def main():
    # sanity check
    if not TOKEN:
        log.error("TOKEN not provided in environment. Exiting.")
        raise SystemExit(1)

    # start background tasks (health server etc.)
    # schedule _startup_tasks() which itself creates tasks (like the health server)
    try:
        # run your startup tasks (they create background tasks internally)
        # call the coroutine so internal create_task calls run on the same loop
        await _startup_tasks()
    except Exception:
        log.exception("Startup tasks failed (continuing to attempt bot start)")

    # start the bot (this blocks until the bot stops)
    try:
        log.info("Starting bot...")
        await bot.start(TOKEN)
    except asyncio.CancelledError:
        # normal shutdown flow
        raise
    except Exception:
        log.exception("Bot terminated with exception")
    finally:
        # Attempt clean shutdown
        try:
            await bot.close()
        except Exception:
            pass

if __name__ == "__main__":
    # Use asyncio.run to create and run the event loop (recommended)
    asyncio.run(main())
