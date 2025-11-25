import os
import asyncio
import datetime

import discord
from discord.ext import commands, tasks
from discord import FFmpegPCMAudio
import yt_dlp
import pytz
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError
from imageio_ffmpeg import get_ffmpeg_exe
import discord.opus

# Try to load Opus library explicitly
if not discord.opus.is_loaded():
    for name in ("libopus.so.0", "libopus.so", "opus"):
        try:
            discord.opus.load_opus(name)
            print(f"[opus] Loaded Opus library: {name}")
            break
        except OSError:
            continue
    if not discord.opus.is_loaded():
        print("[opus] WARNING: Could not load Opus library; voice will not work.")
# ================== CONFIG FROM ENV ====================

TOKEN = os.getenv("TOKEN")

GUILD_ID = int(os.getenv("GUILD_ID"))          # e.g. 8099...
VOICE_CHANNEL_ID = int(os.getenv("VC_ID"))     # voice channel ID
TEXT_CHANNEL_ID = int(os.getenv("TC_ID"))      # text channel ID

MUSIC_CONTROLLER_ROLE = "Music Controller"

YOUTUBE_PLAYLIST_URL = os.getenv("YT")         # main playlist
HARIVARASANAM_URL = os.getenv("YT_2")          # harivarasanam video

# Harivarasanam time (IST)
HARIVARASANAM_HOUR = 21
HARIVARASANAM_MINUTE = 30

IST = pytz.timezone("Asia/Kolkata")

# ================== BOT + STATE ========================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="&", intents=intents)

song_queue: list[str] = []
current_index: int = 0
is_playing_main_loop: bool = False
harivarasanam_playing: bool = False
voice_client: discord.VoiceClient | None = None


# ================== HELPERS ============================

def is_music_controller():
    async def predicate(ctx: commands.Context):
        # admins always allowed
        if ctx.author.guild_permissions.administrator:
            return True
        role = discord.utils.get(ctx.author.roles, name=MUSIC_CONTROLLER_ROLE)
        if role is None:
            await ctx.send("You need the **Music Controller** role to use this command.")
            return False
        return True
    return commands.check(predicate)


