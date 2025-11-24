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
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
voice_client: discord.VoiceClient | None = None

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

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

async def ensure_voice(guild: discord.Guild):
    global voice_client
    vc = discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL_NAME)
    if vc is None:
        raise RuntimeError(f"Voice channel '{VOICE_CHANNEL_NAME}' not found")

    try:
        if guild.voice_client and guild.voice_client.is_connected():
            voice_client = guild.voice_client
            if voice_client.channel != vc:
                await voice_client.move_to(vc)
        else:
            voice_client = await vc.connect(timeout=30)
    except asyncio.TimeoutError:
        raise RuntimeError(
            "⚠️ Timed out connecting to the voice channel. "
            "This is usually a host/network issue."
        )

@bot.command(help="Test: make the bot join the Ayyappa Radio channel")
@is_music_controller()
async def join(ctx: commands.Context):
    guild = ctx.guild
    try:
        await ensure_voice(guild)
        await ctx.send("✅ Joined voice channel.")
    except RuntimeError as e:
        await ctx.send(str(e))

@bot.command(help="Test: make the bot leave the voice channel")
@is_music_controller()
async def leave(ctx: commands.Context):
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()
        await ctx.send("👋 Left voice channel.")
    else:
        await ctx.send("I'm not in a voice channel.")

bot.run(TOKEN)



