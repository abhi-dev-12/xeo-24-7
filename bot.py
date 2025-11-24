import os
import discord
from discord.ext import commands, tasks
from discord import FFmpegPCMAudio
import asyncio
import datetime
import pytz
import yt_dlp

# ================== CONFIG – EDIT ONLY THIS PART ====================

TOKEN = os.getenv("TOKEN")  
# ================== CONFIG – EDIT THESE ====================



GUILD_ID = int(os.getenv("GUILD_ID"))     # your server ID (right-click server icon → Copy ID)
VOICE_CHANNEL_NAME = os.getenv("VC")   # exact voice channel name
TEXT_CHANNEL_NAME = os.getenv("TC")   # exact text channel for messages

MUSIC_CONTROLLER_ROLE = "Music Controller🎶"

# Your main 24x7 playlist (Malayalam Ayyappan songs)
YOUTUBE_PLAYLIST_URL = os.getenv("YT")

# Single Harivarasanam video URL (for nightly play if you want)
HARIVARASANAM_URL = os.getenv("YT_2")

# Harivarasanam time (IST)
HARIVARASANAM_HOUR = 21   # 9 PM
HARIVARASANAM_MINUTE = 30 # 9:30 PM

IST = pytz.timezone("Asia/Kolkata")

# ================== BOT SETUP ==============================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# music state
song_queue = []
current_index = 0
is_playing_main_loop = False
harivarasanam_playing = False
voice_client: discord.VoiceClient | None = None

# ============ HELPERS ======================================

def is_music_controller():
    async def predicate(ctx: commands.Context):
        if ctx.author.guild_permissions.administrator:
            return True
        role = discord.utils.get(ctx.author.roles, name=MUSIC_CONTROLLER_ROLE)
        if role is None:
            await ctx.send("You need the **Music Controller** role to use this command.")
            return False
        return True
    return commands.check(predicate)


def ytdlp_source(url: str):
    """Return FFmpeg audio source for a given YouTube URL using yt-dlp."""
    ytdlp_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "default_search": "auto",
        "extractor_args": {
            "youtube": {
                "player_client": ["default"]
            }
        },
    }
    with yt_dlp.YoutubeDL(ytdlp_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info["url"]
        return FFmpegPCMAudio(
            audio_url,
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
        )



def get_ist_now():
    return datetime.datetime.now(IST)


async def ensure_voice(guild: discord.Guild):
    """Ensure the bot is connected to the configured voice channel."""
    global voice_client
    vc = discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL_NAME)
    if vc is None:
        raise RuntimeError(f"Voice channel '{VOICE_CHANNEL_NAME}' not found")

    if guild.voice_client and guild.voice_client.is_connected():
        voice_client = guild.voice_client
        if voice_client.channel != vc:
            await voice_client.move_to(vc)
    else:
        voice_client = await vc.connect()


async def play_song(guild: discord.Guild, url: str):
    """Play a single song URL in the voice channel."""
    global voice_client

    await ensure_voice(guild)

    if voice_client.is_playing():
        voice_client.stop()

    source = ytdlp_source(url)
    voice_client.play(
        source,
        after=lambda e: asyncio.run_coroutine_threadsafe(on_track_end(guild), bot.loop)
    )


async def on_track_end(guild: discord.Guild):
    """Called when a track ends, decides what to play next."""
    global current_index, song_queue, is_playing_main_loop, harivarasanam_playing

    # If we just finished Harivarasanam, go back to main loop
    if harivarasanam_playing:
        harivarasanam_playing = False
        if is_playing_main_loop:
            await start_main_loop(guild)
        return

    if not is_playing_main_loop or not song_queue:
        return

    current_index = (current_index + 1) % len(song_queue)
    url = song_queue[current_index]
    await play_song(guild, url)


async def start_main_loop(guild: discord.Guild):
    """Start/continue playing main playlist in loop."""
    global is_playing_main_loop, song_queue, current_index
    is_playing_main_loop = True

    if not song_queue:
        # First time: load full playlist
        await load_playlist()

    if not song_queue:
        return

    url = song_queue[current_index]
    await play_song(guild, url)


async def load_playlist():
    """Load all video URLs from the YouTube playlist into song_queue, skipping blocked ones."""
    global song_queue, current_index

    ytdlp_opts = {
        "extract_flat": True,
        "quiet": True,
        "skip_download": True,
        "yes_playlist": True,
        "ignoreerrors": True,  # <-- skip entries that error (age-restricted, etc.)
        "extractor_args": {    # <-- avoid JS runtime issues on Railway
            "youtube": {
                "player_client": ["default"]
            }
        },
    }

    with yt_dlp.YoutubeDL(ytdlp_opts) as ydl:
        info = ydl.extract_info(YOUTUBE_PLAYLIST_URL, download=False)
        entries = info.get("entries", []) or []
        urls = []

        for entry in entries:
            if not entry:
                continue  # failed / blocked entry
            video_id = entry.get("id")
            if video_id:
                urls.append(f"https://www.youtube.com/watch?v={video_id}")

    song_queue = urls
    current_index = 0



async def play_harivarasanam(guild: discord.Guild):
    """Play Harivarasanam track once, then resume main loop."""
    global harivarasanam_playing
    harivarasanam_playing = True
    await play_song(guild, HARIVARASANAM_URL)


def is_sabarimala_season(now: datetime.datetime) -> bool:
    # Simple example: Sabarimala season = November, December, January
    return now.month in (11, 12, 1)

# ================ EVENTS & TASKS ===========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    daily_tasks.start()
    update_status.start()


@bot.event
async def on_voice_state_update(member, before, after):
    """Send Swamiye Saranam Ayyappa when someone joins the radio channel."""
    if member.bot:
        return

    guild = member.guild
    vc = discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL_NAME)
    if vc is None:
        return

    # User joined Ayyappa Radio
    if after.channel == vc and before.channel != vc:
        text_ch = discord.utils.get(guild.text_channels, name=TEXT_CHANNEL_NAME)
        if text_ch:
            await text_ch.send(f"🙏 Swamiye Saranam Ayyappa, {member.mention}!")


@tasks.loop(minutes=1)
async def daily_tasks():
    """Check every minute for Harivarasanam time."""
    now = get_ist_now()
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    # Harivarasanam
    if now.hour == HARIVARASANAM_HOUR and now.minute == HARIVARASANAM_MINUTE:
        text_ch = discord.utils.get(guild.text_channels, name=TEXT_CHANNEL_NAME)
        if text_ch:
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

# ================== COMMANDS ===============================

@bot.command(help="Start the 24x7 Ayyappa Radio loop")
@is_music_controller()
async def start(ctx: commands.Context):
    guild = ctx.guild
    await ctx.send("Starting **Ayyappa 24x7 Malayalam Radio** 🙏")
    await start_main_loop(guild)


@bot.command(help="Stop the radio and disconnect")
@is_music_controller()
async def stop(ctx: commands.Context):
    global is_playing_main_loop, voice_client
    is_playing_main_loop = False
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()
    voice_client = None
    await ctx.send("Stopped Ayyappa Radio and disconnected.")


@bot.command(help="Skip to next song")
@is_music_controller()
async def skip(ctx: commands.Context):
    if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
        ctx.guild.voice_client.stop()
        await ctx.send("⏭ Skipping to the next Ayyappa song...")


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


# ================== RUN BOT ================================

bot.run(TOKEN)



