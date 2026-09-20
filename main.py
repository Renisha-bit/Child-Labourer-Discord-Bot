import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import aiohttp
import asyncio
import xml.etree.ElementTree as ET

load_dotenv()

token = os.getenv("DISCORD_TOKEN")

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

# ========================= BOT SETUP =========================

intents = discord.Intents.all()

bot = commands.Bot(command_prefix=".", intents=intents)

# ========================= EVENTS =========================

@bot.event
async def on_ready():
    print(f"{bot.user.name} is working.")


@bot.event
async def on_member_join(member):
    channel = bot.get_channel(1451964963085095044)

    if channel:
        await channel.send(f"Welcome {member.mention} to the server!")

@bot.event
async def on_message(message):
    # Ignore messages sent by the bot itself
    if message.author == bot.user:
        return

    # Respond when someone pings the bot
    if "shit" in message.content.lower():
        await message.delete()
        await message.channel.send( f" {message.author.mention}, Please be respectful!")

    await bot.process_commands(message)

# ========================= COMMANDS =========================
@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

@bot.command()
async def announce(ctx):
    await ctx.send("Which channel should I send the message to?\nPlease enter the channel ID:")

    def check(message):
        return message.author == ctx.author and message.channel == ctx.channel

    try:
        channel_message = await bot.wait_for("message", timeout=60.0, check=check)

        channel_id = int(channel_message.content.strip())
        channel = bot.get_channel(channel_id)

        if channel is None:
            await ctx.send("I couldn't find that channel.")
            return

        await ctx.send("What message should I send?")

        message_response = await bot.wait_for(
            "message", timeout=300.0, check=check)

        await channel.send(message_response.content)

        await ctx.send("Successfully sent the message!")

    except ValueError:
        await ctx.send("That isn't a valid channel ID.")

    except TimeoutError:
        await ctx.send("Timed out. Please run `.announce` again.")

# ========================= REACTION ROLES =========================

REACTION_ROLE_CHANNEL_ID = 1550216770156232834
# ↑ Put your #roles channel ID here


REACTION_ROLE_CATEGORIES = {

    "music": {
        "title": "🎵 MUSIC ROLES",
        "roles": {
            "🎧": "Listener",
            "🎸": "Musician",
            "🎛️": "Producer",
            "🎤": "Vocalist",
            "✍️": "Songwriter",
            "📀": "DJ"
        }
    },

    "notifications": {
        "title": "🔔 NOTIFICATION ROLES",
        "roles": {
            "🔔": "Upload Ping",
            "📢": "VC Ping"
        }
    },

    "games": {
        "title": "🎮 GAME ROLES",
        "roles": {
            "⚔️": "Valorant",
            "⛏️": "Minecraft"
        }
    },

    "colors": {
        "title": "🎨 COLORS",
        "roles": {
            "🔴": "Red",
            "🟠": "Orange",
            "🟡": "Yellow",
            "🟢": "Green",
            "🔵": "Blue",
            "🟣": "Purple",
            "🩷": "Pink",
            "⚪": "White",
            "⚫": "Black"
        }
    }
}

# ========================= UPLOAD NOTIFICATIONS =========================

UPLOAD_NOTIFICATION_CHANNEL_ID = 1444934366043177033
YOUTUBE_CHANNEL_ID = "UCOovil69O5hsE2pQz9XdtPQ"
UPLOAD_ROLE_NAME = "Upload Ping"
# ========================= YOUTUBE UPLOAD CHECK =========================

import aiohttp
import asyncio

last_video_id = None