def ytdlp_source(url: str) -> FFmpegPCMAudio | None:
    """Return FFmpeg audio source for a given YouTube URL using yt-dlp, or None if blocked."""
    ytdlp_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "default_search": "auto",
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
            }
        },
    }
    with yt_dlp.YoutubeDL(ytdlp_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except (DownloadError, ExtractorError):
            # age-restricted / blocked / needs login
            return None

        audio_url = info["url"]
        return FFmpegPCMAudio(
            audio_url,
            executable=get_ffmpeg_exe(),   # 👈 use bundled ffmpeg
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
        )



async def load_playlist():
    """Load all video URLs from the YouTube playlist into song_queue, skipping blocked ones."""
    global song_queue, current_index

    loop = asyncio.get_running_loop()

    def _load():
        opts = {
            "extract_flat": True,
            "quiet": True,
            "skip_download": True,
            "yes_playlist": True,
            "ignoreerrors": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["default"],
                }
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(YOUTUBE_PLAYLIST_URL, download=False)
            entries = info.get("entries", []) or []
            urls: list[str] = []
            for entry in entries:
                if not entry:
                    continue
                vid = entry.get("id")
                if vid:
                    urls.append(f"https://www.youtube.com/watch?v={vid}")
            return urls

    urls = await loop.run_in_executor(None, _load)
    song_queue = urls
    current_index = 0


def get_ist_now():
    return datetime.datetime.now(IST)


async def ensure_voice(guild: discord.Guild):
    """Ensure the bot is connected to the configured voice channel."""
    global voice_client

    vc = guild.get_channel(VOICE_CHANNEL_ID)
    if vc is None or not isinstance(vc, discord.VoiceChannel):
        raise RuntimeError(f"❌ Voice channel with ID {VOICE_CHANNEL_ID} not found in this guild.")

    try:
        if guild.voice_client and guild.voice_client.is_connected():
            voice_client = guild.voice_client
            if voice_client.channel != vc:
                await voice_client.move_to(vc)
        else:
            # Discord voice can be flaky; discord.py will retry internally.
            voice_client = await vc.connect(timeout=30)
    except asyncio.TimeoutError:
        raise RuntimeError(
            "⚠️ Timed out connecting to the voice channel.\n"
            "This is usually a host/network issue."
        )
    except discord.Forbidden:
        raise RuntimeError(
            "🚫 I don't have permission to join that voice channel.\n"
            "Please give me **Connect** and **Speak** permissions there."
        )
    except discord.ClientException as e:
        # e.g. Already connected
        raise RuntimeError(f"❗ Voice client exception: {e}")
    except discord.HTTPException as e:
        raise RuntimeError(f"❗ Failed to connect to voice: `{e}`")


async def play_song(guild: discord.Guild, url: str):
    """Play a single song URL in the voice channel."""
    global voice_client

    await ensure_voice(guild)

    if voice_client and voice_client.is_playing():
        voice_client.stop()

    loop = asyncio.get_running_loop()

    def _make_source():
        return ytdlp_source(url)

    source = await loop.run_in_executor(None, _make_source)

    if source is None:
        # blocked / age-restricted video – skip to next
        text_ch = guild.get_channel(TEXT_CHANNEL_ID)
        if isinstance(text_ch, discord.TextChannel):
            await text_ch.send("⚠️ Skipping a blocked/age-restricted video (needs sign-in).")
        # behave as if the track ended, so we move to the next one
        await on_track_end(guild, None)
        return

    def after_callback(error: Exception | None):
        fut = asyncio.run_coroutine_threadsafe(on_track_end(guild, error), bot.loop)
        try:
            fut.result()
        except Exception:
            pass

    voice_client.play(source, after=after_callback)



async def on_track_end(guild: discord.Guild, error: Exception | None):
    """Called when a track ends, decides what to play next."""
    global current_index, song_queue, is_playing_main_loop, harivarasanam_playing

    if error:
        print(f"[on_track_end] error: {error}")

    if harivarasanam_playing:
        # resume main loop after harivarasanam
        harivarasanam_playing = False
        if is_playing_main_loop:
            await start_main_loop(guild)
        return

    if not is_playing_main_loop or not song_queue:
        return

    current_index = (current_index + 1) % len(song_queue)
    next_url = song_queue[current_index]
    await play_song(guild, next_url)


async def start_main_loop(guild: discord.Guild):
    """Start/continue playing main playlist in loop."""
    global is_playing_main_loop, song_queue, current_index
    is_playing_main_loop = True

    if not song_queue:
        await load_playlist()
        text_ch = guild.get_channel(TEXT_CHANNEL_ID)
        if isinstance(text_ch, discord.TextChannel):
            try:
                await text_ch.send(f"📜 Loaded {len(song_queue)} Ayyappa songs into the queue.")
            except discord.Forbidden:
                # No permission to send here – ignore and continue
                pass


    if not song_queue:
        return

    url = song_queue[current_index]
    await play_song(guild, url)


async def play_harivarasanam(guild: discord.Guild):
    """Play Harivarasanam track once, then resume main loop."""
    global harivarasanam_playing
    harivarasanam_playing = True
    await play_song(guild, HARIVARASANAM_URL)


def is_sabarimala_season(now: datetime.datetime) -> bool:
    # Example: Sabarimala season = Nov, Dec, Jan
    return now.month in (11, 12, 1)


# ================== EVENTS & TASKS =======================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    daily_tasks.start()
    update_status.start()


@tasks.loop(minutes=1)
async def daily_tasks():
    """Check every minute for Harivarasanam time."""
    now = get_ist_now()
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    # Harivarasanam daily
    if now.hour == HARIVARASANAM_HOUR and now.minute == HARIVARASANAM_MINUTE:
        text_ch = guild.get_channel(TEXT_CHANNEL_ID)
        if isinstance(text_ch, discord.TextChannel):
            await text_ch.send("🎵 Harivarasanam Vishwamohanam... 🙏")
        await play_harivarasanam(guild)


@tasks.loop(minutes=5)
async def update_status():
    """Update bot's status based on season (Sabarimala etc.)."""
    now = get_ist_now()
    if is_sabarimala_season(now):
        activity = discord.Game("Sabarimala Season – Swamiye Saranam Ayyappa")
    else:
        activity = discord.Game("Devotional Radio")
    await bot.change_presence(activity=activity)


# ================== COMMANDS =============================

@bot.command(help="Ping test")
async def ping(ctx: commands.Context):
    await ctx.send("Pong from XEO-24/7 ✅")


@bot.command(help="Join the configured voice channel")
@is_music_controller()
async def join(ctx: commands.Context):
    guild = ctx.guild
    try:
        await ensure_voice(guild)
        await ctx.send("✅ Joined voice channel.")
    except RuntimeError as e:
        await ctx.send(str(e))


@bot.command(help="Start the 24x7 Ayyappa Radio loop")
@is_music_controller()
async def start(ctx: commands.Context):
    guild = ctx.guild
    try:
        await ctx.send("Starting **Ayyappa 24x7 Malayalam Radio** 🙏")
        await start_main_loop(guild)
    except RuntimeError as e:
        await ctx.send(str(e))


@bot.command(help="Stop the radio and disconnect")
@is_music_controller()
async def stop(ctx: commands.Context):
    global is_playing_main_loop, voice_client
    is_playing_main_loop = False
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()
    voice_client = None
    await ctx.send("⏹ Stopped Ayyappa Radio and disconnected.")


@bot.command(help="Skip to next song")
@is_music_controller()
async def skip(ctx: commands.Context):
    if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
        ctx.guild.voice_client.stop()
        await ctx.send("⏭ Skipping to the next Ayyappa song...")
    else:
        await ctx.send("I'm not playing anything right now.")


@bot.command(help="Set volume (0–100)")
@is_music_controller()
async def volume(ctx: commands.Context, vol: int):
    if not (0 <= vol <= 100):
        await ctx.send("Volume must be between 0 and 100.")
        return
    vc = ctx.guild.voice_client
    if vc and vc.source:
        vc.source = discord.PCMVolumeTransformer(vc.source, volume=vol / 100)
        await ctx.send(f"🔊 Volume set to {vol}%")
    else:
        await ctx.send("I'm not playing anything right now.")


# ================== RUN BOT ==============================

bot.run(TOKEN)
