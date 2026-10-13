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

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

# ========================= BOT SETUP =========================

intents = discord.Intents.all()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix=".", intents=intents)

# ========================= EVENTS =========================

@bot.event
async def on_ready():
    print(f"{bot.user.name} is working.")

    if not hasattr(bot, "youtube_task"):
        bot.youtube_task = asyncio.create_task(check_youtube_upload())


@bot.event
async def on_member_join(member):
    channel = bot.get_channel(1451964963085095044)
    # # ↑ #welcome channel ID here

    if channel:
        await channel.send(f"Welcome {member.mention} to the server!")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if "shit" in message.content.lower():
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, please be respectful!"
            )
        except discord.Forbidden:
            print("Missing permission to delete messages.")
        except discord.HTTPException as error:
            print(f"Could not delete message: {error}")

    await bot.process_commands(message)

# ========================= COMMANDS =========================
# NORNAML INTERACTIVE COMMANDS
@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

#ANNOUNCE COMMAND

@bot.command()
@commands.has_permissions(administrator=True)
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
# ↑ #roles channel ID here


REACTION_ROLE_CATEGORIES = {

    "music": {
        "title": "🎵 MUSIC ROLES",
        "roles": {
            "🎧": "♩ Listener",
            "🎸": "♫ Musician",
            "🎛️": "𝄞 Producer",
            "🎤": "♬ Vocalist",
            "✍️": "♯ Songwriter",
            "📀": "♭ DJ"
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
# ========================= SETUP REACTION ROLE PANELS =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def setup_roles(ctx):
    channel = bot.get_channel(REACTION_ROLE_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        await ctx.send(
            "I couldn't find the reaction-role text channel. "
            "Check REACTION_ROLE_CHANNEL_ID."
        )
        return

    try:
        webhook = await channel.create_webhook(
            name="ChildLaborer Role Panels",
            reason="Creating reaction-role panels"
        )
    except discord.Forbidden:
        await ctx.send(
            "I need the Manage Webhooks permission!"
        )
        return
    except discord.HTTPException as error:
        await ctx.send(f"Couldn't create webhook: {error}")
        return

    panel_color = discord.Color.from_str("#C5B3D3")

    for category in REACTION_ROLE_CATEGORIES.values():
        embed = discord.Embed(
            title=category["title"],
            description=(
                "Choose your roles below!\n"
                "React with an emoji to get a role, "
                "or remove your reaction to remove it."
            ),
            color=panel_color
        )

        role_lines = [
            f"{emoji}  →  **{role_name}**"
            for emoji, role_name in category["roles"].items()
        ]

        embed.add_field(
            name="Available Roles",
            value="\n".join(role_lines),
            inline=False
        )

        embed.set_footer(
            text="ChildLaborer • Reaction Roles"
        )

        try:
            message = await webhook.send(
                embed=embed,
                username="ChildLaborer",
                wait=True
            )

            for emoji in category["roles"]:
                await message.add_reaction(emoji)
                await asyncio.sleep(0.3)

        except discord.HTTPException as error:
            print(
                f"Failed to create {category['title']} panel: {error}"
            )
            await ctx.send(
                f"Failed to create {category['title']} panel."
            )
            return

    await ctx.send(
        "All four webhook reaction-role panels have been created!"
    )

# ========================= REACTION ROLE HELPERS =========================
async def get_role_panel_and_role(payload):
    if payload.guild_id is None:
        return None

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return None

    emoji = str(payload.emoji)
    role_name = None
    expected_title = None

    for category in REACTION_ROLE_CATEGORIES.values():
        if emoji in category["roles"]:
            role_name = category["roles"][emoji]
            expected_title = category["title"]
            break

    if role_name is None:
        return None

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return None

    try:
        message = await channel.fetch_message(payload.message_id)
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException
    ):
        return None

    is_role_panel = any(
        embed.title == expected_title
        for embed in message.embeds
    )

    if message.webhook_id is None or not is_role_panel:
        return None

    role = discord.utils.get(guild.roles, name=role_name)

    if role is None:
        print(f"Role not found: {role_name}")
        return None

    try:
        member = guild.get_member(payload.user_id)

        if member is None:
            member = await guild.fetch_member(payload.user_id)

    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException
    ):
        return None

    if member.bot:
        return None

    return member, role


@bot.event
async def on_raw_reaction_add(payload):
    result = await get_role_panel_and_role(payload)

    if result is None:
        return

    member, role = result

    try:
        await member.add_roles(
            role,
            reason="Reaction role selected"
        )
    except discord.Forbidden:
        print(
            f"Cannot give {role.name} to {member.name}. "
            "Check Manage Roles and role hierarchy."
        )
    except discord.HTTPException as error:
        print(f"Failed to add {role.name}: {error}")


@bot.event
async def on_raw_reaction_remove(payload):
    result = await get_role_panel_and_role(payload)

    if result is None:
        return

    member, role = result

    try:
        await member.remove_roles(
            role,
            reason="Reaction role deselected"
        )
    except discord.Forbidden:
        print(
            f"Cannot remove {role.name} from {member.name}. "
            "Check Manage Roles and role hierarchy."
        )
    except discord.HTTPException as error:
        print(f"Failed to remove {role.name}: {error}")


# ========================= YOUTUBE UPLOAD NOTIFICATIONS =========================

UPLOAD_NOTIFICATION_CHANNEL_ID = 1444934366043177033
YOUTUBE_CHANNEL_ID = "UCOovil69O5hsE2pQz9XdtPQ"
UPLOAD_ROLE_NAME = "Upload Ping"
# ========================= YOUTUBE UPLOAD CHECK =========================
# ========================= YOUTUBE STATE HELPERS =========================

def read_last_video_id():
    try:
        with open(YOUTUBE_STATE_FILE, "r", encoding="utf-8") as file:
            return file.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_video_id(video_id):
    with open(YOUTUBE_STATE_FILE, "w", encoding="utf-8") as file:
        file.write(video_id)


# ========================= YOUTUBE UPLOAD CHECKER =========================

async def check_youtube_upload():
    last_video_id = read_last_video_id()

    namespace = {
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "atom": "http://www.w3.org/2005/Atom"
    }

    url = (
        "https://www.youtube.com/feeds/videos.xml"
        f"?channel_id={YOUTUBE_CHANNEL_ID}"
    )

    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            timeout = aiohttp.ClientTimeout(total=30)

            async with aiohttp.ClientSession(
                timeout=timeout
            ) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        print(
                            f"YouTube RSS returned status "
                            f"{response.status}"
                        )
                        await asyncio.sleep(300)
                        continue

                    data = await response.text()

            root = ET.fromstring(data)
            entries = root.findall("atom:entry", namespace)

            if not entries:
                await asyncio.sleep(300)
                continue

            videos = []

            for entry in entries:
                video_id_element = entry.find(
                    "yt:videoId", namespace
                )
                title_element = entry.find(
                    "atom:title", namespace
                )
                author_element = entry.find(
                    "atom:author/atom:name", namespace
                )
                author_url_element = entry.find(
                    "atom:author/atom:uri", namespace
                )

                if (
                    video_id_element is None
                    or not video_id_element.text
                    or title_element is None
                ):
                    continue

                videos.append({
                    "id": video_id_element.text,
                    "title": title_element.text or "New YouTube video",
                    "channel_name": (
                        author_element.text
                        if author_element is not None
                        and author_element.text
                        else "YouTube"
                    ),
                    "channel_url": (
                        author_url_element.text
                        if author_url_element is not None
                        and author_url_element.text
                        else (
                            "https://www.youtube.com/channel/"
                            f"{YOUTUBE_CHANNEL_ID}"
                        )
                    )
                })

            if not videos:
                await asyncio.sleep(300)
                continue

            newest_video_id = videos[0]["id"]

            # First run: save the newest video as a baseline.
            # This avoids announcing old videos when first installed.
            if last_video_id is None:
                last_video_id = newest_video_id
                save_last_video_id(last_video_id)
                print(
                    "YouTube baseline saved. "
                    "Waiting for new uploads."
                )

            elif newest_video_id != last_video_id:
                notification_channel = bot.get_channel(
                    UPLOAD_NOTIFICATION_CHANNEL_ID
                )

                if notification_channel is None:
                    print(
                        "Upload notification channel not found. "
                        "Will retry later."
                    )
                    await asyncio.sleep(300)
                    continue

                # RSS entries are newest-first. Stop at the saved ID,
                # then reverse the new entries to announce oldest-first.
                new_videos = []

                for video in videos:
                    if video["id"] == last_video_id:
                        break
                    new_videos.append(video)

                if not new_videos:
                    print(
                        "Saved video wasn't found in the current RSS "
                        "feed. The feed may have rolled over."
                    )
                    # Avoid repeatedly announcing the same feed.
                    # Save the newest entry as the new baseline.
                    last_video_id = newest_video_id
                    save_last_video_id(last_video_id)

                else:
                    for video in reversed(new_videos):
                        video_id = video["id"]
                        title = video["title"]
                        channel_name = video["channel_name"]
                        channel_url = video["channel_url"]

                        role = discord.utils.get(
                            notification_channel.guild.roles,
                            name=UPLOAD_ROLE_NAME
                        )

                        role_mention = (
                            role.mention if role is not None else ""
                        )

                        video_url = (
                            "https://www.youtube.com/watch?v="
                            f"{video_id}"
                        )

                        embed = discord.Embed(
                            title=title,
                            url=video_url,
                            description=(
                                f"A new video has been uploaded!\n\n"
                                f"[**Watch {discord.utils.escape_markdown(title)}**]"
                                f"({video_url})"
                            ),
                            color=discord.Color.from_str("#C5B3D3")
                        )

                        embed.set_author(
                            name=channel_name,
                            url=channel_url
                        )

                        embed.set_image(
                            url=(
                                "https://img.youtube.com/vi/"
                                f"{video_id}/hqdefault.jpg"
                            )
                        )

                        embed.set_footer(
                            text="YouTube • New Upload"
                        )

                        message_content = (
                            f"{role_mention}\n\n"
                            f"## New video from "
                            f"[{channel_name}]({channel_url})\n\n"
                            f"> If you'd like to stop getting notified, "
                            f"adjust your roles in "
                            f"<#{REACTION_ROLE_CHANNEL_ID}>."
                        )

                        try:
                            await notification_channel.send(
                                content=message_content,
                                embed=embed,
                                allowed_mentions=discord.AllowedMentions(
                                    roles=True
                                )
                            )
                        except discord.HTTPException as error:
                            print(
                                f"Could not announce video {video_id}: "
                                f"{error}"
                            )
                            # Stop here so this video can be retried.
                            break

                        # Save progress after each successful notification.
                        last_video_id = video_id
                        save_last_video_id(last_video_id)

        except (aiohttp.ClientError, ET.ParseError) as error:
            print(f"YouTube feed error: {error}")

        except Exception as error:
            print(f"YouTube notification error: {error}")

        await asyncio.sleep(300)

# ========================= SETUP REACTION ROLES =========================

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_roles(ctx):

    channel = bot.get_channel(REACTION_ROLE_CHANNEL_ID)

    if channel is None:
        await ctx.send("I couldn't find the reaction role channel.")
        return

    # Create a webhook for the role panels
    try:
        webhook = await channel.create_webhook(
            name="ChildLaborer Role Panels",
            reason="Creating reaction role panels"
        )

    except discord.Forbidden:
        await ctx.send(
            "I need the Manage Webhooks permission "
            "to create the role panels!"
        )
        return

    except discord.HTTPException as error:
        await ctx.send(f"Couldn't create webhook: {error}")
        return

    # Embed colour for all role panels
    panel_color = discord.Color.from_str("#C5B3D3")

    # Create one embed per category
    for category in REACTION_ROLE_CATEGORIES.values():

        embed = discord.Embed(
            title=category["title"],
            description=(
                "Choose your roles below!\n"
                "React with an emoji to get a role, "
                "or remove your reaction to remove it."
            ),
            color=panel_color
        )

        role_lines = []

        for emoji, role_name in category["roles"].items():
            role_lines.append(f"{emoji}  →  **{role_name}**")

        embed.add_field(
            name="Available Roles",
            value="\n".join(role_lines),
            inline=False
        )

        embed.set_footer(
            text="ChildLaborer • Reaction Roles"
        )

        try:
            # Send the embed through the webhook
            message = await webhook.send(
                embed=embed,
                username="ChildLaborer",
                wait=True
            )

            # Add the corresponding reactions
            for emoji in category["roles"]:
                await message.add_reaction(emoji)

                # Avoid hitting Discord rate limits
                await asyncio.sleep(0.3)

        except discord.HTTPException as error:
            print(
                f"Failed to create {category['title']} panel: {error}"
            )
            await ctx.send(
                f"Failed to create {category['title']} panel."
            )
            return

    await ctx.send(
        "All four webhook reaction-role panels have been created!"
    )

# ========================= ROLES =========================


# ========================= REACTION ROLE ADD =========================

@bot.event
async def on_raw_reaction_add(payload):

    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    # Ignore bot reactions
    if payload.user_id == bot.user.id:
        return

    try:
        member = guild.get_member(payload.user_id)
        if member is None:
            member = await guild.fetch_member(payload.user_id)
    except discord.NotFound:
        return

    if member.bot:
        return

    emoji = str(payload.emoji)
    role_name = None
    expected_title = None

    # Find the matching role
    for category in REACTION_ROLE_CATEGORIES.values():
        if emoji in category["roles"]:
            role_name = category["roles"][emoji]
            expected_title = category["title"]
            break

    if role_name is None:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    # Accept webhook messages with matching embed titles
    is_role_panel = any(
        embed.title == expected_title
        for embed in message.embeds
    )

    if message.webhook_id is None or not is_role_panel:
        return

    role = discord.utils.get(guild.roles, name=role_name)

    if role is None:
        print(f"Role not found: {role_name}")
        return

    try:
        await member.add_roles(role, reason="Reaction role")
    except discord.Forbidden:
        print(f"Cannot give {role_name} to {member.name}")
    except discord.HTTPException as error:
        print(f"Failed to add {role_name}: {error}")


# ========================= REACTION ROLE REMOVE =========================

@bot.event
async def on_raw_reaction_remove(payload):

    if payload.guild_id is None:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    try:
        member = guild.get_member(payload.user_id)
        if member is None:
            member = await guild.fetch_member(payload.user_id)
    except discord.NotFound:
        return

    if member.bot:
        return

    emoji = str(payload.emoji)
    role_name = None
    expected_title = None

    # Find the matching role
    for category in REACTION_ROLE_CATEGORIES.values():
        if emoji in category["roles"]:
            role_name = category["roles"][emoji]
            expected_title = category["title"]
            break

    if role_name is None:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    # Accept webhook messages with matching embed titles
    is_role_panel = any(
        embed.title == expected_title
        for embed in message.embeds
    )

    if message.webhook_id is None or not is_role_panel:
        return

    role = discord.utils.get(guild.roles, name=role_name)

    if role is None:
        print(f"Role not found: {role_name}")
        return

    try:
        await member.remove_roles(role, reason="Reaction role removed")
    except discord.Forbidden:
        print(f"Cannot remove {role_name} from {member.name}")
    except discord.HTTPException as error:
        print(f"Failed to remove {role_name}: {error}")

# ========================= START BOT =========================

bot.run(
    token, log_handler=handler, log_level=logging.DEBUG)