async def check_youtube_upload():

    global last_video_id

    await bot.wait_until_ready()

    while not bot.is_closed():

        try:
            # YouTube channel videos RSS feed
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id=UCOovil69O5hsE2pQz9XdtPQ"

            async with aiohttp.ClientSession() as session:

                async with session.get(url) as response:

                    if response.status != 200:
                        await asyncio.sleep(300)
                        continue

                    data = await response.text()

            # Get latest video information
            import xml.etree.ElementTree as ET

            root = ET.fromstring(data)

            namespace = {
                "yt": "http://www.youtube.com/xml/schemas/2015",
                "atom": "http://www.w3.org/2005/Atom"
            }

            entry = root.find("atom:entry", namespace)

            if entry is not None:

                video_id = entry.find("yt:videoId", namespace).text
                title = entry.find("atom:title", namespace).text

                # First run: remember the latest video without sending a notification
                if last_video_id is None:
                    last_video_id = video_id

                # New upload detected
                elif video_id != last_video_id:

                    channel = bot.get_channel(UPLOAD_NOTIFICATION_CHANNEL_ID)

                    if channel:

                        role = discord.utils.get(
                            channel.guild.roles,
                            name=UPLOAD_ROLE_NAME
                        )

                        if role:

                            await channel.send(
                                f"{role.mention}\n"
                                f"🎵 **New upload: {title}**\n"
                                f"https://www.youtube.com/watch?v={video_id}"
                            )

                        else:

                            await channel.send(
                                f"🎵 **New upload: {title}**\n"
                                f"https://www.youtube.com/watch?v={video_id}"
                            )

                    last_video_id = video_id

        except Exception as e:
            print(f"YouTube check error: {e}")

        # Check every 5 minutes
        await asyncio.sleep(300)


# ========================= START YOUTUBE CHECKER =========================

@bot.event
async def setup_hook():

    bot.loop.create_task(check_youtube_upload())
# ========================= SETUP REACTION ROLES =========================

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_roles(ctx):

    channel = bot.get_channel(REACTION_ROLE_CHANNEL_ID)

    if channel is None:
        await ctx.send("I couldn't find the reaction role channel.")
        return

    for category in REACTION_ROLE_CATEGORIES.values():

        role_lines = []

        for emoji, role_name in category["roles"].items():
            role_lines.append(f"{emoji} — {role_name}")

        message_content = (
            f"╭──────〔 {category['title']} 〕──────╮\n\n"
            + "\n".join(role_lines)
            + "\n\nReact below to choose your role!"
        )

        message = await channel.send(message_content)

        for emoji in category["roles"]:
            await message.add_reaction(emoji)

    await ctx.send("Reaction role messages created successfully!")


# ========================= REACTION ROLE ADD =========================

@bot.event
async def on_raw_reaction_add(payload):

    # Ignore DMs
    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)

    if guild is None:
        return

    member = guild.get_member(payload.user_id)

    # Ignore bots
    if member is None or member.bot:
        return

    emoji = str(payload.emoji)

    role_name = None

    # Find which role belongs to this emoji
    for category in REACTION_ROLE_CATEGORIES.values():

        if emoji in category["roles"]:
            role_name = category["roles"][emoji]

            channel = bot.get_channel(payload.channel_id)

            if channel is None:
                return

            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.NotFound:
                return

            # Make sure this message was created by our bot
            if message.author != bot.user:
                return

            # Make sure it is actually one of our reaction-role messages
            if not any(
                category_data["title"] in message.content
                for category_data in REACTION_ROLE_CATEGORIES.values()
            ):
                return

            break

    if role_name is None:
        return

    role = discord.utils.get(guild.roles, name=role_name)

    if role is None:
        print(f"Role not found: {role_name}")
        return

    try:
        await member.add_roles(role)

    except discord.Forbidden:
        print(f"Cannot give {role_name} to {member.name}")


# ========================= REACTION ROLE REMOVE =========================

@bot.event
async def on_raw_reaction_remove(payload):

    # Ignore DMs
    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)

    if guild is None:
        return

    member = guild.get_member(payload.user_id)

    # Ignore bots
    if member is None or member.bot:
        return

    emoji = str(payload.emoji)

    role_name = None

    # Find which role belongs to this emoji
    for category in REACTION_ROLE_CATEGORIES.values():

        if emoji in category["roles"]:
            role_name = category["roles"][emoji]

            channel = bot.get_channel(payload.channel_id)

            if channel is None:
                return

            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.NotFound:
                return

            # Make sure this message was created by our bot
            if message.author != bot.user:
                return

            # Make sure it is actually one of our reaction-role messages
            if not any(
                category_data["title"] in message.content
                for category_data in REACTION_ROLE_CATEGORIES.values()
            ):
                return

            break

    if role_name is None:
        return

    role = discord.utils.get(guild.roles, name=role_name)

    if role is None:
        print(f"Role not found: {role_name}")
        return

    try:
        await member.remove_roles(role)

    except discord.Forbidden:
        print(f"Cannot remove {role_name} from {member.name}")

# ========================= START BOT =========================

bot.run(
    token, log_handler=handler, log_level=logging.DEBUG)

