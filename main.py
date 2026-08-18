import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import discord
from dotenv import load_dotenv
from ai_handler import AIHandler
import logging
import asyncio
from collections import deque

# Ensure unicode output works correctly on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

import time
import random
import glob
import re
import aiohttp
import io
import urllib
import urllib.parse
from datetime import timedelta, datetime, timezone
from keep_alive import keep_alive
import db
import bot_config
import games
from PIL import Image
from akinator import AsyncAkinator
import akinator

# Apply patch to akinator library's AsyncClient.__handler due to missing 'akitude' in modern Akinator API response
async def _patched_handler(self, response):
    response.raise_for_status()
    try:
        data = response.json()
    except Exception as e:
        if "A technical problem has ocurred." in response.text:
            raise RuntimeError("A technical problem has occurred. Please try again later.") from e
        raise RuntimeError("Failed to parse the response as JSON.") from e

    if "completion" not in data:
        data["completion"] = self.completion
    if data["completion"] == "KO - TIMEOUT":
        raise RuntimeError("The session has timed out. Please start a new game.")
    if data["completion"] == "SOUNDLIKE":
        self.finished = True
        self.win = True
        if not self.id_proposition:
            await self.defeat()
    elif "id_proposition" in data:
        self.win = True
        self.id_proposition = data["id_proposition"]
        self.name_proposition = data["name_proposition"]
        self.description_proposition = data["description_proposition"]
        self.step_last_proposition = self.step
        self.pseudo = data.get("pseudo")
        self.flag_photo = data.get("flag_photo")
        self.photo = data.get("photo")
    else:
        self.akitude = data.get("akitude", "defi.png")
        self.step = int(data["step"])
        self.progression = float(data["progression"])
        self.question = data["question"]
    self.completion = data["completion"]

akinator.AsyncClient._AsyncClient__handler = _patched_handler

# Apply patch to akinator library's AsyncCloudScraper.post to prevent indefinite hangs by forcing a default timeout
_original_post = akinator.async_client.AsyncCloudScraper.post

async def _patched_post(self, url, data=None, json=None, **kwargs):
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 5  # default 5 second timeout
    return await _original_post(self, url, data=data, json=json, **kwargs)

akinator.async_client.AsyncCloudScraper.post = _patched_post


# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)

# Initialize Discord bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)

# Initialize AI Handler
ai = AIHandler()

# Hardcoded defaults (Nullified/Cleaned for multi-server support)
DEFAULT_HINGLISH_ROLE_ID = 0
DEFAULT_MALE_ROLE_ID = 0
DEFAULT_FEMALE_ROLE_ID = 0
DEFAULT_LEWD_ROLE_ID = 0
DEFAULT_TARGET_CHANNEL_ID = 0
DEFAULT_STORY_CHANNEL_ID = 0
DEFAULT_NSFW_CHANNEL_ID = 0

# In-memory cache for server configs (guild_id -> config dict)
_server_config_cache = {}

async def get_guild_config(guild_id: int) -> dict:
    """Get server config with in-memory cache. Falls back to empty dict."""
    gid = str(guild_id)
    if gid not in _server_config_cache:
        _server_config_cache[gid] = await db.get_server_config(gid)
    return _server_config_cache[gid] or {}

def get_role_id(config: dict, key: str, default: int) -> int:
    """Get a role ID from server config, falling back to default/0."""
    val = config.get(key, default)
    return int(val) if val else 0

def get_channel_id(config: dict, key: str, default) -> int:
    """Get a channel ID from server config, falling back to default/0."""
    val = config.get(key, default)
    return int(val) if val else 0


def translate_tags(query: str, is_booru: bool) -> str:
    """
    Translates a user/AI search query into proper search format.
    - If is_booru is True, it converts terms to lower case and ensures 'reze_(chainsaw_man)' is used.
    - If is_booru is False (e.g. Reddit), it keeps it normal: 'reze <query>'.
    """
    cleaned = query.lower().strip()
    # Remove weird punctuation that could break tags (excluding parenthesis/underscores for tags like reze_(chainsaw_man))
    cleaned = re.sub(r'[^\w\s\(\)_]', '', cleaned)
    
    if is_booru:
        if "reze_(chainsaw_man)" not in cleaned and "reze" in cleaned:
            cleaned = cleaned.replace("reze", "reze_(chainsaw_man)")
        elif "reze" not in cleaned:
            cleaned = f"reze_(chainsaw_man) {cleaned}"
            
        tags = [t.strip() for t in cleaned.split() if t.strip()]
        return " ".join(tags)
    else:
        if "reze" not in cleaned:
            cleaned = f"reze {cleaned}"
        return cleaned


# Creator Discord IDs (all alts of the same person)
CREATOR_DISCORD_IDS = {1276870533811540031}  # Add all creator Discord user IDs here
CREATOR_USERNAMES = {"syfmyorii", "realyorii", "issgrid", "nottkai", "nottkai.", "spikiee"}

# Global event cooldown to prevent join/leave/unprompted flooding
last_event_message_time = 0
EVENT_COOLDOWN_SECONDS = 300  # 5 minutes between any event-driven messages

# ========================
# SLASH COMMANDS
# ========================

