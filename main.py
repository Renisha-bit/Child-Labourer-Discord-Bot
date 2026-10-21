import asyncio
import difflib
import logging
import os
import traceback
import xml.etree.ElementTree as ET
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Reaction roles live entirely in role_manager.py (database-driven). main.py must not
# define its own reaction handlers or panel commands, or the two would clash.
from role_manager import ROLE_CHANNEL_ID, install_role_manager

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

if not token:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")

BASE_DIR = Path(__file__).resolve().parent

# Rotating so the log neither grows forever nor gets wiped on every restart.
handler = RotatingFileHandler(
    BASE_DIR / "discord.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)

# ========================= BOT SETUP =========================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=".", intents=intents)

error_log = logging.getLogger("discord.bot")

# Kept as a module variable (not an attribute on `bot`) so type checkers don't complain.
youtube_task: Optional[asyncio.Task] = None

# ========================= EVENTS =========================

# Prints what the bot receives and what it does to THIS terminal, so you can see exactly where
# a command stops working. Set to False once everything works.
DEBUG_PRINTS = True

WELCOME_CHANNEL_ID = 1451964963085095044
# ↑ #welcome channel ID here


def report_setup():
    """Print the things that most often make role commands fail, once at startup."""
    for guild in bot.guilds:
        me = guild.me
        perms = me.guild_permissions
        print(
            f"[setup] Server '{guild.name}': Manage Roles={'YES' if perms.manage_roles else 'NO'}, "
            f"Administrator={'YES' if perms.administrator else 'no'}, my highest role='{me.top_role.name}'"
        )
    channel = bot.get_channel(ROLE_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print(f"[setup] PROBLEM: ROLE_CHANNEL_ID {ROLE_CHANNEL_ID} is not a text channel I can see.")
        return
    channel_perms = channel.permissions_for(channel.guild.me)
    needed = ("view_channel", "send_messages", "embed_links", "add_reactions", "read_message_history")
    missing = [name for name in needed if not getattr(channel_perms, name)]
    if missing:
        print(f"[setup] PROBLEM: in #{channel.name} I'm missing: {', '.join(missing)}")
    else:
        print(f"[setup] Role channel #{channel.name}: permissions OK")


@bot.event
async def on_ready():
    print(f"{bot.user} is working.")
    print("Commands loaded: " + ", ".join(sorted(command.name for command in bot.commands)))
    report_setup()

    # on_ready can fire again after a reconnect, so only (re)start the task if needed.
    global youtube_task
    if youtube_task is None or youtube_task.done():
        youtube_task = asyncio.create_task(check_youtube_upload())


@bot.event
async def on_member_join(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)

    if isinstance(channel, discord.TextChannel):
        await channel.send(f"Welcome {member.mention} to the server!")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if DEBUG_PRINTS and message.content.startswith("."):
        print(f"[received] {message.author}: {message.content[:100]}")

    if "niqqa" in message.content.lower():
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, please be respectful!"
            )
        except discord.Forbidden:
            print("Missing permission to delete messages.")
        except discord.HTTPException as error:
            print(f"Could not delete message: {error}")
        # Don't run commands from a message that was just removed for breaking the rules.
        return

    await bot.process_commands(message)


@bot.listen("on_command")
async def log_command(ctx):
    if DEBUG_PRINTS:
        print(f"[command] {ctx.prefix}{ctx.command} run by {ctx.author}")


@bot.event
async def on_command_error(ctx, error):
    if DEBUG_PRINTS:
        if isinstance(error, commands.CommandNotFound):
            print(f"[unknown command] {ctx.prefix}{ctx.invoked_with}  (this bot doesn't have that command)")
        else:
            print(f"[command error] {ctx.command}: {type(error).__name__}: {error}")

    # Without this, every failed permission check dumps a traceback and the user gets no reply.
    if ctx.command is not None and ctx.command.has_error_handler():
        return  # that command reports its own errors (the role commands do)

    if isinstance(error, commands.CommandNotFound):
        typed = (ctx.invoked_with or "").lower()
        close = difflib.get_close_matches(typed, [command.name for command in bot.commands], n=1, cutoff=0.7)
        if close:
            await ctx.send(f"I don't know `{ctx.prefix}{typed}`. Did you mean `{ctx.prefix}{close[0]}`?")
        return

    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("That command only works inside a server.")
    elif isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        await ctx.send("You don't have permission to use that command.")
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)) and ctx.command is not None:
        usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
        await ctx.send(f"Usage: `{usage}`")
    else:
        # Show it in the terminal too: log_handler sends discord's logging to discord.log only.
        traceback.print_exception(type(error), error, error.__traceback__)
        error_log.error(
            "Unhandled error in command %s", ctx.command, exc_info=error
        )
        await ctx.send("Something went wrong running that command.")