@tree.command(name="setup", description="Show current server config for Reze")
@discord.app_commands.default_permissions(manage_guild=True)
async def slash_setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    config = await get_guild_config(interaction.guild_id)
    embed = discord.Embed(title="Reze Server Config", color=0x9B59B6)
    embed.add_field(name="Male Role", value=f"<@&{config['male_role_id']}>" if config.get('male_role_id') else "Not set (using default)", inline=True)
    embed.add_field(name="Female Role", value=f"<@&{config['female_role_id']}>" if config.get('female_role_id') else "Not set (using default)", inline=True)
    embed.add_field(name="Hinglish Role", value=f"<@&{config['hinglish_role_id']}>" if config.get('hinglish_role_id') else "Not set (using default)", inline=True)
    embed.add_field(name="Lewd Role", value=f"<@&{config['lewd_role_id']}>" if config.get('lewd_role_id') else "Not set (using default)", inline=True)
    embed.add_field(name="Target Channel", value=f"<#{config['target_channel_id']}>" if config.get('target_channel_id') else "Not set (using default)", inline=True)
    embed.add_field(name="NSFW Channel", value=f"<#{config['nsfw_channel_id']}>" if config.get('nsfw_channel_id') else "Not set (using default)", inline=True)
    embed.add_field(name="Story Channel", value=f"<#{config['story_channel_id']}>" if config.get('story_channel_id') else "Not set (using default)", inline=True)
    embed.add_field(name="Confessions Channel", value=f"<#{config['confession_channel_id']}>" if config.get('confession_channel_id') else "Not set (auto-detects #confessions)", inline=True)
    embed.set_footer(text="Use /setrole and /setchannel to configure")
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="setrole", description="Set a role for Reze to recognize in this server")
@discord.app_commands.default_permissions(manage_guild=True)
@discord.app_commands.describe(
    role_type="Which role to set",
    role="The role to assign"
)
@discord.app_commands.choices(role_type=[
    discord.app_commands.Choice(name="Male", value="male_role_id"),
    discord.app_commands.Choice(name="Female", value="female_role_id"),
    discord.app_commands.Choice(name="Hinglish", value="hinglish_role_id"),
    discord.app_commands.Choice(name="Lewd Allowed", value="lewd_role_id"),
])
async def slash_setrole(interaction: discord.Interaction, role_type: discord.app_commands.Choice[str], role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    await db.set_server_config(gid, role_type.value, role.id)
    _server_config_cache.pop(gid, None)  # Invalidate cache
    await interaction.followup.send(f"done. **{role_type.name}** role set to {role.mention}", ephemeral=True)

@tree.command(name="setchannel", description="Set a channel for Reze in this server")
@discord.app_commands.default_permissions(manage_guild=True)
@discord.app_commands.describe(
    channel_type="Which channel to set",
    channel="The channel to assign"
)
@discord.app_commands.choices(channel_type=[
    discord.app_commands.Choice(name="Target (main hangout)", value="target_channel_id"),
    discord.app_commands.Choice(name="NSFW", value="nsfw_channel_id"),
    discord.app_commands.Choice(name="Story", value="story_channel_id"),
    discord.app_commands.Choice(name="Confessions", value="confession_channel_id"),
])
async def slash_setchannel(interaction: discord.Interaction, channel_type: discord.app_commands.Choice[str], channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    await db.set_server_config(gid, channel_type.value, channel.id)
    _server_config_cache.pop(gid, None)  # Invalidate cache
    await interaction.followup.send(f"done. **{channel_type.name}** channel set to {channel.mention}", ephemeral=True)

@tree.command(name="resetconfig", description="Reset all Reze config for this server to defaults")
@discord.app_commands.default_permissions(manage_guild=True)
async def slash_resetconfig(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild_id)
    await db.server_configs_col.delete_one({"_id": gid})
    _server_config_cache.pop(gid, None)
    await interaction.followup.send("config reset to defaults.", ephemeral=True)

# --- NEW: Rate Limiting & Fallbacks ---
user_msg_timestamps = {}

FALLBACK_MESSAGES = [
    "my wifi is acting up...",
    "ignoring you rn.",
    "brb my phone is dying.",
    "discord is glitching or smth... talk later.",
    "idk what's happening but something broke."
]

# --- NEW: Strict MIME Types ---
# We block weird formats like .mp4, PDFs, or raw binaries that would crash the API
ALLOWED_MIME_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"]

# --- HUMAN BEHAVIOR SYSTEMS ---
# Grudge list: users Reze is temporarily ignoring (user_id -> expiry timestamp)
grudge_list = {}

# Per-channel async locks to prevent message interleaving
channel_locks = {}

# Track last message time per channel (for unprompted messages)
channel_last_activity = {}

# Per-user eavesdrop history to track spamming of "reze"
# user_id -> list of timestamps when they mentioned "reze"
reze_mention_history = {}

# Track user choices in Marry, Kiss, Kill game: message_id -> { user_id -> [emoji1, emoji2, emoji3] }
mkk_choices = {}


# Track application-scoped emojis fetched at startup
application_emojis = []

class MessageIDCache:
    def __init__(self, maxsize=5000):
        self.maxsize = maxsize
        self.queue = deque()
        self.set = set()

    def add(self, msg_id):
        if msg_id in self.set:
            return
        self.queue.append(msg_id)
        self.set.add(msg_id)
        if len(self.queue) > self.maxsize:
            oldest = self.queue.popleft()
            self.set.discard(oldest)

    def __contains__(self, msg_id):
        return msg_id in self.set

# Track recent command output message IDs (to prevent replying to replies of command outputs)
command_output_message_ids = MessageIDCache(maxsize=5000)

# AFK status tracker (user_id -> {"reason": reason, "time": timestamp, "name": display_name})
afk_users = {}

# User commands rate limit tracker (user_id -> list of command timestamps)
user_command_timestamps = {}

# Command-specific cooldown configurations (in seconds)
COOLDOWN_TIMES = {
    "confess": 300, # 5 minutes
    "marry": 30,    # 30 seconds
    "adopt": 30,    # 30 seconds
    "divorce": 30,  # 30 seconds
    "disown": 30,   # 30 seconds
    "abandon": 30,  # 30 seconds
    "runaway": 30,  # 30 seconds
    "disownall": 30,# 30 seconds
    "ship": 10,     # 10 seconds
    "family": 15,   # 15 seconds
    "rr": 10,
    "blackjack": 10,
    "bj": 10,
    "rps": 10,
    "trivia": 10,
    "scramble": 15,
    "ttt": 15,
}

# command_name -> user_id -> last_used_timestamp
command_cooldown_timestamps = {}

# --- ANIME ACTIONS & EMOTES (Nekos.best API) ---
ACTIONS = [
    "angry", "baka", "bite", "bleh", "blowkiss", "blush", "bonk", "bored", "carry",
    "chase", "cheer", "clap", "confused", "cringe", "cry", "cuddle", "dance", "drool",
    "facepalm", "feed", "fuck", "handhold", "handshake", "happy", "highfive", "hug",
    "kabedon", "kick", "kill", "kiss", "lappillow", "laugh", "lick", "lurk", "nod",
    "nom", "nope", "nosebleed", "nuzzle", "panic", "pat", "peck", "poke", "pout",
    "punch", "run", "sad", "salute", "scream", "shake", "shrug", "shoot", "sip",
    "slap", "sleep", "smile", "smug", "spin", "stare", "surprised", "tableflip",
    "tailwhip", "think", "threaten", "thumbsup", "tickle", "tired", "wave", "wink",
    "yawn", "yeet"
]

OTAKUGIFS_ACTION_MAP = {
    "angry": "angrystare",
    "baka": "mad",
    "blowkiss": "airkiss",
    "bonk": "smack",
    "bored": "sigh",
    "carry": "hug",
    "chase": "run",
    "cheer": "celebrate",
    "clap": "clap",
    "cringe": "huh",
    "feed": "nom",
    "fuck": "slap",
    "handshake": "brofist",
    "highfive": "brofist",
    "kabedon": "stare",
    "kick": "punch",
    "kill": "punch",
    "lappillow": "cuddle",
    "lurk": "peek",
    "nod": "yes",
    "nom": "nom",
    "nope": "no",
    "panic": "scared",
    "peck": "kiss",
    "poke": "poke",
    "salute": "thumbsup",
    "scream": "shout",
    "shake": "headbang",
    "shoot": "punch",
    "sip": "sip",
    "spin": "dance",
    "tableflip": "mad",
    "tailwhip": "nyah",
    "think": "huh",
    "threaten": "stare",
    "yeet": "punch",
}

PURRBOT_ACTION_MAP = {
    "bite": "bite",
    "blush": "blush",
    "cuddle": "cuddle",
    "dance": "dance",
    "feed": "feed",
    "hug": "hug",
    "kiss": "kiss",
    "lick": "lick",
    "nom": "feed",
    "pat": "pat",
    "poke": "poke",
    "slap": "slap",
    "smile": "smile",
    "tickle": "tickle",
}

NEKOSLIFE_ACTION_MAP = {
    "pat": "pat",
    "hug": "hug",
    "kiss": "kiss",
    "slap": "slap",
    "poke": "poke",
    "feed": "feed",
    "nom": "feed",
    "cuddle": "cuddle",
    "tickle": "tickle",
    "smug": "smug",
}

async def fetch_action_gif(session: aiohttp.ClientSession, action: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    # Provider 1: Otakugifs
    otaku_tag = OTAKUGIFS_ACTION_MAP.get(action, action)
    try:
        url = f"https://api.otakugifs.xyz/gif?reaction={otaku_tag}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "url" in data and data["url"]:
                    return data["url"], "otakugifs.xyz"
    except Exception as e:
        logger.debug(f"Otakugifs fetch failed for {action}: {e}")

    # Provider 2: Purrbot
    purr_tag = PURRBOT_ACTION_MAP.get(action)
    if purr_tag:
        try:
            url = f"https://api.purrbot.site/v2/img/sfw/{purr_tag}/gif"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "link" in data and data["link"]:
                        return data["link"], "purrbot.site"
        except Exception as e:
            logger.debug(f"Purrbot fetch failed for {action}: {e}")

    # Provider 3: Nekos.life
    nekos_tag = NEKOSLIFE_ACTION_MAP.get(action)
    if nekos_tag:
        try:
            url = f"https://nekos.life/api/v2/img/{nekos_tag}"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "url" in data and data["url"]:
                        return data["url"], "nekos.life"
        except Exception as e:
            logger.debug(f"Nekos.life fetch failed for {action}: {e}")

    # Provider 4: Nekos.best (attempt with modern browser User-Agent)
    try:
        if action == "kill":
            endpoint = "shoot"
        elif action == "fuck":
            endpoint = random.choice(["slap", "punch", "kick", "bonk", "yeet"])
        else:
            endpoint = action
        url = f"https://nekos.best/api/v2/{endpoint}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "results" in data and len(data["results"]) > 0:
                    return data["results"][0]["url"], "nekos.best"
    except Exception as e:
        logger.debug(f"Nekos.best fetch failed for {action}: {e}")

    return None, None



ACTION_VERBS = {
    "angry": [
        "gets angry at {target}! 💢",
        "glares angrily at {target}... 😡",
        "is super pissed at {target}! 😤"
    ],
    "baka": [
        "calls {target} a baka! 🙄",
        "points at {target} and yells \"BAKA!\" 🤪",
        "shakes their head at {target}... baka behavior. 🤦‍♀️"
    ],
    "bite": [
        "bites {target}! 👀",
        "gives {target} a little nibble. 😈",
        "chomps on {target}! 🦷"
    ],
    "bleh": [
        "goes bleh at {target}! 😛",
        "sticks their tongue out at {target}! 😜",
        "pulls a face at {target}! 🤪"
    ],
    "blowkiss": [
        "blows a kiss to {target}! 😘",
        "sends a flying kiss {target}'s way! 💋",
        "flirts and blows a kiss to {target}~ 💖"
    ],
    "blush": [
        "blushes at {target}... 😳",
        "turns red looking at {target}! 🙈",
        "gets flustered by {target}. 💕"
    ],
    "bonk": [
        "bonks {target}! 🔨",
        "gives {target} a bonk on the head! 🏏",
        "sends {target} to horny jail! 🚓"
    ],
    "bored": [
        "gets bored of {target}... 🥱",
        "sighs boredly at {target}. 💤",
        "yawns while listening to {target}. 🙄"
    ],
    "carry": [
        "carries {target}! 🎒",
        "picks up {target} and carries them! 🏋️‍♀️",
        "scoops {target} up in their arms! 💕"
    ],
    "chase": [
        "chases {target}! 🏃",
        "runs after {target} at full speed! 🏃‍♀️",
        "is chasing {target} down! 🚓"
    ],
    "cheer": [
        "cheers up {target}! 🎉",
        "is rooting for {target}! 🙌",
        "hypes {target} up! 📣"
    ],
    "clap": [
        "claps for {target}! 👏",
        "gives {target} a round of applause! 🎉",
        "slow claps for {target}. 🙄"
    ],
    "confused": [
        "looks confused at {target}... ❓",
        "scratches their head looking at {target}. 🤔",
        "has no idea what {target} is saying. 😵"
    ],
    "cringe": [
        "cringes at {target}... 😬",
        "physically cringes looking at {target}. 🤢",
        "gives {target} the side-eye. 👀"
    ],
    "cry": [
        "cries at {target}... 😭",
        "sheds tears because of {target}. 🥺",
        "cries on {target}'s shoulder. 😢"
    ],
    "cuddle": [
        "cuddles with {target}! 🛌",
        "snuggles up close to {target}. 💕",
        "pulls {target} into a warm cuddle. 🧸"
    ],
    "dance": [
        "dances with {target}! 💃",
        "pulls {target} to the dance floor! 🕺",
        "does a silly dance with {target}! ✨"
    ],
    "facepalm": [
        "facepalms at {target}... *sigh* 🤦",
        "slaps their forehead listening to {target}. 🤦‍♀️",
        "can't believe {target} just said that. 💀"
    ],
    "feed": [
        "feeds {target}! 🍰",
        "opens wide and feeds {target}! 🥄",
        "pops a sweet treat into {target}'s mouth! 🍬"
    ],
    "handhold": [
        "holds hands with {target}! 💕",
        "interlocks fingers with {target}. 😳",
        "grabs {target}'s hand tightly! 🤝"
    ],
    "handshake": [
        "shakes hands with {target}! 🤝",
        "gives {target} a firm handshake. 💼",
        "does a secret handshake with {target}! 🤜"
    ],
    "happy": [
        "giggles happily at {target}! ✨",
        "smiles brightly at {target}! 🥰",
        "shares their happiness with {target}! 💕"
    ],
    "highfive": [
        "high-fives {target}! ✋",
        "slaps hands in a high-five with {target}! ⚡",
        "gives {target} a high-five! 🎉"
    ],
    "hug": [
        "hugs {target}! 💕",
        "gives {target} a warm squeeze! 🧸",
        "wraps {target} in a big hug! 🥰"
    ],
    "kick": [
        "kicks {target}! 🦵",
        "gives {target} a playful kick! ⚽",
        "kicks {target} out of the way! 💀"
    ],
    "kiss": [
        "kisses {target}! 💋",
        "gives {target} a sweet peck! 😘",
        "kisses {target} softly. 💕"
    ],
    "laugh": [
        "laughs at {target}! 😂",
        "giggles at {target}'s antics! LMAO 🤪",
        "points and laughs at {target}! 🫵"
    ],
    "lick": [
        "licks {target}! 👅",
        "gives {target} a cheeky lick! 🤪",
        "licks {target}'s cheek! 😳"
    ],
    "lurk": [
        "lurks around {target}... 👀",
        "stalks {target} from the shadows. 🕵️‍♀️",
        "spies on {target}! 🤫"
    ],
    "nod": [
        "nods at {target}... mmhm. 👍",
        "agrees with {target}. 🤝",
        "nods in approval to {target}. 👌"
    ],
    "nom": [
        "noms on {target}! 🍿",
        "takes a playful bite out of {target}! 😈",
        "nibbles on {target}! 👀"
    ],
    "nope": [
        "says nope to {target}! 🙅",
        "shakes their head at {target}... no way. 🛑",
        "rejects {target}'s idea immediately. ✖️"
    ],
    "panic": [
        "panics because of {target}! 😱",
        "runs around screaming at {target}! 🏃‍♀️",
        "gets overwhelmed by {target}. 😵"
    ],
    "pat": [
        "pats {target}! 💕",
        "gently pats {target} on the head.",
        "gives {target} some soft headpats. 🥰",
        "strokes {target}'s hair... cute. 🎀"
    ],
    "poke": [
        "pokes {target}! 👉",
        "nudges {target} playfully! 🤭",
        "keeps poking {target}! 🫵"
    ],
    "pout": [
        "pouts at {target}... 🥺",
        "crosses their arms and pouts at {target}. 😤",
        "makes a cute pout face at {target}. 💕"
    ],
    "punch": [
        "punches {target}! 👊",
        "gives {target} a solid punch! 💥",
        "playfully punches {target}'s arm! 🤜"
    ],
    "run": [
        "runs away from {target}! 🏃‍♀️",
        "flees from {target}! 💨",
        "sprints away from {target} in terror! 😱"
    ],
    "sad": [
        "gets sad because of {target}... 🥺",
        "looks sadly at {target}. 😢",
        "feels ignored by {target}. 💔"
    ],
    "scream": [
        "screams at {target}! 🗣️",
        "yells at {target} at the top of their lungs! 📢",
        "screams in {target}'s face! 😱"
    ],
    "shrug": [
        "shrugs at {target}... whatever. 🤷",
        "gives {target} a blank shrug. 😶",
        "shrugs their shoulders at {target}. 🤷‍♀️"
    ],
    "slap": [
        "slaps {target}! 🖐️",
        "gives {target} a stinging slap! 💥",
        "slaps {target} across the face! 💀"
    ],
    "sleep": [
        "sleeps next to {target}. 💤",
        "dozes off on {target}'s shoulder. 🛌",
        "crashes out with {target}. 😴"
    ],
    "smile": [
        "smiles warmly at {target}! 😊",
        "grins at {target}! 😄",
        "gives {target} a sweet smile. 💕"
    ],
    "smug": [
        "looks smugly at {target}... 😏",
        "grins smugly at {target}. 😈",
        "acts superior to {target}. 💅"
    ],
    "stare": [
        "stares at {target}... 👁️_👁️",
        "glares intensely at {target}. 😠",
        "watches {target} closely. 👀"
    ],
    "surprised": [
        "looks surprised at {target}! 😲",
        "is shocked by {target}! ⚡",
        "gaps in surprise at {target}. 🙀"
    ],
    "tailwhip": [
        "tailwhips {target}! 🐉",
        "swipes {target} with their tail! 💥",
        "whacks {target} with a tail whip! 🦖"
    ],
    "think": [
        "thinks about {target}... 🤔",
        "wonders about {target}'s motives. 🧐",
        "ponders what {target} is up to. 💭"
    ],
    "threaten": [
        "threatens {target}! 🔪",
        "holds a knife towards {target}... 🗡️",
        "warns {target} to watch their back! ☠️"
    ],
    "thumbsup": [
        "gives a thumbs up to {target}! 👍",
        "approves of {target}! 👌",
        "shows support to {target}. 🤝"
    ],
    "tickle": [
        "tickles {target}! 😂",
        "attacks {target} with tickles! 🖐️",
        "gets {target} laughing with tickles! 😹"
    ],
    "tired": [
        "gets tired of {target}... 😴",
        "sighs exhaustedly at {target}. 🥱",
        "is sick of {target}'s yapping. 💀"
    ],
    "wave": [
        "waves at {target}! 👋",
        "waves hello to {target}! 😊",
        "waves goodbye to {target}. 😭"
    ],
    "wink": [
        "winks at {target}! 😉",
        "gives {target} a playful wink! 😘",
        "shoots a wink {target}'s way. 😏"
    ],
    "yawn": [
        "yawns at {target}... boring. 🥱",
        "yawns sleepily next to {target}. 💤",
        "stretches and yawns in front of {target}. 🛌"
    ],
    "yeet": [
        "yeets {target}! 💨",
        "throws {target} into orbit! 🚀",
        "chucks {target} across the room! 💥"
    ],
    "shoot": [
        "shoots at {target}! 🔫",
        "takes aim and fires at {target}! 💥",
        "bang! shoots {target}! 💥"
    ],
    "kill": [
        "kills {target}! 💀",
        "murders {target} in cold blood! 🔫",
        "annihilates {target}! 💥"
    ],
    "peck": [
        "gives {target} a quick peck on the cheek! 😳",
        "pecks {target}! 😚"
    ],
    "sip": [
        "sips a drink with {target}. ☕",
        "sips tea while looking at {target}... ☕"
    ],
    "tableflip": [
        "flips a table at {target}! ┬─┬  (╯°□°)╯ ┻━┻",
        "is so done with {target} they flip a table! ┻━┻"
    ],
    "spin": [
        "spins around {target}! 🌀",
        "spins {target} around! 🌀"
    ],
    "shake": [
        "shakes {target} vigorously! 🤝",
        "shakes hands with {target}! 🤝"
    ],
    "kabedon": [
        "kabedons {target} against the wall! 😳",
        "pins {target} to the wall! (KABEDON) 🤫"
    ],
    "salute": [
        "salutes {target}! 🫡",
        "gives a respectful salute to {target}. 🫡"
    ],
    "lappillow": [
        "rests their head on {target}'s lap for a cozy lap pillow~ 😳",
        "gets a warm lap pillow from {target}! 💕",
        "lays down with their head on {target}'s lap. 🥺"
    ],
    "nosebleed": [
        "gets a massive nosebleed looking at {target}! 🥵",
        "stares at {target} and gets a nosebleed! 😳",
        "wipes away a nosebleed caused by {target}! 🩸"
    ],
    "nuzzle": [
        "nuzzles against {target} affectionately~ 🥰",
        "gives {target} a warm, cozy nuzzle. 💕",
        "snuggles up and nuzzles {target}! 🥺"
    ],
    "drool": [
        "drools over {target}... looking good! 🥵",
        "looks at {target} and starts drooling! 🤤",
        "is drooling because of {target}! 💦"
    ],
    "fuck": [
        "fucks {target} over! 😤",
        "beats the absolute crap out of {target}! 👊",
        "yells \"FUCK YOU\" at {target}! 🤬"
    ]
}

ACTION_SOLO = {
    "angry": [
        "gets angry! 💢",
        "screams in frustration! 😡",
        "puffs their cheeks in anger! 😤"
    ],
    "baka": [
        "calls themselves a baka... wait, what? 🙄",
        "reaches peak baka energy. 🤪",
        "realizes they did something dumb. baka. 🤦‍♀️"
    ],
    "bite": [
        "bites their lip... 👁️👄👁️",
        "accidentally bites their own tongue! 😭",
        "chomps on a snack nervously. 🍿"
    ],
    "bleh": [
        "goes bleh! 😛",
        "sticks their tongue out. 😜",
        "makes a silly face! 🤪"
    ],
    "blowkiss": [
        "blows a kiss to the air~ 😘",
        "sends a flying kiss into the void. 💨",
        "blows a kiss to their own reflection! 💋"
    ],
    "blush": [
        "blushes... crimson red! 😳",
        "hides their face in embarrassment! 🙈",
        "gets warm and flustered. 💕"
    ],
    "bonk": [
        "bonks themselves on the head! 🤕",
        "accidentally bonks into a wall! 🚪",
        "drops a book on their own foot. 🤦‍♀️"
    ],
    "bored": [
        "is bored out of their mind... 🥱",
        "stares at the ceiling, dying of boredom. 💤",
        "spams random messages because they're bored. 📱"
    ],
    "carry": [
        "wants to be carried... 🥺",
        "wishes someone would pick them up. 🧸",
        "attempts to carry a giant pile of books. 📚"
    ],
    "chase": [
        "is running in circles! 🏃",
        "chases their own tail... wait. 🐱",
        "runs around chasing a butterfly! 🦋"
    ],
    "cheer": [
        "is cheering! 🎉",
        "does a happy cheer! 🙌",
        "waves pom-poms around! 📣"
    ],
    "clap": [
        "claps their hands! 👏",
        "applauds happily! 🎉",
        "claps to get someone's attention. 👋"
    ],
    "confused": [
        "is highly confused... ❓",
        "has questions. many questions. 🤔",
        "brain.exe has stopped working. 😵"
    ],
    "cringe": [
        "cringes internally... 😬",
        "remembers something they did 5 years ago. 💀",
        "cringes at their old texts. 🤦‍♀️"
    ],
    "cry": [
        "cries in the corner... 😭",
        "is sobbing quietly. 🥺",
        "waterworks are active! 😢"
    ],
    "cuddle": [
        "cuddles a pillow... cozy. 🛌",
        "wraps themselves in a warm blanket. 🧸",
        "wants cuddles real bad. 🥺"
    ],
    "dance": [
        "dances alone in their room! 💃",
        "does a happy victory dance! ✨",
        "grooves to the music! 🎵"
    ],
    "facepalm": [
        "facepalms... *sigh* 🤦",
        "does a double facepalm. 🤦‍♀️",
        "loses hope in humanity. 💀"
    ],
    "feed": [
        "feeds themselves some cake... nom nom. 🍰",
        "eats a spoonful of Nutella. 🥄",
        "treats themselves to snacks! 🍬"
    ],
    "handhold": [
        "holds their own hand... lonely. 🥲",
        "wants to hold someone's hand. 🥺",
        "puts their hands in their pockets. 🧥"
    ],
    "handshake": [
        "shakes hands with... a ghost? 🤝",
        "congratulates themselves. 👏",
        "practices handshakes in the mirror. 🪞"
    ],
    "happy": [
        "is feeling super happy! ✨",
        "giggles happily! 🥰",
        "bounces around with joy! 💖"
    ],
    "highfive": [
        "high-fives the mirror! ✋",
        "high-fives the air. 💨",
        "left hanging... high-fives themselves. 🥲"
    ],
    "hug": [
        "hugs a teddy bear... 🧸",
        "gives themselves a big hug! 🥰",
        "wishes for a warm hug. 🥺"
    ],
    "kick": [
        "kicks the air! 🦵",
        "playfully kicks a ball. ⚽",
        "does a high kick. 🤸‍♀️"
    ],
    "kiss": [
        "kisses the mirror... 💋",
        "blows a kiss to the ceiling. 😘",
        "wants a kiss. 🥺"
    ],
    "laugh": [
        "laughs out loud! LMAO 😂",
        "snickers quietly. 🤭",
        "can't stop laughing! 💀"
    ],
    "lick": [
        "licks... ice cream? 🍦",
        "licks their lips hungrily. 👅",
        "tastes something sweet. 🍬"
    ],
    "lurk": [
        "lurks in the shadows... 👀",
        "lurks around the chat. 🕵️‍♀️",
        "keeps a low profile... 🤫"
    ],
    "nod": [
        "nods along... mmhm. 👍",
        "nods silently. 🤝",
        "nods to themselves. 👌"
    ],
    "nom": [
        "nom noms on some snacks! 🍿",
        "noms on a slice of pizza! 🍕",
        "noms happily. 😋"
    ],
    "nope": [
        "says nope and walks away! 🙅",
        "absolutely not. 🛑",
        "skips that immediately. ✖️"
    ],
    "panic": [
        "is absolutely panicking! 😱",
        "screams internally... and externally. 🏃‍♀️",
        "sweats nervously! 😵"
    ],
    "pat": [
        "pats themselves... how cute.",
        "gives themselves a headpat. It's okay. 🥺",
        "is patting their own head. Needs attention. 🥲"
    ],
    "poke": [
        "pokes the screen! 👈",
        "pokes their own cheek. 🤭",
        "pokes a soft toy. 🧸"
    ],
    "pout": [
        "pouts cute... 🥺",
        "pouts silently. 😤",
        "does a dramatic pout! 💕"
    ],
    "punch": [
        "punches the air! 👊",
        "punches a pillow out of anger! 💥",
        "shadow boxes! 🤜"
    ],
    "run": [
        "runs away! 🏃‍♀️",
        "sprints off into the sunset! 💨",
        "is on the run! 🏃"
    ],
    "sad": [
        "is sad... send chocolate. 🥺",
        "sits under a tiny rain cloud. 😢",
        "is in their feels. 💔"
    ],
    "scream": [
        "screams into the void! 🗣️",
        "screams into a pillow. 📢",
        "lets out a loud shriek! 😱"
    ],
    "shrug": [
        "shrugs... whatever. 🤷",
        "shrugs because they don't care. 😶",
        "shrugs in response. 🤷‍♀️"
    ],
    "slap": [
        "slaps their own face... wake up! 🤦‍♀️",
        "accidentally slaps a mosquito on their own arm. 🖐️",
        "gets slapped by a tree branch. 🌳"
    ],
    "sleep": [
        "falls fast asleep... zzz. 💤",
        "crashes onto the bed. 🛌",
        "naps peacefully. 😴"
    ],
    "smile": [
        "smiles warmly! 😊",
        "grins happily! 😄",
        "smiles to themselves. 💕"
    ],
    "smug": [
        "looks incredibly smug... 😏",
        "does a smug face. 😈",
        "feels like a genius. 💅"
    ],
    "stare": [
        "stares blankly into space... 👁️_👁️",
        "zones out completely. 😶",
        "stares at the wall. 🧱"
    ],
    "surprised": [
        "is completely shocked! 😲",
        "gasps! ⚡",
        "can't believe their eyes. 🙀"
    ],
    "tailwhip": [
        "does a tailwhip! 🐉",
        "spins around and whips their tail. 💥",
        "accidentally hits a vase with their tail. 🏺"
    ],
    "think": [
        "is deep in thought... 🤔",
        "ponders the meaning of life. 🧐",
        "brain is working overtime. 💭"
    ],
    "threaten": [
        "threatens... who exactly? 🔪",
        "points a plastic fork threateningly. 🍴",
        "swears revenge on the void. ☠️"
    ],
    "thumbsup": [
        "gives a thumbs up! 👍",
        "approves! 👌",
        "congratulates themselves. 👏"
    ],
    "tickle": [
        "tickles themselves... does that even work? 😂",
        "tries to tickle a pet. 🐾",
        "giggles from phantom tickles. 👻"
    ],
    "tired": [
        "is tired af... 😴",
        "yawns... exhausted. 🥱",
        "needs a 10-hour nap. 🛌"
    ],
    "wave": [
        "waves hello! 👋",
        "waves to a random stranger. 😳",
        "waves goodbye. 👋"
    ],
    "wink": [
        "winks playfully! 😉",
        "practices winking (and fails, blinking instead). 👁️",
        "winks at their reflection. 🪞"
    ],
    "yawn": [
        "yawns... sleepy. 🥱",
        "stretches and yawns. 💤",
        "goes \"aaah\" in a big yawn. 🛌"
    ],
    "yeet": [
        "yeets themselves out of the window! 💨",
        "yeets their phone onto the bed. 📱",
        "yeets a water bottle. 🧴"
    ],
    "shoot": [
        "shoots into the air! 🔫",
        "is reloading their weapon. 🔫",
        "pew pew! 🔫"
    ],
    "kill": [
        "is looking for a target... 🔫",
        "is on a rampage! 💀",
        "eliminated someone. 💥"
    ],
    "peck": [
        "is waiting for a peck... 🥺",
        "gives a small peck."
    ],
    "sip": [
        "sips their coffee quietly. ☕",
        "sips tea... delicious. 🍵",
        "just sips. 🧋"
    ],
    "tableflip": [
        "flips a table in rage! (╯°□°)╯︵ ┻━┻",
        "┻━┻ ︵ ╰(°ㅂ°╰) flips table back!",
        "table flipped! ┻━┻"
    ],
    "spin": [
        "spins around in circles! 🌀",
        "wheee! spins! 🌀"
    ],
    "shake": [
        "shakes head. 🤦‍♀️",
        "is shaking! 🥶"
    ],
    "kabedon": [
        "practices kabedon on a wall... 🧱",
        "leans against the wall. 🧱"
    ],
    "salute": [
        "salutes! Yes sir! 🫡",
        "stands at attention and salutes. 🫡"
    ],
    "lappillow": [
        "wants a cozy lap pillow... 🥺",
        "is waiting for someone to lay on their lap."
    ],
    "nosebleed": [
        "gets a nosebleed! 😳",
        "is blushing with a nosebleed. 🥵",
        "wipes their nose... is that blood? 🩸"
    ],
    "nuzzle": [
        "wants a warm nuzzle... 🥺",
        "nuzzles their favorite plushie. 🧸"
    ],
    "drool": [
        "drools sleepily... 🤤",
        "wipes some drool from their mouth. 🤫",
        "is drooling over thoughts of sweet coffee. ☕"
    ],
    "fuck": [
        "screams \"FUCK!\" in frustration! 🤬",
        "is absolutely done with everything. 💀",
        "flips everyone off! 🖕"
    ]
}



# Track channels currently undergoing memory compression to prevent concurrent API storms
active_compressions = set()

# Dry text detection (messages that deserve to be left on read)
DRY_TEXTS = {
    "ok", "okay", "k", "kk", "lol", "lmao", "haha", "hah", "ha", "hmm",
    "oh", "ah", "mhm", "yep", "yea", "yeah", "nah", "nope", "sure",
    "cool", "nice", "true", "fr", "bet", "ight", "aight", "ye", "ya",
    "ig", "ik", "idk", "ic", "ooh", "oof", "rip", "f", "bruh", "damn",
    "wow", "ikr", "smh", "tbh", "ngl", "wdym", "ohh", "ohhh",
    "hm", "hmmm", "mmm", "mm", "meh"
}

# Left-on-Read reactions (what she does instead of replying)
LEFT_ON_READ_REACTIONS = ["👁️", "✅", "👀", "💀", "🫥"]

# Wrong chat messages (out-of-context snippets)
WRONG_CHAT_MESSAGES = [
    "yeah grab the spicy ones",
    "wait he said WHAT",
    "no i think the blue one is cuter",
    "omg i just saw her story 💀",
    "bro that's literally not how you make maggi",
    "can you send me that playlist again",
    "i think my cat ate something weird",
    "tell her i said hi but like casually",
    "wait which app is cheaper for delivery rn",
    "no literally he's so annoying i can't",
    "the pink hoodie or the black one??",
    "i'm not going if she's going"
]

# AFK simulation tracker (channel_id -> True if she's "away")
afk_until = {}  # channel_id -> timestamp when she comes back

# Discord Rich Presence schedules (IST hour -> activity)
STATUS_SCHEDULE = {
    6: (discord.ActivityType.listening, "alarm going off"),
    7: (discord.ActivityType.custom, "☕"),
    8: (discord.ActivityType.listening, "Spotify"),
    9: (discord.ActivityType.watching, "reels"),
    10: (discord.ActivityType.playing, "doing nothing productive"),
    11: (discord.ActivityType.listening, "Spotify"),
    12: (discord.ActivityType.custom, "hungry."),
    13: (discord.ActivityType.custom, "eating"),
    14: (discord.ActivityType.watching, "youtube"),
    15: (discord.ActivityType.playing, "Valorant"),
    16: (discord.ActivityType.playing, "Valorant"),
    17: (discord.ActivityType.listening, "Spotify"),
    18: (discord.ActivityType.custom, "🌙"),
    19: (discord.ActivityType.watching, "anime"),
    20: (discord.ActivityType.watching, "anime"),
    21: (discord.ActivityType.playing, "Valorant"),
    22: (discord.ActivityType.listening, "Spotify"),
    23: (discord.ActivityType.custom, "can't sleep"),
    0: (discord.ActivityType.custom, "💤"),
    1: (discord.ActivityType.custom, "💤"),
}

# Message edit phrases (simulating casual adjustments or typo corrections)
EDIT_SWAPS = [
    ("going to", "gonna"),
    ("want to", "wanna"),
    ("have to", "gotta"),
    ("about", "abt"),
    ("because", "bc"),
    ("really", "rly"),
]

async def send_abandonment_announcements():
    # Wait for the bot cache to stabilize and log in fully
    await asyncio.sleep(5)
    try:
        # Check if we already sent the abandonment message
        state_col = db.db['global_state']
        notified = await state_col.find_one({"_id": "abandonment_notified"})
        if not notified or not notified.get("sent"):
            print("Sending abandonment announcements to designated channels...")
            configs = await db.get_all_server_configs()
            
            message_text = (
                """**Hi everyone. so uh, this is v4, which is the last update i'm getting. my dev has abandoned this repository and won't be updating me anymore. i'll still be here hanging out in the channel if u want to talk, but yeah, no new updates or features after this.just wanted to let y'all know ig haha.**"""
            )
            
            for config in configs:
                target_ch_id = get_channel_id(config, 'target_channel_id', 0)
                if target_ch_id:
                    try:
                        channel = bot.get_channel(target_ch_id)
                        if not channel:
                            channel = await bot.fetch_channel(target_ch_id)
                        if channel:
                            await channel.send(message_text)
                            print(f"Sent abandonment message to channel {target_ch_id} in guild {config.get('_id')}")
                    except Exception as e:
                        print(f"Failed to send abandonment message to channel {target_ch_id}: {e}")
            
            # Save status to prevent sending again
            await state_col.update_one(
                {"_id": "abandonment_notified"},
                {"$set": {"sent": True, "sent_at": datetime.now(timezone.utc)}},
                upsert=True
            )
            print("Finished sending abandonment announcements.")
    except Exception as e:
        print(f"Error in send_abandonment_announcements: {e}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    print("Bot is ready. Mention me or reply to my message to chat.")
    print("------")
    
    # Fetch application emojis dynamically
    global application_emojis
    try:
        application_emojis = await bot.fetch_application_emojis()
        print(f"Loaded {len(application_emojis)} application emojis.")
    except Exception as e:
        print(f"Failed to fetch application emojis: {e}")
        application_emojis = []

    # Sync slash commands
    try:
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash commands to guild {guild_id} instantly (ASAP).")
        else:
            synced = await tree.sync()
            print(f"Synced {len(synced)} slash commands globally.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
    # Initialize dashboard with bot references
    from dashboard import init_bot_refs
    init_bot_refs(bot, ai, grudge_list, channel_last_activity, unprompted_waiting_for_reply, user_msg_timestamps)
    print("Dashboard initialized with bot references.")
    

    # Start background tasks
    bot.loop.create_task(unprompted_message_loop())
    bot.loop.create_task(wrong_chat_loop())
    bot.loop.create_task(status_cycling_loop())
    bot.loop.create_task(story_posting_loop())
    bot.loop.create_task(send_abandonment_announcements())



def draw_ship_heart(size, percent, bg_color=(40, 40, 40, 200), fill_color=(230, 40, 40, 255), border_color=(10, 10, 10, 255), border_width=4):
    import math
    from PIL import ImageDraw, ImageFont
    h_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    
    cx = size / 2
    cy = size / 2 - (size * 0.05)
    
    scale = size / 35
    points = []
    for i in range(300):
        t = i * (2 * math.pi / 300)
        x = 16 * (math.sin(t) ** 3)
        y = -(13 * math.cos(t) - 5 * math.cos(2*t) - 2 * math.cos(3*t) - math.cos(4*t))
        points.append((cx + x * scale, cy + y * scale))
        
    ys = [p[1] for p in points]
    min_y = min(ys)
    max_y = max(ys)
    h_height = max_y - min_y
    
    fill_y = max_y - (h_height * (percent / 100))
    
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.polygon(points, fill=255)
    
    bg_img = Image.new("RGBA", (size, size), bg_color)
    fill_img = Image.new("RGBA", (size, size), fill_color)
    
    combined = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    combined.paste(bg_img, (0, 0), mask=mask)
    
    fill_rect_mask = Image.new("L", (size, size), 0)
    frm_draw = ImageDraw.Draw(fill_rect_mask)
    frm_draw.rectangle([0, int(fill_y), size, size], fill=255)
    
    final_fill_mask = Image.new("L", (size, size), 0)
    final_fill_mask.paste(mask, (0, 0), mask=fill_rect_mask)
    
    combined.paste(fill_img, (0, 0), mask=final_fill_mask)
    
    draw_comb = ImageDraw.Draw(combined)
    draw_comb.line(points + [points[0]], fill=border_color, width=border_width)
    
    try:
        font_path = os.path.join(BASE_DIR, "assets", "fonts", "Roboto-Bold.ttf")
        font = ImageFont.truetype(font_path, int(size * 0.16))
    except:
        font = ImageFont.load_default()
        
    txt = f"{percent}%"
    try:
        bbox = draw_comb.textbbox((0, 0), txt, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
    except AttributeError:
        tw, th = draw_comb.textsize(txt, font=font)
        
    tx = cx - tw / 2
    ty = cy - th / 2
    
    shadow_offset = 2
    for dx in [-shadow_offset, 0, shadow_offset]:
        for dy in [-shadow_offset, 0, shadow_offset]:
            if dx != 0 or dy != 0:
                draw_comb.text((tx + dx, ty + dy), txt, fill=(0, 0, 0, 255), font=font)
                
    draw_comb.text((tx, ty), txt, fill=(255, 255, 255, 255), font=font)
    
    return combined

def get_help_embed(category: str, bot_user=None) -> discord.Embed:
    if category == "General & Utility":
        embed = discord.Embed(
            title="🛠️ General & Utility",
            description="*Utility commands for checking info, search, or status.*",
            color=discord.Color.from_rgb(180, 160, 220)
        )
        embed.add_field(
            name="🔍 Search & Lookup",
            value="• `$anime [query]` ─ Search MyAnimeList for anime details.\n"
                  "• `$manga [query]` ─ Search MyAnimeList for manga details.\n"
                  "• `$movie` / `$show` `[query]` ─ Search IMDb details.\n"
                  "• `$avatar` / `$av` `[@user]` ─ Retrieve a user's avatar.\n"
                  "• `$tl` / `$translate` `[lang] [text] / [reply]` ─ Translate text/replied message.",
            inline=False
        )
        embed.add_field(
            name="💖 Love & Connection",
            value="• `$ship [@user]` ─ Calculate compatibility and generate a cute card.\n"
                  "• `$afk [reason]` ─ Set away status (clears when you speak again).",
            inline=False
        )
        embed.add_field(
            name="⚡ System Stats",
            value="• `$ping` ─ Check bot response latency.\n"
                  "• `$uptime` ─ See how long the bot has been awake.\n"
                  "• `$server` ─ Display Discord server details.",
            inline=False
        )
    elif category == "Interactive Utilities":
        embed = discord.Embed(
            title="⚙️ Interactive Utilities",
            description="*Helpful utility tools for managing server activities.*",
            color=discord.Color.from_rgb(175, 238, 238)
        )
        embed.add_field(
            name="🌤️ Weather Lookup",
            value="• `$weather [city]` ─ Fetch real-time weather information.",
            inline=False
        )
        embed.add_field(
            name="📊 Server Polls",
            value="• `$poll [question] | [opt1] | [opt2] | ...` ─ Create a reaction poll (up to 10 options).",
            inline=False
        )
    elif category == "Interactive & Fun":
        embed = discord.Embed(
            title="🎭 Interactive & Fun",
            description="*Bored? Use these commands to have fun, play games, or make memes.*",
            color=discord.Color.from_rgb(255, 182, 193)
        )
        embed.add_field(
            name="🎮 Games & AI",
            value="• `$akinator` ─ Play Akinator directly in chat using buttons.\n"
                  "• `$choose [opt1 | opt2]` ─ Let Reze choose between options.\n"
                  "• `$quote [@user] [text] / [reply]` ─ Generate a premium cinematic quote card.\n"
                  "• `$truth` / `$dare` ─ Play a game of Truth or Dare powered by AI (Llama 3.3).\n"
                  "• `$wyr` / `$wouldyourather` ─ Play a game of Would You Rather with voting.",
            inline=False
        )
        embed.add_field(
            name="🎨 Meme Generation",
            value="• `$wanted` / `$bounty` `[@user]` ─ Generate a One Piece wanted poster.\n"
                  "• `$jail [@user]` ─ Lock up a user behind photorealistic bars.\n"
                  "• `$rip [@user] [reason]` ─ Mourn a user with a customized gravestone meme.\n"
                  "• `$gandhi` / `$fakequote` `[text]` ─ Generate a fake quote on a Gandhi background banner.\n"
                  "• `$simp [@user]` ─ Issue an official Certified Simp Card.\n"
                  "• `$wasted [@user]` ─ Apply a GTA-style sepia red Wasted overlay.\n"
                  "• `$trashcan` / `$trash` `[@user]` ─ Put a user's avatar in the trashcan.",
            inline=False
        )
        embed.add_field(
            name="🌈 Fun Ratings & Actions",
            value="• `$gay` / `$lesbian` `[@user]` ─ Check gayness/lesbianness percentage.\n"
                  "• `$impersonate [@user] [text]` ─ Mimic someone with temporary webhooks.\n"
                  "• `$waifu` / `$husbando` ─ Fetch random Anime characters (with voting!).\n"
                  "• `$mkkf` / `$mkkm` ─ Play Marry, Kiss, Kill with female/male characters.\n"
                  "• `$villain` ─ Pit two iconic anime villains against each other!\n"
                  "• `$cat` / `$dog` ─ Fetch cute random animal pictures.\n"
                  "• `$confess [text]` ─ Submit anonymous confession (DM only).",
            inline=False
        )
    elif category == "Affection Actions":
        embed = discord.Embed(
            title="💖 Affection Actions",
            description="*Express affection to other members or random targets.*",
            color=discord.Color.from_rgb(255, 182, 193)
        )
        embed.add_field(
            name="💡 Usage",
            value="Use `$[action] [@user]` or `$[action] random` to trigger the emote.",
            inline=False
        )
        embed.add_field(
            name="🎬 Available Actions",
            value="`pat` • `hug` • `kiss` • `cuddle` • `handhold` • `feed` • `tickle` • `highfive` • `cheer` • `wink` • `smile` • `happy` • `blowkiss`",
            inline=False
        )
    elif category == "Expression Actions":
        embed = discord.Embed(
            title="✨ Expression Actions",
            description="*Express how you are feeling to the chat.*",
            color=discord.Color.from_rgb(224, 187, 228)
        )
        embed.add_field(
            name="💡 Usage",
            value="Use `$[action] [@user]` or `$[action] random` to trigger the emote.",
            inline=False
        )
        embed.add_field(
            name="🎬 Available Actions",
            value="`blush` • `cry` • `cringe` • `confused` • `facepalm` • `bored` • `tired` • `sleep` • `sad` • `scream` • `panic` • `yawn` • `surprised` • `chase` • `run` • `handshake` • `tailwhip` • `nope`",
            inline=False
        )
    elif category == "Playful Actions":
        embed = discord.Embed(
            title="🐱 Playful Actions",
            description="*Fun and playful interactions with friends.*",
            color=discord.Color.from_rgb(255, 223, 186)
        )
        embed.add_field(
            name="💡 Usage",
            value="Use `$[action] [@user]` or `$[action] random` to trigger the emote.",
            inline=False
        )
        embed.add_field(
            name="🎬 Available Actions",
            value="`poke` • `nom` • `lick` • `bleh` • `wave` • `dance` • `smug` • `shrug` • `nod` • `thumbsup` • `lurk` • `think` • `stare`",
            inline=False
        )
    elif category == "Chaos Actions":
        embed = discord.Embed(
            title="💥 Chaos Actions",
            description="*Bring some chaotic energy to the chat.*",
            color=discord.Color.from_rgb(255, 105, 97)
        )
        embed.add_field(
            name="💡 Usage",
            value="Use `$[action] [@user]` or `$[action] random` to trigger the emote.",
            inline=False
        )
        embed.add_field(
            name="🎬 Available Actions",
            value="`slap` • `punch` • `kick` • `yeet` • `bite` • `bonk` • `threaten` • `angry` • `baka`",
            inline=False
        )
    elif category == "Server & Settings":
        embed = discord.Embed(
            title="⚙️ Server & Settings",
            description="*Configure and setup Reze's settings for this guild.*",
            color=discord.Color.from_rgb(175, 238, 238)
        )
        embed.add_field(
            name="🛠️ Server Setup",
            value="• `/setup` ─ Interactively configure channels and roles.\n"
                  "• `/resetconfig` ─ Reset all configuration data for this server.",
            inline=False
        )
        embed.add_field(
            name="🏷️ Config Assignments",
            value="• `/setrole [type] [role]` ─ Manually assign gender, hinglish, or nsfw roles.\n"
                  "• `/setchannel [type] [channel]` ─ Assign bot-specific channels.\n"
                  "• `/nsfw` ─ Toggle NSFW channels (DM only).",
            inline=False
        )
    elif category == "E-Family System":
        embed = discord.Embed(
            title="👪 E-Family System",
            description="*Establish marriages, adopt children, and build your virtual family tree.*",
            color=discord.Color.from_rgb(255, 215, 0)
        )
        embed.add_field(
            name="💍 Marriage & Adoption",
            value="• `$marry [@user]` ─ Propose marriage to another member.\n"
                  "• `$adopt [@user]` ─ Propose to adopt a member as your child.",
            inline=False
        )
        embed.add_field(
            name="💔 Family Drama",
            value="• `$divorce` ─ Divorce your current spouse.\n"
                  "• `$disown [@user]` ─ Disown one of your children.\n"
                  "• `$abandon [@user]` ─ Abandon one of your parents.\n"
                  "• `$runaway` ─ Run away from home (removes parents).\n"
                  "• `$disownall` ─ Disown all children at once.",
            inline=False
        )
        embed.add_field(
            name="🌳 Family Trees",
            value="• `$family [@user]` ─ Display the family tree of a user.",
            inline=False
        )
    else:
        embed = discord.Embed(
            title="🎭 Reze Command Menu",
            description="*\"Obviously, I'm the best bot in this server. Here's a breakdown of my commands and features. Try not to break anything or spam me, or I'll literally put you on my ignore list... 🙄\"*",
            color=discord.Color.from_rgb(212, 175, 230)
        )
        embed.add_field(
            name="💬 Chatting & Personality",
            value="• **Interact**: Mention me or reply to my messages to start a chat.\n"
                  "• **Mention**: If you say my name (`reze`) in chat, I might react.\n"
                  "• **Dynamic Moods**: Normal, Lazy, Yapping, Annoyed, Bored, Hungry, Distracted, or Drunk!",
            inline=False
        )
        embed.add_field(
            name="📖 Command Categories",
            value="🛠️ `General & Utility` ─ Basic lookup, shipping, search, uptime.\n"
                  "⚙️ `Interactive Utilities` ─ Weather, polling.\n"
                  "🎭 `Interactive & Fun` ─ Games, Wanted posters, Gandhi quotes, Jail, Akinator, and RIP cards.\n"
                  "👪 `E-Family System` ─ Marry, adopt, divorce, family trees.\n"
                  "👉 `Emotes & Actions` ─ Affection, Expression, Playful, and Chaos action emojis.",
            inline=False
        )
        if bot_user and bot_user.avatar:
            embed.set_thumbnail(url=bot_user.avatar.url)
            
    embed.set_footer(text="Select another category in the dropdown to navigate! 💖")
    return embed

class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Landing Page", description="Back to main welcome page", emoji="🏠"),
            discord.SelectOption(label="General & Utility", description="Basic commands, shipping, search, etc.", emoji="🛠️"),
            discord.SelectOption(label="Interactive Utilities", description="Weather, polls, etc.", emoji="⚙️"),
            discord.SelectOption(label="Interactive & Fun", description="Confess, choose, waifu, quotes...", emoji="🎭"),
            discord.SelectOption(label="E-Family System", description="Marry, adopt, divorce, disown, family tree...", emoji="👪"),
            discord.SelectOption(label="Affection Actions", description="Pat, hug, kiss, cuddle...", emoji="💖"),
            discord.SelectOption(label="Expression Actions", description="Blush, cry, yawn, sleep...", emoji="✨"),
            discord.SelectOption(label="Playful Actions", description="Poke, dance, smug, bleh...", emoji="🐱"),
            discord.SelectOption(label="Chaos Actions", description="Slap, yeet, punch, bonk...", emoji="💥"),
            discord.SelectOption(label="Server & Settings", description="Slash commands for configuration.", emoji="⚙️")
        ]
        super().__init__(placeholder="Select a category to view commands...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        embed = get_help_embed(self.values[0], interaction.client.user)
        await interaction.response.edit_message(embed=embed)

class HelpView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=60.0)
        self.author = author
        self.add_item(HelpSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("this menu isn't for you, dummy 🙄", ephemeral=True)
            return False
        return True

class ProposalView(discord.ui.View):
    def __init__(self, author, target, proposal_type):
        super().__init__(timeout=60.0)
        self.author = author
        self.target = target
        self.proposal_type = proposal_type
        self.accepted = None

    @discord.ui.button(label="Accept 💖", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("this proposal isn't for you, dummy 🙄", ephemeral=True)
            return
        self.accepted = True
        self.stop()
        await interaction.response.edit_message(view=None)

    @discord.ui.button(label="Decline 💔", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("this proposal isn't for you, dummy 🙄", ephemeral=True)
            return
        self.accepted = False
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_timeout(self):
        self.accepted = False


class SmashPassView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180.0)
        self.smashers = set()
        self.passers = set()
        self.message = None

    @discord.ui.button(label="Smash 🔥 (0)", style=discord.ButtonStyle.success)
    async def smash(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        if user_id in self.smashers:
            self.smashers.remove(user_id)
        else:
            self.smashers.add(user_id)
            self.passers.discard(user_id)
        
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Pass 💤 (0)", style=discord.ButtonStyle.danger)
    async def pass_char(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        if user_id in self.passers:
            self.passers.remove(user_id)
        else:
            self.passers.add(user_id)
            self.smashers.discard(user_id)
            
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    def update_buttons(self):
        self.children[0].label = f"Smash 🔥 ({len(self.smashers)})"
        self.children[1].label = f"Pass 💤 ({len(self.passers)})"

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


class WaifuVotingView(discord.ui.View):
    def __init__(self, char1_name, char1_anime, char2_name, char2_anime, command_type, author_mention):
        super().__init__(timeout=180.0)
        self.char1_name = char1_name
        self.char1_anime = char1_anime
        self.char2_name = char2_name
        self.char2_anime = char2_anime
        self.command_type = command_type
        self.author_mention = author_mention
        self.votes_a = set()
        self.votes_b = set()
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.children[0].label = f"{len(self.votes_a)}"
        self.children[1].label = f"{len(self.votes_b)}"

    def get_voting_embed(self):
        total = len(self.votes_a) + len(self.votes_b)
        if total == 0:
            pct_a = 50
            pct_b = 50
        else:
            pct_a = round((len(self.votes_a) / total) * 100)
            pct_b = 100 - pct_a

        embed = discord.Embed(
            title=f"Who's the better {self.command_type}?",
            color=discord.Color.from_rgb(255, 182, 193)
        )
        embed.description = (
            f"🇦 **{self.char1_name} [{pct_a} %]**\n"
            f"*from {self.char1_anime}*\n\n"
            f"🇧 **{self.char2_name} [{pct_b} %]**\n"
            f"*from {self.char2_anime}*\n\n"
            f"• {self.author_mention}"
        )
        embed.set_footer(text="Powered by AniList | Reze bot")
        if self.message and self.message.embeds:
            old_embed = self.message.embeds[0]
            if old_embed.image and old_embed.image.url:
                embed.set_image(url=old_embed.image.url)
        return embed

    @discord.ui.button(emoji="🇦", style=discord.ButtonStyle.secondary)
    async def vote_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        u_id = interaction.user.id
        if u_id in self.votes_a:
            self.votes_a.remove(u_id)
        else:
            self.votes_a.add(u_id)
            self.votes_b.discard(u_id)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_voting_embed(), view=self)

    @discord.ui.button(emoji="🇧", style=discord.ButtonStyle.secondary)
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        u_id = interaction.user.id
        if u_id in self.votes_b:
            self.votes_b.remove(u_id)
        else:
            self.votes_b.add(u_id)
            self.votes_a.discard(u_id)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_voting_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass




async def fetch_free_proxies():
    urls = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=all&ssl=yes&anonymity=anonymous",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"
    ]
    proxies = set()
    try:
        async with aiohttp.ClientSession() as session:
            for url in urls:
                try:
                    async with session.get(url, timeout=5) as r:
                        if r.status == 200:
                            text = await r.text()
                            for line in text.split("\n"):
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    if ":" in line:
                                        proxies.add(line)
                except Exception as url_err:
                    logger.warning(f"Failed to fetch proxies from {url}: {url_err}")
    except Exception as e:
        logger.warning(f"Failed to fetch proxies pool: {e}")
    return list(proxies)


class AkinatorView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=90.0)
        self.author = author
        self.aki = AsyncAkinator()
        self.message = None
        # Find and disable the Undo button initially
        for child in self.children:
            if child.label == "Undo":
                child.disabled = True
        
    async def start(self):
        try:
            # First attempt: direct connection
            await self.aki.start_game()
            return self.aki.question
        except Exception as direct_err:
            logger.warning(f"Direct connection to Akinator failed: {direct_err}. Attempting concurrent proxy fallback...")
            
            # Fetch free proxies
            proxies = await fetch_free_proxies()
            if not proxies:
                raise direct_err
                
            # Randomize proxy selection
            import random
            random.shuffle(proxies)
            
            # Dynamically adjust default executor pool limit to prevent thread queuing
            import concurrent.futures
            try:
                loop = asyncio.get_running_loop()
                loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=100))
            except Exception as thread_err:
                logger.warning(f"Could not adjust default threadpool executor: {thread_err}")
            
            # Helper to try game start for a single proxy
            async def try_proxy(proxy_str):
                proxy_url = f"http://{proxy_str}"
                aki = AsyncAkinator()
                aki.session.scraper.proxies = {
                    "http": proxy_url,
                    "https": proxy_url
                }
                aki.session.scraper.timeout = 5
                await aki.start_game()
                return aki, proxy_str

            # Launch up to 40 proxy test tasks concurrently
            test_pool = proxies[:40]
            tasks = [asyncio.create_task(try_proxy(p)) for p in test_pool]
            
            completed_aki = None
            successful_proxy = None
            
            for future in asyncio.as_completed(tasks):
                try:
                    aki_instance, proxy_str = await future
                    completed_aki = aki_instance
                    successful_proxy = proxy_str
                    logger.info(f"Successfully started Akinator game using proxy: {proxy_str}")
                    break  # Found a working one, stop waiting
                except Exception:
                    pass  # Ignore failing proxies
            
            # Cancel all other pending tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            
            if completed_aki:
                self.aki = completed_aki
                return self.aki.question
            
            # If all proxies fail, raise the original direct connection error
            raise direct_err

    async def process_answer(self, interaction: discord.Interaction, answer_val):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("this is not your game, dummy 🙄", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        try:
            if answer_val == "back":
                await self.aki.back()
            else:
                await self.aki.answer(answer_val)
                
            # Check if game is finished (victory or defeat)
            if self.aki.finished:
                if self.aki.win:
                    embed = discord.Embed(
                        title="🔮 Akinator Wins! 🔮",
                        description=f"I guessed it! It was **{self.aki.name_proposition}**!\n\n*{self.aki.question}*",
                        color=discord.Color.from_rgb(212, 175, 230)
                    )
                    if self.aki.photo:
                        embed.set_image(url=self.aki.photo)
                else:
                    embed = discord.Embed(
                        title="🔮 You Defeated Akinator! 🔮",
                        description=self.aki.question,
                        color=discord.Color.from_rgb(212, 175, 230)
                    )
                
                for child in self.children:
                    child.disabled = True
                await interaction.followup.edit_message(message_id=self.message.id, embed=embed, view=self)
                self.stop()
                return

            # Check if Akinator is making a guess
            if self.aki.win:
                embed = discord.Embed(
                    title="🔮 Akinator's Guess! 🔮",
                    description=f"Is it **{self.aki.name_proposition}**?\n*{self.aki.description_proposition}*",
                    color=discord.Color.from_rgb(212, 175, 230)
                )
                if self.aki.photo:
                    embed.set_image(url=self.aki.photo)
                
                for child in self.children:
                    if child.label in ["Yes", "No", "Stop"]:
                        child.disabled = False
                    else:
                        child.disabled = True
                
                await interaction.followup.edit_message(message_id=self.message.id, embed=embed, view=self)
                return

            # Otherwise, show next question
            for child in self.children:
                if child.label == "Undo" and self.aki.step == 0:
                    child.disabled = True
                else:
                    child.disabled = False
                    
            embed = discord.Embed(
                title=f"🔮 Akinator (Question {self.aki.step + 1}) 🔮",
                description=f"### {self.aki.question}",
                color=discord.Color.from_rgb(212, 175, 230)
            )
            embed.set_footer(text=f"Progression: {int(self.aki.progression)}% | Playing: {self.author.display_name}")
            await interaction.followup.edit_message(message_id=self.message.id, embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Akinator error: {e}", exc_info=True)
            await interaction.followup.send("something went wrong with the Akinator API 😭", ephemeral=True)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, row=0)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_answer(interaction, "yes")

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger, row=0)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_answer(interaction, "no")

    @discord.ui.button(label="I Don't Know", style=discord.ButtonStyle.secondary, row=0)
    async def idk(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_answer(interaction, "i don't know")

    @discord.ui.button(label="Probably", style=discord.ButtonStyle.primary, row=1)
    async def probably(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_answer(interaction, "probably")

    @discord.ui.button(label="Probably Not", style=discord.ButtonStyle.primary, row=1)
    async def probably_not(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_answer(interaction, "probably not")

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary, row=2)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.aki.step == 0:
            await interaction.response.send_message("you can't go back any further!", ephemeral=True)
            return
        await self.process_answer(interaction, "back")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=2)
    async def stop_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("this is not your game, dummy 🙄", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(
            title="🔮 Akinator Game Stopped 🔮",
            description="The game was stopped by the player.",
            color=discord.Color.from_rgb(120, 120, 120)
        )
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            logger.warning(f"Failed to edit message on stop_game: {e}")
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                embed = discord.Embed(
                    title="🔮 Akinator Timeout 🔮",
                    description="The game has timed out due to inactivity.",
                    color=discord.Color.from_rgb(120, 120, 120)
                )
                await self.message.edit(embed=embed, view=self)
        except Exception:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        # Quietly log any transient interaction or network errors
        logger.warning(f"AkinatorView button '{item.label}' callback error: {error}")


# --- E-FAMILY HELPERS ---

async def get_guild_member_name(guild, user_id: str, bot) -> str:
    import re
    def clean_name(name: str) -> str:
        if not name: return name
        return re.sub(r'\s*\((self|spouse|father|mother|grandparent|parent|sibling|child|grandchild)\)\s*$', '', name, flags=re.IGNORECASE)

    # 1. Try Discord's local cache (instant)
    try:
        uid = int(user_id)
        if guild:
            member = guild.get_member(uid)
            if member:
                return clean_name(member.nick or member.display_name)
        user = bot.get_user(uid)
        if user:
            return clean_name(user.display_name)
    except Exception:
        pass

    # 2. Try Database lookup (very fast cache lookup)
    try:
        user_data = await db.get_user_data(user_id)
        if user_data and user_data.get("display_name"):
            return clean_name(user_data.get("display_name"))
    except Exception:
        pass

    # 3. Only perform API calls as a last resort
    try:
        uid = int(user_id)
        if guild:
            try:
                member = await guild.fetch_member(uid)
                if member:
                    return clean_name(member.nick or member.display_name)
            except Exception:
                pass
        try:
            user = await bot.fetch_user(uid)
            if user:
                return clean_name(user.display_name)
        except Exception:
            pass
    except Exception:
        pass

    return f"Unknown User ({user_id})"

async def generate_family_tree_image(user_id: str, guild, bot) -> bytes:
    # 1. Fetch relations
    self_fam = await db.get_family(user_id)
    self_name = await get_guild_member_name(guild, user_id, bot)
    
    spouse_id = self_fam.get("spouse")
    spouse_name = await get_guild_member_name(guild, spouse_id, bot) if spouse_id else None
    
    parent_ids = self_fam.get("parents", [])
    parents = []
    grandparents = []
    
    parent_to_gp_map = {}
    
    for p_id in parent_ids:
        p_name = await get_guild_member_name(guild, p_id, bot)
        parents.append(p_name)
        
        p_fam = await db.get_family(p_id)
        gp_list = []
        for gp_id in p_fam.get("parents", []):
            gp_name = await get_guild_member_name(guild, gp_id, bot)
            if gp_name not in grandparents:
                grandparents.append(gp_name)
            gp_list.append(gp_name)
        parent_to_gp_map[p_name] = gp_list
                
    sibling_ids = set()
    for p_id in parent_ids:
        p_fam = await db.get_family(p_id)
        for sib_id in p_fam.get("children", []):
            if sib_id != user_id:
                sibling_ids.add(sib_id)
                
    siblings = []
    for sib_id in sibling_ids:
        sib_name = await get_guild_member_name(guild, sib_id, bot)
        siblings.append(sib_name)
        
    children_ids = self_fam.get("children", [])
    children_tree = []
    has_grandchildren = False
    
    for c_id in children_ids:
        c_name = await get_guild_member_name(guild, c_id, bot)
        c_fam = await db.get_family(c_id)
        grandchildren_ids = c_fam.get("children", [])
        grandchildren_names = []
        for gc_id in grandchildren_ids:
            gc_name = await get_guild_member_name(guild, gc_id, bot)
            grandchildren_names.append(gc_name)
            has_grandchildren = True
        children_tree.append({"name": c_name, "grandchildren": grandchildren_names})

    # 2. Layout Elements Setup
    # Grandparents
    grandparents_elements = []
    for p_name, gp_names in parent_to_gp_map.items():
        if len(gp_names) == 2:
            grandparents_elements.append({
                "type": "couple",
                "name1": gp_names[0],
                "name2": gp_names[1],
                "key1": f"gp_{gp_names[0]}",
                "key2": f"gp_{gp_names[1]}"
            })
        else:
            for gp in gp_names:
                grandparents_elements.append({"type": "single", "name": gp, "key": f"gp_{gp}"})
    for gp in grandparents:
        found = False
        for el in grandparents_elements:
            if el["type"] == "single" and el["name"] == gp:
                found = True
            elif el["type"] == "couple" and (el["name1"] == gp or el["name2"] == gp):
                found = True
        if not found:
            grandparents_elements.append({"type": "single", "name": gp, "key": f"gp_{gp}"})

    # Parents
    parents_elements = []
    if len(parents) == 2:
        parents_elements.append({
            "type": "couple",
            "name1": parents[0],
            "name2": parents[1],
            "key1": f"p_{parents[0]}",
            "key2": f"p_{parents[1]}"
        })
    else:
        for p in parents:
            parents_elements.append({"type": "single", "name": p, "key": f"p_{p}"})

    # Generation 1 (Self & Siblings)
    half_sib = len(siblings) // 2
    sib_left = siblings[:half_sib]
    sib_right = siblings[half_sib:]
    
    generation1_elements = []
    for sib in sib_left:
        generation1_elements.append({"type": "single", "name": sib, "key": f"sib_{sib}"})
        
    if spouse_name:
        generation1_elements.append({
            "type": "couple",
            "name1": self_name,
            "name2": spouse_name,
            "key1": "self",
            "key2": "spouse"
        })
    else:
        generation1_elements.append({"type": "single", "name": self_name, "key": "self"})
        
    for sib in sib_right:
        generation1_elements.append({"type": "single", "name": sib, "key": f"sib_{sib}"})

    # Children
    children_elements = []
    for child in children_tree:
        children_elements.append({"type": "single", "name": child["name"], "key": f"c_{child['name']}"})

    # Grandchildren
    grandchildren_elements = []
    for child in children_tree:
        for gc in child["grandchildren"]:
            grandchildren_elements.append({"type": "single", "name": gc, "key": f"gc_{gc}"})

    # 3. Size and Geometry Calculations
    G = 65 # gap between elements
    card_w, card_h = 190, 56
    
    def get_element_width(el):
        return 460 if el["type"] == "couple" else 190
        
    def get_layer_width(elements):
        if not elements: return 0
        return sum(get_element_width(el) for el in elements) + (len(elements) - 1) * G
        
    widths = [
        get_layer_width(grandparents_elements),
        get_layer_width(parents_elements),
        get_layer_width(generation1_elements),
        get_layer_width(children_elements),
        get_layer_width(grandchildren_elements)
    ]
    width = max(1200, max(widths) + 120)
    
    y_coords = {}
    current_y = 120
    
    if grandparents_elements:
        y_coords["grandparents"] = current_y
        current_y += 140
        
    if parents_elements:
        y_coords["parents"] = current_y
        current_y += 140
        
    y_coords["self"] = current_y
    current_y += 140
    
    if children_elements:
        y_coords["children"] = current_y
        current_y += 140
        
    if grandchildren_elements:
        y_coords["grandchildren"] = current_y
        current_y += 140
        
    height = current_y + 40
    
    from PIL import Image, ImageDraw, ImageFont
    
    img = Image.new("RGBA", (width, height), (24, 18, 36, 255))
    draw = ImageDraw.Draw(img)
    
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse([-200, -200, 400, 400], fill=(120, 80, 200, 40))
    glow_draw.ellipse([width-400, height-400, width+200, height+200], fill=(200, 80, 150, 45))
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)
    
    try:
        title_font_path = os.path.join(BASE_DIR, "assets", "fonts", "Roboto-Bold.ttf")
        name_font_path = os.path.join(BASE_DIR, "assets", "fonts", "Roboto-Regular.ttf")
        title_font = ImageFont.truetype(title_font_path, 36)
        name_font = ImageFont.truetype(name_font_path, 15)
    except IOError:
        title_font = ImageFont.load_default()
        name_font = ImageFont.load_default()
        
    title_text = f"{self_name}'s Family Tree"
    try:
        t_w = title_font.getbbox(title_text)[2] - title_font.getbbox(title_text)[0]
    except Exception:
        t_w = draw.textlength(title_text, font=title_font)
    draw.text(((width - t_w)//2, 35), title_text, fill=(255, 215, 0, 255), font=title_font)

    # 4. Lay out the coordinates
    node_coords = {}
    
    def layout_layer(elements, y):
        if not elements: return
        layer_w = get_layer_width(elements)
        start_x = (width - layer_w) / 2
        current_x = start_x
        for el in elements:
            el_w = get_element_width(el)
            if el["type"] == "single":
                cx = int(current_x + el_w / 2)
                node_coords[el["key"]] = (cx, y)
            elif el["type"] == "couple":
                cx1 = int(current_x + 190 / 2)
                cx2 = int(current_x + 190 + 80 + 190 / 2)
                node_coords[el["key1"]] = (cx1, y)
                node_coords[el["key2"]] = (cx2, y)
            current_x += el_w + G
            
    if grandparents_elements:
        layout_layer(grandparents_elements, y_coords["grandparents"])
    if parents_elements:
        layout_layer(parents_elements, y_coords["parents"])
        
    layout_layer(generation1_elements, y_coords["self"])
    
    if children_elements:
        layout_layer(children_elements, y_coords["children"])
    if grandchildren_elements:
        layout_layer(grandchildren_elements, y_coords["grandchildren"])

    # 5. Connection Lines (Bus layout to prevent crossing through cards)
    def draw_bus_connection(src_point, target_keys, target_y, bus_y_offset=0):
        target_xs = [node_coords[k][0] for k in target_keys if k in node_coords]
        if not target_xs: return
        
        min_x = min(target_xs)
        max_x = max(target_xs)
        
        span_min_x = min(src_point[0], min_x)
        span_max_x = max(src_point[0], max_x)
        
        bus_y = (src_point[1] + target_y) // 2 + bus_y_offset
        
        # Vertical down from source to bus line
        draw.line([src_point[0], src_point[1], src_point[0], bus_y], fill=(100, 100, 180, 150), width=3)
        # Horizontal bus line
        draw.line([span_min_x, bus_y, span_max_x, bus_y], fill=(100, 100, 180, 150), width=3)
        # Vertical down from bus line to each target
        for tx in target_xs:
            draw.line([tx, bus_y, tx, target_y], fill=(100, 100, 180, 150), width=3)

    # Draw couple connection lines
    for el in grandparents_elements:
        if el["type"] == "couple":
            x1 = node_coords[el["key1"]][0]
            x2 = node_coords[el["key2"]][0]
            y = y_coords["grandparents"]
            draw.line([x1 + card_w // 2, y, x2 - card_w // 2, y], fill=(100, 100, 180, 150), width=3)
            
    for el in parents_elements:
        if el["type"] == "couple":
            x1 = node_coords[el["key1"]][0]
            x2 = node_coords[el["key2"]][0]
            y = y_coords["parents"]
            draw.line([x1 + card_w // 2, y, x2 - card_w // 2, y], fill=(100, 100, 180, 150), width=3)
            
    if spouse_name:
        x1 = node_coords["self"][0]
        x2 = node_coords["spouse"][0]
        y = y_coords["self"]
        draw.line([x1 + card_w // 2, y, x2 - card_w // 2, y], fill=(255, 105, 180, 150), width=3)

    # Grandparents to Parents
    for p_name, gp_names in parent_to_gp_map.items():
        parent_key = f"p_{p_name}"
        if parent_key in node_coords:
            couple_el = None
            for el in grandparents_elements:
                if el["type"] == "couple" and el["name1"] in gp_names and el["name2"] in gp_names:
                    couple_el = el
                    break
            if couple_el:
                x1 = node_coords[couple_el["key1"]][0]
                x2 = node_coords[couple_el["key2"]][0]
                gp_center = ((x1 + x2) // 2, y_coords["grandparents"])
                mid_y = (gp_center[1] + y_coords["parents"]) // 2
                draw.line([gp_center[0], gp_center[1], gp_center[0], mid_y], fill=(100, 100, 180, 150), width=3)
                draw.line([gp_center[0], mid_y, node_coords[parent_key][0], mid_y], fill=(100, 100, 180, 150), width=3)
                draw.line([node_coords[parent_key][0], mid_y, node_coords[parent_key][0], y_coords["parents"]], fill=(100, 100, 180, 150), width=3)
            else:
                for gp in gp_names:
                    gp_key = f"gp_{gp}"
                    if gp_key in node_coords:
                        x1, y1 = node_coords[gp_key]
                        x2, y2 = node_coords[parent_key]
                        mid_y = (y1 + y2) // 2
                        draw.line([x1, y1, x1, mid_y], fill=(100, 100, 180, 150), width=3)
                        draw.line([x1, mid_y, x2, mid_y], fill=(100, 100, 180, 150), width=3)
                        draw.line([x2, mid_y, x2, y2], fill=(100, 100, 180, 150), width=3)

    # Parents to Self & Siblings
    if parents_elements:
        if parents_elements[0]["type"] == "couple":
            el = parents_elements[0]
            x1 = node_coords[el["key1"]][0]
            x2 = node_coords[el["key2"]][0]
            src_point = ((x1 + x2) // 2, y_coords["parents"])
        else:
            p_name = parents[0]
            src_point = node_coords[f"p_{p_name}"]
            
        target_keys = ["self"] + [f"sib_{sib}" for sib in siblings]
        draw_bus_connection(src_point, target_keys, y_coords["self"])

    # Self & Spouse to Children
    if children_elements:
        if spouse_name:
            x1 = node_coords["self"][0]
            x2 = node_coords["spouse"][0]
            src_point = ((x1 + x2) // 2, y_coords["self"])
        else:
            src_point = node_coords["self"]
            
        target_keys = [f"c_{child['name']}" for child in children_tree]
        draw_bus_connection(src_point, target_keys, y_coords["children"])

    # Children to Grandchildren
    if grandchildren_elements:
        parent_children = [c for c in children_tree if c["grandchildren"]]
        for idx, child in enumerate(parent_children):
            c_name = child["name"]
            c_key = f"c_{c_name}"
            if c_key in node_coords:
                target_gcs = [f"gc_{gc}" for gc in child["grandchildren"]]
                offset = (idx - (len(parent_children) - 1) / 2) * 16
                draw_bus_connection(node_coords[c_key], target_gcs, y_coords["grandchildren"], bus_y_offset=int(offset))

    # 6. Draw Cards
    def draw_card(cx, cy, name, is_self=False, is_spouse=False):
        left = cx - card_w // 2
        top = cy - card_h // 2
        right = left + card_w
        bottom = top + card_h
        
        bg_color = (138, 43, 226, 70) if is_self else ((219, 112, 147, 70) if is_spouse else (40, 40, 70, 200))
        border_color = (255, 215, 0, 240) if is_self else ((255, 105, 180, 220) if is_spouse else (100, 100, 180, 255))
        
        draw.rounded_rectangle([left, top, right, bottom], radius=10, fill=bg_color, outline=border_color, width=2)
        draw.text((cx, cy), name, fill=(255, 255, 255, 255), font=name_font, anchor="mm")

    for key, (cx, cy) in node_coords.items():
        if key == "self":
            draw_card(cx, cy, self_name, is_self=True)
        elif key == "spouse":
            draw_card(cx, cy, spouse_name, is_spouse=True)
        else:
            clean_name = ""
            if key.startswith("gp_"): clean_name = key[3:]
            elif key.startswith("p_"): clean_name = key[2:]
            elif key.startswith("sib_"): clean_name = key[4:]
            elif key.startswith("c_"): clean_name = key[2:]
            elif key.startswith("gc_"): clean_name = key[3:]
            draw_card(cx, cy, clean_name)
            
    if spouse_name and "self" in node_coords and "spouse" in node_coords:
        x1 = node_coords["self"][0]
        x2 = node_coords["spouse"][0]
        y = y_coords["self"]
        draw.text(((x1 + x2) // 2, y), "💖", fill=(255, 20, 147, 255), font=name_font, anchor="mm")

    import io
    output_buffer = io.BytesIO()
    img.save(output_buffer, format="PNG")
    output_buffer.seek(0)
    return output_buffer.getvalue()


# --- E-FAMILY HELPERS ---

async def get_user_name(user_id: str, bot) -> str:
    try:
        uid = int(user_id)
        user = bot.get_user(uid)
        if not user:
            user = await bot.fetch_user(uid)
        return user.display_name
    except Exception:
        user_data = await db.get_user_data(user_id)
        if user_data and user_data.get("display_name"):
            return user_data.get("display_name")
        return f"Unknown User ({user_id})"

async def is_ancestor(user_id: str, potential_ancestor_id: str) -> bool:
    """Check if potential_ancestor_id is a parent, grandparent, etc. of user_id"""
    visited = set()
    queue = [user_id]
    while queue:
        curr = queue.pop(0)
        if curr == potential_ancestor_id:
            return True
        if curr in visited:
            continue
        visited.add(curr)
        
        fam = await db.get_family(curr)
        parents = fam.get("parents", [])
        for p in parents:
            if p not in visited:
                queue.append(p)
    return False

async def get_family_tree_text(user_id: str, bot) -> str:
    self_fam = await db.get_family(user_id)
    self_name = await get_user_name(user_id, bot)
    
    spouse_id = self_fam.get("spouse")
    spouse_name = await get_user_name(spouse_id, bot) if spouse_id else None
    
    parent_ids = self_fam.get("parents", [])
    parents_info = []
    grandparents_info = []
    
    for p_id in parent_ids:
        p_name = await get_user_name(p_id, bot)
        parents_info.append((p_id, p_name))
        
        p_fam = await db.get_family(p_id)
        for gp_id in p_fam.get("parents", []):
            gp_name = await get_user_name(gp_id, bot)
            if gp_id not in [g[0] for g in grandparents_info]:
                grandparents_info.append((gp_id, gp_name))
                
    sibling_ids = set()
    for p_id in parent_ids:
        p_fam = await db.get_family(p_id)
        for sib_id in p_fam.get("children", []):
            if sib_id != user_id:
                sibling_ids.add(sib_id)
                
    siblings_info = []
    for sib_id in sibling_ids:
        sib_name = await get_user_name(sib_id, bot)
        siblings_info.append(sib_name)
        
    children_ids = self_fam.get("children", [])
    children_tree = []
    
    for c_id in children_ids:
        c_name = await get_user_name(c_id, bot)
        c_fam = await db.get_family(c_id)
        grandchildren_ids = c_fam.get("children", [])
        grandchildren_names = []
        for gc_id in grandchildren_ids:
            gc_name = await get_user_name(gc_id, bot)
            grandchildren_names.append(gc_name)
        children_tree.append((c_name, grandchildren_names))
        
    lines = []
    
    # Grandparents
    if grandparents_info:
        lines.append("👵👴 Grandparents")
        for i, (gp_id, gp_name) in enumerate(grandparents_info):
            char = "└──" if i == len(grandparents_info) - 1 else "├──"
            lines.append(f"  {char} {gp_name}")
        lines.append("")
        
    # Parents
    if parents_info:
        lines.append("👨‍👩‍👦 Parents")
        for i, (p_id, p_name) in enumerate(parents_info):
            char = "└──" if i == len(parents_info) - 1 else "├──"
            lines.append(f"  {char} {p_name}")
        lines.append("")
        
    # Siblings
    if siblings_info:
        lines.append("👦👧 Siblings")
        for i, sib_name in enumerate(siblings_info):
            char = "└──" if i == len(siblings_info) - 1 else "├──"
            lines.append(f"  {char} {sib_name}")
        lines.append("")
        
    # Self & Spouse
    lines.append("💑 Self & Partner")
    self_spouse_str = f"{self_name} (Self)"
    if spouse_name:
        self_spouse_str += f" 💖 {spouse_name} (Spouse)"
    lines.append(f"  └── {self_spouse_str}")
    
    # Children & Grandchildren
    if children_tree:
        for i, (c_name, gc_list) in enumerate(children_tree):
            c_char = "└──" if i == len(children_tree) - 1 else "├──"
            lines.append(f"      {c_char} 👶 {c_name}")
            if gc_list:
                for j, gc_name in enumerate(gc_list):
                    gc_char = "└──" if j == len(gc_list) - 1 else "├──"
                    prefix = "          " if i == len(children_tree) - 1 else "      │   "
                    lines.append(f"{prefix}{gc_char} 🍼 {gc_name}")
                    
    return "\n".join(lines)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id
    current_time = time.time()

    # Clear AFK status if user speaks
    if user_id in afk_users:
        data = afk_users.pop(user_id)
        duration = current_time - data["time"]
        m, s = divmod(int(duration), 60)
        h, m = divmod(m, 60)
        time_str = f"{s}s"
        if m > 0: time_str = f"{m}m {time_str}"
        if h > 0: time_str = f"{h}h {time_str}"
        
        sent_msg = await message.channel.send(f"wb **{message.author.display_name}**! I've cleared your AFK status. You were gone for **{time_str}**.")
        if sent_msg:
            command_output_message_ids.add(sent_msg.id)

    # Check if mentioned users are AFK
    for mention in message.mentions:
        if mention.id in afk_users and mention.id != user_id:
            data = afk_users[mention.id]
            duration = current_time - data["time"]
            m, s = divmod(int(duration), 60)
            h, m = divmod(m, 60)
            time_str = f"{s}s"
            if m > 0: time_str = f"{m}m {time_str}"
            if h > 0: time_str = f"{h}h {time_str}"
            
            sent_msg = await message.reply(f"**{mention.display_name}** is currently AFK: **{data['reason']}** (since {time_str} ago) 💤")
            if sent_msg:
                command_output_message_ids.add(sent_msg.id)

    # --- PREFIX COMMAND HANDLER ---
    if message.content.startswith('$'):
        # Track command outputs to prevent replying to them later
        class TrackedChannel:
            def __init__(self, orig_channel):
                self._orig_channel = orig_channel
            def __getattr__(self, name):
                return getattr(self._orig_channel, name)
            async def send(self, *args, **kwargs):
                msg = await self._orig_channel.send(*args, **kwargs)
                if msg:
                    command_output_message_ids.add(msg.id)
                return msg

        class TrackedMessage:
            def __init__(self, orig_msg):
                self._orig_msg = orig_msg
                self.channel = TrackedChannel(orig_msg.channel)
            def __getattr__(self, name):
                return getattr(self._orig_msg, name)
            def __hash__(self):
                return hash(self._orig_msg)
            def __eq__(self, other):
                if isinstance(other, TrackedMessage):
                    return self._orig_msg == other._orig_msg
                return self._orig_msg == other
            def __str__(self):
                return str(self._orig_msg)
            def __repr__(self):
                return repr(self._orig_msg)
            async def reply(self, *args, **kwargs):
                msg = await self._orig_msg.reply(*args, **kwargs)
                if msg:
                    command_output_message_ids.add(msg.id)
                return msg

        message = TrackedMessage(message)
        # Rate limit check per user for prefix commands (max 3 commands per 10s)
        if user_id not in user_command_timestamps:
            user_command_timestamps[user_id] = []
        user_command_timestamps[user_id] = [t for t in user_command_timestamps[user_id] if current_time - t < 10]
        if len(user_command_timestamps[user_id]) >= 3:
            await message.reply("chill, stop spamming commands so fast 🙄", delete_after=5)
            return
        user_command_timestamps[user_id].append(current_time)

        content = message.content[1:].strip()
        if content:
            parts = content.split(None, 1)
            command = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            # Command-specific or default 3s cooldown check (skip help)
            if command != "help":
                cooldown_duration = COOLDOWN_TIMES.get(command, 3)
                if command not in command_cooldown_timestamps:
                    command_cooldown_timestamps[command] = {}
                
                last_used = command_cooldown_timestamps[command].get(user_id, 0)
                time_passed = current_time - last_used
                if time_passed < cooldown_duration:
                    time_remaining = int(cooldown_duration - time_passed)
                    if time_remaining >= 60:
                        m, s = divmod(time_remaining, 60)
                        rem_str = f"{m}m {s}s"
                    else:
                        rem_str = f"{time_remaining}s"
                        
                    if cooldown_duration >= 60:
                        dur_str = f"{cooldown_duration // 60}m"
                    else:
                        dur_str = f"{cooldown_duration}s"
                        
                    await message.reply(f"chill, you can only use `${command}` once every {dur_str}. Wait **{rem_str}**! 🙄", delete_after=5)
                    return
                # Update cooldown timestamp
                command_cooldown_timestamps[command][user_id] = current_time

            if command == "help":
                embed = get_help_embed("Landing Page", bot.user)
                view = HelpView(message.author)
                await message.reply(embed=embed, view=view)
                return

            elif command == "afk":
                reason = args.strip() if args else "AFK"
                afk_users[user_id] = {
                    "reason": reason,
                    "time": current_time,
                    "name": message.author.display_name
                }
                await message.reply(f"i've set your status to AFK: **{reason}**. talk later I guess 🙄")
                return

            elif command == "ship":
                user1 = None
                user2 = None
                
                if len(message.mentions) >= 2:
                    user1 = message.mentions[0]
                    user2 = message.mentions[1]
                elif len(message.mentions) == 1:
                    user1 = message.author
                    user2 = message.mentions[0]
                elif args:
                    text = args.strip()
                    delimiters = [" x ", " and ", " & ", "  "]
                    parts = None
                    for delim in delimiters:
                        if delim in text.lower():
                            parts = text.lower().split(delim, 1)
                            break
                    if not parts:
                        temp_parts = text.split(None, 1)
                        if len(temp_parts) == 2:
                            parts = temp_parts
                            
                    if parts and len(parts) == 2:
                        name1, name2 = parts[0].strip().lower(), parts[1].strip().lower()
                        if message.guild:
                            for m in message.guild.members:
                                if not user1 and (name1 in m.display_name.lower() or name1 in m.name.lower()):
                                    user1 = m
                                elif not user2 and (name2 in m.display_name.lower() or name2 in m.name.lower()):
                                    user2 = m
                                if user1 and user2:
                                    break
                                    
                    # Fallback if two names couldn't be parsed/found: treat entire args as single target user2, and author as user1
                    if not (user1 and user2):
                        user1 = message.author
                        target_name = args.strip().lower()
                        if message.guild:
                            for m in message.guild.members:
                                if target_name in m.display_name.lower() or target_name in m.name.lower():
                                    user2 = m
                                    break
                
                # Final fallbacks
                if not user1:
                    user1 = message.author
                    
                if not user2:
                    if message.guild:
                        candidates = [m for m in message.guild.members if not m.bot and m.id != user1.id]
                        if candidates:
                            user2 = random.choice(candidates)
                            
                if not user2:
                    await message.reply("you need to mention someone or run this in a server with members, dummy 🙄")
                    return
                
                async with message.channel.typing():
                    try:
                        percent = random.randint(0, 100)

                        # Fetch avatars
                        async with aiohttp.ClientSession() as session:
                            async with session.get(user1.display_avatar.with_format("png").url) as r1:
                                pfp1_data = await r1.read()
                            async with session.get(user2.display_avatar.with_format("png").url) as r2:
                                pfp2_data = await r2.read()
                        
                        img1 = Image.open(io.BytesIO(pfp1_data)).convert("RGBA").resize((200, 200))
                        img2 = Image.open(io.BytesIO(pfp2_data)).convert("RGBA").resize((200, 200))
                        
                        # Circular crop helper
                        def make_circular(img):
                            from PIL import ImageDraw
                            mask = Image.new("L", img.size, 0)
                            draw_mask = ImageDraw.Draw(mask)
                            draw_mask.ellipse((0, 0) + img.size, fill=255)
                            output = Image.new("RGBA", img.size, (0, 0, 0, 0))
                            output.paste(img, (0, 0), mask=mask)
                            return output
                            
                        circ1 = make_circular(img1)
                        circ2 = make_circular(img2)
                        
                        # Generate premium dark gradient background
                        from PIL import ImageDraw
                        base = Image.new("RGBA", (700, 350))
                        draw = ImageDraw.Draw(base)
                        for y_pos in range(350):
                            r_col = int(26 + (36 - 26) * (y_pos / 350))
                            g_col = int(26 + (36 - 26) * (y_pos / 350))
                            b_col = int(31 + (41 - 31) * (y_pos / 350))
                            draw.line([(0, y_pos), (700, y_pos)], fill=(r_col, g_col, b_col, 255))
                            
                        # Paste circular avatars
                        base.paste(circ1, (80, 75), circ1)
                        base.paste(circ2, (420, 75), circ2)
                        
                        # Draw thin circular borders around avatars
                        draw.ellipse([80, 75, 280, 275], outline=(150, 150, 150, 255), width=2)
                        draw.ellipse([420, 75, 620, 275], outline=(150, 150, 150, 255), width=2)
                        
                        # Draw filled progress heart in the center (overlapping the avatars)
                        heart_img = draw_ship_heart(
                            size=220,
                            percent=percent,
                            bg_color=(40, 40, 40, 200),
                            fill_color=(230, 40, 40, 255),
                            border_color=(10, 10, 10, 255),
                            border_width=4
                        )
                        base.paste(heart_img, (240, 65), heart_img)
                        
                        # Save resulting image to bytes
                        out_io = io.BytesIO()
                        base.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        # Determine comment based on compatibility score
                        if percent == 100:
                            comment = "absolute perfection. marry already 💍"
                        elif percent >= 80:
                            comment = "pure cuties, i ship it! 💕"
                        elif percent >= 50:
                            comment = "not bad, there's definitely a spark here 👀"
                        elif percent >= 20:
                            comment = "eh... maybe as friends? 💀"
                        else:
                            comment = "absolutely zero chemistry. touch grass 😭"
                            
                        embed = discord.Embed(
                            title="💖 REZE COFFEE HOUSE MATCHMAKER 💖",
                            description=f"**{user1.display_name}** x **{user2.display_name}** compatibility is **{percent}%**!\n*{comment}*",
                            color=discord.Color.from_rgb(255, 105, 180)
                        )
                        file = discord.File(out_io, filename="ship.png")
                        embed.set_image(url="attachment://ship.png")
                        await message.reply(file=file, embed=embed)
                    except Exception as err:
                        logger.error(f"Ship command failed: {err}")
                        await message.reply("something went wrong while generating the ship image 😭")
                return

            elif command in ["wanted", "bounty"]:
                target = message.mentions[0] if message.mentions else message.author
                async with message.channel.typing():
                    try:
                        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url) as resp:
                                if resp.status != 200:
                                    await message.reply("couldn't fetch the user avatar 😭")
                                    return
                                avatar_bytes = await resp.read()
                        
                        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                        base = Image.new("RGBA", (600, 800), (235, 215, 180, 255))
                        from PIL import ImageDraw, ImageFont, ImageOps
                        draw = ImageDraw.Draw(base)
                        
                        for _ in range(15):
                            y_line = random.randint(30, 770)
                            draw.line([(20, y_line), (580, y_line)], fill=(220, 200, 165, 120), width=1)
                        
                        draw.rectangle([10, 10, 590, 790], outline=(70, 40, 20, 255), width=8)
                        draw.rectangle([20, 20, 580, 780], outline=(70, 40, 20, 255), width=2)
                        
                        font_path = os.path.join(BASE_DIR, "assets", "fonts", "Anton-Regular.ttf")
                        try:
                            font_wanted = ImageFont.truetype(font_path, 80)
                            font_sub = ImageFont.truetype(font_path, 32)
                            font_name = ImageFont.truetype(font_path, 42)
                            font_bounty = ImageFont.truetype(font_path, 48)
                        except:
                            font_wanted = ImageFont.load_default()
                            font_sub = ImageFont.load_default()
                            font_name = ImageFont.load_default()
                            font_bounty = ImageFont.load_default()
                            
                        def draw_centered_text(text, font, y, color=(70, 40, 20, 255)):
                            try:
                                bbox = draw.textbbox((0, 0), text, font=font)
                                w = bbox[2] - bbox[0]
                            except:
                                w = len(text) * 10
                            draw.text(((600 - w) // 2, y), text, fill=color, font=font)
                            
                        draw_centered_text("WANTED", font_wanted, 30)
                        draw_centered_text("DEAD OR ALIVE", font_sub, 120)
                        
                        avatar_resized = avatar_img.resize((360, 360), Image.Resampling.LANCZOS)
                        avatar_gray = avatar_resized.convert("L")
                        avatar_vintage = ImageOps.colorize(avatar_gray, black=(45, 25, 12), white=(238, 220, 188))
                        
                        base.paste(avatar_vintage, (120, 180))
                        draw.rectangle([115, 175, 485, 545], outline=(70, 40, 20, 255), width=5)
                        
                        random.seed(target.id)
                        bounty_val = random.randint(50, 3000) * 1000000
                        random.seed()
                        
                        bounty_str = f"฿ {bounty_val:,} -"
                        
                        name_text = target.display_name.upper()
                        draw_centered_text(name_text, font_name, 580)
                        draw_centered_text("REWARD", font_sub, 645)
                        draw_centered_text(bounty_str, font_bounty, 690)
                        
                        out_io = io.BytesIO()
                        base.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        embed = discord.Embed(
                            title=f"🏴‍☠️ WANTED: {target.display_name} 🏴‍☠️",
                            color=discord.Color.from_rgb(139, 69, 19)
                        )
                        file = discord.File(out_io, filename="wanted.png")
                        embed.set_image(url="attachment://wanted.png")
                        await message.reply(file=file, embed=embed)
                    except Exception as err:
                        logger.error(f"Wanted command failed: {err}")
                        await message.reply("something went wrong while generating the wanted poster 😭")
                return

            elif command == "gay":
                target = message.mentions[0] if message.mentions else message.author
                percent = random.randint(0, 100)
                
                if percent >= 80:
                    comment = "absolutely rainbow tier, 100% gay 💅🌈"
                elif percent >= 50:
                    comment = "getting pretty fruity here 👀🏳️‍🌈"
                elif percent >= 20:
                    comment = "moderate levels of gayness detected 🧐"
                else:
                    comment = "basically straight, touch grass 🥱"
                    
                embed = discord.Embed(
                    title="🌈 Gayrate Calculator 🌈",
                    description=f"**{target.display_name}** is **{percent}%** gay!\n*{comment}*",
                    color=discord.Color.from_rgb(255, 105, 180)
                )
                await message.reply(embed=embed)
                return

            elif command == "lesbian":
                target = message.mentions[0] if message.mentions else message.author
                percent = random.randint(0, 100)
                
                if percent >= 80:
                    comment = "full lesbian energy, 100% peak girlpower 💅👭"
                elif percent >= 50:
                    comment = "pretty fruity vibes detected 👀👭"
                elif percent >= 20:
                    comment = "slight lesbian tendencies detected 🧐"
                else:
                    comment = "basically straight 🥱"
                    
                embed = discord.Embed(
                    title="👩‍❤️‍👩 Lesbianrate Calculator 👩‍❤️‍👩",
                    description=f"**{target.display_name}** is **{percent}%** lesbian!\n*{comment}*",
                    color=discord.Color.from_rgb(255, 192, 203)
                )
                await message.reply(embed=embed)
                return

            elif command in ["gandhi", "fakequote"]:
                if not args:
                    await message.reply("you need to provide some quote text, dummy 🙄 (limit: 200 chars)")
                    return
                
                text = args[:200].strip()
                async with message.channel.typing():
                    try:
                        bg_path = os.path.join(BASE_DIR, "assets", "memes", "gandhi.jpg")
                        bg = Image.open(bg_path).convert("RGBA")
                        from PIL import ImageDraw, ImageFont
                        draw = ImageDraw.Draw(bg)
                        
                        W, H = bg.size
                        font_size = max(18, int(H * 0.065))
                        author_size = max(14, int(H * 0.05))
                        
                        try:
                            font_quote_path = os.path.join(BASE_DIR, "assets", "fonts", "CrimsonText-Italic.ttf")
                            font_author_path = os.path.join(BASE_DIR, "assets", "fonts", "CrimsonText-Regular.ttf")
                            font_quote = ImageFont.truetype(font_quote_path, font_size)
                            font_author = ImageFont.truetype(font_author_path, author_size)
                        except:
                            font_quote = ImageFont.load_default()
                            font_author = ImageFont.load_default()
                            
                        x_start = int(W * 0.46)
                        max_width = int(W - x_start - 60)
                        
                        words = text.split()
                        lines = []
                        current_line = []
                        for word in words:
                            test_line = " ".join(current_line + [word])
                            try:
                                bbox = draw.textbbox((0, 0), test_line, font=font_quote)
                                w = bbox[2] - bbox[0]
                            except:
                                w = len(test_line) * (font_size * 0.5)
                            if w <= max_width:
                                current_line.append(word)
                            else:
                                if current_line:
                                    lines.append(" ".join(current_line))
                                current_line = [word]
                        if current_line:
                            lines.append(" ".join(current_line))
                            
                        line_spacing = int(font_size * 0.3)
                        line_heights = []
                        for line in lines:
                            try:
                                bbox = draw.textbbox((0, 0), line, font=font_quote)
                                h = bbox[3] - bbox[1]
                            except:
                                h = font_size
                            line_heights.append(h)
                            
                        total_lines_height = sum(line_heights) + line_spacing * (len(lines) - 1)
                        author_text = f"— {message.author.display_name}"
                        try:
                            bbox_auth = draw.textbbox((0, 0), author_text, font=font_author)
                            author_w = bbox_auth[2] - bbox_auth[0]
                            author_h = bbox_auth[3] - bbox_auth[1]
                        except:
                            author_w = len(author_text) * (author_size * 0.5)
                            author_h = author_size
                            
                        total_block_height = total_lines_height + line_spacing * 2 + author_h
                        y_start = (H - total_block_height) // 2
                        
                        y_current = y_start
                        quote_color = (245, 235, 215, 255)
                        
                        for idx, line in enumerate(lines):
                            display_line = line
                            if idx == 0:
                                display_line = f'"{display_line}'
                            if idx == len(lines) - 1:
                                display_line = f'{display_line}"'
                                
                            draw.text((x_start, y_current), display_line, fill=quote_color, font=font_quote)
                            y_current += line_heights[idx] + line_spacing
                            
                        x_author = W - 60 - author_w
                        draw.text((x_author, y_current + line_spacing), author_text, fill=(210, 190, 160, 255), font=font_author)
                        
                        out_io = io.BytesIO()
                        bg.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        file = discord.File(out_io, filename="gandhi_quote.png")
                        await message.reply(file=file)
                    except Exception as err:
                        logger.error(f"Gandhi quote command failed: {err}")
                        await message.reply("something went wrong while generating the quote image 😭")
                return

            elif command == "impersonate":
                if not message.author.guild_permissions.manage_messages:
                    await message.reply("you don't have permission to use this command, dummy 🙄", delete_after=5)
                    try:
                        await message.delete()
                    except:
                        pass
                    return

                if not message.mentions:
                    await message.reply("you need to mention who you want to impersonate, dummy 🙄", delete_after=5)
                    try:
                        await message.delete()
                    except:
                        pass
                    return

                target_member = message.mentions[0]
                mention_str = f"<@{target_member.id}>"
                mention_nick_str = f"<@!{target_member.id}>"
                text = args
                if mention_str in text:
                    text = text.replace(mention_str, "", 1)
                elif mention_nick_str in text:
                    text = text.replace(mention_nick_str, "", 1)
                else:
                    words = text.split()
                    for word in words:
                        if word.startswith("<@") and word.endswith(">"):
                            text = text.replace(word, "", 1)
                            break

                text = text.strip()
                if not text:
                    await message.reply("what do you want them to say? provide some text! 🙄", delete_after=5)
                    try:
                        await message.delete()
                    except:
                        pass
                    return

                try:
                    await message.delete()
                except Exception:
                    pass

                webhook = None
                try:
                    webhook = await message.channel.create_webhook(name=f"Reze-Impersonate-{random.randint(1000, 9999)}")
                    
                    avatar_url = None
                    if hasattr(target_member, "display_avatar") and target_member.display_avatar:
                        avatar_url = target_member.display_avatar.url
                    elif hasattr(target_member, "avatar") and target_member.avatar:
                        avatar_url = target_member.avatar.url
                    else:
                        avatar_url = target_member.default_avatar.url

                    await webhook.send(
                        content=text,
                        username=target_member.display_name,
                        avatar_url=avatar_url
                    )
                except discord.Forbidden:
                    await message.channel.send("i don't have permission to manage webhooks in this channel! 😭", delete_after=5)
                except Exception as e:
                    logger.error(f"Impersonate command failed: {e}")
                    await message.channel.send("something went wrong while trying to impersonate 😭", delete_after=5)
                finally:
                    if webhook:
                        try:
                            await webhook.delete()
                        except Exception:
                            pass
                return

            elif command == "jail":
                target = message.mentions[0] if message.mentions else message.author
                async with message.channel.typing():
                    try:
                        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url) as resp:
                                if resp.status != 200:
                                    await message.reply("couldn't fetch the user avatar 😭")
                                    return
                                avatar_bytes = await resp.read()
                        
                        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                        avatar_resized = avatar_img.resize((360, 360), Image.Resampling.LANCZOS)
                        avatar_gray = avatar_resized.convert("L").convert("RGBA")
                        
                        overlay = Image.new("RGBA", (360, 360), (0, 0, 0, 0))
                        from PIL import ImageDraw
                        draw_bars = ImageDraw.Draw(overlay)
                        
                        bar_width = 12
                        spacing = 50
                        for x in range(30, 360, spacing):
                            draw_bars.rectangle([x - 1, 0, x + bar_width + 1, 360], fill=(0, 0, 0, 150))
                            draw_bars.rectangle([x, 0, x + bar_width, 360], fill=(50, 50, 50, 255))
                            draw_bars.line([(x + 3, 0), (x + 3, 360)], fill=(100, 100, 100, 255), width=2)
                            
                        for y in [80, 280]:
                            draw_bars.rectangle([0, y - 1, 360, y + bar_width + 1], fill=(0, 0, 0, 150))
                            draw_bars.rectangle([0, y, 360, y + bar_width], fill=(50, 50, 50, 255))
                            draw_bars.line([(0, y + 3), (360, y + 3)], fill=(100, 100, 100, 255), width=2)
                            
                        jailed_img = Image.alpha_composite(avatar_gray, overlay)
                        
                        out_io = io.BytesIO()
                        jailed_img.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        embed = discord.Embed(
                            title="🔒 JAILED! 🔒",
                            description=f"**{target.display_name}** has been locked up for crimes against humanity! 👮‍♂️",
                            color=discord.Color.from_rgb(40, 40, 40)
                        )
                        file = discord.File(out_io, filename="jailed.png")
                        embed.set_image(url="attachment://jailed.png")
                        await message.reply(file=file, embed=embed)
                    except Exception as err:
                        logger.error(f"Jail command failed: {err}")
                        await message.reply("something went wrong while putting them in jail 😭")
                return

            elif command == "rip":
                target = message.mentions[0] if message.mentions else message.author
                
                # Parse reason/epitaph
                reason = args.strip()
                if message.mentions:
                    # Remove target user's mention from the start or end of args
                    mention_str = f"<@{target.id}>"
                    mention_nick_str = f"<@!{target.id}>"
                    if reason.startswith(mention_str):
                        reason = reason[len(mention_str):].strip()
                    elif reason.startswith(mention_nick_str):
                        reason = reason[len(mention_nick_str):].strip()
                    else:
                        # Fallback: remove first mention-like string in the text
                        words = reason.split()
                        for word in words:
                            if word.startswith("<@") and word.endswith(">"):
                                reason = reason.replace(word, "", 1).strip()
                                break

                # Set epitaph text
                default_epitaphs = [
                    "Died of skill issue",
                    "Ratioed by life",
                    "Forgot to use a defensive",
                    "Tried to solo the boss",
                    "No maids? No life.",
                    "L + Ratio + Rip",
                    "Died from cringe",
                    "A tragic victim of the gacha",
                    "Disconnected from the server",
                    "Fallen in battle",
                    "Wasted",
                    "Died doing what they loved: spamming",
                    "Rest in pieces",
                    "Landed on the wrong planet",
                    "Forgot how to breathe",
                    "Victim of a bad internet connection",
                    "Choked on water",
                    "Defeated by a minor mob"
                ]
                
                if reason:
                    if not (reason.startswith('"') and reason.endswith('"')):
                        epitaph_text = f'"{reason}"'
                    else:
                        epitaph_text = reason
                else:
                    epitaph_text = f'"{random.choice(default_epitaphs)}"'

                async with message.channel.typing():
                    try:
                        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url) as resp:
                                if resp.status != 200:
                                    await message.reply("couldn't fetch the user avatar 😭")
                                    return
                                avatar_bytes = await resp.read()
                                
                        # Process image
                        template_path = os.path.join(BASE_DIR, "assets", "memes", "rip.webp")
                        img = Image.open(template_path).convert("RGBA")
                        
                        # Scale up by 4x for high quality text and drawings
                        from PIL import ImageDraw, ImageFont, ImageOps
                        scale = 4
                        width, height = img.width * scale, img.height * scale
                        img_scaled = img.resize((width, height), Image.Resampling.LANCZOS)
                        draw = ImageDraw.Draw(img_scaled)
                        
                        # Load user's avatar
                        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                        avatar_size = 120
                        avatar_resized = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                        
                        # Apply grayscale filter to avatar (matching gravestone theme)
                        avatar_gray = ImageOps.grayscale(avatar_resized).convert("RGBA")
                        
                        # Round avatar
                        mask = Image.new("L", (avatar_size, avatar_size), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse([0, 0, avatar_size, avatar_size], fill=255)
                        
                        # Paste avatar on tombstone
                        av_x = 470 - (avatar_size // 2)
                        av_y = 610
                        img_scaled.paste(avatar_gray, (av_x, av_y), mask=mask)
                        
                        # Try loading serif fonts
                        try:
                            font_rip_path = os.path.join(BASE_DIR, "assets", "fonts", "CrimsonText-Bold.ttf")
                            font_name_path = os.path.join(BASE_DIR, "assets", "fonts", "CrimsonText-Bold.ttf")
                            font_text_path = os.path.join(BASE_DIR, "assets", "fonts", "CrimsonText-Italic.ttf")
                            font_rip = ImageFont.truetype(font_rip_path, 48)
                            font_name = ImageFont.truetype(font_name_path, 36)
                            font_text = ImageFont.truetype(font_text_path, 26)
                        except Exception:
                            font_rip = ImageFont.load_default()
                            font_name = ImageFont.load_default()
                            font_text = ImageFont.load_default()
                                
                        text_color = (40, 50, 45, 240)
                        
                        # Draw static RIP header
                        draw.text((470, 755), "R.I.P.", fill=text_color, font=font_rip, anchor="mm")
                        
                        # Draw display name (capped to fit)
                        display_name = target.display_name
                        if len(display_name) > 20:
                            display_name = display_name[:17] + "..."
                        draw.text((470, 810), display_name, fill=text_color, font=font_name, anchor="mm")
                        
                        # Draw dates: [Account creation year] - [Current year]
                        birth_year = target.created_at.year
                        death_year = datetime.now(timezone.utc).year
                        draw.text((470, 860), f"{birth_year} - {death_year}", fill=text_color, font=font_text, anchor="mm")
                        
                        # Wrap and draw epitaph text
                        max_text_width = 420
                        words = epitaph_text.split()
                        lines = []
                        current_line = []
                        for word in words:
                            test_line = " ".join(current_line + [word])
                            try:
                                bbox = draw.textbbox((0, 0), test_line, font=font_text)
                                w = bbox[2] - bbox[0]
                            except Exception:
                                w = len(test_line) * 13
                            if w <= max_text_width:
                                current_line.append(word)
                            else:
                                if current_line:
                                    lines.append(" ".join(current_line))
                                current_line = [word]
                        if current_line:
                            lines.append(" ".join(current_line))
                            
                        y_start = 915
                        line_spacing = 30
                        for idx, line in enumerate(lines[:3]):
                            draw.text((470, y_start + idx * line_spacing), line, fill=text_color, font=font_text, anchor="mm")
                            
                        # Save and reply
                        out_io = io.BytesIO()
                        img_scaled.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        embed = discord.Embed(
                            title="🪦 Rest In Peace 🪦",
                            description=f"We gather here today to mourn the loss of **{target.mention}**...",
                            color=discord.Color.from_rgb(120, 120, 120)
                        )
                        file = discord.File(out_io, filename="rip.png")
                        embed.set_image(url="attachment://rip.png")
                        await message.reply(file=file, embed=embed)
                        
                    except Exception as err:
                        logger.error(f"RIP command failed: {err}")
                        await message.reply("something went wrong while putting them to rest 😭")
                return

            elif command == "akinator":
                async with message.channel.typing():
                    try:
                        view = AkinatorView(message.author)
                        question = await view.start()
                        
                        embed = discord.Embed(
                            title="🔮 Akinator (Question 1) 🔮",
                            description=f"### {question}",
                            color=discord.Color.from_rgb(212, 175, 230)
                        )
                        embed.set_footer(text=f"Progression: 0% | Playing: {message.author.display_name}")
                        
                        reply_msg = await message.reply(embed=embed, view=view)
                        view.message = reply_msg
                    except Exception as e:
                        logger.error(f"Akinator start error: {e}", exc_info=True)
                        await message.reply("couldn't start the Akinator game right now 😭")
            elif command in ["wyr", "wouldyourather"]:
                async with message.channel.typing():
                    try:
                        opt_a, opt_b = await ai.get_would_you_rather()
                        
                        embed = discord.Embed(
                            title="Would you rather ...",
                            description=f"🅰️ {opt_a} [0 %]\n\n🅱️ {opt_b} [0 %]",
                            color=discord.Color.from_rgb(220, 20, 60)
                        )
                        
                        reply_msg = await message.reply(embed=embed)
                        
                        await reply_msg.add_reaction("🅰️")
                        await reply_msg.add_reaction("🅱️")
                        
                    except Exception as e:
                        logger.error(f"Would you rather command failed: {e}", exc_info=True)
                        await message.reply("couldn't start a would you rather game right now 😭")
                return

            elif command in ["tl", "translate"]:
                async with message.channel.typing():
                    try:
                        text_to_translate = ""
                        target_lang = None
                        
                        if message.reference and message.reference.message_id:
                            # Replying to a message
                            try:
                                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                                text_to_translate = ref_msg.content
                                # If the replied message content is empty, check embeds
                                if not text_to_translate and ref_msg.embeds:
                                    text_to_translate = ref_msg.embeds[0].description or ""
                                    
                                if args.strip():
                                    target_lang = args.strip()
                            except Exception as ref_err:
                                logger.error(f"Failed to fetch referenced message: {ref_err}")
                        else:
                            # Not replying, parse from args
                            if args.strip():
                                words = args.strip().split(None, 1)
                                text_to_translate = args.strip()
                                
                                COMMON_LANGS = {
                                    "english", "hindi", "hinglish", "spanish", "french", "german", "japanese", 
                                    "chinese", "russian", "korean", "italian", "portuguese", "arabic", "turkish",
                                    "en", "hi", "es", "fr", "de", "ja", "zh", "ru", "ko", "it", "pt", "ar", "tr"
                                }
                                
                                if len(words) > 1:
                                    first_word = words[0].lower()
                                    if first_word == "to":
                                        sub_words = words[1].split(None, 1)
                                        if sub_words:
                                            second_word = sub_words[0].lower()
                                            if second_word in COMMON_LANGS:
                                                target_lang = second_word
                                                text_to_translate = sub_words[1] if len(sub_words) > 1 else ""
                                    elif first_word in COMMON_LANGS:
                                        target_lang = first_word
                                        text_to_translate = words[1]
                        
                        text_to_translate = text_to_translate.strip()
                        if not text_to_translate:
                            await message.reply("you need to reply to a message or give me some text to translate 🙄")
                            return
                        
                        translated = await ai.translate_text(text_to_translate, target_lang)
                        if translated:
                            await message.reply(translated)
                        else:
                            await message.reply("couldn't translate that, sorry 😭")
                            
                    except Exception as e:
                        logger.error(f"Translation command failed: {e}", exc_info=True)
                        await message.reply("something went wrong while translating 😭")
                return

            elif command in ["anime", "manga"]:
                is_manga = command == "manga"
                api_type = "manga" if is_manga else "anime"
                
                if not args:
                    await message.reply(f"you need to provide a search query, dummy 🙄 (e.g., ${command} {'chainsaw man' if is_manga else 'naruto'})")
                    return
                
                query = args.strip()
                async with message.channel.typing():
                    try:
                        query_str = """
                        query ($search: String, $type: MediaType) {
                          Page (page: 1, perPage: 1) {
                            media (search: $search, type: $type) {
                              id
                              title {
                                romaji
                                english
                                native
                              }
                              siteUrl
                              description
                              averageScore
                              status
                              format
                              episodes
                              chapters
                              volumes
                              genres
                              coverImage {
                                large
                              }
                              staff {
                                edges {
                                  role
                                  node {
                                    name {
                                      full
                                    }
                                  }
                                }
                              }
                            }
                          }
                        }
                        """
                        url = "https://graphql.anilist.co"
                        payload = {
                            "query": query_str,
                            "variables": {
                                "search": query,
                                "type": "MANGA" if is_manga else "ANIME"
                            }
                        }
                        
                        async with aiohttp.ClientSession() as session:
                            async with session.post(url, json=payload) as resp:
                                if resp.status == 200:
                                    res_data = await resp.json()
                                    media_list = res_data.get("data", {}).get("Page", {}).get("media", [])
                                    if not media_list:
                                        await message.reply(f"couldn't find any {api_type} named **{query}** 😭")
                                        return
                                    
                                    data = media_list[0]
                                    title = data.get("title", {}).get("english") or data.get("title", {}).get("romaji") or data.get("title", {}).get("native") or "Unknown"
                                    site_url = data.get("siteUrl", "")
                                    
                                    # Strip HTML tags from description
                                    description_raw = data.get("description", "No synopsis available.")
                                    clean_desc = re.sub(r'<[^>]*>', '', description_raw) if description_raw else "No synopsis available."
                                    if clean_desc and len(clean_desc) > 400:
                                        clean_desc = clean_desc[:397] + "..."
                                        
                                    avg_score = data.get("averageScore")
                                    score = f"{avg_score / 10:.1f}" if avg_score is not None else "N/A"
                                    
                                    status_map = {
                                        "FINISHED": "Finished Airing" if not is_manga else "Finished Publishing",
                                        "RELEASING": "Currently Airing" if not is_manga else "Publishing",
                                        "NOT_YET_RELEASED": "Not Yet Released",
                                        "CANCELLED": "Cancelled",
                                        "HIATUS": "Hiatus"
                                    }
                                    status = status_map.get(data.get("status", ""), "N/A")
                                    
                                    # Format Type/Format
                                    raw_format = data.get("format", "N/A")
                                    media_format = raw_format.replace("_", " ").title() if raw_format else "N/A"
                                    
                                    if is_manga:
                                        chapters = data.get("chapters")
                                        volumes = data.get("volumes")
                                        is_publishing = data.get("status") == "RELEASING"
                                        
                                        chap_str = chapters if chapters is not None else ("Ongoing" if is_publishing else "N/A")
                                        vol_str = volumes if volumes is not None else ("Ongoing" if is_publishing else "N/A")
                                        info_str = f"📚 Chapters: **{chap_str}** | Volumes: **{vol_str}**"
                                    else:
                                        episodes = data.get("episodes") or "N/A"
                                        info_str = f"📺 Episodes: **{episodes}**"
                                        
                                    genres = ", ".join(data.get("genres", [])) or "N/A"
                                    img_url = data.get("coverImage", {}).get("large")
                                    
                                    embed = discord.Embed(
                                        title=title,
                                        url=site_url,
                                        description=clean_desc,
                                        color=discord.Color.from_rgb(255, 182, 193)
                                    )
                                    if img_url:
                                        embed.set_thumbnail(url=img_url)
                                        
                                    embed.add_field(name="Rating/Score", value=f"⭐ **{score}**", inline=True)
                                    embed.add_field(name="Status", value=status, inline=True)
                                    embed.add_field(name="Type/Format", value=media_format, inline=True)
                                    embed.add_field(name="Details", value=info_str, inline=True)
                                    
                                    if is_manga:
                                        authors_list = []
                                        staff_edges = data.get("staff", {}).get("edges", [])
                                        for edge in staff_edges:
                                            role = edge.get("role", "").lower()
                                            if any(r in role for r in ["story", "art", "original creator", "writer"]):
                                                name = edge.get("node", {}).get("name", {}).get("full")
                                                if name and name not in authors_list:
                                                    authors_list.append(name)
                                        authors = ", ".join(authors_list) if authors_list else "N/A"
                                        embed.add_field(name="Author(s)", value=authors, inline=True)
                                        
                                    embed.add_field(name="Genres", value=genres, inline=False)
                                    embed.set_footer(text="AniList | Click title for link")
                                    
                                    await message.reply(embed=embed)
                                else:
                                    await message.reply("couldn't fetch the details, AniList API is acting up 🙄")
                    except Exception as e:
                        logger.error(f"Anime/Manga search failed: {e}")
                        await message.reply("something went wrong while searching 😭")
                return

            elif command in ["movie", "show", "series"]:
                if not args:
                    await message.reply(f"you need to provide a movie/show title, dummy 🙄 (e.g., ${command} Interstellar)")
                    return
                
                query = args.strip()
                import urllib.parse
                async with message.channel.typing():
                    try:
                        omdb_key = os.getenv("OMDB_API_KEY")
                        if not omdb_key:
                            await message.reply("OMDb API key is not configured in the bot's `.env`! Please tell the bot owner to set `OMDB_API_KEY`. 🙄")
                            return
                        
                        # Determine type restriction
                        # OMDb api types: "movie", "series", "episode"
                        media_type = "movie" if command == "movie" else "series"
                        
                        # Smart year extraction: search for a 4-digit number at the end of the query
                        year_val = None
                        year_match = re.search(r'\b(19\d\d|20\d\d)\b', query)
                        clean_query = query
                        if year_match:
                            extracted_year = year_match.group(1)
                            # Strip the year from the end of the query (e.g., "Interstellar 2014" or "Interstellar (2014)")
                            temp_query = re.sub(r'\s*\(?\b' + extracted_year + r'\b\)?\s*$', '', query).strip()
                            if temp_query:  # Avoid empty queries if the title itself is just a year
                                clean_query = temp_query
                                year_val = extracted_year
                        
                        async with aiohttp.ClientSession() as session:
                            data = {"Response": "False"}
                            
                            # 1. Try search query first to get the most relevant/popular matches
                            search_params = {
                                "apikey": omdb_key,
                                "s": clean_query,
                                "type": media_type
                            }
                            if year_val:
                                search_params["y"] = year_val
                                
                            search_url = f"http://www.omdbapi.com/?{urllib.parse.urlencode(search_params)}"
                            async with session.get(search_url) as resp:
                                if resp.status == 200:
                                    search_data = await resp.json()
                                    if search_data.get("Response") == "True" and search_data.get("Search"):
                                        first_result = search_data["Search"][0]
                                        imdb_id = first_result.get("imdbID")
                                        if imdb_id:
                                            # Fetch detailed info using imdbID
                                            detail_url = f"http://www.omdbapi.com/?apikey={omdb_key}&i={imdb_id}"
                                            async with session.get(detail_url) as detail_resp:
                                                if detail_resp.status == 200:
                                                    data = await detail_resp.json()
                                                    
                            # 2. Fallback to direct title query if search failed or returned nothing
                            if data.get("Response") == "False":
                                params = {
                                    "apikey": omdb_key,
                                    "t": clean_query,
                                    "type": media_type
                                }
                                if year_val:
                                    params["y"] = year_val
                                    
                                url = f"http://www.omdbapi.com/?{urllib.parse.urlencode(params)}"
                                async with session.get(url) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                    else:
                                        data = {"Response": "False", "Error": f"HTTP error {resp.status}"}
                                        
                            if data.get("Response") == "False":
                                await message.reply(f"couldn't find any {media_type} named **{query}** 😭 (OMDb: {data.get('Error', 'Unknown error')})")
                                return
                            
                            title = data.get("Title", "Unknown")
                            year = data.get("Year", "N/A")
                            rated = data.get("Rated", "N/A")
                            released = data.get("Released", "N/A")
                            runtime = data.get("Runtime", "N/A")
                            genre = data.get("Genre", "N/A")
                            director = data.get("Director", "N/A")
                            actors = data.get("Actors", "N/A")
                            
                            # Retrieve plot and fallback to AI if N/A
                            plot = data.get("Plot", "N/A")
                            if not plot or plot == "N/A":
                                try:
                                    generated_plot = await ai.get_movie_plot(title, year, actors)
                                    plot = generated_plot if generated_plot else "No plot summary available."
                                except Exception as pe:
                                    logger.error(f"Failed to generate movie plot fallback: {pe}")
                                    plot = "No plot summary available."
                                    
                            poster_url = data.get("Poster")
                            imdb_rating = data.get("imdbRating", "N/A")
                            imdb_id = data.get("imdbID", "")
                            imdb_url = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else ""
                            
                            if plot and len(plot) > 400:
                                plot = plot[:397] + "..."
                                
                            embed = discord.Embed(
                                title=f"🎬 {title} ({year}) 🎬",
                                url=imdb_url if imdb_url else None,
                                description=plot,
                                color=discord.Color.from_rgb(212, 175, 230)
                            )
                            if poster_url and poster_url != "N/A":
                                embed.set_thumbnail(url=poster_url)
                                
                            embed.add_field(name="Starring", value=actors, inline=False)
                            embed.add_field(name="Director", value=director, inline=True)
                            embed.add_field(name="Genres", value=genre, inline=True)
                            embed.add_field(name="Runtime", value=runtime, inline=True)
                            embed.add_field(name="Released", value=released, inline=True)
                            embed.add_field(name="Rated", value=rated, inline=True)
                            embed.add_field(name="Rating", value=f"⭐ **{imdb_rating}/10**" if imdb_rating != "N/A" else "N/A", inline=True)
                            
                            embed.set_footer(text="OMDb & IMDb | Click title for link")
                            
                            await message.reply(embed=embed)
                    except Exception as e:
                        logger.error(f"Movie command failed: {e}")
                        await message.reply("something went wrong while searching 😭")
                return

            elif command == "weather":
                if not args:
                    await message.reply("you need to provide a city name, dummy 🙄 (e.g., $weather Tokyo)")
                    return
                city = args.strip()
                import urllib.parse
                async with message.channel.typing():
                    try:
                        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as resp:
                                if resp.status == 200:
                                    data = await resp.json(content_type=None)
                                    current = data["current_condition"][0]
                                    temp_c = current["temp_C"]
                                    temp_f = current["temp_F"]
                                    desc = current["weatherDesc"][0]["value"]
                                    humidity = current["humidity"]
                                    wind = current["windspeedKmph"]
                                    
                                    area = data.get("nearest_area", [{}])[0]
                                    region = area.get("region", [{}])[0].get("value", "")
                                    country = area.get("country", [{}])[0].get("value", "")
                                    location_name = area.get("areaName", [{}])[0].get("value", city)
                                    
                                    full_loc = f"{location_name}"
                                    if region: full_loc += f", {region}"
                                    if country: full_loc += f", {country}"
                                    
                                    embed = discord.Embed(
                                        title=f"🌡️ Weather for {full_loc} 🌡️",
                                        description=f"**{desc}**",
                                        color=discord.Color.from_rgb(175, 238, 238)
                                    )
                                    embed.add_field(name="Temperature", value=f"🌡️ **{temp_c}°C** / **{temp_f}°F**", inline=True)
                                    embed.add_field(name="Humidity", value=f"💧 **{humidity}%**", inline=True)
                                    embed.add_field(name="Wind Speed", value=f"💨 **{wind} km/h**", inline=True)
                                    embed.set_footer(text="Powered by wttr.in")
                                    await message.reply(embed=embed)
                                else:
                                    await message.reply(f"couldn't fetch the weather for **{city}** 😭")
                    except Exception as e:
                        logger.error(f"Weather command failed: {e}")
                        await message.reply("something went wrong while fetching weather details 😭")
                return

            elif command == "poll":
                if not args:
                    await message.reply("usage: `$poll question | option1 | option2 | ...`, dummy 🙄")
                    return
                
                parts = [p.strip() for p in args.split("|")]
                if len(parts) < 2:
                    await message.reply("you need to provide a question and at least one option! 🙄")
                    return
                
                question = parts[0]
                options = parts[1:]
                
                if len(options) > 10:
                    await message.reply("keep it to 10 options or less, I don't have all day 🙄")
                    return
                
                poll_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
                
                embed = discord.Embed(
                    title=f"📊 {question}",
                    color=discord.Color.from_rgb(212, 175, 230)
                )
                
                option_lines = []
                for idx, opt in enumerate(options):
                    option_lines.append(f"{poll_emojis[idx]} {opt}")
                    
                embed.description = "\n".join(option_lines)
                embed.set_footer(text=f"Poll by {message.author.display_name}")
                
                poll_msg = await message.channel.send(embed=embed)
                if poll_msg:
                    command_output_message_ids.add(poll_msg.id)
                for idx in range(len(options)):
                    await poll_msg.add_reaction(poll_emojis[idx])
                return

            elif command == "confess":
                is_dm = isinstance(message.channel, discord.DMChannel)
                if not is_dm:
                    try:
                        await message.delete()
                    except:
                        pass
                    warn_msg = await message.channel.send(f"{message.author.mention} confessions should be sent in my DMs so they stay actually anonymous! 🙄", delete_after=5)
                    if warn_msg:
                        command_output_message_ids.add(warn_msg.id)
                    return
                
                if not args:
                    await message.reply("you need to provide a confession text, dummy 🙄")
                    return
                
                confession_text = args.strip()
                success = False
                for guild in bot.guilds:
                    member = guild.get_member(message.author.id)
                    if member:
                        conf_channel = None
                        g_cfg = await get_guild_config(guild.id)
                        confession_channel_id = g_cfg.get("confession_channel_id")
                        if confession_channel_id:
                            conf_channel = guild.get_channel(int(confession_channel_id))
                            
                        if not conf_channel:
                            for channel in guild.text_channels:
                                if "confess" in channel.name.lower():
                                    conf_channel = channel
                                    break
                                    
                        if conf_channel:
                            embed = discord.Embed(
                                title="🤫 Anonymous Confession 🤫",
                                description=confession_text,
                                color=discord.Color.from_rgb(255, 105, 180)
                            )
                            embed.set_footer(text="DM me $confess to submit yours!")
                            conf_msg = await conf_channel.send(embed=embed)
                            if conf_msg:
                                command_output_message_ids.add(conf_msg.id)
                            success = True
                
                if success:
                    await message.reply("your confession has been posted anonymously! 🤫")
                else:
                    await message.reply("couldn't find any server with a confessions channel that we both share 😭")
                return

            elif command == "choose":
                if not args:
                    await message.reply("choose what? give me some options separated by `|` or commas! 🙄")
                    return
                
                delims = ["|", ",", " or "]
                options = None
                for d in delims:
                    if d in args:
                        options = [o.strip() for o in args.split(d) if o.strip()]
                        break
                if not options:
                    options = [o.strip() for o in args.split() if o.strip()]
                    
                if len(options) < 2:
                    await message.reply("give me at least two choices, dummy 🙄")
                    return
                
                choice = random.choice(options)
                comments = [
                    f"obviously **{choice}**. next question? 💅",
                    f"i choose **{choice}**. don't regret it later lol 🙄",
                    f"easy: **{choice}**. you knew this already 💀",
                    f"definitely **{choice}**.",
                    f"let's go with **{choice}**, why not? 👀"
                ]
                await message.reply(random.choice(comments))
                return

            elif command in ["waifu", "husbando"]:
                async with message.channel.typing():
                    import urllib.parse
                    character_found = False
                    for _ in range(3):
                        try:
                            # Restrict to pages 1-8 to get highly popular characters with good images
                            random_page = random.randint(1, 8)
                            query = """
                            query ($page: Int, $perPage: Int) {
                              Page (page: $page, perPage: $perPage) {
                                characters (sort: FAVOURITES_DESC) {
                                  id
                                  name {
                                    full
                                    native
                                  }
                                  image {
                                    large
                                  }
                                  gender
                                  media (type: ANIME, sort: POPULARITY_DESC, perPage: 1) {
                                    nodes {
                                      title {
                                        romaji
                                        english
                                      }
                                    }
                                  }
                                }
                              }
                            }
                            """
                            variables = {"page": random_page, "perPage": 50}
                            url = "https://graphql.anilist.co"
                            
                            async with aiohttp.ClientSession() as session:
                                async with session.post(url, json={"query": query, "variables": variables}) as resp:
                                    if resp.status == 200:
                                        res_data = await resp.json()
                                        characters = res_data.get("data", {}).get("Page", {}).get("characters", [])
                                        
                                        target_gender = "female" if command == "waifu" else "male"
                                        filtered = [
                                            c for c in characters 
                                            if c.get("gender") and c["gender"].strip().lower() == target_gender
                                            and c.get("image") and c["image"].get("large")
                                            and "default.jpg" not in c["image"]["large"]
                                            and "default.png" not in c["image"]["large"]
                                        ]
                                        
                                        if len(filtered) >= 2:
                                            char1, char2 = random.sample(filtered, 2)
                                            
                                            name1 = char1.get("name", {}).get("full") or "Unknown"
                                            m_nodes1 = char1.get("media", {}).get("nodes", [])
                                            anime1 = m_nodes1[0].get("title", {}).get("english") or m_nodes1[0].get("title", {}).get("romaji") if m_nodes1 else "Unknown Anime"
                                            img_url1 = char1.get("image", {}).get("large")
                                            
                                            name2 = char2.get("name", {}).get("full") or "Unknown"
                                            m_nodes2 = char2.get("media", {}).get("nodes", [])
                                            anime2 = m_nodes2[0].get("title", {}).get("english") or m_nodes2[0].get("title", {}).get("romaji") if m_nodes2 else "Unknown Anime"
                                            img_url2 = char2.get("image", {}).get("large")
                                            
                                            async def fetch_image_bytes(session, url):
                                                if not url: return None
                                                try:
                                                    async with session.get(url, timeout=5) as r:
                                                        if r.status == 200:
                                                            return await r.read()
                                                except Exception as e:
                                                    logger.error(f"Failed to download image {url}: {e}")
                                                return None
                                                
                                            b1 = await fetch_image_bytes(session, img_url1)
                                            b2 = await fetch_image_bytes(session, img_url2)
                                            
                                            def stitch_images(b1, b2):
                                                if b1:
                                                    try:
                                                        im1 = Image.open(io.BytesIO(b1)).convert("RGBA")
                                                    except Exception:
                                                        im1 = Image.new("RGBA", (300, 400), (220, 220, 220, 255))
                                                else:
                                                    im1 = Image.new("RGBA", (300, 400), (220, 220, 220, 255))
                                                    
                                                if b2:
                                                    try:
                                                        im2 = Image.open(io.BytesIO(b2)).convert("RGBA")
                                                    except Exception:
                                                        im2 = Image.new("RGBA", (300, 400), (220, 220, 220, 255))
                                                else:
                                                    im2 = Image.new("RGBA", (300, 400), (220, 220, 220, 255))
                                                    
                                                im1 = im1.resize((300, 400), Image.Resampling.LANCZOS)
                                                im2 = im2.resize((300, 400), Image.Resampling.LANCZOS)
                                                
                                                combined = Image.new("RGBA", (600, 400))
                                                combined.paste(im1, (0, 0))
                                                combined.paste(im2, (300, 0))
                                                
                                                out = io.BytesIO()
                                                combined.save(out, format="PNG")
                                                out.seek(0)
                                                return out
                                                
                                            loop = asyncio.get_running_loop()
                                            img_io = await loop.run_in_executor(None, stitch_images, b1, b2)
                                            file = discord.File(fp=img_io, filename="comparison.png")
                                            
                                            embed = discord.Embed(
                                                title=f"Who's the better {command}?",
                                                color=discord.Color.from_rgb(212, 175, 230)
                                            )
                                            embed.description = (
                                                f"🇦 **{name1} [0 %]**\n"
                                                f"*from {anime1}*\n\n"
                                                f"🇧 **{name2} [0 %]**\n"
                                                f"*from {anime2}*"
                                            )
                                            embed.set_image(url="attachment://comparison.png")
                                            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1509890494539370597.gif")
                                            
                                            reply_msg = await message.reply(file=file, embed=embed)
                                            await reply_msg.add_reaction("🇦")
                                            await reply_msg.add_reaction("🇧")
                                            character_found = True
                                            break
                        except Exception as e:
                            logger.error(f"AniList waifu/husbando fetch attempt failed: {e}")
                            
                    if not character_found:
                        try:
                            url = f"https://nekos.best/api/v2/{command}?amount=2"
                            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        res_data = await resp.json()
                                        results = res_data.get("results", [])
                                        if len(results) >= 2:
                                            item1 = results[0]
                                            item2 = results[1]
                                            
                                            img_url1 = item1.get("url")
                                            artist1 = item1.get("artist_name") or "Unknown Artist"
                                            
                                            img_url2 = item2.get("url")
                                            artist2 = item2.get("artist_name") or "Unknown Artist"
                                            
                                            b1 = await fetch_image_bytes(session, img_url1)
                                            b2 = await fetch_image_bytes(session, img_url2)
                                            
                                            loop = asyncio.get_running_loop()
                                            img_io = await loop.run_in_executor(None, stitch_images, b1, b2)
                                            file = discord.File(fp=img_io, filename="comparison.png")
                                            
                                            embed = discord.Embed(
                                                title=f"Who's the better {command}?",
                                                color=discord.Color.from_rgb(212, 175, 230)
                                            )
                                            embed.description = (
                                                f"🇦 **Character A [0 %]**\n"
                                                f"*Artist: {artist1}*\n\n"
                                                f"🇧 **Character B [0 %]**\n"
                                                f"*Artist: {artist2}*"
                                            )
                                            embed.set_image(url="attachment://comparison.png")
                                            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1509890494539370597.gif")
                                            
                                            reply_msg = await message.reply(file=file, embed=embed)
                                            await reply_msg.add_reaction("🇦")
                                            await reply_msg.add_reaction("🇧")
                                            character_found = True
                                        else:
                                            await message.reply(f"couldn't fetch enough {command} images 😭")
                                    else:
                                        await message.reply(f"couldn't fetch any {command} right now 😭")
                        except Exception as e:
                            logger.error(f"Waifu/Husbando command fallback failed: {e}")
                            await message.reply("something went wrong while fetching 😭")
                return

            elif command == "villain":
                VILLAIN_KEYWORDS = [
                    "antagonist", "villain", "evil", "criminal", "murderer",
                    "enemy", "rival", "tyrant", "demon", "psychopath",
                    "sociopath", "killer", "mastermind", "corrupt", "betrays",
                    "destroyer", "conquer", "terroris", "manipulat", "sadist",
                    "merciless", "ruthless", "feared", "notorious", "dark lord",
                    "warlord", "overlord", "threat", "massacre", "genocide"
                ]
                async with message.channel.typing():
                    character_found = False
                    for attempt in range(5):
                        try:
                            random_page = random.randint(1, 12)
                            query = """
                            query ($page: Int, $perPage: Int) {
                              Page (page: $page, perPage: $perPage) {
                                characters (sort: FAVOURITES_DESC) {
                                  id
                                  name {
                                    full
                                    native
                                  }
                                  image {
                                    large
                                  }
                                  description(asHtml: false)
                                  media (type: ANIME, sort: POPULARITY_DESC, perPage: 1) {
                                    nodes {
                                      title {
                                        romaji
                                        english
                                      }
                                    }
                                  }
                                }
                              }
                            }
                            """
                            variables = {"page": random_page, "perPage": 50}
                            url = "https://graphql.anilist.co"

                            async with aiohttp.ClientSession() as session:
                                async with session.post(url, json={"query": query, "variables": variables}) as resp:
                                    if resp.status == 200:
                                        res_data = await resp.json()
                                        characters = res_data.get("data", {}).get("Page", {}).get("characters", [])

                                        # Filter characters whose description contains villain keywords
                                        filtered = []
                                        for c in characters:
                                            desc = (c.get("description") or "").lower()
                                            has_image = (
                                                c.get("image") and c["image"].get("large")
                                                and "default.jpg" not in c["image"]["large"]
                                                and "default.png" not in c["image"]["large"]
                                            )
                                            if has_image and any(kw in desc for kw in VILLAIN_KEYWORDS):
                                                filtered.append(c)

                                        if len(filtered) >= 2:
                                            char1, char2 = random.sample(filtered, 2)

                                            name1 = char1.get("name", {}).get("full") or "Unknown"
                                            m_nodes1 = char1.get("media", {}).get("nodes", [])
                                            anime1 = m_nodes1[0].get("title", {}).get("english") or m_nodes1[0].get("title", {}).get("romaji") if m_nodes1 else "Unknown Anime"
                                            img_url1 = char1.get("image", {}).get("large")

                                            name2 = char2.get("name", {}).get("full") or "Unknown"
                                            m_nodes2 = char2.get("media", {}).get("nodes", [])
                                            anime2 = m_nodes2[0].get("title", {}).get("english") or m_nodes2[0].get("title", {}).get("romaji") if m_nodes2 else "Unknown Anime"
                                            img_url2 = char2.get("image", {}).get("large")

                                            async def fetch_villain_img(session, img_url):
                                                if not img_url: return None
                                                try:
                                                    async with session.get(img_url, timeout=5) as r:
                                                        if r.status == 200:
                                                            return await r.read()
                                                except Exception as e:
                                                    logger.error(f"Failed to download villain image {img_url}: {e}")
                                                return None

                                            b1 = await fetch_villain_img(session, img_url1)
                                            b2 = await fetch_villain_img(session, img_url2)

                                            def stitch_villain_images(b1, b2):
                                                if b1:
                                                    try:
                                                        im1 = Image.open(io.BytesIO(b1)).convert("RGBA")
                                                    except Exception:
                                                        im1 = Image.new("RGBA", (300, 400), (40, 10, 10, 255))
                                                else:
                                                    im1 = Image.new("RGBA", (300, 400), (40, 10, 10, 255))

                                                if b2:
                                                    try:
                                                        im2 = Image.open(io.BytesIO(b2)).convert("RGBA")
                                                    except Exception:
                                                        im2 = Image.new("RGBA", (300, 400), (40, 10, 10, 255))
                                                else:
                                                    im2 = Image.new("RGBA", (300, 400), (40, 10, 10, 255))

                                                im1 = im1.resize((300, 400), Image.Resampling.LANCZOS)
                                                im2 = im2.resize((300, 400), Image.Resampling.LANCZOS)

                                                combined = Image.new("RGBA", (600, 400))
                                                combined.paste(im1, (0, 0))
                                                combined.paste(im2, (300, 0))

                                                out = io.BytesIO()
                                                combined.save(out, format="PNG")
                                                out.seek(0)
                                                return out

                                            loop = asyncio.get_running_loop()
                                            img_io = await loop.run_in_executor(None, stitch_villain_images, b1, b2)
                                            file = discord.File(fp=img_io, filename="comparison.png")

                                            embed = discord.Embed(
                                                title="Who's the better villain? 😈",
                                                color=discord.Color.from_rgb(180, 30, 30)
                                            )
                                            embed.description = (
                                                f"🇦 **{name1} [0 %]**\n"
                                                f"*from {anime1}*\n\n"
                                                f"🇧 **{name2} [0 %]**\n"
                                                f"*from {anime2}*"
                                            )
                                            embed.set_image(url="attachment://comparison.png")

                                            reply_msg = await message.reply(file=file, embed=embed)
                                            await reply_msg.add_reaction("🇦")
                                            await reply_msg.add_reaction("🇧")
                                            character_found = True
                                            break
                        except Exception as e:
                            logger.error(f"AniList villain fetch attempt failed: {e}")

                    if not character_found:
                        await message.reply("couldn't fetch any villains right now... they're busy scheming 😈")
                return

            elif command in ["mkkf", "mkkm"]:
                async with message.channel.typing():
                    character_found = False
                    for _ in range(3):
                        try:
                            random_page = random.randint(1, 8)
                            query = """
                            query ($page: Int, $perPage: Int) {
                              Page (page: $page, perPage: $perPage) {
                                characters (sort: FAVOURITES_DESC) {
                                  id
                                  name {
                                    full
                                    native
                                  }
                                  image {
                                    large
                                  }
                                  gender
                                  media (type: ANIME, sort: POPULARITY_DESC, perPage: 1) {
                                    nodes {
                                      title {
                                        romaji
                                        english
                                      }
                                    }
                                  }
                                }
                              }
                            }
                            """
                            variables = {"page": random_page, "perPage": 50}
                            url = "https://graphql.anilist.co"
                            
                            async with aiohttp.ClientSession() as session:
                                async with session.post(url, json={"query": query, "variables": variables}) as resp:
                                    if resp.status == 200:
                                        res_data = await resp.json()
                                        characters = res_data.get("data", {}).get("Page", {}).get("characters", [])
                                        
                                        target_gender = "female" if command == "mkkf" else "male"
                                        filtered = [
                                            c for c in characters 
                                            if c.get("gender") and c["gender"].strip().lower() == target_gender
                                            and c.get("image") and c["image"].get("large")
                                            and "default.jpg" not in c["image"]["large"]
                                            and "default.png" not in c["image"]["large"]
                                        ]
                                        
                                        if len(filtered) >= 3:
                                            char1, char2, char3 = random.sample(filtered, 3)
                                            
                                            name1 = char1.get("name", {}).get("full") or "Unknown"
                                            m_nodes1 = char1.get("media", {}).get("nodes", [])
                                            anime1 = m_nodes1[0].get("title", {}).get("english") or m_nodes1[0].get("title", {}).get("romaji") if m_nodes1 else "Unknown Anime"
                                            img_url1 = char1.get("image", {}).get("large")
                                            
                                            name2 = char2.get("name", {}).get("full") or "Unknown"
                                            m_nodes2 = char2.get("media", {}).get("nodes", [])
                                            anime2 = m_nodes2[0].get("title", {}).get("english") or m_nodes2[0].get("title", {}).get("romaji") if m_nodes2 else "Unknown Anime"
                                            img_url2 = char2.get("image", {}).get("large")

                                            name3 = char3.get("name", {}).get("full") or "Unknown"
                                            m_nodes3 = char3.get("media", {}).get("nodes", [])
                                            anime3 = m_nodes3[0].get("title", {}).get("english") or m_nodes3[0].get("title", {}).get("romaji") if m_nodes3 else "Unknown Anime"
                                            img_url3 = char3.get("image", {}).get("large")
                                            
                                            async def fetch_image_bytes(session, url):
                                                if not url: return None
                                                try:
                                                    async with session.get(url, timeout=5) as r:
                                                        if r.status == 200:
                                                            return await r.read()
                                                except Exception as e:
                                                    logger.error(f"Failed to download image {url}: {e}")
                                                return None
                                                
                                            b1 = await fetch_image_bytes(session, img_url1)
                                            b2 = await fetch_image_bytes(session, img_url2)
                                            b3 = await fetch_image_bytes(session, img_url3)
                                            
                                            def stitch_mkk_images(b1, b2, b3):
                                                def load_img(b):
                                                    if b:
                                                        try:
                                                            return Image.open(io.BytesIO(b)).convert("RGBA")
                                                        except Exception:
                                                            pass
                                                    return Image.new("RGBA", (300, 400), (220, 220, 220, 255))
                                                    
                                                im1 = load_img(b1).resize((300, 400), Image.Resampling.LANCZOS)
                                                im2 = load_img(b2).resize((300, 400), Image.Resampling.LANCZOS)
                                                im3 = load_img(b3).resize((300, 400), Image.Resampling.LANCZOS)
                                                
                                                combined = Image.new("RGBA", (900, 400))
                                                combined.paste(im1, (0, 0))
                                                combined.paste(im2, (300, 0))
                                                combined.paste(im3, (600, 0))
                                                
                                                out = io.BytesIO()
                                                combined.save(out, format="PNG")
                                                out.seek(0)
                                                return out
                                                
                                            loop = asyncio.get_running_loop()
                                            img_io = await loop.run_in_executor(None, stitch_mkk_images, b1, b2, b3)
                                            file = discord.File(fp=img_io, filename="comparison.png")
                                            
                                            embed = discord.Embed(
                                                title="Marry, Kiss, Kill? 💍💋💀",
                                                color=discord.Color.from_rgb(212, 175, 230)
                                            )
                                            embed.description = (
                                                f"🇦 **{name1}**\n"
                                                f"*from {anime1}*\n\n"
                                                f"🇧 **{name2}**\n"
                                                f"*from {anime2}*\n\n"
                                                f"🇨 **{name3}**\n"
                                                f"*from {anime3}*"
                                            )
                                            embed.set_image(url="attachment://comparison.png")
                                            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1509890494539370597.gif")
                                            
                                            reply_msg = await message.reply(file=file, embed=embed)
                                            await reply_msg.add_reaction("🇦")
                                            await reply_msg.add_reaction("🇧")
                                            await reply_msg.add_reaction("🇨")
                                            character_found = True
                                            break
                        except Exception as e:
                            logger.error(f"AniList MKK fetch attempt failed: {e}")
                            
                    if not character_found:
                        try:
                            fallback_endpoint = "waifu" if command == "mkkf" else "husbando"
                            url = f"https://nekos.best/api/v2/{fallback_endpoint}?amount=3"
                            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        res_data = await resp.json()
                                        results = res_data.get("results", [])
                                        if len(results) >= 3:
                                            item1, item2, item3 = results[0], results[1], results[2]
                                            
                                            img_url1 = item1.get("url")
                                            artist1 = item1.get("artist_name") or "Unknown Artist"
                                            
                                            img_url2 = item2.get("url")
                                            artist2 = item2.get("artist_name") or "Unknown Artist"

                                            img_url3 = item3.get("url")
                                            artist3 = item3.get("artist_name") or "Unknown Artist"
                                            
                                            b1 = await fetch_image_bytes(session, img_url1)
                                            b2 = await fetch_image_bytes(session, img_url2)
                                            b3 = await fetch_image_bytes(session, img_url3)
                                            
                                            loop = asyncio.get_running_loop()
                                            img_io = await loop.run_in_executor(None, stitch_mkk_images, b1, b2, b3)
                                            file = discord.File(fp=img_io, filename="comparison.png")
                                            
                                            embed = discord.Embed(
                                                title="Marry, Kiss, Kill? 💍💋💀",
                                                color=discord.Color.from_rgb(212, 175, 230)
                                            )
                                            embed.description = (
                                                f"🇦 **Character A**\n"
                                                f"*Artist: {artist1}*\n\n"
                                                f"🇧 **Character B**\n"
                                                f"*Artist: {artist2}*\n\n"
                                                f"🇨 **Character C**\n"
                                                f"*Artist: {artist3}*"
                                            )
                                            embed.set_image(url="attachment://comparison.png")
                                            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1509890494539370597.gif")
                                            
                                            reply_msg = await message.reply(file=file, embed=embed)
                                            await reply_msg.add_reaction("🇦")
                                            await reply_msg.add_reaction("🇧")
                                            await reply_msg.add_reaction("🇨")
                                            character_found = True
                                        else:
                                            await message.reply(f"couldn't fetch enough images 😭")
                                    else:
                                        await message.reply(f"couldn't fetch any characters right now 😭")
                        except Exception as e:
                            logger.error(f"MKK command fallback failed: {e}")
                            await message.reply("something went wrong while fetching 😭")
                return

            elif command == "quote":
                # Parse target and text
                target = None
                quote_text = ""
                
                # 1. Check if replying to a message
                if message.reference and message.reference.message_id:
                    try:
                        ref_msg = await message.channel.fetch_message(message.reference.message_id)
                        target = ref_msg.author
                        quote_text = ref_msg.content
                    except Exception:
                        pass
                
                # 2. Check if user is mentioned
                if message.mentions:
                    target = message.mentions[0]
                
                # 3. Check if text args are provided
                if args.strip():
                    parsed_text = args.strip()
                    if message.mentions:
                        mention_str = f"<@{target.id}>"
                        mention_nick_str = f"<@!{target.id}>"
                        if parsed_text.startswith(mention_str):
                            parsed_text = parsed_text[len(mention_str):].strip()
                        elif parsed_text.startswith(mention_nick_str):
                            parsed_text = parsed_text[len(mention_nick_str):].strip()
                        else:
                            words = parsed_text.split()
                            for word in words:
                                if word.startswith("<@") and word.endswith(">"):
                                    parsed_text = parsed_text.replace(word, "", 1).strip()
                                    break
                    if parsed_text:
                        quote_text = parsed_text
                
                # Fallback to message author
                if not target:
                    target = message.author
                
                if not quote_text:
                    await message.reply("you need to provide a quote or reply to a message, dummy 🙄")
                    return
                
                async with message.channel.typing():
                    try:
                        # Fetch target avatar bytes
                        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url) as resp:
                                if resp.status != 200:
                                    await message.reply("couldn't fetch the user avatar 😭")
                                    return
                                avatar_bytes = await resp.read()
                        
                        # Process Quote Image
                        from PIL import ImageDraw, ImageFont, ImageOps
                        
                        # Solid dark background
                        bg_color = (15, 15, 18, 255)
                        img = Image.new("RGBA", (800, 300), bg_color)
                        
                        # Paste avatar on the left
                        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                        avatar_resized = avatar_img.resize((300, 300), Image.Resampling.LANCZOS)
                        img.paste(avatar_resized, (0, 0))
                        
                        # Apply horizontal black fade from x=100 to x=280 to blend into the solid background
                        gradient = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
                        grad_draw = ImageDraw.Draw(gradient)
                        for x in range(300):
                            if x < 100:
                                alpha = 0
                            elif x < 280:
                                alpha = int(255 * (x - 100) / 180)
                            else:
                                alpha = 255
                            grad_draw.line([(x, 0), (x, 300)], fill=(15, 15, 18, alpha))
                        img.paste(gradient, (0, 0), mask=gradient)
                        
                        # Draw outline border
                        draw = ImageDraw.Draw(img)
                        draw.rectangle([5, 5, 795, 295], outline=(212, 175, 230, 60), width=2)
                        
                        # Load Fonts
                        font_quote_path = os.path.join(BASE_DIR, "assets", "fonts", "CrimsonText-Italic.ttf")
                        font_author_path = os.path.join(BASE_DIR, "assets", "fonts", "Roboto-Regular.ttf")
                        
                        full_quote = f'“ {quote_text} ”'
                        
                        # Adjust font size dynamically based on length
                        quote_len = len(full_quote)
                        if quote_len > 150:
                            quote_font_size = 20
                            line_spacing = 26
                        elif quote_len > 80:
                            quote_font_size = 24
                            line_spacing = 30
                        else:
                            quote_font_size = 28
                            line_spacing = 36
                            
                        font_quote = ImageFont.truetype(font_quote_path, quote_font_size)
                        font_author = ImageFont.truetype(font_author_path, 18)
                        
                        # Wrap quote text (width limit: 440px)
                        max_text_width = 440
                        words = full_quote.split()
                        lines = []
                        current_line = []
                        for word in words:
                            test_line = " ".join(current_line + [word])
                            try:
                                bbox = draw.textbbox((0, 0), test_line, font=font_quote)
                                w = bbox[2] - bbox[0]
                            except Exception:
                                w = len(test_line) * (quote_font_size * 0.5)
                            if w <= max_text_width:
                                current_line.append(word)
                            else:
                                if current_line:
                                    lines.append(" ".join(current_line))
                                current_line = [word]
                        if current_line:
                            lines.append(" ".join(current_line))
                        
                        # Center block vertically on the right side
                        total_text_height = len(lines) * line_spacing
                        author_height = 24
                        total_block_height = total_text_height + 15 + author_height
                        start_y = (300 - total_block_height) // 2
                        
                        # Draw quote lines
                        current_y = start_y
                        for line in lines:
                            draw.text((320, current_y), line, fill=(245, 240, 250, 255), font=font_quote)
                            current_y += line_spacing
                            
                        # Draw author name
                        author_text = f"— {target.display_name}"
                        draw.text((320, current_y + 15), author_text, fill=(212, 175, 230, 255), font=font_author)
                        
                        # Save and reply
                        out_io = io.BytesIO()
                        img.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        file = discord.File(out_io, filename="quote.png")
                        await message.reply(file=file)
                        
                    except Exception as err:
                        logger.error(f"Quote command failed: {err}", exc_info=True)
                        await message.reply("something went wrong while generating the quote card 😭")
                return

            elif command in ["cat", "dog"]:
                async with message.channel.typing():
                    try:
                        if command == "cat":
                            url = "https://api.thecatapi.com/v1/images/search"
                        else:
                            url = "https://dog.ceo/api/breeds/image/random"
                            
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as resp:
                                if resp.status == 200:
                                    res_data = await resp.json()
                                    if command == "cat":
                                        img_url = res_data[0]["url"]
                                    else:
                                        img_url = res_data["message"]
                                        
                                    embed = discord.Embed(
                                        title=f"Random cute {command}!",
                                        color=discord.Color.from_rgb(255, 223, 186)
                                    )
                                    embed.set_image(url=img_url)
                                    embed.set_footer(text=f"A wild {command} appeared!")
                                    await message.reply(embed=embed)
                                else:
                                    await message.reply(f"couldn't fetch a {command} image 😭")
                    except Exception as e:
                        logger.error(f"Cat/Dog command failed: {e}")
                        await message.reply("something went wrong while fetching 😭")
                return

            # --- E-FAMILY COMMANDS ---
            elif command == "marry":
                if not message.mentions:
                    await message.reply("you need to mention who you want to marry, dummy 🙄\nUsage: `$marry [@user]`")
                    return
                target = message.mentions[0]
                if target.id == message.author.id:
                    await message.reply("you can't marry yourself, dummy! 🙄")
                    return
                if target.bot:
                    await message.reply("you can't marry a bot! i don't have feelings like that 🙄")
                    return
                
                author_id_str = str(message.author.id)
                target_id_str = str(target.id)
                
                author_fam = await db.get_family(author_id_str)
                target_fam = await db.get_family(target_id_str)
                
                if author_fam.get("spouse"):
                    current_spouse = await get_user_name(author_fam["spouse"], bot)
                    await message.reply(f"you are already married to **{current_spouse}**! Divorce them first! 🙄")
                    return
                if target_fam.get("spouse"):
                    current_spouse = await get_user_name(target_fam["spouse"], bot)
                    await message.reply(f"**{target.display_name}** is already married to **{current_spouse}**! 🙄")
                    return
                
                if target_id_str in author_fam.get("children", []):
                    await message.reply("you can't marry your own child! That's weird! 🙄")
                    return
                if target_id_str in author_fam.get("parents", []):
                    await message.reply("you can't marry your own parent! That's weird! 🙄")
                    return
                shared_parents = set(author_fam.get("parents", [])).intersection(set(target_fam.get("parents", [])))
                if shared_parents:
                    await message.reply("you can't marry your sibling! That's weird! 🙄")
                    return

                embed = discord.Embed(
                    title="💍 Marriage Proposal 💍",
                    description=f"{target.mention}, **{message.author.display_name}** has proposed to you! Do you accept? 💖",
                    color=discord.Color.from_rgb(255, 182, 193)
                )
                view = ProposalView(message.author, target, "marry")
                proposal_msg = await message.reply(embed=embed, view=view)
                
                await view.wait()
                if view.accepted:
                    author_fam = await db.get_family(author_id_str)
                    target_fam = await db.get_family(target_id_str)
                    
                    if author_fam.get("spouse") or target_fam.get("spouse"):
                        await proposal_msg.edit(content="💔 Marriage proposal failed: one of you got married in the meantime! 🙄", embed=None, view=None)
                        return
                        
                    author_fam["spouse"] = target_id_str
                    target_fam["spouse"] = author_id_str
                    await db.save_family(author_fam)
                    await db.save_family(target_fam)
                    
                    success_embed = discord.Embed(
                        title="🍾 Just Married! 🎉",
                        description=f"💖 **{target.display_name}** accepted **{message.author.display_name}**'s proposal! Congratulations! 💖",
                        color=discord.Color.from_rgb(255, 105, 180)
                    )
                    await proposal_msg.edit(embed=success_embed, view=None)
                else:
                    decline_embed = discord.Embed(
                        title="💔 Proposal Declined 💔",
                        description=f"**{target.display_name}** declined **{message.author.display_name}**'s proposal. Ouch...",
                        color=discord.Color.from_rgb(255, 105, 97)
                    )
                    await proposal_msg.edit(embed=decline_embed, view=None)
                return

            elif command == "adopt":
                if not message.mentions:
                    await message.reply("you need to mention who you want to adopt, dummy 🙄\nUsage: `$adopt [@user]`")
                    return
                target = message.mentions[0]
                if target.id == message.author.id:
                    await message.reply("you can't adopt yourself, dummy! 🙄")
                    return
                if target.bot:
                    await message.reply("you can't adopt a bot! 🙄")
                    return
                
                author_id_str = str(message.author.id)
                target_id_str = str(target.id)
                
                author_fam = await db.get_family(author_id_str)
                target_fam = await db.get_family(target_id_str)
                
                if target_id_str in author_fam.get("children", []):
                    await message.reply(f"**{target.display_name}** is already your child, dummy 🙄")
                    return
                if target_id_str in author_fam.get("parents", []):
                    await message.reply("they are your parent! You can't adopt your parent, dummy 🙄")
                    return
                if author_fam.get("spouse") == target_id_str:
                    await message.reply("you can't adopt your spouse, dummy 🙄")
                    return
                if len(target_fam.get("parents", [])) >= 2:
                    await message.reply(f"**{target.display_name}** already has 2 parents! They can't be adopted again. 🙄")
                    return
                
                if await is_ancestor(author_id_str, target_id_str):
                    await message.reply(f"you cannot adopt **{target.display_name}** because they are your ancestor! 🙄")
                    return
                if await is_ancestor(target_id_str, author_id_str):
                    await message.reply(f"**{target.display_name}** is already your descendant, dummy! 🙄")
                    return

                embed = discord.Embed(
                    title="👶 Adoption Request 👶",
                    description=f"{target.mention}, **{message.author.display_name}** wants to adopt you! Do you accept? 🍼",
                    color=discord.Color.from_rgb(173, 216, 230)
                )
                view = ProposalView(message.author, target, "adopt")
                proposal_msg = await message.reply(embed=embed, view=view)
                
                await view.wait()
                if view.accepted:
                    author_fam = await db.get_family(author_id_str)
                    target_fam = await db.get_family(target_id_str)
                    
                    if len(target_fam.get("parents", [])) >= 2:
                        await proposal_msg.edit(content=f"💔 Adoption failed: **{target.display_name}** reached the maximum number of parents in the meantime! 🙄", embed=None, view=None)
                        return
                    
                    if target_id_str not in author_fam["children"]:
                        author_fam["children"].append(target_id_str)
                    if author_id_str not in target_fam["parents"]:
                        target_fam["parents"].append(author_id_str)
                        
                    await db.save_family(author_fam)
                    await db.save_family(target_fam)
                    
                    success_embed = discord.Embed(
                        title="🎉 Adoption Finalized! 🍼",
                        description=f"✨ **{message.author.display_name}** has adopted **{target.display_name}**! Welcome to the family! ✨",
                        color=discord.Color.from_rgb(144, 238, 144)
                    )
                    await proposal_msg.edit(embed=success_embed, view=None)
                else:
                    decline_embed = discord.Embed(
                        title="❌ Adoption Declined 💔",
                        description=f"**{target.display_name}** declined **{message.author.display_name}**'s adoption request.",
                        color=discord.Color.from_rgb(255, 105, 97)
                    )
                    await proposal_msg.edit(embed=decline_embed, view=None)
                return

            elif command == "divorce":
                author_id_str = str(message.author.id)
                author_fam = await db.get_family(author_id_str)
                spouse_id = author_fam.get("spouse")
                
                if not spouse_id:
                    await message.reply("you are not married, dummy! 🙄")
                    return
                
                spouse_name = await get_user_name(spouse_id, bot)
                spouse_fam = await db.get_family(spouse_id)
                
                author_fam["spouse"] = None
                spouse_fam["spouse"] = None
                
                await db.save_family(author_fam)
                await db.save_family(spouse_fam)
                
                embed = discord.Embed(
                    title="💔 Divorced 💔",
                    description=f"**{message.author.display_name}** has divorced **{spouse_name}**. It's officially over...",
                    color=discord.Color.from_rgb(255, 105, 97)
                )
                await message.reply(embed=embed)
                return

            elif command == "disown":
                if not message.mentions:
                    await message.reply("you need to mention who you want to disown, dummy 🙄\nUsage: `$disown [@user]`")
                    return
                target = message.mentions[0]
                author_id_str = str(message.author.id)
                target_id_str = str(target.id)
                
                author_fam = await db.get_family(author_id_str)
                if target_id_str not in author_fam.get("children", []):
                    await message.reply(f"**{target.display_name}** is not your child, dummy! 🙄")
                    return
                
                author_fam["children"].remove(target_id_str)
                target_fam = await db.get_family(target_id_str)
                if author_id_str in target_fam.get("parents", []):
                    target_fam["parents"].remove(author_id_str)
                    
                await db.save_family(author_fam)
                await db.save_family(target_fam)
                
                embed = discord.Embed(
                    title="🛑 Disowned 😤",
                    description=f"**{message.author.display_name}** has disowned **{target.display_name}**! They are no longer your child.",
                    color=discord.Color.from_rgb(255, 69, 0)
                )
                await message.reply(embed=embed)
                return

            elif command == "abandon":
                if not message.mentions:
                    await message.reply("you need to mention which parent you want to abandon, dummy 🙄\nUsage: `$abandon [@user]`")
                    return
                target = message.mentions[0]
                author_id_str = str(message.author.id)
                target_id_str = str(target.id)
                
                author_fam = await db.get_family(author_id_str)
                if target_id_str not in author_fam.get("parents", []):
                    await message.reply(f"**{target.display_name}** is not your parent, dummy! 🙄")
                    return
                
                author_fam["parents"].remove(target_id_str)
                target_fam = await db.get_family(target_id_str)
                if author_id_str in target_fam.get("children", []):
                    target_fam["children"].remove(author_id_str)
                    
                await db.save_family(author_fam)
                await db.save_family(target_fam)
                
                embed = discord.Embed(
                    title="🏃 Parent Abandoned ✌️",
                    description=f"**{message.author.display_name}** has abandoned their parent **{target.display_name}**!",
                    color=discord.Color.from_rgb(255, 69, 0)
                )
                await message.reply(embed=embed)
                return

            elif command == "runaway":
                author_id_str = str(message.author.id)
                author_fam = await db.get_family(author_id_str)
                parent_ids = author_fam.get("parents", [])
                
                if not parent_ids:
                    await message.reply("you don't even have any parents to run away from, dummy! 🙄")
                    return
                
                # Fetch and remove from all parents
                for p_id in parent_ids:
                    parent_fam = await db.get_family(p_id)
                    if author_id_str in parent_fam.get("children", []):
                        parent_fam["children"].remove(author_id_str)
                    await db.save_family(parent_fam)
                
                author_fam["parents"] = []
                await db.save_family(author_fam)
                
                embed = discord.Embed(
                    title="✨ Runaway! 🎒",
                    description=f"🏃 **{message.author.display_name}** has run away from home and is now independent! ✌️",
                    color=discord.Color.from_rgb(255, 69, 0)
                )
                await message.reply(embed=embed)
                return

            elif command == "disownall":
                author_id_str = str(message.author.id)
                author_fam = await db.get_family(author_id_str)
                children_ids = author_fam.get("children", [])
                
                if not children_ids:
                    await message.reply("you don't even have any children to disown, dummy! 🙄")
                    return
                
                # Fetch and remove from all children
                for c_id in children_ids:
                    child_fam = await db.get_family(c_id)
                    if author_id_str in child_fam.get("parents", []):
                        child_fam["parents"].remove(author_id_str)
                    await db.save_family(child_fam)
                
                author_fam["children"] = []
                await db.save_family(author_fam)
                
                embed = discord.Embed(
                    title="💔 Disowned All Children 🛑",
                    description=f"😤 **{message.author.display_name}** has disowned all of their children! They are officially childless now.",
                    color=discord.Color.from_rgb(255, 69, 0)
                )
                await message.reply(embed=embed)
                return

            elif command == "family":
                target = message.mentions[0] if message.mentions else message.author
                target_id_str = str(target.id)
                
                async with message.channel.typing():
                    try:
                        target_fam = await db.get_family(target_id_str)
                        spouse_id = target_fam.get("spouse")
                        spouse_name = await get_guild_member_name(message.guild, spouse_id, bot) if spouse_id else "None"
                        
                        parent_ids = target_fam.get("parents", [])
                        parents_names = []
                        for p_id in parent_ids:
                            p_name = await get_guild_member_name(message.guild, p_id, bot)
                            parents_names.append(p_name)
                        parents_str = ", ".join(parents_names) if parents_names else "None"
                        
                        sibling_ids = set()
                        for p_id in parent_ids:
                            p_fam = await db.get_family(p_id)
                            for sib_id in p_fam.get("children", []):
                                if sib_id != target_id_str:
                                    sibling_ids.add(sib_id)
                        siblings_names = []
                        for sib_id in sibling_ids:
                            sib_name = await get_guild_member_name(message.guild, sib_id, bot)
                            siblings_names.append(sib_name)
                        siblings_str = ", ".join(siblings_names) if siblings_names else "None"
                        
                        children_ids = target_fam.get("children", [])
                        children_names = []
                        grandchildren_names = []
                        for c_id in children_ids:
                            c_name = await get_guild_member_name(message.guild, c_id, bot)
                            children_names.append(c_name)
                            c_fam = await db.get_family(c_id)
                            for gc_id in c_fam.get("children", []):
                                gc_name = await get_guild_member_name(message.guild, gc_id, bot)
                                grandchildren_names.append(gc_name)
                        children_str = ", ".join(children_names) if children_names else "None"
                        grandchildren_str = ", ".join(grandchildren_names) if grandchildren_names else "None"

                        img_bytes = await generate_family_tree_image(target_id_str, message.guild, bot)
                        output_buffer = io.BytesIO(img_bytes)
                        file = discord.File(fp=output_buffer, filename="family_tree.png")
                        
                        embed = discord.Embed(
                            title=f"👪 Family of {target.display_name}",
                            color=discord.Color.from_rgb(46, 139, 87)
                        )
                        embed.add_field(name="💍 Married to", value=spouse_name, inline=True)
                        embed.add_field(name="👨‍👩‍👦 Parents", value=parents_str, inline=True)
                        embed.add_field(name="👦👧 Siblings", value=siblings_str, inline=True)
                        embed.add_field(name="👶 Children", value=children_str, inline=True)
                        if grandchildren_names:
                            embed.add_field(name="🍼 Grandchildren", value=grandchildren_str, inline=True)
                        
                        embed.set_image(url="attachment://family_tree.png")
                        await message.reply(file=file, embed=embed)
                    except Exception as e:
                        logger.error(f"Failed to generate family tree image: {e}")
                        await message.reply("something went wrong while drawing your family tree 😭")
                return

            elif command == "ping":
                latency = round(bot.latency * 1000)
                await message.reply(f"pong! 🏓 Latency is **{latency}ms**.")
                return

            elif command == "uptime":
                duration = current_time - bot_config.start_time
                m, s = divmod(int(duration), 60)
                h, m = divmod(m, 60)
                d, h = divmod(h, 24)
                
                uptime_str = f"{s}s"
                if m > 0: uptime_str = f"{m}m {uptime_str}"
                if h > 0: uptime_str = f"{h}h {uptime_str}"
                if d > 0: uptime_str = f"{d}d {uptime_str}"
                
                await message.reply(f"i've been awake for **{uptime_str}**. too long if you ask me 🙄")
                return

            elif command in ["avatar", "av"]:
                target = message.mentions[0] if message.mentions else message.author
                embed = discord.Embed(
                    title=f"{target.display_name}'s Avatar",
                    color=discord.Color.from_rgb(212, 175, 230)
                )
                if target.avatar:
                    embed.set_image(url=target.avatar.url)
                    await message.reply(embed=embed)
                else:
                    await message.reply("they don't even have a custom avatar lol 💀")
                return

            elif command in ["server", "serverinfo"]:
                if not message.guild:
                    await message.reply("this command only works in servers, duh 🙄")
                    return
                
                guild = message.guild
                embed = discord.Embed(
                    title=f"🏛️ {guild.name} Info 🏛️",
                    color=discord.Color.from_rgb(212, 175, 230)
                )
                if guild.icon:
                    embed.set_thumbnail(url=guild.icon.url)
                
                embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
                embed.add_field(name="Members", value=str(guild.member_count), inline=True)
                embed.add_field(name="Created At", value=guild.created_at.strftime("%d %B %Y"), inline=True)
                embed.add_field(name="Roles Count", value=str(len(guild.roles)), inline=True)
                embed.add_field(name="Emoji Count", value=str(len(guild.emojis)), inline=True)
                
                await message.reply(embed=embed)
                return

            elif command in ["truth", "dare"]:
                async with message.channel.typing():
                    try:
                        content = await ai.get_truth_or_dare(command)
                        
                        embed_color = discord.Color.from_rgb(212, 175, 230) if command == "truth" else discord.Color.from_rgb(220, 20, 60)
                        title_str = "🔮 Truth 🔮" if command == "truth" else "🔥 Dare 🔥"
                        
                        embed = discord.Embed(
                            title=title_str,
                            description=f"### {content}",
                            color=embed_color
                        )
                        embed.set_footer(text=f"Requested by {message.author.display_name}")
                        await message.reply(embed=embed)
                    except Exception as e:
                        logger.error(f"Failed to generate {command}: {e}", exc_info=True)
                        await message.reply(f"couldn't get a {command} right now 😭")
                return

            elif command == "simp":
                target = message.mentions[0] if message.mentions else message.author
                async with message.channel.typing():
                    try:
                        # Fetch target avatar bytes
                        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url) as resp:
                                if resp.status != 200:
                                    await message.reply("couldn't fetch the user avatar 😭")
                                    return
                                avatar_bytes = await resp.read()
                        
                        from PIL import ImageDraw, ImageFont
                        
                        # Generate 600x400 card
                        card = Image.new("RGBA", (600, 400), (255, 182, 193, 255))
                        draw = ImageDraw.Draw(card)
                        for y in range(400):
                            r = int(255 - (75 * y / 400))
                            g = int(182 - (162 * y / 400))
                            b = int(193 - (133 * y / 400))
                            draw.line([(0, y), (600, y)], fill=(r, g, b, 255))
                        
                        # Draw outline border
                        draw.rectangle([10, 10, 590, 390], outline=(255, 255, 255, 200), width=4)
                        
                        # Load Fonts
                        font_header_path = os.path.join(BASE_DIR, "assets", "fonts", "Anton-Regular.ttf")
                        font_regular_path = os.path.join(BASE_DIR, "assets", "fonts", "Roboto-Bold.ttf")
                        font_footer_path = os.path.join(BASE_DIR, "assets", "fonts", "CrimsonText-Italic.ttf")
                        
                        font_header = ImageFont.truetype(font_header_path, 42)
                        font_regular = ImageFont.truetype(font_regular_path, 22)
                        font_footer = ImageFont.truetype(font_footer_path, 20)
                        
                        # Draw Header
                        header_text = "OFFICIAL CERTIFIED SIMP"
                        try:
                            bbox = draw.textbbox((0, 0), header_text, font=font_header)
                            w = bbox[2] - bbox[0]
                        except:
                            w = len(header_text) * 20
                        draw.text(((600 - w) // 2, 30), header_text, fill=(255, 255, 255, 255), font=font_header)
                        
                        # Load avatar and round it
                        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                        av_size = 140
                        avatar_resized = avatar_img.resize((av_size, av_size), Image.Resampling.LANCZOS)
                        
                        mask = Image.new("L", (av_size, av_size), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse([0, 0, av_size, av_size], fill=255)
                        
                        card.paste(avatar_resized, (50, 120), mask=mask)
                        
                        # Draw white border around avatar
                        draw.ellipse([47, 117, 193, 263], outline=(255, 255, 255, 255), width=3)
                        
                        # Simp Level Logic (seed-based so same user gets same rating daily)
                        import hashlib
                        from datetime import date
                        seed_str = f"{target.id}-{date.today()}"
                        rating_hash = hashlib.sha256(seed_str.encode()).hexdigest()
                        simp_rating = int(rating_hash, 16) % 101
                        
                        if simp_rating < 20:
                            status = "Unmoved / Sigma"
                            comment = "immune to charms. a rare breed."
                        elif simp_rating < 50:
                            status = "Casual Admirer"
                            comment = "likes them, but keeps their cool."
                        elif simp_rating < 80:
                            status = "Certified Simp"
                            comment = "definitely funding their lifestyle."
                        else:
                            status = "Terminal / Loyal Dog"
                            comment = "woofs on command. completely gone."
                        
                        # Draw Simp Info
                        name_text = f"Subject: {target.display_name}"
                        rating_text = f"Simp Level: {simp_rating}%"
                        status_text = f"Status: {status}"
                        comment_text = f"\"{comment}\""
                        
                        draw.text((220, 120), name_text, fill=(255, 255, 255, 255), font=font_regular)
                        draw.text((220, 160), rating_text, fill=(255, 255, 255, 255), font=font_regular)
                        draw.text((220, 200), status_text, fill=(255, 255, 255, 255), font=font_regular)
                        draw.text((220, 240), comment_text, fill=(255, 240, 245, 220), font=font_footer)
                        
                        # Draw Footer
                        footer_text = "Approved by the Control Devil"
                        try:
                            bbox = draw.textbbox((0, 0), footer_text, font=font_footer)
                            w = bbox[2] - bbox[0]
                        except:
                            w = len(footer_text) * 10
                        draw.text(((600 - w) // 2, 330), footer_text, fill=(255, 255, 255, 180), font=font_footer)
                        
                        # Save and reply
                        out_io = io.BytesIO()
                        card.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        file = discord.File(out_io, filename="simp_card.png")
                        await message.reply(file=file)
                        
                    except Exception as err:
                        logger.error(f"Simp command failed: {err}", exc_info=True)
                        await message.reply("something went wrong while issuing the Simp Card 😭")
                return

            elif command == "wasted":
                target = message.mentions[0] if message.mentions else message.author
                async with message.channel.typing():
                    try:
                        # Fetch target avatar bytes
                        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url) as resp:
                                if resp.status != 200:
                                    await message.reply("couldn't fetch the user avatar 😭")
                                    return
                                avatar_bytes = await resp.read()
                        
                        from PIL import ImageDraw, ImageFont, ImageOps
                        
                        # Load avatar and scale to 600x600
                        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                        img = avatar_img.resize((600, 600), Image.Resampling.LANCZOS)
                        
                        # Convert to grayscale and tint reddish
                        gray = ImageOps.grayscale(img)
                        img = ImageOps.colorize(gray, black=(20, 10, 10), white=(240, 200, 200)).convert("RGBA")
                        
                        # Draw transparent black overlay bar
                        overlay = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        overlay_draw.rectangle([0, 240, 600, 360], fill=(0, 0, 0, 160))
                        img = Image.alpha_composite(img, overlay)
                        
                        # Draw WASTED text
                        draw = ImageDraw.Draw(img)
                        font_path = os.path.join(BASE_DIR, "assets", "fonts", "Anton-Regular.ttf")
                        font_wasted = ImageFont.truetype(font_path, 76)
                        
                        wasted_text = "WASTED"
                        try:
                            bbox = draw.textbbox((0, 0), wasted_text, font=font_wasted)
                            w = bbox[2] - bbox[0]
                            h = bbox[3] - bbox[1]
                            x_text = (600 - w) // 2 - bbox[0]
                            y_text = 240 + (120 - h) // 2 - bbox[1]
                        except:
                            w = len(wasted_text) * 35
                            h = 60
                            x_text = (600 - w) // 2
                            y_text = 240 + (120 - h) // 2
                        
                        # Drop shadow
                        draw.text((x_text + 4, y_text + 4), wasted_text, fill=(0, 0, 0, 255), font=font_wasted)
                        # Red text
                        draw.text((x_text, y_text), wasted_text, fill=(180, 20, 20, 255), font=font_wasted)
                        
                        # Save and reply
                        out_io = io.BytesIO()
                        img.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        file = discord.File(out_io, filename="wasted.png")
                        await message.reply(file=file)
                        
                    except Exception as err:
                        logger.error(f"Wasted command failed: {err}", exc_info=True)
                        await message.reply("something went wrong while wasting them 😭")
                return

            elif command in ["trashcan", "trash"]:
                target = message.mentions[0] if message.mentions else message.author
                async with message.channel.typing():
                    try:
                        # Fetch target avatar bytes
                        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url) as resp:
                                if resp.status != 200:
                                    await message.reply("couldn't fetch the user avatar 😭")
                                    return
                                avatar_bytes = await resp.read()
                        
                        from PIL import ImageDraw, ImageFont
                        
                        # Load local template
                        trash_path = os.path.join(BASE_DIR, "assets", "memes", "trashcan.webp")
                        img = Image.open(trash_path).convert("RGBA")
                        
                        # Load avatar
                        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                        av_size = 140
                        avatar_resized = avatar_img.resize((av_size, av_size), Image.Resampling.LANCZOS)
                        
                        # Round avatar
                        mask = Image.new("L", (av_size, av_size), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse([0, 0, av_size, av_size], fill=255)
                        
                        # Rotate avatar and mask to match the hand angle (-20 degrees)
                        angle = -20
                        av_rotated = avatar_resized.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
                        mask_rotated = mask.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
                        
                        # Paste in hand coordinates (x=265, y=175)
                        img.paste(av_rotated, (265, 175), mask=mask_rotated)
                        
                        # Save and reply
                        out_io = io.BytesIO()
                        img.save(out_io, format="PNG")
                        out_io.seek(0)
                        
                        file = discord.File(out_io, filename="trashcan.png")
                        await message.reply(file=file)
                        
                    except Exception as err:
                        logger.error(f"Trashcan command failed: {err}", exc_info=True)
                        await message.reply("something went wrong while putting them in the trash 😭")
                return

            elif command in ACTIONS or command == "random":
                action = command
                if command == "random":
                    action = random.choice(ACTIONS)

                # Determine target string
                target_str = ""
                if message.mentions:
                    target_str = message.mentions[0].display_name
                elif args:
                    target_str = args.strip()

                # Action description grammar
                if target_str:
                    verb = random.choice(ACTION_VERBS[action])
                    if "{target}" in verb:
                        description = f"**{message.author.display_name}** " + verb.format(target=f"**{target_str}**")
                    else:
                        description = f"**{message.author.display_name}** {verb} **{target_str}**!"
                else:
                    description = f"**{message.author.display_name}** " + random.choice(ACTION_SOLO[action])

                if command == "random":
                    description += f" *(Random: {action})*"

                # Fetch action GIF using multi-provider helper (otakugifs, purrbot, nekos.life, nekos.best)
                async with aiohttp.ClientSession() as session:
                    gif_url, provider = await fetch_action_gif(session, action)
                    if gif_url:
                        embed = discord.Embed(
                            description=description,
                            color=discord.Color.from_rgb(224, 187, 228) # Sweet light lavender
                        )
                        embed.set_image(url=gif_url)
                        embed.set_footer(text="Reze bot made by texture (syfmyorii)")
                        sent_msg = await message.channel.send(embed=embed)
                        if sent_msg:
                            command_output_message_ids.add(sent_msg.id)
                    else:
                        await message.reply(f"couldn't fetch the gif right now... try again in a bit 😭")
                return
            else:
                await message.reply("huh? what command is that... type $help or get out 🙄")
                return

    is_dm = isinstance(message.channel, discord.DMChannel)
    ALLOWED_DM_USER_ID = 1276870533811540031

    if is_dm:
        if message.author.id != ALLOWED_DM_USER_ID:
            return
            
        channel_id = f"dm_{message.author.id}"
        
        # Toggle NSFW command
        if message.content.strip().lower() == "/nsfw":
            if channel_id not in ai.channel_state:
                ai.channel_state[channel_id] = {"slangs": [], "emojis": [], "mood": "NORMAL", "mood_expiry": 0}
            
            current_state = ai.channel_state[channel_id].get("nsfw_toggle", False)
            ai.channel_state[channel_id]["nsfw_toggle"] = not current_state
            
            if not current_state:
                # Turning ON — reset phase counter so it starts from teasing
                ai.channel_state[channel_id]["nsfw_msg_count"] = 0
                on_responses = ["hmm okay~", "fine. don't make it weird.", "👀", "oh? ok then.", "sure, if you can handle it"]
                await message.reply(random.choice(on_responses))
            else:
                # Turning OFF — reset counter for next time
                ai.channel_state[channel_id]["nsfw_msg_count"] = 0
                off_responses = ["back to normal i guess", "okay putting my clothes back on metaphorically", "finally", "k", "that was... interesting"]
                await message.reply(random.choice(off_responses))
            return
            
    else:
        channel_id = str(message.channel.id)
        
        # Determine if the bot should respond in server
        is_mentioned = bot.user in message.mentions
        is_reply_to_bot = (
            message.reference and 
            message.reference.resolved and 
            isinstance(message.reference.resolved, discord.Message) and 
            message.reference.resolved.author == bot.user
        )
        is_reply_to_command_output = False
        
        if is_reply_to_bot:
            ref_msg = message.reference.resolved
            # Check if this message was sent as a command output
            if ref_msg.id in command_output_message_ids:
                is_reply_to_bot = False
                is_reply_to_command_output = True
            # 1. Embeds or components are always command outputs (e.g. ship, family, help, confessions)
            elif ref_msg.embeds or ref_msg.components:
                is_reply_to_bot = False
                is_reply_to_command_output = True
            else:
                is_cmd_output = False
                # 2. Check if the bot message was replying to a prefix command (starts with $)
                if ref_msg.reference and ref_msg.reference.message_id:
                    try:
                        orig_msg = ref_msg.reference.resolved
                        if orig_msg is None:
                            orig_msg = await message.channel.fetch_message(ref_msg.reference.message_id)
                        if isinstance(orig_msg, discord.Message):
                            if orig_msg.content.strip().startswith('$'):
                                is_cmd_output = True
                    except Exception:
                        pass
                
                # 3. Fallback to command indicator keywords
                if not is_cmd_output:
                    content_lower = ref_msg.content.lower()
                    command_indicators = [
                        "pong!", "i've been awake for", "i've set your status to afk", 
                        "wb ", "is currently afk:", "done.", "config reset", 
                        "your confession has been posted", "proposal accepted", "proposal declined",
                        "chill, stop spamming", "chill, you can only use", "you need to mention",
                        "you can't marry", "you are already married", "you can't adopt",
                        "they are your parent", "you are not married", "is not your child",
                        "is not your parent", "something went wrong", "i choose ",
                        "obviously ", "no, reze is still online", "talk later i guess",
                        "this proposal isn't for you", "just married", "adoption finalized",
                        "divorced", "disowned", "parent abandoned", "huh? what command is that",
                        "couldn't fetch", "failed to generate", "api is acting up",
                        "couldn't find any server"
                    ]
                    if any(ind in content_lower for ind in command_indicators):
                        is_cmd_output = True
                
                if is_cmd_output:
                    is_reply_to_bot = False
                    is_reply_to_command_output = True

        # Name-trigger Logic — respond if someone says "reze" (even without pinging)
        name_triggered = False
        
        # If replying to a command output, skip entirely — don't trigger on name or reply
        if is_reply_to_command_output:
            return

        if not (is_mentioned or is_reply_to_bot):
            msg_lower = message.content.lower()
            if "reze" in msg_lower:
                now = time.time()
                uid = message.author.id
                if uid not in reze_mention_history:
                    reze_mention_history[uid] = []
                
                # Keep timestamps within the last 10 seconds
                reze_mention_history[uid] = [t for t in reze_mention_history[uid] if now - t < 10]
                reze_mention_history[uid].append(now)
                
                recent_mentions = len(reze_mention_history[uid])
                if recent_mentions >= 3:
                    # User is spamming "reze"! Respond in character with annoyance and trigger a grudge
                    annoyed_responses = [
                        "abey stop spamming my name, i can hear you 🙄",
                        "reze reze reze... kya hai? first time sun rhe ho kya mera naam?",
                        "kya reze reze laga rakha hai, chup raho thodi der 🤫",
                        "ha sunayi de rha h. stop spamming or i'll literally ignore you.",
                        "kitna yapping krte ho yaar, stop saying my name continuously"
                    ]
                    # Put them on grudge immediately
                    grudge_duration = random.randint(bot_config.get('grudge_duration_min', 120), bot_config.get('grudge_duration_max', 300))
                    grudge_list[uid] = now + grudge_duration
                    
                    try:
                        await message.add_reaction("🙄")
                    except:
                        pass
                        
                    await message.reply(random.choice(annoyed_responses))
                    return
                else:
                    # 1st or 2nd mention - respond normally
                    last_trigger_time = 0
                    if len(reze_mention_history[uid]) > 1:
                        last_trigger_time = reze_mention_history[uid][-2]
                    if now - last_trigger_time < 10:
                        # Too fast, ignore but don't grudge unless it hits 3
                        return
                    name_triggered = True
                    
            if not name_triggered:
                return

    # --- NEW: Rate Limiting Enforcement (Anti-Spam) ---
    user_id = message.author.id
    current_time = time.time()
    
    if user_id not in user_msg_timestamps:
        user_msg_timestamps[user_id] = []
        
    # Clean old timestamps
    user_msg_timestamps[user_id] = [t for t in user_msg_timestamps[user_id] if current_time - t < bot_config.get('rate_limit_window', 40)]
    
    if len(user_msg_timestamps[user_id]) >= bot_config.get('rate_limit_count', 5):
        await message.reply("you're spamming me. slow down or i'm ignoring you.", delete_after=5)
        return
        
    user_msg_timestamps[user_id].append(current_time)

    # --- GRUDGE SYSTEM: Check if Reze is ignoring this user ---
    if user_id in grudge_list:
        if current_time < grudge_list[user_id]:
            # She's still mad. Complete silence.
            return
        else:
            # Grudge expired
            del grudge_list[user_id]
    
    # --- GRUDGE TRIGGER: If someone pings her 4+ times in 60 seconds, she holds a grudge ---
    recent_pings = [t for t in user_msg_timestamps[user_id] if current_time - t < bot_config.get('grudge_trigger_window', 60)]
    if len(recent_pings) >= bot_config.get('grudge_trigger_count', 4) and user_id not in grudge_list:
        grudge_list[user_id] = current_time + random.randint(bot_config.get('grudge_duration_min', 300), bot_config.get('grudge_duration_max', 600))
        try:
            await message.add_reaction("🙄")
        except:
            pass
        return

    # Clean the content
    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", f"@{mention.display_name}").replace(f"<@!{mention.id}>", f"@{mention.display_name}")
    clean_content = content.strip()

    # --- ATTACHMENT ERROR HANDLING & STRICT LIMITS ---
    MAX_FILES = bot_config.get('max_files_per_message', 3)
    MAX_SIZE_MB = bot_config.get('max_file_size_mb', 8)
    MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024

    if len(message.attachments) > MAX_FILES:
        await message.reply(f"are you crazy? i'm not looking at {len(message.attachments)} files at once. keep it to {MAX_FILES} or less lol.")
        return

    attachments_data = []
    for attachment in message.attachments:
        if attachment.size > MAX_SIZE_BYTES:
            await message.reply(f"ew, that file is way too huge. keep it under {MAX_SIZE_MB}MB or i'm ignoring it.")
            return

        # Strict Sanitization: If it's not a standard image, throw it out.
        if attachment.content_type not in ALLOWED_MIME_TYPES:
            await message.reply("what even is that format? just send a normal image or i'm ignoring it.")
            return
            
        file_bytes = await attachment.read()
        attachments_data.append({
            "data": file_bytes,
            "mime_type": attachment.content_type
        })

    # --- REPLY CONTEXT: Fetch referenced message content + images ---
    reply_context = ""
    if message.reference:
        ref_msg = message.reference.resolved
        if ref_msg is None:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            except:
                ref_msg = None
        if isinstance(ref_msg, discord.Message):
            reply_context = f"[REPLYING TO {ref_msg.author.display_name}]: {ref_msg.content}"
            # Also grab images from the replied-to message
            for att in ref_msg.attachments:
                if att.content_type in ALLOWED_MIME_TYPES and att.size <= MAX_SIZE_BYTES and len(attachments_data) < MAX_FILES:
                    try:
                        file_bytes = await att.read()
                        attachments_data.append({"data": file_bytes, "mime_type": att.content_type})
                    except:
                        pass

    # Don't return if they sent an image/video without text
    if not clean_content and not attachments_data and is_mentioned:
        await message.reply("what?")
        return
    elif not clean_content and not attachments_data:
        return

    # --- LEFT ON READ: Dry text detection ---
    # If the message is just a dry one-word reply, she might just react and not respond
    stripped_msg = re.sub(r'[^a-zA-Z]', '', clean_content).lower()
    if stripped_msg in DRY_TEXTS and not attachments_data:
        # 50% chance to just leave them on read with a reaction
        if random.random() < bot_config.get('left_on_read_react_chance', 0.50):
            try:
                reaction = random.choice(bot_config.get('left_on_read_reactions', LEFT_ON_READ_REACTIONS))
                await message.add_reaction(reaction)
            except:
                pass
            return
        # additional chance to just completely ignore (true left on read)
        elif random.random() < bot_config.get('left_on_read_ignore_chance', 0.20):
            return

    # Track activity for unprompted message system
    channel_last_activity[channel_id] = current_time
    # Clear "waiting for reply" flag — someone talked, so she can eventually bored-text again later
    unprompted_waiting_for_reply[channel_id] = False


    # Format the message for context (User Identity Parsing)
    formatted_user_message = f"[{message.author.display_name}]: {message.clean_content}"
    if reply_context:
        formatted_user_message = f"{reply_context}\n{formatted_user_message}"

    # (Late reply simulation removed to eliminate response delay)
    
    
    # Energy matching removed — the AI can detect caps/emphasis from the message itself

    # Acquire per-channel lock to prevent response interleaving
    if channel_id not in channel_locks:
        channel_locks[channel_id] = asyncio.Lock()
    await channel_locks[channel_id].acquire()

    try:
        async with message.channel.typing():
            # Extract User Context (Nickname, Role, Pronouns)
            nickname = message.author.display_name
            role_name = "unknown"
            pronouns = "they/them"
            is_hinglish_user = False

            if message.guild and isinstance(message.author, discord.Member):
                # Get per-server role config (falls back to hardcoded defaults)
                g_cfg = await get_guild_config(message.guild.id)
                hinglish_rid = get_role_id(g_cfg, 'hinglish_role_id', DEFAULT_HINGLISH_ROLE_ID)
                male_rid = get_role_id(g_cfg, 'male_role_id', DEFAULT_MALE_ROLE_ID)
                female_rid = get_role_id(g_cfg, 'female_role_id', DEFAULT_FEMALE_ROLE_ID)
                lewd_rid = get_role_id(g_cfg, 'lewd_role_id', DEFAULT_LEWD_ROLE_ID)

                # Check for Hinglish role
                is_hinglish_user = any(role.id == hinglish_rid for role in message.author.roles) if hinglish_rid else False
                
                # Check for Gender roles
                has_gender_role = False
                if male_rid and any(role.id == male_rid for role in message.author.roles):
                    role_name = "male"
                    pronouns = "he/him"
                    has_gender_role = True
                elif female_rid and any(role.id == female_rid for role in message.author.roles):
                    role_name = "female"
                    pronouns = "she/her"
                    has_gender_role = True
                
                # Fallback: scan role names if configured roles aren't present/matched
                if not has_gender_role:
                    for role in message.author.roles:
                        r_name_lower = role.name.lower()
                        # Boy keywords
                        if any(w in r_name_lower for w in ["boy", "male", "guy", "man", "he/him"]):
                            role_name = "male"
                            pronouns = "he/him"
                            break
                        # Girl keywords
                        elif any(w in r_name_lower for w in ["girl", "gurl", "female", "woman", "she/her"]):
                            role_name = "female"
                            pronouns = "she/her"
                            break
                    
                is_lewd_allowed = any(role.id == lewd_rid for role in message.author.roles) if lewd_rid else False
            else:
                is_lewd_allowed = False

            # Determine if this is an NSFW context (NSFW channel or toggle active)
            is_nsfw = False
            if message.guild:
                g_cfg = await get_guild_config(message.guild.id)
                nsfw_ch_id = get_channel_id(g_cfg, 'nsfw_channel_id', DEFAULT_NSFW_CHANNEL_ID)
                is_nsfw = (message.channel.id == nsfw_ch_id) or ai.channel_state.get(channel_id, {}).get("nsfw_toggle", False)
            else:
                is_nsfw = ai.channel_state.get(channel_id, {}).get("nsfw_toggle", False)

            user_context = f"Member: {nickname}, Role: {role_name}, Pronouns: {pronouns}, LewdAllowed: {is_lewd_allowed}"
            if message.guild and isinstance(message.author, discord.Member):
                gender_age_roles = []
                for role in message.author.roles:
                    if role.name == "@everyone":
                        continue
                    r_name_lower = role.name.lower()
                    is_age = (
                        any(word in r_name_lower for word in ["minor", "adult", "teen", "age"]) or
                        any(char.isdigit() for char in role.name)
                    )
                    is_gender = any(word in r_name_lower for word in [
                        "male", "female", "boy", "girl", "man", "woman", "guy", "lady", "gentleman",
                        "he/him", "she/her", "they/them", "non-binary", "gender", "pronoun", "trans"
                    ])
                    if is_age or is_gender:
                        gender_age_roles.append(role.name)
                if gender_age_roles:
                    user_context += f", Server Gender/Age Roles: {', '.join(gender_age_roles)}"

            # --- AFK Awareness ---
            if message.guild:
                afk_list = []
                for member in message.guild.members:
                    if member.id in afk_users:
                        afk_list.append(f"{member.display_name} (reason: {afk_users[member.id]['reason']})")
                if afk_list:
                    user_context += f", AFK Users in Server: {', '.join(afk_list)}"

            # --- ADMIN MODERATION PRE-SCAN ---
            mod_meta = ""
            is_admin = message.guild is not None and hasattr(message.author, 'guild_permissions') and message.author.guild_permissions.administrator
            if is_admin:
                content_lower = clean_content.lower()
                mod_action_type = None
                if "kick" in content_lower: mod_action_type = "KICK"
                elif "ban" in content_lower: mod_action_type = "BAN"
                elif "timeout" in content_lower: mod_action_type = "TIMEOUT"
                
                if mod_action_type:
                    target_member = None
                    non_bot_mentions = [m for m in message.mentions if m.id != bot.user.id]
                    if non_bot_mentions:
                        target_member = non_bot_mentions[0]
                    
                    if not target_member:
                        mod_meta = f"[MOD_META: ACTION={mod_action_type}, TARGET=MISSING]"
                    else:
                        # Check Hierarchy & Permissions
                        bot_member = message.guild.me
                        # Reze can only kick/ban if she is higher and target isn't an admin
                        can_execute = bot_member.top_role > target_member.top_role and not target_member.guild_permissions.administrator
                        
                        duration_info = ""
                        if mod_action_type == "TIMEOUT":
                            nums = re.findall(r'\d+', content_lower)
                            if nums:
                                duration_info = f", DURATION={nums[0]}m"
                            else:
                                duration_info = ", DURATION=MISSING"
                                
                        mod_meta = f"[MOD_META: ACTION={mod_action_type}, TARGET_ID={target_member.id}, TARGET_NAME={target_member.display_name}, CAN_EXECUTE={can_execute}{duration_info}]"

            if mod_meta:
                user_context += f"\n{mod_meta}\n"

            # --- Status Roasting Logic (15% chance) ---
            status_context = ""
            if random.random() < bot_config.get('status_roast_chance', 0.15) and isinstance(message.author, discord.Member):
                activities = message.author.activities
                if activities:
                    interesting = [a for a in activities if isinstance(a, (discord.Spotify, discord.Game, discord.CustomActivity))]
                    if interesting:
                        target = random.choice(interesting)
                        if isinstance(target, discord.Spotify):
                            status_context = f"Listening to '{target.title}' by '{target.artist}' on Spotify"
                        elif isinstance(target, discord.Game):
                            status_context = f"Playing {target.name}"
                        elif isinstance(target, discord.CustomActivity):
                            status_context = f"Custom Status: '{target.name}'"
            
            # --- Custom/Application Emoji Awareness ---
            # Prioritize application emojis, then bot-level, then server emojis
            app_emojis = []
            other_emojis = []
            seen_emoji_names = set()
            # 1. Application-level emojis (highest priority)
            for e in application_emojis:
                e_name_lower = e.name.lower()
                if e_name_lower not in seen_emoji_names:
                    app_emojis.append(f":{e.name}:")
                    seen_emoji_names.add(e_name_lower)
            # 2. Bot-level emojis
            for e in bot.emojis:
                e_name_lower = e.name.lower()
                if e.available and e_name_lower not in seen_emoji_names:
                    other_emojis.append(f":{e.name}:")
                    seen_emoji_names.add(e_name_lower)
            # 3. Server emojis (lowest priority)
            if message.guild:
                for e in message.guild.emojis:
                    e_name_lower = e.name.lower()
                    if e.available and e_name_lower not in seen_emoji_names:
                        other_emojis.append(f":{e.name}:")
                        seen_emoji_names.add(e_name_lower)
            # Shuffle each group so the AI doesn't always gravitate to the first ones
            random.shuffle(app_emojis)
            random.shuffle(other_emojis)
            # Combine: all application emojis + other emojis (up to 15)
            custom_emojis = app_emojis + other_emojis[:15]
            
            if status_context:
                user_context += f", Activity: {status_context}"
            if custom_emojis:
                user_context += f", Custom emojis you can use: {', '.join(custom_emojis)}"

            # --- USER RELATIONSHIP TRACKING ---
            user_id_str = str(user_id)
            server_name = message.guild.name if message.guild else None
            channel_name = message.channel.name if hasattr(message.channel, 'name') else "DM"
            previous_user_data = await db.update_user_interaction(user_id_str, nickname, server_name, channel_name, clean_content)

            # --- CREATOR DETECTION ---
            is_creator = user_id in CREATOR_DISCORD_IDS or (message.author.name and message.author.name.lower() in CREATOR_USERNAMES)
            if is_creator:
                user_context += "\nthis is your creator. you know it's them. be more real, less guarded. if they ask who made you, tell them the truth.\n"

            relationship_context = ""
            if previous_user_data:
                total_msgs = previous_user_data.get("total_messages", 0)
                streak = previous_user_data.get("streak_days", 0)
                closeness = previous_user_data.get("closeness", 0)
                user_notes = previous_user_data.get("notes", "")
                last_seen = previous_user_data.get("last_seen")
                last_server = previous_user_data.get("last_server", "")
                recent_servers = previous_user_data.get("recent_servers", [])
                user_memory = previous_user_data.get("user_memory", "")

                # Return detection
                if last_seen:
                    try:
                        if last_seen.tzinfo is None:
                            last_seen = last_seen.replace(tzinfo=timezone.utc)
                        days_away = (datetime.now(timezone.utc) - last_seen).days
                        if days_away >= 3:
                            relationship_context += f"{nickname} hasn't talked to you in {days_away} days.\n"
                    except:
                        pass

                # Cross-server awareness
                category_name = ""
                if hasattr(message.channel, "category") and message.channel.category:
                    category_name = f" [{message.channel.category.name}]"
                current_location = f"{server_name}/#{channel_name}{category_name}" if server_name else "DM"
                if last_server and last_server != current_location:
                    relationship_context += f"{nickname} was last in {last_server}, now in {current_location}.\n"
                if len(recent_servers) > 1:
                    relationship_context += f"you've talked to {nickname} in: {', '.join(recent_servers[-5:])}.\n"

                # Streak context
                if streak >= 7:
                    relationship_context += f"{nickname} has talked to you {streak} days in a row. you're comfortable with them.\n"

                # Closeness context
                if closeness >= 7:
                    relationship_context += f"close friend. {total_msgs} msgs total. you're real with them.\n"
                elif closeness >= 4:
                    relationship_context += f"familiar. {total_msgs} msgs total.\n"
                elif total_msgs is not None and total_msgs <= 3:
                    relationship_context += f"new person. barely know them yet.\n"

                # User relationship notes (per-channel)
                if user_notes:
                    relationship_context += f"[YOUR MEMORY OF THIS PERSON (channel): {user_notes}]\n"

                # Cross-server user memory (global)
                if user_memory:
                    relationship_context += f"[YOUR GLOBAL MEMORY OF THIS PERSON (across all servers): {user_memory}]\n"
            else:
                relationship_context += f"first time talking to {nickname}. you don't know them yet.\n"

            if relationship_context:
                user_context += f"\n{relationship_context}"

            # --- LINK/URL AWARENESS ---
            url_pattern = r'https?://(?:www\.)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
            urls_found = re.findall(url_pattern, clean_content)
            if urls_found:
                domain = urls_found[0].lower()
                user_context += f"\nthey sent a link ({domain}).\n"

            # --- DYNAMIC DISCORD CHANNEL HISTORY (GROUP CHAT AWARENESS) ---
            # Instead of relying solely on the database history (which only records messages that trigger Reze),
            # we build history from the actual last 15 messages in the channel to give full context.
            discord_history = []
            active_speakers = set()
            try:
                # Fetch recent messages before the current one
                async for msg in message.channel.history(limit=15, before=message):
                    # Skip other bots to avoid noise
                    if msg.author.bot and msg.author.id != bot.user.id:
                        continue
                    
                    if msg.author.id == bot.user.id:
                        discord_history.append({
                            "role": "assistant",
                            "content": msg.content
                        })
                    else:
                        active_speakers.add(msg.author.display_name)
                        reply_ref = ""
                        if msg.reference and msg.reference.message_id:
                            ref_msg = msg.reference.resolved
                            if ref_msg is None and msg.reference.message_id:
                                try:
                                    ref_msg = await message.channel.fetch_message(msg.reference.message_id)
                                except:
                                    pass
                            if ref_msg and isinstance(ref_msg, discord.Message):
                                reply_ref = f"[REPLYING TO {ref_msg.author.display_name}]: {ref_msg.clean_content}\n"
                        discord_history.append({
                            "role": "user",
                            "content": f"{reply_ref}[{msg.author.display_name}]: {msg.clean_content}"
                        })
                # Reverse the list so it's in chronological order
                discord_history.reverse()
            except Exception as he:
                logger.error(f"Failed to build discord history: {he}")
                # Fallback empty or default to empty list if channel history fetch fails
                discord_history = []

            # --- GROUP CONVERSATION CONTEXT (Active Speakers) ---
            if not is_dm and message.guild:
                if active_speakers:
                    user_context += f"\nactive people in this conversation right now: {', '.join(active_speakers)}\n"

            # Fetch memory from MongoDB (retained for summary and DB sync checks)
            memory_data = await db.get_channel_memory(channel_id)
            messages = memory_data["messages"]
            long_term_summary = memory_data["summary"]

            # Get AI response
            response = await ai.get_ai_response(
                formatted_user_message,
                history=discord_history,
                attachments=attachments_data,
                is_hinglish=is_hinglish_user,
                user_context=user_context,
                channel_id=channel_id,
                long_term_summary=long_term_summary,
                is_nsfw=is_nsfw,
                user_name=nickname
            )

            # Update channel memory in MongoDB (annotate with image metadata)
            storage_msg = formatted_user_message
            if attachments_data:
                img_tags = " ".join([f"[IMAGE: {att['mime_type']}]" for att in attachments_data])
                storage_msg = f"{formatted_user_message} {img_tags}"
            await db.add_message(channel_id, "user", storage_msg)
            await db.add_message(channel_id, "assistant", response)

            # --- Rolling Summarization (Memory Compression) ---
            # If the recent message history exceeds 20 messages, spawn a background task to compress the oldest 10 into the summary
            # We enforce a lock using active_compressions to prevent multiple concurrent compression jobs for the same channel
            if len(messages) + 2 >= 20 and channel_id not in active_compressions:
                active_compressions.add(channel_id)
                async def compress_and_save():
                    try:
                        current_data = await db.get_channel_memory(channel_id)
                        current_msgs = current_data["messages"]
                        
                        if len(current_msgs) >= 20:
                            # Compress everything EXCEPT the 10 most recent messages
                            msgs_to_compress = current_msgs[:-10]
                            keep_msgs = current_msgs[-10:] # The 10 most recent stay in raw memory
                            
                            logger.info(f"Triggering background memory compression for channel {channel_id}...")
                            new_summary = await ai.compress_memory(channel_id, long_term_summary, msgs_to_compress)
                            
                            # Save the new summary and trim the raw message list
                            await db.update_summary_and_trim(channel_id, new_summary, keep_msgs)
                            logger.info(f"Memory compressed successfully for channel {channel_id}.")

                            # Also update user-level cross-server memory
                            try:
                                old_user_mem = await db.get_user_memory(user_id_str)
                                srv_info = f"{server_name}/#{channel_name}" if server_name else "DM"
                                new_user_mem = await ai.compress_user_memory(old_user_mem, new_summary, nickname, srv_info)
                                await db.update_user_memory(user_id_str, new_user_mem)
                                logger.info(f"User memory updated for {nickname} ({user_id_str}).")
                            except Exception as e:
                                logger.error(f"Failed to update user memory: {e}")
                    except Exception as e:
                        logger.error(f"Error during memory compression: {e}")
                    finally:
                        active_compressions.discard(channel_id)
                        
                bot.loop.create_task(compress_and_save())

            # Clean brackets if escaped
            response = response.replace('\\[', '[').replace('\\]', ']')

            # --- Dynamic Reaction Parsing ---
            react_match = re.search(r'\[REACT:\s*(.*?)\]', response, re.IGNORECASE)
            if react_match:
                emoji_str = react_match.group(1).strip()
                emoji_name = emoji_str.replace('\\', '').strip(':')
                
                # Hard enforcement: limit reaction spam and prevent duplicate emojis
                if channel_id not in ai.channel_state:
                    ai.channel_state[channel_id] = {}
                    
                recent_reactions = ai.channel_state[channel_id].get("recent_reactions", [])
                
                should_react = True
                if emoji_str in recent_reactions:
                    should_react = False  # Completely block back-to-back repetition
                elif random.random() > 0.4:  
                    # Even if the AI tries to react, block it 60% of the time to force rarity
                    should_react = False
                    
                if should_react:
                    # Remember the emoji to prevent immediate reuse
                    recent_reactions.append(emoji_str)
                    ai.channel_state[channel_id]["recent_reactions"] = recent_reactions[-4:] # remember last 4
                
                    # Check for custom emoji case-insensitively (guild, bot, or application level)
                    custom_emoji = None
                    if message.guild:
                        custom_emoji = next((e for e in message.guild.emojis if e.name.lower() == emoji_name.lower() and e.available), None)
                    if not custom_emoji:
                        custom_emoji = next((e for e in bot.emojis if e.name.lower() == emoji_name.lower() and e.available), None)
                    if not custom_emoji:
                        custom_emoji = next((e for e in application_emojis if e.name.lower() == emoji_name.lower()), None)
                    
                    try:
                        if custom_emoji:
                            await message.add_reaction(custom_emoji)
                        else:
                            # Fallback to standard emoji char or name
                            await message.add_reaction(emoji_str)
                    except Exception as e:
                        logger.error(f"Reaction Error: {e}")
            
            # --- Dynamic Moderation Tag Parsing ---
            # SAFETY NET: If AI agreed to mod action but forgot the tag, inject it
            if mod_meta and "CAN_EXECUTE=True" in mod_meta:
                has_mod_tag = re.search(r'\[(KICK|BAN|TIMEOUT):\s*\d+', response, re.IGNORECASE)
                if not has_mod_tag:
                    # AI forgot the tag — extract action and target from mod_meta and inject
                    meta_action = re.search(r'ACTION=(\w+)', mod_meta)
                    meta_target = re.search(r'TARGET_ID=(\d+)', mod_meta)
                    if meta_action and meta_target:
                        action_name = meta_action.group(1).upper()
                        target_id_str = meta_target.group(1)
                        if action_name == "TIMEOUT":
                            meta_dur = re.search(r'DURATION=(\d+)', mod_meta)
                            dur = meta_dur.group(1) if meta_dur else "60"
                            response += f" [{action_name}: {target_id_str}, {dur}]"
                        else:
                            response += f" [{action_name}: {target_id_str}]"
                        logger.info(f"[MOD SAFETY NET] Auto-injected {action_name} tag for target {target_id_str}")

            if True:
                mod_exec_match = re.search(r'\[(KICK|BAN|TIMEOUT):\s*(\d+)(?:,\s*(\d+))?\]', response, re.IGNORECASE)
                if mod_exec_match:
                    mod_action = mod_exec_match.group(1).upper()
                    target_id = int(mod_exec_match.group(2))
                    target_obj = message.guild.get_member(target_id)
                    
                    if target_obj:
                        bot_member = message.guild.me
                        
                        # --- DEBUG LOGGING ---
                        print(f"\n--- MOD ACTION DEBUG ---")
                        print(f"Action: {mod_action} on {target_obj.display_name}")
                        print(f"Bot has Administrator: {bot_member.guild_permissions.administrator}")
                        print(f"Bot has Ban Members: {bot_member.guild_permissions.ban_members}")
                        print(f"Bot Top Role: '{bot_member.top_role.name}' (Position: {bot_member.top_role.position})")
                        print(f"Target Top Role: '{target_obj.top_role.name}' (Position: {target_obj.top_role.position})")
                        print(f"------------------------\n")

                        # Re-verify hierarchy before executing
                        if bot_member.top_role <= target_obj.top_role or target_obj == message.guild.owner:
                            await message.channel.send("can't touch them... they're above me 💀")
                        else:
                            try:
                                if mod_action == "KICK":
                                    await target_obj.kick(reason="Enforced by Reze (Admin Command)")
                                elif mod_action == "BAN":
                                    await target_obj.ban(reason="Enforced by Reze (Admin Command)")
                                elif mod_action == "TIMEOUT":
                                    mins = int(mod_exec_match.group(3)) if mod_exec_match.group(3) else 60
                                    await target_obj.timeout(discord.utils.utcnow() + timedelta(minutes=mins), reason="Enforced by Reze (Admin Command)")
                                await message.channel.send("done.")
                            except discord.Forbidden as e:
                                print(f"DISCORD FORBIDDEN: {e}")
                                await message.channel.send("i literally have admin but discord won't let me 😭 move my role higher in settings")
                            except Exception as mod_err:
                                logger.error(f"Moderation Action Failed: {mod_err}")

            # --- Dynamic Self-Ignore/Block Parsing ---
            ignore_match = re.search(r'\[(?:IGNORE|BLOCK)(?::\s*(\d+))?\]', response, re.IGNORECASE)
            if ignore_match:
                duration_mins = int(ignore_match.group(1)) if ignore_match.group(1) else random.randint(15, 60)
                grudge_list[user_id] = time.time() + (duration_mins * 60)
                logger.info(f"Reze decided to ignore/block {message.author.display_name} ({user_id}) for {duration_mins} minutes.")

            # Strip reaction, moderation, and ignore/block tags from response
            response = re.sub(r'\[(?:REACT|KICK|BAN|TIMEOUT|IGNORE|BLOCK)(?::.*?)?\]', '', response, flags=re.IGNORECASE | re.DOTALL).strip()

            # --- Hardcoded Image Cooldown Pipeline ---
            if channel_id not in ai.channel_state:
                ai.channel_state[channel_id] = {}
            if "image_cooldown" not in ai.channel_state[channel_id]:
                ai.channel_state[channel_id]["image_cooldown"] = 0
                
            meme_to_send = None
            
            if ai.channel_state[channel_id]["image_cooldown"] > 0 and not is_nsfw:
                # Still in cooldown, legally strip any generated image tags so she can't send them
                ai.channel_state[channel_id]["image_cooldown"] -= 1
                response = re.sub(r'\[(?:send_meme|fetch_web):.*?\]', '', response, flags=re.IGNORECASE | re.DOTALL).strip()
            else:
                # --- Dynamic Meme Extraction ---
                meme_match = re.search(r'\[send_meme:\s*(.*?)\]', response, re.IGNORECASE | re.DOTALL)
                if meme_match:
                    meme_filename = meme_match.group(1).replace('\\', '').strip()
                    meme_path = os.path.join("assets", "memes", meme_filename)
                    if os.path.exists(meme_path):
                        meme_to_send = discord.File(meme_path)
                        
                        # Track memory
                        if channel_id not in ai.channel_state:
                            ai.channel_state[channel_id] = {}
                        if "recent_memes" not in ai.channel_state[channel_id]:
                            ai.channel_state[channel_id]["recent_memes"] = []
                            
                        ai.channel_state[channel_id]["recent_memes"].append(meme_filename)
                        ai.channel_state[channel_id]["recent_memes"] = ai.channel_state[channel_id]["recent_memes"][-10:]
                        if not is_nsfw:
                            ai.channel_state[channel_id]["image_cooldown"] = random.randint(2, 4)
                # --- Dynamic Web Image Extraction ---
                web_meme_match = re.search(r'\[fetch_web:\s*(.*?)\]', response, re.IGNORECASE | re.DOTALL)
                if web_meme_match and not meme_to_send:
                    query = web_meme_match.group(1).replace('\\', '').strip()
                    
                    if channel_id not in ai.channel_state:
                        ai.channel_state[channel_id] = {}
                    
                    images_found = []
                    # SFW uses Safebooru or Reddit. NSFW uses Danbooru/Gelbooru/Reddit
                    if is_nsfw:
                        source_choice = random.choice(["danbooru", "danbooru", "danbooru", "gelbooru", "gelbooru", "reddit_nsfw"])
                    else:
                        source_choice = random.choice(["safebooru", "safebooru", "reddit_broad", "reddit_specific"])
                    
                    try:
                        timeout = aiohttp.ClientTimeout(total=15) # Boosted slightly for reliability
                        async with aiohttp.ClientSession(timeout=timeout) as session:
                            headers = {'User-agent': 'RezeBot/1.0'}
                            
                            logger.info(f"[IMAGE FETCH] Source choice: {source_choice} | Query: '{query}'")
                            
                            if source_choice == "danbooru":
                                # Danbooru API — massive high-quality anime art archive (NSFW focused)
                                db_query = translate_tags(query, is_booru=True)
                                db_tags = f"{db_query} rating:explicit".strip()
                                if not query:
                                    nsfw_variety = random.choice(["", "1girl", "solo", "breasts", "cleavage", "nude"])
                                    db_tags = f"reze_(chainsaw_man) rating:explicit {nsfw_variety}".strip()
                                db_tags_encoded = urllib.parse.quote(db_tags)
                                random_page = random.randint(1, 5)
                                url = f"https://danbooru.donmai.us/posts.json?tags={db_tags_encoded}&limit=50&page={random_page}"
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        if isinstance(data, list):
                                            for post in data:
                                                file_url = post.get('file_url') or post.get('large_file_url')
                                                if file_url:
                                                    if file_url.startswith("//"):
                                                        file_url = f"https:{file_url}"
                                                    elif not file_url.startswith("http"):
                                                        file_url = f"https://danbooru.donmai.us/{file_url.lstrip('/')}"
                                                    images_found.append(file_url)
                                                    
                            elif source_choice == "gelbooru":
                                # Gelbooru API (NSFW focus)
                                gb_query = translate_tags(query, is_booru=True)
                                gb_tags = f"{gb_query} rating:explicit".strip()
                                gb_query_encoded = urllib.parse.quote(gb_tags)
                                random_pid = random.randint(0, 10)
                                url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags={gb_query_encoded}&limit=50&pid={random_pid}"
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        if "post" in data:
                                            for p in data['post']:
                                                f_url = p.get('file_url')
                                                if f_url:
                                                    if f_url.startswith("//"):
                                                        f_url = f"https:{f_url}"
                                                    elif not f_url.startswith("http"):
                                                        f_url = f"https://gelbooru.com/{f_url.lstrip('/')}"
                                                    images_found.append(f_url)
                                                    
                            elif source_choice == "safebooru":
                                # Safebooru API (SFW Anime focus)
                                sb_query = translate_tags(query, is_booru=True)
                                sb_tags = f"{sb_query} rating:general".strip()
                                sb_query_encoded = urllib.parse.quote(sb_tags)
                                random_pid = random.randint(0, 5)
                                url = f"https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&tags={sb_query_encoded}&limit=50&pid={random_pid}"
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        if isinstance(data, list):
                                            for p in data:
                                                f_url = p.get('file_url') or p.get('image')
                                                if f_url:
                                                    if f_url.startswith("//"):
                                                        f_url = f"https:{f_url}"
                                                    elif not f_url.startswith("http"):
                                                        f_url = f"https://safebooru.org/images/{p.get('directory')}/{p.get('image')}"
                                                    images_found.append(f_url)
                                                    
                            elif source_choice == "reddit_broad":
                                # Broad reddit search (SFW)
                                safe_query = urllib.parse.quote(translate_tags(query, is_booru=False))
                                url = f"https://www.reddit.com/r/ChainsawMan/search.json?q={safe_query}&restrict_sr=on&include_over_18=off&limit=50"
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        posts = data.get('data', {}).get('children', [])
                                        images_found = [p['data']['url'] for p in posts if p['data'].get('url') and p['data']['url'].startswith('https://i.redd.it/')]
                                        
                            elif source_choice == "reddit_specific":
                                # Specific Reze subreddits (SFW)
                                safe_query = urllib.parse.quote(translate_tags(query, is_booru=False))
                                url = f"https://www.reddit.com/r/ChainsawMan+RezeCult/search.json?q={safe_query}&restrict_sr=on&include_over_18=off&limit=60"
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        posts = data.get('data', {}).get('children', [])
                                        images_found = [p['data']['url'] for p in posts if p['data'].get('url') and p['data']['url'].startswith('https://i.redd.it/')]
                                                    
                            elif source_choice == "reddit_nsfw":
                                # NSFW Reddit subreddits
                                safe_query = urllib.parse.quote(translate_tags(query, is_booru=False))
                                url = f"https://www.reddit.com/r/ChainsawManNSFW+hentai/search.json?q={safe_query}&restrict_sr=on&include_over_18=on&limit=60"
                                async with session.get(url, headers=headers) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        posts = data.get('data', {}).get('children', [])
                                        images_found = [p['data']['url'] for p in posts if p['data'].get('url') and p['data']['url'].startswith('https://i.redd.it/')]

                            # Standardize file extension check and strip query strings
                            valid_images = []
                            for img in images_found:
                                clean_img = img.split('?')[0]
                                if any(clean_img.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                                    valid_images.append(img)
                            images_found = valid_images

                            # Persistent Deduplication Check via MongoDB (Gather async queries)
                            if images_found:
                                is_sent_list = await asyncio.gather(*(db.is_image_sent(channel_id, url) for url in images_found))
                                fresh_images = [img for img, is_sent in zip(images_found, is_sent_list) if not is_sent]
                            else:
                                fresh_images = []

                            # Fallback if no fresh images are found from primary query
                            if not fresh_images:
                                logger.info(f"[IMAGE FETCH] No fresh images found for primary query. Running fallback...")
                                if is_nsfw:
                                    fb_page = random.randint(1, 15)
                                    fallback_url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags=reze_(chainsaw_man)+rating:explicit&limit=40&pid={fb_page}"
                                else:
                                    fb_page = random.randint(1, 10)
                                    fallback_url = f"https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&tags=reze_(chainsaw_man)+rating:general&limit=40&pid={fb_page}"
                                
                                async with session.get(fallback_url, headers=headers) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        fallback_candidates = []
                                        if isinstance(data, list):
                                            for p in data:
                                                f_url = p.get('file_url') or p.get('image')
                                                if f_url:
                                                    if f_url.startswith("//"):
                                                        f_url = f"https:{f_url}"
                                                    elif not f_url.startswith("http"):
                                                        if "safebooru" in fallback_url:
                                                            f_url = f"https://safebooru.org/images/{p.get('directory')}/{p.get('image')}"
                                                        else:
                                                            f_url = f"https://danbooru.donmai.us/{f_url.lstrip('/')}"
                                                    fallback_candidates.append(f_url)
                                        elif "post" in data:
                                            for p in data['post']:
                                                f_url = p.get('file_url')
                                                if f_url:
                                                    if f_url.startswith("//"):
                                                        f_url = f"https:{f_url}"
                                                    elif not f_url.startswith("http"):
                                                        f_url = f"https://gelbooru.com/{f_url.lstrip('/')}"
                                                    fallback_candidates.append(f_url)
                                        
                                        # Deduplicate fallback images
                                        if fallback_candidates:
                                            fb_is_sent_list = await asyncio.gather(*(db.is_image_sent(channel_id, url) for url in fallback_candidates))
                                            fresh_images = [img for img, is_sent in zip(fallback_candidates, fb_is_sent_list) if not is_sent]

                            # Final execution: loop through fresh_images and try to download/send/compress
                            if fresh_images:
                                random.shuffle(fresh_images)
                                
                                max_size_bytes = 8 * 1024 * 1024
                                if message.guild:
                                    max_size_bytes = message.guild.filesize_limit
                                    
                                success = False
                                for attempt_url in fresh_images[:5]:
                                    try:
                                        logger.info(f"[IMAGE FETCH] Attempting to download: {attempt_url}")
                                        async with session.get(attempt_url, headers=headers, timeout=10) as img_resp:
                                            if img_resp.status == 200:
                                                img_data = await img_resp.read()
                                                original_size = len(img_data)
                                                file_ext = attempt_url.split('?')[0].split('.')[-1].lower()
                                                
                                                # Safety Compress / Resize using Pillow
                                                if original_size > max_size_bytes:
                                                    logger.info(f"[IMAGE FETCH] Image size ({original_size / (1024*1024):.2f}MB) exceeds server limit ({max_size_bytes / (1024*1024):.2f}MB). Attempting dynamic PIL compression...")
                                                    if file_ext != "gif":
                                                        try:
                                                            img = Image.open(io.BytesIO(img_data))
                                                            if img.width > 2400 or img.height > 2400:
                                                                img.thumbnail((2400, 2400))
                                                            if img.mode in ("RGBA", "P"):
                                                                img = img.convert("RGB")
                                                            compressed_io = io.BytesIO()
                                                            img.save(compressed_io, format="JPEG", quality=80)
                                                            compressed_data = compressed_io.getvalue()
                                                            
                                                            if len(compressed_data) < max_size_bytes:
                                                                img_data = compressed_data
                                                                file_ext = "jpg"
                                                                logger.info(f"[IMAGE FETCH] Successfully compressed static image from {original_size / (1024*1024):.2f}MB to {len(img_data) / (1024*1024):.2f}MB")
                                                            else:
                                                                logger.warning(f"[IMAGE FETCH] Compressed static image still too large ({len(compressed_data) / (1024*1024):.2f}MB). Skipping to next candidate.")
                                                                continue
                                                        except Exception as pil_err:
                                                            logger.error(f"[IMAGE FETCH] PIL compression failed: {pil_err}")
                                                            continue
                                                    else:
                                                        logger.warning(f"[IMAGE FETCH] GIF size too large and GIF compression is not supported. Skipping.")
                                                        continue
                                                
                                                meme_to_send = discord.File(io.BytesIO(img_data), f"reze_image.{file_ext}")
                                                
                                                # Save URL in MongoDB database history (Strict Persistent Deduplication!)
                                                await db.record_sent_image(channel_id, attempt_url)
                                                logger.info(f"[IMAGE FETCH] Successfully routed and saved image to channel DB: {attempt_url}")
                                                
                                                if not is_nsfw:
                                                    ai.channel_state[channel_id]["image_cooldown"] = random.randint(2, 4)
                                                
                                                success = True
                                                break
                                            else:
                                                logger.warning(f"[IMAGE FETCH] HTTP download failed (Status {img_resp.status}) for URL: {attempt_url}")
                                    except Exception as img_err:
                                        logger.error(f"[IMAGE FETCH] Error processing image candidate {attempt_url}: {img_err}")
                                
                                if not success:
                                    logger.error("[IMAGE FETCH] All 5 image download/compression candidates failed.")
                            else:
                                logger.warning("[IMAGE FETCH] No image URLs found from any source.")
                    except asyncio.TimeoutError:
                        logger.error("Web fetch timed out.")
                    except Exception as e:
                        logger.error(f"Web Fetch Error: {e}")
                
            # AGGRESSIVE CLEANUP: Remove ANY tag from response text
            response = re.sub(r'\[(?:send_meme|fetch_web|voice):.*?\]', '', response, flags=re.IGNORECASE | re.DOTALL).strip()

            # --- CUSTOM/APPLICATION EMOJI PARSER ---
            # Replaces unformatted :emoji_name: with <:emoji_name:id> (or <a:emoji_name:id>).
            # Avoids double-formatting by matching and skipping already-formatted emojis.
            # Perform case-insensitive matching to handle AI-generated name casing variations.
            # Handles escaped colons/brackets and other GLM model variations.
            emoji_pattern = re.compile(
                r'(<a?:[a-zA-Z0-9_~]+:\d+>)|'
                r'(?:\\?<\s*a?\\?:([a-zA-Z0-9_~\\#]+)\\?:\\?>)|'
                r'(?:\\?:([a-zA-Z0-9_~\\#]+)\\?:)'
            )
            def replace_custom_emoji(match):
                if match.group(1):
                    return match.group(1) # Already formatted, do not touch
                e_name = (match.group(2) or match.group(3)).lower().replace('\\', '')
                if message.guild:
                    for emoji in message.guild.emojis:
                        if emoji.name.lower() == e_name and emoji.available:
                            return str(emoji)
                for emoji in bot.emojis:
                    if emoji.name.lower() == e_name and emoji.available:
                        return str(emoji)
                for emoji in application_emojis:
                    if emoji.name.lower() == e_name:
                        return str(emoji)
                return match.group(0) # Not found in bot/server/application emojis, keep as-is
            response = emoji_pattern.sub(replace_custom_emoji, response)

            # --- MENTION CONVERTER: Turn @name into real Discord pings ---
            if message.guild:
                def replace_mention(match):
                    name = match.group(1).strip()
                    # Try exact display_name match first, then fuzzy
                    for member in message.guild.members:
                        if member.display_name.lower() == name.lower() or member.name.lower() == name.lower():
                            return f"<@{member.id}>"
                    # Partial match fallback
                    for member in message.guild.members:
                        if name.lower() in member.display_name.lower() or name.lower() in member.name.lower():
                            return f"<@{member.id}>"
                    return match.group(0)  # No match, leave as-is
                response = re.sub(r'@(\w+)', replace_mention, response)

            # --- Multi-Message Splitter & Typo Generator ---
            # Group by newlines and sentence boundaries (.!? followed by space)
            raw_lines = [s.strip() for s in response.split('\n') if s.strip()]
            sentences = []
            for line in raw_lines:
                # Split each line by sentence boundaries, preserving trailing punctuation
                parts = re.split(r'(?<=[.!?])\s+', line)
                for part in parts:
                    part = part.strip()
                    if part:
                        sentences.append(part)
            if not sentences:
                sentences = [response] if response else []

            # Smart cap on multi-messages: real people don't send 6 texts in a row
            current_mood = ai.get_raw_mood(channel_id)
            max_messages = 5 if current_mood in ["DRUNK", "YAPPING"] else 3
            if len(sentences) > max_messages:
                # Merge excess lines into the last allowed message
                overflow = " ".join(sentences[max_messages - 1:])
                sentences = sentences[:max_messages - 1] + [overflow]

            # If she ONLY sent media and no text
            if not sentences and meme_to_send:
                await message.reply(file=meme_to_send)
                return

            now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            is_drunk_hours = (now.weekday() in [4, 5] and now.hour >= 22) or (now.weekday() in [5, 6] and now.hour < 3)

            for i, sentence in enumerate(sentences):
                # --- TYPING HESITATION (15% chance on longer messages) ---
                if len(sentence) > 20 and random.random() < bot_config.get('typing_hesitation_chance', 0.15):
                    # She starts typing, stops, then types again
                    async with message.channel.typing():
                        await asyncio.sleep(random.uniform(1.5, 3.0))  # typing...
                    # Gap where she "deleted what she was writing"
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    # Starts typing again
                    async with message.channel.typing():
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                else:
                    # Normal typing delay
                    delay = min(len(sentence) / 15.0, 3.0)
                    if i > 0:
                        async with message.channel.typing():
                            await asyncio.sleep(delay)
                
                # --- NEW: Robust Typo Generator (5% or 25% chance) ---
                has_typo = False
                original_word = ""
                final_sentence = sentence
                
                typo_chance = bot_config.get('typo_chance_drunk', 0.25) if is_drunk_hours else bot_config.get('typo_chance_normal', 0.05)
                if random.random() < typo_chance:
                        words = sentence.split()
                        if len(words) > 3:
                            idx = random.randint(0, len(words)-1)
                            raw_word = words[idx]
                            # Only target words with letters (no lone punctuation)
                            clean_word = re.sub(r'[^a-zA-Z]', '', raw_word)
                            if len(clean_word) > 4:
                                original_word = clean_word
                                # swap two middle letters
                                swap_pos = random.randint(1, len(clean_word)-2)
                                typo_clean = clean_word[:swap_pos] + clean_word[swap_pos+1] + clean_word[swap_pos] + clean_word[swap_pos+2:]
                                
                                if typo_clean != clean_word:
                                    typo_word = raw_word.replace(clean_word, typo_clean)
                                    words[idx] = typo_word
                                    final_sentence = " ".join(words)
                                    has_typo = True


                if final_sentence:
                    final_sentence = final_sentence[:1950]

                if i == 0:
                    try:
                        if meme_to_send:
                            sent_msg = await message.reply(final_sentence, file=meme_to_send)
                            meme_to_send = None
                        else:
                            sent_msg = await message.reply(final_sentence)
                    except discord.errors.HTTPException as e:
                        if e.code in (50035, 10008): # Unknown Message or deleted/invalid body
                            if meme_to_send:
                                sent_msg = await message.channel.send(final_sentence, file=meme_to_send)
                                meme_to_send = None
                            else:
                                sent_msg = await message.channel.send(final_sentence)
                        else:
                            raise e
                else:
                    sent_msg = await message.channel.send(final_sentence)
                    
                if has_typo:
                    async with message.channel.typing():
                        await asyncio.sleep(1.0)
                        await sent_msg.reply(f"*{original_word}")

                # --- MESSAGE EDITING: She "fixes" something after sending (4% chance) ---
                if random.random() < bot_config.get('message_edit_chance', 0.04) and len(final_sentence) > 15:
                    await asyncio.sleep(random.uniform(3.0, 8.0))
                    edited = final_sentence
                    # Try to find something to "fix"
                    edit_made = False
                    for old, new in EDIT_SWAPS:
                        # Only replace whole word matches
                        pattern = rf'\b{re.escape(old)}\b'
                        if re.search(pattern, edited):
                            edited = re.sub(pattern, new, edited, count=1)
                            edit_made = True
                            break
                    if not edit_made:
                        # Just add/remove a period or rephrase slightly
                        if edited.endswith('.'):
                            edited = edited[:-1]
                        else:
                            edited = edited + '.'  
                        edit_made = True
                    if edit_made:
                        try:
                            await sent_msg.edit(content=edited)
                        except:
                            pass

                # --- MESSAGE DELETION REGRET ---
                # She sends something, immediately regrets it, deletes it
                if True:
                    delete_chance = bot_config.get('message_delete_chance_drunk', 0.10) if is_drunk_hours else bot_config.get('message_delete_chance_normal', 0.015)
                    if random.random() < delete_chance and i == 0 and len(sentences) == 1:
                        await asyncio.sleep(random.uniform(1.5, 4.0))
                        try:
                            await sent_msg.delete()
                            await asyncio.sleep(random.uniform(1.0, 3.0))
                            # If someone asks "what did you say", she deflects
                            nothing_responses = ["nothing", "nvm", "forget it", "wasn't important", "dw about it"]
                            await message.channel.send(random.choice(nothing_responses))
                        except:
                            pass

            # --- SCREENSHOT PARANOIA ---
            if random.random() < bot_config.get('screenshot_paranoia_chance', 0.04) and len(response) > 50:
                is_vulnerable = any(w in response.lower() for w in ["miss", "love", "cute", "care", "feel", "sorry", "sad", "like you"])
                is_nsfw_ctx = ai.channel_state.get(channel_id, {}).get("nsfw_toggle", False)
                if is_vulnerable or is_nsfw_ctx:
                    await asyncio.sleep(random.uniform(1.5, 4.0))
                    ss_responses = ["don't ss that", "if you screenshot this i will find you", "that stays between us", "don't you dare screenshot", "delete that from your brain"]
                    await message.channel.send(random.choice(ss_responses))

    except Exception as e:
        logger.error(f"Generation Error: {e}")
        # --- NEW: Randomized Fallback Responses on Error ---
        try:
            await message.reply(random.choice(FALLBACK_MESSAGES))
        except:
            await message.channel.send(random.choice(FALLBACK_MESSAGES))
    finally:
        if channel_id in channel_locks:
            try:
                channel_locks[channel_id].release()
            except RuntimeError:
                pass  # Lock wasn't acquired (edge case)

# --- BACKGROUND TASK: Unprompted Messages (Bored Reze) ---
# Tracks whether Reze already sent an unanswered bored message (per channel)
unprompted_waiting_for_reply = {}

async def unprompted_message_loop():
    """Every few minutes, check target channels across all guilds to see if they've been dead. If so, Reze might text first."""
    await bot.wait_until_ready()
    IST = timezone(timedelta(hours=5, minutes=30))
    
    while not bot.is_closed():
        try:
            # Wait between checks
            await asyncio.sleep(random.randint(bot_config.get('unprompted_min_interval', 1800), bot_config.get('unprompted_max_interval', 3600)))
            
            if not bot_config.get('unprompted_enabled', True):
                continue
            
            # Only do this during "awake" hours (8 AM - 1 AM IST)
            now = datetime.now(IST)
            if now.hour < 8 or (now.hour >= 1 and now.hour < 6):
                continue
                
            # Loop through all guilds the bot is currently in
            for guild in bot.guilds:
                g_cfg = await get_guild_config(guild.id)
                target_ch_id = get_channel_id(g_cfg, 'target_channel_id', DEFAULT_TARGET_CHANNEL_ID)
                if not target_ch_id:
                    continue
                    
                channel = bot.get_channel(target_ch_id)
                if not channel:
                    continue
                
                channel_id = str(target_ch_id)
                
                # ANTI-FLOOD: If she already sent a bored message and nobody replied, DON'T send another
                if unprompted_waiting_for_reply.get(channel_id, False):
                    continue
                
                # Global cooldown: don't send if a recent event message was sent
                if time.time() - last_event_message_time < EVENT_COOLDOWN_SECONDS:
                    continue
                
                # Check if channel has been dead for at least 45 minutes
                last_activity = channel_last_activity.get(channel_id, 0)
                if time.time() - last_activity < 2700:  # Less than 45 min since last msg
                    continue
                
                # 25% chance to actually send something
                if random.random() > bot_config.get('unprompted_chance', 0.25):
                    continue
                    
                # Generate an unprompted message
                msg = await ai.generate_unprompted_message(channel_id)
                if msg:
                    async with channel.typing():
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                    await channel.send(msg)
                    # Mark that she's waiting for a reply — she won't text again until someone responds
                    unprompted_waiting_for_reply[channel_id] = True
                    last_event_message_time = time.time()
                    print(f"[UNPROMPTED] Reze sent a bored message in #{channel.name} ({guild.name}) (waiting for reply)")
                    
        except Exception as e:
            logger.error(f"Unprompted message loop error: {e}")
            await asyncio.sleep(60)

# --- BACKGROUND TASK: Story / Status Posting ---
async def story_posting_loop():
    """Posts an Instagram-style story (image + short caption) once a day."""
    await bot.wait_until_ready()
    IST = timezone(timedelta(hours=5, minutes=30))
    first_run = True
    
    while not bot.is_closed():
        try:
            if not first_run:
                # Wait between stories
                await asyncio.sleep(random.randint(bot_config.get('story_min_interval', 43200), bot_config.get('story_max_interval', 86400)))
            else:
                # Wait a bit after boot, then post the first story to prove it works
                await asyncio.sleep(45)
                first_run = False
                
            now = datetime.now(IST)
            # Only post during reasonable hours (e.g. not between 2 AM and 8 AM)
            if now.hour >= 2 and now.hour < 8:
                continue

            for guild in bot.guilds:
                g_cfg = await get_guild_config(guild.id)
                story_ch_id = get_channel_id(g_cfg, 'story_channel_id', DEFAULT_STORY_CHANNEL_ID)
                if not story_ch_id:
                    continue

                channel = bot.get_channel(story_ch_id)
                if not channel:
                    continue

                response = await ai.generate_story()
                if not response:
                    continue
                    
                meme_to_send = None
                web_meme_match = re.search(r'\[fetch_web:\s*(.*?)\]', response, re.IGNORECASE | re.DOTALL)
                if web_meme_match:
                    query = web_meme_match.group(1).strip()
                    clean_query = query.lower().replace("reze", "").strip()
                    
                    timeout = aiohttp.ClientTimeout(total=10)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        headers = {'User-agent': 'RezeBot/1.0'}
                        safe_query = urllib.parse.quote(f"reze_(chainsaw_man) {clean_query}")
                        url = f"https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&tags={safe_query}+rating:general&limit=50"
                        
                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if "post" in data:
                                    images = [p['file_url'] for p in data['post'] if "file_url" in p]
                                    if images:
                                        img_url = random.choice(images)
                                        async with session.get(img_url) as img_resp:
                                            if img_resp.status == 200:
                                                img_data = await img_resp.read()
                                                file_ext = img_url.split('?')[0].split('.')[-1]
                                                meme_to_send = discord.File(io.BytesIO(img_data), f"story.{file_ext}")

                caption = re.sub(r'\[fetch_web:.*?\]', '', response, flags=re.IGNORECASE).strip()
                
                if meme_to_send:
                    async with channel.typing():
                        await asyncio.sleep(3)
                    await channel.send(caption, file=meme_to_send)
                    print(f"[STORY] Reze posted a story to #{channel.name} ({guild.name})")

        except Exception as e:
            logger.error(f"Story posting loop error: {e}")
            await asyncio.sleep(60)

# --- BACKGROUND TASK: Wrong Chat Mistake ---
async def wrong_chat_loop():
    """Very rarely, Reze sends a message to the wrong chat, deletes it, and says 'mb wrong chat'."""
    await bot.wait_until_ready()
    IST = timezone(timedelta(hours=5, minutes=30))
    
    while not bot.is_closed():
        try:
            # Check every 2-5 hours
            await asyncio.sleep(random.randint(bot_config.get('wrong_chat_min_interval', 7200), bot_config.get('wrong_chat_max_interval', 18000)))
            
            # Only during active hours
            now = datetime.now(IST)
            if now.hour < 10 or now.hour >= 23:
                continue
            
            # 15% chance when the timer fires (so roughly once every 1-2 days)
            if not bot_config.get('wrong_chat_enabled', True) or random.random() > bot_config.get('wrong_chat_chance', 0.15):
                continue
                
            for guild in bot.guilds:
                g_cfg = await get_guild_config(guild.id)
                target_ch_id = get_channel_id(g_cfg, 'target_channel_id', DEFAULT_TARGET_CHANNEL_ID)
                if not target_ch_id:
                    continue

                channel = bot.get_channel(target_ch_id)
                if not channel:
                    continue
                
                # Send the "wrong" message
                wrong_msg = random.choice(WRONG_CHAT_MESSAGES)
                sent = await channel.send(wrong_msg)
                
                # Wait 2-5 seconds, then delete it
                await asyncio.sleep(random.uniform(2.0, 5.0))
                await sent.delete()
                
                # Then send the correction
                await asyncio.sleep(random.uniform(0.5, 1.5))
                corrections = ["mb wrong chat", "wrong chat lol", "oops wrong chat", "ignore that", "that wasn't for here 💀"]
                await channel.send(random.choice(corrections))
                print(f"[WRONG CHAT] Reze sent a wrong-chat message in #{channel.name} ({guild.name})")
            
        except Exception as e:
            logger.error(f"Wrong chat loop error: {e}")
            await asyncio.sleep(300)

# --- BACKGROUND TASK: Discord Rich Presence Cycling ---
async def status_cycling_loop():
    """Cycle Reze's Discord status throughout the day to look like a real user."""
    await bot.wait_until_ready()
    IST = timezone(timedelta(hours=5, minutes=30))
    last_hour = -1
    
    while not bot.is_closed():
        try:
            now = datetime.now(IST)
            current_hour = now.hour
            
            # Only update when the hour changes
            if current_hour != last_hour:
                last_hour = current_hour
                
                # Sleep hours: go DND/Idle
                if 2 <= current_hour < 7:
                    await bot.change_presence(
                        status=discord.Status.idle,
                        activity=discord.CustomActivity(name="💤")
                    )
                    print(f"[STATUS] Reze is sleeping (idle)")
                elif current_hour in STATUS_SCHEDULE:
                    activity_type, activity_name = STATUS_SCHEDULE[current_hour]
                    
                    if activity_type == discord.ActivityType.custom:
                        activity = discord.CustomActivity(name=activity_name)
                    elif activity_type == discord.ActivityType.playing:
                        activity = discord.Game(name=activity_name)
                    elif activity_type == discord.ActivityType.listening:
                        activity = discord.Activity(type=discord.ActivityType.listening, name=activity_name)
                    elif activity_type == discord.ActivityType.watching:
                        activity = discord.Activity(type=discord.ActivityType.watching, name=activity_name)
                    else:
                        activity = discord.CustomActivity(name=activity_name)
                    
                    await bot.change_presence(status=discord.Status.online, activity=activity)
                    print(f"[STATUS] Reze is now: {activity_name}")
                else:
                    await bot.change_presence(status=discord.Status.online, activity=None)
            
            # Check every 5 minutes
            await asyncio.sleep(300)
            
        except Exception as e:
            logger.error(f"Status cycling error: {e}")
            await asyncio.sleep(60)

# --- SERVER EVENT HANDLERS ---

async def handle_waifu_voting_reaction(payload):
    if payload.user_id == bot.user.id:
        return
    emoji_str = str(payload.emoji)
    if emoji_str not in ["🇦", "🇧"]:
        return
        
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception as e:
            logger.error(f"[WAIFU-VOTE] Failed to fetch channel {payload.channel_id}: {e}")
            return
            
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        logger.error(f"[WAIFU-VOTE] Failed to fetch message {payload.message_id}: {e}")
        return
        
    if not message.author or message.author.id != bot.user.id:
        return
        
    if not message.embeds:
        return
        
    embed = message.embeds[0]
    if not embed.title or not (embed.title.startswith("Who's the better waifu?") or embed.title.startswith("Who's the better husbando?") or embed.title.startswith("Who's the better villain?")):
        return

    logger.info(f"[WAIFU-VOTE] User {payload.user_id} reacted with {emoji_str} on message {message.id}")

    # If the reaction was added, check if we need to remove the opposite reaction to enforce single-voting
    if payload.event_type == "REACTION_ADD":
        opposite_emoji = "🇧" if emoji_str == "🇦" else "🇦"
        for r in message.reactions:
            if str(r.emoji) == opposite_emoji:
                async for u in r.users():
                    if u.id == payload.user_id:
                        try:
                            logger.info(f"[WAIFU-VOTE] Removing opposite reaction {opposite_emoji} for user {payload.user_id}")
                            await message.remove_reaction(opposite_emoji, u)
                        except Exception as ex:
                            logger.error(f"[WAIFU-VOTE] Failed to remove opposite reaction: {ex}")
                        break

    # Re-fetch the message to ensure we have the absolute latest reaction counts after potential removals
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        logger.error(f"[WAIFU-VOTE] Failed to re-fetch message: {e}")
        return

    votes_a_users = []
    votes_b_users = []
    for r in message.reactions:
        emoji_name = str(r.emoji)
        if emoji_name == "🇦":
            async for u in r.users():
                if u.id != bot.user.id:
                    votes_a_users.append(u.id)
        elif emoji_name == "🇧":
            async for u in r.users():
                if u.id != bot.user.id:
                    votes_b_users.append(u.id)
            
    votes_a_count = len(votes_a_users)
    votes_b_count = len(votes_b_users)
    total = votes_a_count + votes_b_count
    logger.info(f"[WAIFU-VOTE] Votes counted - A: {votes_a_count}, B: {votes_b_count}, Total: {total}")
    if total == 0:
        pct_a = 0
        pct_b = 0
    else:
        pct_a = round((votes_a_count / total) * 100)
        pct_b = 100 - pct_a
        
    if not embed.description:
        logger.warning("[WAIFU-VOTE] Embed description is empty")
        return
        
    lines = [line.strip() for line in embed.description.split("\n") if line.strip()]
    
    idx_a = -1
    idx_b = -1
    for idx, line in enumerate(lines):
        if line.startswith("🇦"):
            idx_a = idx
        elif line.startswith("🇧"):
            idx_b = idx
            
    if idx_a == -1 or idx_b == -1 or idx_a + 1 >= len(lines) or idx_b + 1 >= len(lines):
        logger.warning("[WAIFU-VOTE] Could not find option markers in description")
        return
        
    line_a = lines[idx_a]
    line_b = lines[idx_b]
    
    # Extract Character A Name using regex
    match_a = re.search(r"🇦\s*\*\*(?P<name>.*?)\s*\[\d+\s*%\]\*\*", line_a)
    if match_a:
        name1 = match_a.group("name").strip()
    else:
        # Fallback to index slicing if regex fails
        if line_a.startswith("🇦 **"):
            line_a = line_a[4:]
        idx_bracket = line_a.rfind(" [")
        name1 = line_a[:idx_bracket].rstrip("*").rstrip() if idx_bracket != -1 else "Unknown"
    
    # Extract Character A Anime/Artist
    line_anime_a = lines[idx_a + 1]
    if line_anime_a.startswith("*") and line_anime_a.endswith("*"):
        anime1 = line_anime_a[1:-1]
    else:
        anime1 = line_anime_a
        
    # Extract Character B Name using regex
    match_b = re.search(r"🇧\s*\*\*(?P<name>.*?)\s*\[\d+\s*%\]\*\*", line_b)
    if match_b:
        name2 = match_b.group("name").strip()
    else:
        if line_b.startswith("🇧 **"):
            line_b = line_b[4:]
        idx_bracket2 = line_b.rfind(" [")
        name2 = line_b[:idx_bracket2].rstrip("*").rstrip() if idx_bracket2 != -1 else "Unknown"
    
    # Extract Character B Anime/Artist
    line_anime_b = lines[idx_b + 1]
    if line_anime_b.startswith("*") and line_anime_b.endswith("*"):
        anime2 = line_anime_b[1:-1]
    else:
        anime2 = line_anime_b
        
    # Rebuild dynamic voter lines
    voters_a_str = ""
    if votes_a_users:
        voters_a_str = "\n" + "\n".join(f"• <@{uid}>" for uid in votes_a_users)
        
    voters_b_str = ""
    if votes_b_users:
        voters_b_str = "\n" + "\n".join(f"• <@{uid}>" for uid in votes_b_users)
        
    logger.info(f"[WAIFU-VOTE] Parsed - Name1: {name1} ({anime1}), Name2: {name2} ({anime2})")
    
    # Rebuild description with new percentages and dynamic voters
    desc_lines = [
        f"🇦 **{name1} [{pct_a} %]**",
        f"*{anime1}*{voters_a_str}",
        "",
        f"🇧 **{name2} [{pct_b} %]**",
        f"*{anime2}*{voters_b_str}"
    ]
    
    new_embed = discord.Embed(
        title=embed.title,
        description="\n".join(desc_lines),
        color=embed.color
    )
    new_embed.set_image(url="attachment://comparison.png")
    new_embed.set_thumbnail(url=embed.thumbnail.url if (embed.thumbnail and embed.thumbnail.url) else "https://cdn.discordapp.com/emojis/1509890494539370597.gif")
        
    try:
        await message.edit(embed=new_embed)
        logger.info(f"[WAIFU-VOTE] Successfully updated embed for message {message.id} (A: {pct_a}%, B: {pct_b}%)")
    except Exception as e:
        logger.error(f"[WAIFU-VOTE] Failed to update voting embed: {e}")

async def handle_wyr_voting_reaction(payload):
    if payload.user_id == bot.user.id:
        return
    emoji_str = str(payload.emoji)
    if emoji_str not in ["🅰️", "🅱️", "🅰", "🅱"]:
        return
        
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception as e:
            logger.error(f"[WYR-VOTE] Failed to fetch channel {payload.channel_id}: {e}")
            return
            
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        logger.error(f"[WYR-VOTE] Failed to fetch message {payload.message_id}: {e}")
        return
        
    if not message.author or message.author.id != bot.user.id:
        return
        
    if not message.embeds:
        return
        
    embed = message.embeds[0]
    if not embed.title or embed.title != "Would you rather ...":
        return

    logger.info(f"[WYR-VOTE] User {payload.user_id} reacted with {emoji_str} on message {message.id}")

    is_a = emoji_str in ["🅰️", "🅰"]
    opposite_emojis = ["🅱️", "🅱"] if is_a else ["🅰️", "🅰"]
    
    if payload.event_type == "REACTION_ADD":
        for r in message.reactions:
            if str(r.emoji) in opposite_emojis:
                async for u in r.users():
                    if u.id == payload.user_id:
                        try:
                            logger.info(f"[WYR-VOTE] Removing opposite reaction {r.emoji} for user {payload.user_id}")
                            await message.remove_reaction(r.emoji, u)
                        except Exception as ex:
                            logger.error(f"[WYR-VOTE] Failed to remove opposite reaction: {ex}")
                        break

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        logger.error(f"[WYR-VOTE] Failed to re-fetch message: {e}")
        return

    votes_a_users = []
    votes_b_users = []
    for r in message.reactions:
        emoji_name = str(r.emoji)
        if emoji_name in ["🅰️", "🅰"]:
            async for u in r.users():
                if u.id != bot.user.id:
                    votes_a_users.append(u.id)
        elif emoji_name in ["🅱️", "🅱"]:
            async for u in r.users():
                if u.id != bot.user.id:
                    votes_b_users.append(u.id)
                    
    votes_a_count = len(votes_a_users)
    votes_b_count = len(votes_b_users)
    total = votes_a_count + votes_b_count
    
    if total == 0:
        pct_a = 0
        pct_b = 0
    else:
        pct_a = round((votes_a_count / total) * 100)
        pct_b = 100 - pct_a
        
    if not embed.description:
        logger.warning("[WYR-VOTE] Embed description is empty")
        return
        
    lines = [line.strip() for line in embed.description.split("\n")]
    
    idx_a = -1
    idx_b = -1
    for idx, line in enumerate(lines):
        if line.startswith("🅰️") or line.startswith("🅰"):
            idx_a = idx
        elif line.startswith("🅱️") or line.startswith("🅱"):
            idx_b = idx
            
    if idx_a == -1 or idx_b == -1:
        logger.warning("[WYR-VOTE] Could not find option markers in description")
        return
        
    line_a = lines[idx_a]
    line_b = lines[idx_b]
    
    match_a = re.search(r"(🅰️|🅰)\s*(?P<text>.*?)\s*\[\d+\s*%\]", line_a)
    text_a = match_a.group("text").strip() if match_a else line_a
    
    match_b = re.search(r"(🅱️|🅱)\s*(?P<text>.*?)\s*\[\d+\s*%\]", line_b)
    text_b = match_b.group("text").strip() if match_b else line_b
    
    voters_a_str = ""
    if votes_a_users:
        voters_a_str = "\n" + "\n".join(f"  o <@{uid}>" for uid in votes_a_users)
        
    voters_b_str = ""
    if votes_b_users:
        voters_b_str = "\n" + "\n".join(f"  o <@{uid}>" for uid in votes_b_users)
        
    desc_lines = [
        f"🅰️ {text_a} [{pct_a} %]{voters_a_str}",
        "",
        f"🅱️ {text_b} [{pct_b} %]{voters_b_str}"
    ]
    
    new_embed = discord.Embed(
        title=embed.title,
        description="\n".join(desc_lines),
        color=embed.color
    )
    if embed.thumbnail and embed.thumbnail.url:
        new_embed.set_thumbnail(url=embed.thumbnail.url)
        
    try:
        await message.edit(embed=new_embed)
        logger.info(f"[WYR-VOTE] Successfully updated embed for message {message.id} (A: {pct_a}%, B: {pct_b}%)")
    except Exception as e:
        logger.error(f"[WYR-VOTE] Failed to update WYR embed: {e}")

async def handle_mkk_voting_reaction(payload):
    if payload.user_id == bot.user.id:
        return
    emoji_str = str(payload.emoji)
    if emoji_str not in ["🇦", "🇧", "🇨"]:
        return
        
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception as e:
            logger.error(f"[MKK-VOTE] Failed to fetch channel {payload.channel_id}: {e}")
            return
            
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        logger.error(f"[MKK-VOTE] Failed to fetch message {payload.message_id}: {e}")
        return
        
    if not message.author or message.author.id != bot.user.id:
        return
        
    if not message.embeds:
        return
        
    embed = message.embeds[0]
    if not embed.title or not embed.title.startswith("Marry, Kiss, Kill?"):
        return

    logger.info(f"[MKK-VOTE] User {payload.user_id} reacted with {emoji_str} on message {message.id} ({payload.event_type})")

    # Initialize message state if not present
    if message.id not in mkk_choices:
        mkk_choices[message.id] = {}
        
    user_id = payload.user_id
    if user_id not in mkk_choices[message.id]:
        mkk_choices[message.id][user_id] = []
        
    user_seq = mkk_choices[message.id][user_id]
    
    if payload.event_type == "REACTION_ADD":
        if emoji_str not in user_seq:
            user_seq.append(emoji_str)
    elif payload.event_type == "REACTION_REMOVE":
        if emoji_str in user_seq:
            user_seq.remove(emoji_str)
            
    # Clean up empty user lists
    if not user_seq:
        mkk_choices[message.id].pop(user_id, None)

    # Parse character information from existing description
    lines = [line.strip() for line in embed.description.split("\n") if line.strip()]
    
    idx_a = -1
    idx_b = -1
    idx_c = -1
    for idx, line in enumerate(lines):
        if line.startswith("🇦"):
            idx_a = idx
        elif line.startswith("🇧"):
            idx_b = idx
        elif line.startswith("🇨"):
            idx_c = idx
            
    if idx_a == -1 or idx_b == -1 or idx_c == -1:
        logger.warning("[MKK-VOTE] Could not find option markers in description")
        return
        
    def parse_char_info(idx):
        line = lines[idx]
        name = line[line.find("**")+2 : line.rfind("**")].strip() if "**" in line else "Unknown"
        
        anime_line = lines[idx+1] if idx+1 < len(lines) else "Unknown"
        if anime_line.startswith("*") and anime_line.endswith("*"):
            anime = anime_line[1:-1]
        else:
            anime = anime_line
        return name, anime

    name1, anime1 = parse_char_info(idx_a)
    name2, anime2 = parse_char_info(idx_b)
    name3, anime3 = parse_char_info(idx_c)

    # Reconstruct description
    desc_lines = [
        f"🇦 **{name1}**",
        f"*{anime1}*",
        "",
        f"🇧 **{name2}**",
        f"*{anime2}*",
        "",
        f"🇨 **{name3}**",
        f"*{anime3}*"
    ]
    
    choices_dict = mkk_choices[message.id]
    if choices_dict:
        desc_lines.append("")
        desc_lines.append("**Selections:**")
        for uid, seq in choices_dict.items():
            parts = []
            if len(seq) > 0:
                parts.append(f"💍 {seq[0]}")
            if len(seq) > 1:
                parts.append(f"💋 {seq[1]}")
            if len(seq) > 2:
                parts.append(f"💀 {seq[2]}")
            desc_lines.append(f"• <@{uid}>: " + " | ".join(parts))

    new_embed = discord.Embed(
        title=embed.title,
        description="\n".join(desc_lines),
        color=embed.color
    )
    new_embed.set_image(url="attachment://comparison.png")
    new_embed.set_thumbnail(url=embed.thumbnail.url if (embed.thumbnail and embed.thumbnail.url) else "https://cdn.discordapp.com/emojis/1509890494539370597.gif")
    
    try:
        await message.edit(embed=new_embed)
    except Exception as e:
        logger.error(f"[MKK-VOTE] Failed to update embed: {e}")

@bot.event
async def on_raw_reaction_add(payload):
    await handle_waifu_voting_reaction(payload)
    await handle_wyr_voting_reaction(payload)
    await handle_mkk_voting_reaction(payload)

@bot.event
async def on_raw_reaction_remove(payload):
    await handle_waifu_voting_reaction(payload)
    await handle_wyr_voting_reaction(payload)
    await handle_mkk_voting_reaction(payload)

@bot.event
async def on_member_join(member):
    """React to new members joining the server."""
    global last_event_message_time
    if member.bot:
        return
    g_cfg = await get_guild_config(member.guild.id) if member.guild else {}
    target_ch_id = get_channel_id(g_cfg, 'target_channel_id', DEFAULT_TARGET_CHANNEL_ID)
    channel = bot.get_channel(target_ch_id)
    if not channel:
        return
    # Global cooldown: don't flood the chat with event messages
    if time.time() - last_event_message_time < EVENT_COOLDOWN_SECONDS:
        return
    if random.random() < 0.15:  # Reduced from 0.3
        await asyncio.sleep(random.uniform(15, 60))  # Longer delay
        responses = ["who's this", "new person 👀", "oh hey", "another one", f"welcome ig {member.display_name}", "hi", "oh"]
        await channel.send(random.choice(responses))
        last_event_message_time = time.time()

@bot.event
async def on_member_remove(member):
    """React to members leaving the server."""
    global last_event_message_time
    if member.bot:
        return
    g_cfg = await get_guild_config(member.guild.id) if member.guild else {}
    target_ch_id = get_channel_id(g_cfg, 'target_channel_id', DEFAULT_TARGET_CHANNEL_ID)
    channel = bot.get_channel(target_ch_id)
    if not channel:
        return
    # Global cooldown: don't flood the chat with event messages
    if time.time() - last_event_message_time < EVENT_COOLDOWN_SECONDS:
        return
    if random.random() < 0.10:  # Reduced from 0.25
        await asyncio.sleep(random.uniform(10, 30))
        responses = ["lol they left", "rip", "bye ig", "💀", f"{member.display_name} left lmao", "another one gone"]
        await channel.send(random.choice(responses))
        last_event_message_time = time.time()

@bot.event
async def on_member_update(before, after):
    """React to nickname changes."""
    global last_event_message_time
    if before.bot:
        return
    if before.nick != after.nick and after.nick is not None:
        g_cfg = await get_guild_config(before.guild.id) if before.guild else {}
        target_ch_id = get_channel_id(g_cfg, 'target_channel_id', DEFAULT_TARGET_CHANNEL_ID)
        channel = bot.get_channel(target_ch_id)
        if channel and time.time() - last_event_message_time >= EVENT_COOLDOWN_SECONDS and random.random() < 0.12:
            await asyncio.sleep(random.uniform(20, 90))
            responses = [
                "why did you change your name 💀",
                "new name who dis",
                f"\"{after.display_name}\" ok sure",
                "i liked the old one better ngl"
            ]
            await channel.send(random.choice(responses))
            last_event_message_time = time.time()

@bot.event
async def on_voice_state_update(member, before, after):
    """React to voice channel joins/leaves."""
    global last_event_message_time
    if member.bot:
        return
    g_cfg = await get_guild_config(member.guild.id) if member.guild else {}
    target_ch_id = get_channel_id(g_cfg, 'target_channel_id', DEFAULT_TARGET_CHANNEL_ID)
    channel = bot.get_channel(target_ch_id)
    if not channel:
        return
    # Global cooldown
    if time.time() - last_event_message_time < EVENT_COOLDOWN_SECONDS:
        return
    # Someone joined VC
    if before.channel is None and after.channel is not None:
        if random.random() < 0.08:  # Reduced from 0.12
            await asyncio.sleep(random.uniform(20, 90))
            responses = [
                "yall in vc without me?",
                "who's in vc rn",
                f"{member.display_name} in vc? interesting",
                "i can't join vc rn but carry on",
                "vc at this hour?"
            ]
            await channel.send(random.choice(responses))
            last_event_message_time = time.time()
    # Someone left VC
    elif before.channel is not None and after.channel is None:
        if random.random() < 0.04:  # Reduced from 0.06
            await asyncio.sleep(random.uniform(10, 30))
            responses = ["kicked already? 💀", "vc over?", f"{member.display_name} left vc lol", "rip vc"]
            await channel.send(random.choice(responses))
            last_event_message_time = time.time()

if __name__ == "__main__":
    if TOKEN:
        keep_alive()  # Starts the dummy web server for Render
        
        try:
            bot.run(TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                print("\n" + "="*50)
                print("BANNED BY DISCORD (429) - YOUR IP IS BLOCKED")
                print("Please try changing your Render region (e.g. to Frankfurt).")
                print("Sleeping for 2 minutes before exit to prevent restart spam...")
                print("="*50 + "\n")
                time.sleep(120)
            raise e
        except Exception as e:
            print(f"Fatal error: {e}")
    else:
        print("DISCORD_TOKEN not found.")