# ========================= COMMANDS =========================
# NORMAL INTERACTIVE COMMANDS
@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

# ANNOUNCE COMMAND

@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def announce(ctx):
    await ctx.send("Which channel should I send the message to?\nPlease enter the channel ID:")

    def check(message):
        return message.author == ctx.author and message.channel == ctx.channel

    try:
        channel_message = await bot.wait_for("message", timeout=60.0, check=check)

        channel_id = int(channel_message.content.strip())
        channel = bot.get_channel(channel_id)

        # Only allow channels in this server, so an admin here can't post in another server.
        if (
            not isinstance(channel, discord.abc.Messageable)
            or getattr(channel, "guild", None) != ctx.guild
        ):
            await ctx.send("I couldn't find that channel.")
            return

        await ctx.send("What message should I send?")

        message_response = await bot.wait_for(
            "message", timeout=300.0, check=check)

        await channel.send(message_response.content)

        await ctx.send("Successfully sent the message!")

    except ValueError:
        await ctx.send("That isn't a valid channel ID.")

    except discord.Forbidden:
        await ctx.send("I don't have permission to send messages in that channel.")

    except discord.HTTPException as error:
        await ctx.send(f"Couldn't send the message: {error}")

    except asyncio.TimeoutError:
        await ctx.send("Timed out. Please run `.announce` again.")

# ========================= YOUTUBE UPLOAD NOTIFICATIONS =========================

UPLOAD_NOTIFICATION_CHANNEL_ID = 1444934366043177033
YOUTUBE_CHANNEL_ID = "UCOovil69O5hsE2pQz9XdtPQ"
UPLOAD_ROLE_NAME = "Upload Ping"
# Absolute path so the saved state doesn't depend on where the bot is launched from.
YOUTUBE_STATE_FILE = BASE_DIR / "last_video_id.txt"

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

                if not isinstance(notification_channel, discord.TextChannel):
                    print(
                        "Upload notification channel not found. "
                        "Will retry later."
                    )
                    await asyncio.sleep(300)
                    continue

                # RSS entries are newest-first. Stop at the saved ID,
                # then reverse the new entries to announce oldest-first.
                new_videos = []
                found = False

                for video in videos:
                    if video["id"] == last_video_id:
                        found = True
                        break
                    new_videos.append(video)

                if not found:
                    print(
                        "Saved video wasn't found in the current RSS "
                        "feed. The feed may have rolled over."
                    )
                    # Don't announce the whole feed. Save the newest
                    # entry as the new baseline instead.
                    last_video_id = newest_video_id
                    save_last_video_id(last_video_id)

                else:
                    role = discord.utils.get(
                        notification_channel.guild.roles,
                        name=UPLOAD_ROLE_NAME
                    )

                    role_mention = (
                        role.mention if role is not None else ""
                    )

                    for video in reversed(new_videos):
                        video_id = video["id"]
                        title = video["title"]
                        channel_name = video["channel_name"]
                        channel_url = video["channel_url"]

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
                            color=discord.Color.from_str("#C033DD")
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
                            f"<#{ROLE_CHANNEL_ID}>."
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

# ========================= YOUTUBE MANUAL TEST COMMAND =========================

@bot.command(name="youtubetest")
@commands.has_permissions(administrator=True)
async def youtubetest(ctx: commands.Context):
    """Manually test the YouTube upload notification system."""

    await ctx.send("🧪 Checking the YouTube feed and preparing a test notification...")

    url = (
        "https://www.youtube.com/feeds/videos.xml"
        f"?channel_id={YOUTUBE_CHANNEL_ID}"
    )

    namespace = {
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "atom": "http://www.w3.org/2005/Atom",
    }

    try:
        # Fetch the RSS feed without changing the automatic checker's state.
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    await ctx.send(
                        f"❌ YouTube RSS returned HTTP {response.status}."
                    )
                    return

                data = await response.text()

        root = ET.fromstring(data)
        entries = root.findall("atom:entry", namespace)

        # The feed is newest-first. Select the second entry so the
        # test uses an older upload instead of the latest one.
        if len(entries) < 2:
            await ctx.send(
                "❌ The YouTube feed doesn't contain enough videos "
                "to select an older upload. Try again later."
            )
            return

        entry = entries[1]

        video_id_element = entry.find("yt:videoId", namespace)
        title_element = entry.find("atom:title", namespace)
        author_element = entry.find("atom:author/atom:name", namespace)
        author_url_element = entry.find("atom:author/atom:uri", namespace)

        if (
            video_id_element is None
            or not video_id_element.text
            or title_element is None
        ):
            await ctx.send("❌ Couldn't read the older video's details.")
            return

        video_id = video_id_element.text
        title = title_element.text or "YouTube video"
        channel_name = (
            author_element.text
            if author_element is not None and author_element.text
            else "YouTube"
        )
        channel_url = (
            author_url_element.text
            if author_url_element is not None and author_url_element.text
            else f"https://www.youtube.com/channel/{YOUTUBE_CHANNEL_ID}"
        )
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        # Resolve the configured notification channel.
        notification_channel = bot.get_channel(
            UPLOAD_NOTIFICATION_CHANNEL_ID
        )

        if notification_channel is None:
            try:
                notification_channel = await bot.fetch_channel(
                    UPLOAD_NOTIFICATION_CHANNEL_ID
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                await ctx.send(
                    f"❌ Couldn't access the upload notification channel: {error}"
                )
                return

        if not isinstance(notification_channel, discord.TextChannel):
            await ctx.send(
                "❌ The configured upload notification destination "
                "isn't a regular text channel."
            )
            return

        # Use the same Upload Ping role as the automatic notifier.
        role = discord.utils.get(
            notification_channel.guild.roles,
            name=UPLOAD_ROLE_NAME,
        )
        role_mention = role.mention if role is not None else ""

        embed = discord.Embed(
            title=title,
            url=video_url,
            description=(
                "🧪 **Manual YouTube notification test**\n\n"
                f"[**Watch {discord.utils.escape_markdown(title)}**]"
                f"({video_url})"
            ),
            color=discord.Color.from_str("#C033DD"),
        )

        embed.set_author(name=channel_name, url=channel_url)
        embed.set_image(
            url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
        )
        embed.set_footer(text="YouTube • Manual Test")

        message_content = (
            f"{role_mention}\n\n"
            f"## YouTube notification test from "
            f"[{channel_name}]({channel_url})\n\n"
            f"> This is a manual test. No new upload was detected or "
            f"announced by the automatic checker.\n"
            f"> Manage notifications in <#{ROLE_CHANNEL_ID}>."
        )

        await notification_channel.send(
            content=message_content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

        await ctx.send(
            f"✅ Test notification sent to "
            f"{notification_channel.mention}!\n"
            f"📹 Video: **{title}**\n"
            f"🆔 Video ID: `{video_id}`\n"
            "The automatic notifier's saved state was not changed."
        )

    except (aiohttp.ClientError, ET.ParseError) as error:
        await ctx.send(f"❌ YouTube feed error: `{error}`")
    except discord.Forbidden:
        await ctx.send(
            "❌ Discord denied permission to send the test notification. "
            "Check the bot's channel permissions."
        )
    except discord.HTTPException as error:
        await ctx.send(f"❌ Discord couldn't send the test notification: {error}")
    except Exception as error:
        logging.exception("YouTube manual test failed")
        await ctx.send(f"❌ The YouTube test failed: `{error}`")

# ========================= START BOT =========================

install_role_manager(bot)

if __name__ == "__main__":
    bot.run(
        token, log_handler=handler, log_level=logging.DEBUG)