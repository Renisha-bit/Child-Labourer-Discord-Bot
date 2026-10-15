"""Reaction-role & Interactive Button panels with Discord admin dashboard & Slash Commands.

Integrate into the existing bot with:
    from role_manager import ROLE_CHANNEL_ID, install_role_manager
    install_role_manager(bot)

How it works:
  * Every category is ONE message posted by the bot in the designated role channel.
  * Roles are presented with a rich Embed, Interactive UI Buttons, and Reaction Emojis.
  * Adding a role to an existing category EDITS that message directly (adding the button and reaction).
    Nothing is reposted, so existing reactions and messages stay in place.
  * Adding a new category sends a brand new message to the designated channel.
  * Users can click the interactive button OR react with the emoji to toggle roles.
  * Fully persistent across bot restarts using discord.py's DynamicItem[Button].
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, DynamicItem, Item, View

# Child of the "discord" logger so messages end up in discord.log too.
log = logging.getLogger("discord.role_manager")

DB_PATH = Path(__file__).with_name("role_manager.sqlite3")
ROLE_CHANNEL_ID = 1550216770156232834
DEFAULT_COLOR = 0xC5B3D3
MAX_REACTIONS = 20  # Discord limit of different reactions per message
MAX_BUTTONS = 25    # Discord limit of buttons per message (5 rows of 5)

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,      # Blurple
    "secondary": discord.ButtonStyle.secondary,  # Grey
    "success": discord.ButtonStyle.success,      # Green
    "danger": discord.ButtonStyle.danger,        # Red
}

INITIAL_CATEGORIES = {
    "music": {
        "title": "🎵 MUSIC ROLES",
        "color": DEFAULT_COLOR,
        "roles": {
            "🎧": "♩ Listener",
            "🎸": "♫ Musician",
            "🎛️": "𝄞 Producer",
            "🎤": "♬ Vocalist",
            "✍️": "♯ Songwriter",
            "📀": "♭ DJ",
        },
    },
    "notifications": {
        "title": "🔔 NOTIFICATION ROLES",
        "color": DEFAULT_COLOR,
        "roles": {"🔔": "Upload Ping", "📢": "VC Ping"},
    },
    "games": {
        "title": "🎮 GAME ROLES",
        "color": DEFAULT_COLOR,
        "roles": {"⚔️": "Valorant", "⛏️": "Minecraft"},
    },
    "colors": {
        "title": "🎨 COLORS",
        "color": DEFAULT_COLOR,
        "roles": {
            "🔴": "Red", "🟠": "Orange", "🟡": "Yellow", "🟢": "Green",
            "🔵": "Blue", "🟣": "Purple", "🩷": "Pink", "⚪": "White", "⚫": "Black",
        },
    },
}


# ========================= DATABASE =========================

@contextmanager
def _db():
    """Open a connection, commit on success / roll back on error, and always close it."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _init_db() -> None:
    with _db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS categories (
            key TEXT PRIMARY KEY, title TEXT NOT NULL, color INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '', footer TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '', thumbnail_url TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS role_mappings (
            category_key TEXT NOT NULL, emoji TEXT NOT NULL, role_name TEXT NOT NULL,
            role_id INTEGER, style TEXT NOT NULL DEFAULT 'primary',
            PRIMARY KEY (category_key, emoji),
            FOREIGN KEY(category_key) REFERENCES categories(key) ON DELETE CASCADE
        )""")
        panel_table = """CREATE TABLE IF NOT EXISTS role_panels (
            category_key TEXT PRIMARY KEY, channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            FOREIGN KEY(category_key) REFERENCES categories(key) ON DELETE CASCADE
        )"""

        # Upgrade from legacy versions if needed
        columns = [row["name"] for row in db.execute("PRAGMA table_info(role_panels)")]
        if "webhook_id" in columns:
            db.execute("ALTER TABLE role_panels RENAME TO role_panels_old")
            db.execute(panel_table)
            db.execute(
                "INSERT INTO role_panels(category_key, channel_id, message_id) "
                "SELECT category_key, channel_id, message_id FROM role_panels_old"
            )
            db.execute("DROP TABLE role_panels_old")
        else:
            db.execute(panel_table)

        # Ensure 'style' column exists in role_mappings
        role_cols = [row["name"] for row in db.execute("PRAGMA table_info(role_mappings)")]
        if "style" not in role_cols:
            db.execute("ALTER TABLE role_mappings ADD COLUMN style TEXT NOT NULL DEFAULT 'primary'")

        count = db.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"]
        if count == 0:
            for key, category in INITIAL_CATEGORIES.items():
                db.execute(
                    "INSERT INTO categories(key, title, color) VALUES (?, ?, ?)",
                    (key, category["title"], category["color"]),
                )
                for emoji, role_name in category["roles"].items():
                    db.execute(
                        "INSERT INTO role_mappings(category_key, emoji, role_name, style) VALUES (?, ?, ?, 'primary')",
                        (key, emoji, role_name),
                    )


def _category(key: str):
    with _db() as db:
        return db.execute("SELECT * FROM categories WHERE key = ?", (key.lower().strip(),)).fetchone()


def _roles(category_key: str):
    with _db() as db:
        return db.execute(
            "SELECT emoji, role_name, role_id, style FROM role_mappings WHERE category_key = ? ORDER BY rowid",
            (category_key,),
        ).fetchall()


def _all_categories():
    with _db() as db:
        return db.execute("SELECT * FROM categories ORDER BY rowid").fetchall()


def _panel(category_key: str):
    with _db() as db:
        return db.execute("SELECT * FROM role_panels WHERE category_key = ?", (category_key,)).fetchone()


def _save_panel(category_key: str, channel_id: int, message_id: int) -> None:
    with _db() as db:
        db.execute(
            "INSERT INTO role_panels(category_key, channel_id, message_id) VALUES (?, ?, ?) "
            "ON CONFLICT(category_key) DO UPDATE SET channel_id=excluded.channel_id, "
            "message_id=excluded.message_id",
            (category_key, channel_id, message_id),
        )

def _resolve_and_save_role_ids(guild: Optional[discord.Guild], category_key: str) -> None:
    """Finds matching server roles by name/id and caches role_id in SQLite."""
    if guild is None:
        return
    rows = _roles(category_key)
    with _db() as db:
        for row in rows:
            role = None
            if row["role_id"]:
                role = guild.get_role(int(row["role_id"]))
            if not role:
                role = discord.utils.find(lambda r: r.name.casefold() == row["role_name"].casefold(), guild.roles)
            if role and role.id != row["role_id"]:
                db.execute(
                    "UPDATE role_mappings SET role_id = ? WHERE category_key = ? AND emoji = ?",
                    (role.id, category_key, row["emoji"]),
                )

# ========================= HELPERS =========================

def _norm_emoji(value: str) -> str:
    """Ignore variation selectors so emoji comparisons match cleanly."""
    return value.replace("\ufe0f", "").strip()


def _find_mapping(category_key: str, emoji: str):
    wanted = _norm_emoji(emoji)
    for row in _roles(category_key):
        if _norm_emoji(row["emoji"]) == wanted:
            return row
    return None


def parse_emoji(emoji_str: Optional[str]) -> Optional[Union[str, discord.PartialEmoji]]:
    """Parses unicode or custom Discord emojis."""
    if not emoji_str:
        return None
    clean = emoji_str.strip()
    if clean.startswith("<") and clean.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(clean)
        except Exception:
            return clean
    return clean


def _parse_color(color_hex: str) -> Optional[int]:
    raw = color_hex.strip().lstrip("#")
    if len(raw) not in (3, 6):
        return None
    try:
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        return int(raw, 16)
    except ValueError:
        return None


def _find_guild_role(guild: Optional[discord.Guild], query: str) -> Optional[discord.Role]:
    """Finds role by mention (<@&id>), numeric ID, or case-insensitive name."""
    if guild is None:
        return None
    clean = query.strip()
    if clean.startswith("<@&") and clean.endswith(">"):
        try:
            return guild.get_role(int(clean[3:-1]))
        except ValueError:
            pass
    if clean.isdigit():
        role = guild.get_role(int(clean))
        if role:
            return role
    for role in guild.roles:
        if role.name.casefold() == clean.casefold():
            return role
    return None

def _role_description(category_key: str) -> str:
    rows = _roles(category_key)
    if not rows:
        return "No roles have been added to this category yet."
    return "\n".join(f"{row['emoji']} — **{row['role_name']}**" for row in rows)


def _make_embed(category_key: str) -> discord.Embed:
    category = _category(category_key)
    embed = discord.Embed(
        title=category["title"],
        description=_role_description(category_key),
        color=int(category["color"]),
    )
    if category["footer"]:
        embed.set_footer(text=category["footer"])
    else:
        embed.set_footer(text="Click a button below or react with an emoji to toggle roles.")
    if category["image_url"]:
        embed.set_image(url=category["image_url"])
    if category["thumbnail_url"]:
        embed.set_thumbnail(url=category["thumbnail_url"])
    return embed


def _make_view(category_key: str, guild: Optional[discord.Guild] = None) -> View:
    """Builds an interactive View with persistent buttons for each role."""
    view = View(timeout=None)
    rows = _roles(category_key)
    for row in rows[:MAX_BUTTONS]:
        role = None
        if guild:
            if row["role_id"]:
                role = guild.get_role(int(row["role_id"]))
            if not role:
                role = discord.utils.find(lambda r: r.name.casefold() == row["role_name"].casefold(), guild.roles)

        role_id = role.id if role else (row["role_id"] or 0)
        label = row["role_name"][:80]
        emoji = parse_emoji(row["emoji"])
        style = BUTTON_STYLES.get(row["style"] if "style" in row.keys() else "primary", discord.ButtonStyle.primary)

        button = RoleToggleButton(
            role_id=role_id,
            label=label,
            style=style,
            emoji=emoji,
        )
        view.add_item(button)
    return view


async def _get_channel(bot: commands.Bot, channel_id: int = ROLE_CHANNEL_ID) -> Optional[discord.TextChannel]:
    """Return the text channel, or None if it can't be reached or isn't a text channel."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


_NO_CHANNEL = "I couldn't access the role panel channel. Check ROLE_CHANNEL_ID and my permissions."
_NO_PERMS = "Missing permission. In the role channel I need: View Channel, Send Messages, Embed Links, Add Reactions and Read Message History."


# ========================= PERSISTENT DYNAMIC BUTTON =========================

class RoleToggleButton(DynamicItem[Button], template=r"role_toggle:(?P<role_id>[0-9]+)"):
    """
    Persistent button that responds to role clicks.
    Remains functional across bot restarts automatically without polling or manual reloading.
    """

    def __init__(
        self,
        role_id: int,
        label: str = "Role",
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        emoji: Optional[Union[str, discord.PartialEmoji]] = None,
        row: Optional[int] = None,
    ):
        super().__init__(
            Button(
                label=label[:80],
                style=style,
                emoji=emoji,
                custom_id=f"role_toggle:{role_id}",
                row=row,
            )
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: Item[Any], match: re.Match[str], /
    ):
        role_id = int(match.group("role_id"))
        btn_label = getattr(item, "label", None) or "Role"
        btn_style = getattr(item, "style", discord.ButtonStyle.primary)
        btn_emoji = getattr(item, "emoji", None)
        return cls(
            role_id=role_id,
            label=btn_label,
            style=btn_style,
            emoji=btn_emoji,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            await interaction.followup.send(
                "❌ This button can only be used inside a Discord server.", ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user
        if not isinstance(member, discord.Member):
            try:
                member = await guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                await interaction.followup.send(
                    "❌ Could not fetch your member information.", ephemeral=True
                )
                return

        role = guild.get_role(self.role_id)
        if not role:
            await interaction.followup.send(
                "❌ This role no longer exists in the server. Please inform an administrator.",
                ephemeral=True,
            )
            return

        bot_member = guild.me
        if not bot_member.guild_permissions.manage_roles:
            await interaction.followup.send(
                "❌ I lack the **Manage Roles** permission to modify your roles.",
                ephemeral=True,
            )
            return

        if role >= bot_member.top_role:
            await interaction.followup.send(
                f"❌ I cannot assign or remove {role.mention} because it is positioned higher than (or equal to) my highest role ({bot_member.top_role.mention}) in Server Settings > Roles.",
                ephemeral=True,
            )
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Role panel button toggle")
                await interaction.followup.send(
                    f"➖ Removed {role.mention} from your roles.",
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ Permission denied: Unable to remove {role.mention}.",
                    ephemeral=True,
                )
            except Exception as e:
                log.error("Error removing role %s: %s", role.id, e)
                await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)
        else:
            try:
                await member.add_roles(role, reason="Role panel button toggle")
                await interaction.followup.send(
                    f"➕ Added {role.mention} to your roles!",
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ Permission denied: Unable to assign {role.mention}.",
                    ephemeral=True,
                )
            except Exception as e:
                log.error("Error adding role %s: %s", role.id, e)
                await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)


# ========================= PANEL MESSAGES =========================

async def _panel_state(bot: commands.Bot, key: str):
    """Inspects a category's saved panel message."""
    panel = _panel(key)
    if panel is None:
        return "none", None
    channel = await _get_channel(bot, int(panel["channel_id"]))
    if channel is None:
        return "missing", None
    try:
        message = await channel.fetch_message(int(panel["message_id"]))
    except discord.NotFound:
        return "missing", None
    if bot.user is None or message.author.id != bot.user.id:
        return "foreign", message
    return "ok", message


async def _find_panels(bot: commands.Bot, channel: discord.TextChannel):
    """Discover panels already in the channel by embed title."""
    title_to_key = {row["title"]: row["key"] for row in _all_categories()}
    bot_id = bot.user.id if bot.user else 0
    found: dict = {}
    old: dict = {}
    notes: list = []
    async for message in channel.history(limit=1000, oldest_first=False):
        if not message.embeds:
            continue
        key = title_to_key.get(message.embeds[0].title)
        if key is None:
            continue
        if message.author.id == bot_id:
            if key in found:
                notes.append(f"{key}: found another copy (message `{message.id}`); left untouched")
            else:
                found[key] = message
        else:
            old.setdefault(key, message)
    return found, old, notes


async def _post_panel(channel: discord.TextChannel, key: str) -> discord.Message:
    """Post a new panel message with embed, interactive buttons, and reaction emojis."""
    guild = channel.guild
    _resolve_and_save_role_ids(guild, key)

    embed = _make_embed(key)
    view = _make_view(key, guild)

    message = await channel.send(embed=embed, view=view)
    _save_panel(key, channel.id, message.id)

    # Also add reaction emojis for users who prefer reactions
    for row in _roles(key):
        try:
            emoji = parse_emoji(row["emoji"])
            if emoji:
                await message.add_reaction(emoji)
                await asyncio.sleep(0.3)
        except Exception:
            pass
    return message


async def _ensure_panel(bot: commands.Bot, key: str, scan: Optional[dict] = None) -> tuple[str, str]:
    """Ensures a category has a working panel message."""
    panel = _panel(key)
    channel_id = int(panel["channel_id"]) if panel else ROLE_CHANNEL_ID
    channel = await _get_channel(bot, channel_id)
    if channel is None:
        return "error", _NO_CHANNEL

    try:
        state, _message = await _panel_state(bot, key)
        if state == "ok":
            return "ok", ""
        if scan is None:
            scan = {}
        if "result" not in scan:
            scan["result"] = await _find_panels(bot, channel)
        found, old, _notes = scan["result"]
        if key in found:
            _save_panel(key, channel.id, found[key].id)
            return "linked", f"linked existing panel (message `{found[key].id}`)"
        message = await _post_panel(channel, key)
        if key in old:
            return "created", (
                f"posted a new panel (message `{message.id}`). The older panel {old[key].jump_url} "
                "was an old message and can be deleted."
            )
        return "created", f"posted a new panel (message `{message.id}`)"
    except discord.Forbidden:
        return "error", _NO_PERMS
    except discord.HTTPException as exc:
        return "error", f"Discord error: {exc}"


async def _update_existing_panel(bot: commands.Bot, key: str) -> tuple[bool, str]:
    """Updates the existing panel message's embed and interactive buttons."""
    try:
        state, message = await _panel_state(bot, key)
        if state != "ok" or message is None:
            return False, "I couldn't find my panel message for that category. Run `.setup_roles`."

        guild = message.guild
        _resolve_and_save_role_ids(guild, key)

        if message.embeds:
            embed = discord.Embed.from_dict(message.embeds[0].to_dict())
            embed.description = _role_description(key)
            if not embed.footer.text:
                embed.set_footer(text="Click a button below or react with an emoji to toggle roles.")
        else:
            embed = _make_embed(key)

        view = _make_view(key, guild)
        await message.edit(embed=embed, view=view)
        return True, "Updated the panel message."
    except discord.Forbidden:
        return False, _NO_PERMS
    except discord.HTTPException as exc:
        return False, f"Discord couldn't update the panel: {exc}"


async def _sync_panel(bot: commands.Bot, key: str) -> tuple[bool, str, bool]:
    """Ensure the panel exists, then update embed and buttons to match the database."""
    status, detail = await _ensure_panel(bot, key)
    if status == "error":
        return False, detail, False
    if status == "created":
        return True, f"The panel was missing, so I {detail}.", True
    ok, message = await _update_existing_panel(bot, key)
    if ok and status == "linked":
        message = f"I {detail} first. {message}"
    return ok, message, False


async def _setup_panels(bot: commands.Bot) -> str:
    """Safe, repeatable: posts missing panels and links existing ones."""
    scan: dict = {}
    working, linked, created, problems = [], [], [], []
    for category in _all_categories():
        key = category["key"]
        status, detail = await _ensure_panel(bot, key, scan)
        if status == "error" and detail == _NO_CHANNEL:
            return detail
        if status == "ok":
            # Refresh buttons and embed on working panels too
            await _update_existing_panel(bot, key)
            working.append(key)
        elif status == "linked":
            await _update_existing_panel(bot, key)
            linked.append(key)
        elif status == "created":
            created.append(f"{key}: {detail}")
        else:
            problems.append(f"{key}: {detail}")
    lines = [
        f"**Already working:** {', '.join(working) or 'none'}",
        f"**Linked existing panels:** {', '.join(linked) or 'none'}",
        f"**Created new panels:** {'; '.join(created) or 'none'}",
    ]
    if problems:
        lines += ["**Problems:**", *problems]
    return "\n".join(lines)


# ========================= ROLE / CATEGORY EDITING =========================

async def _add_role(
    bot: commands.Bot,
    guild: Optional[discord.Guild],
    category_key: str,
    emoji: str,
    role_query: str,
    style: str = "primary",
) -> str:
    """Add a role to a category: edits the existing message to add button and reaction."""
    if guild is None:
        return "❌ This action can only be performed inside a server."
    category_key = category_key.lower().strip()
    if _category(category_key) is None:
        return f"Unknown category `{category_key}`. Use `.rolecategories` to view available categories."
    emoji = emoji.strip()
    if not emoji:
        return "Please provide an emoji."

    role = _find_guild_role(guild, role_query)
    if role is None:
        return f"I couldn't find a server role named `{role_query}`. Create that role in Discord first."
    if role.is_default() or role.managed:
        return f"`{role.name}` can't be assigned (it's @everyone or managed by an integration)."
    if role >= guild.me.top_role:
        return f"`{role.name}` is above or equal to my highest role ({guild.me.top_role.name}), so I couldn't assign it. Move my role higher in Server Settings > Roles first."
    if _find_mapping(category_key, emoji) is not None:
        return f"Emoji {emoji} is already used in `{category_key}`. Remove or change that mapping first."
    if len(_roles(category_key)) >= MAX_BUTTONS:
        return f"Discord allows at most {MAX_BUTTONS} buttons on one message. Please create another category instead."

    def undo() -> None:
        with _db() as db:
            db.execute("DELETE FROM role_mappings WHERE category_key = ? AND emoji = ?", (category_key, emoji))

    with _db() as db:
        db.execute(
            "INSERT INTO role_mappings(category_key, emoji, role_name, role_id, style) VALUES (?, ?, ?, ?, ?)",
            (category_key, emoji, role.name, role.id, style),
        )

    # 1) Edit the panel message to add the new button and update the embed
    ok, message, rebuilt = await _sync_panel(bot, category_key)
    if not ok:
        undo()
        return message

    if rebuilt:
        return f"Added {emoji} → **{role.name}** to `{category_key}`. {message}"

    # 2) Add the reaction emoji to the message as well
    try:
        _state, panel_message = await _panel_state(bot, category_key)
        if panel_message is not None:
            parsed = parse_emoji(emoji)
            if parsed:
                await panel_message.add_reaction(parsed)
    except Exception as exc:
        log.warning("Could not add reaction emoji %s: %s", emoji, exc)

    return f"Added {emoji} → **{role.name}** to `{category_key}`! I edited the panel message with the new button and reaction."


async def _remove_role(bot: commands.Bot, category_key: str, emoji_or_role: str) -> str:
    """Remove a role from a category: edits the panel message and clears the reaction."""
    category_key = category_key.lower().strip()
    if _category(category_key) is None:
        return f"Unknown category `{category_key}`. Use `.rolecategories` to see categories."

    # Search by emoji first, then by role name
    old = _find_mapping(category_key, emoji_or_role)
    if old is None:
        clean = emoji_or_role.strip().casefold()
        for row in _roles(category_key):
            if row["role_name"].casefold() == clean:
                old = row
                break

    if old is None:
        return f"No mapping for `{emoji_or_role}` exists in `{category_key}`."

    with _db() as db:
        db.execute("DELETE FROM role_mappings WHERE category_key = ? AND emoji = ?", (category_key, old["emoji"]))

    ok, message, rebuilt = await _sync_panel(bot, category_key)
    if not ok:
        with _db() as db:
            db.execute(
                "INSERT INTO role_mappings(category_key, emoji, role_name, role_id, style) VALUES (?, ?, ?, ?, ?)",
                (category_key, old["emoji"], old["role_name"], old["role_id"], old.get("style", "primary")),
            )
        return message

    if rebuilt:
        return f"Removed {old['emoji']} ({old['role_name']}) from `{category_key}`. {message}"

    try:
        _state, panel_message = await _panel_state(bot, category_key)
        if panel_message is not None:
            parsed = parse_emoji(old["emoji"])
            if parsed is not None:
                try:
                    await panel_message.clear_reaction(parsed)
                except discord.Forbidden:
                    if bot.user is not None:
                        await panel_message.remove_reaction(parsed, bot.user)
                except Exception:
                    pass
    except Exception:
        pass

    return f"Removed {old['emoji']} ({old['role_name']}) from `{category_key}` and updated the panel buttons."


async def _edit_panel_appearance(
    bot: commands.Bot, category_key: str, title: str, color_hex: str, footer: str, image_url: str, thumbnail_url: str
) -> str:
    category_key = category_key.lower().strip()
    category = _category(category_key)
    if category is None:
        return f"Unknown category `{category_key}`. Use `.rolecategories` to see categories."
    color = _parse_color(color_hex)
    if color is None:
        return "Enter a valid hex color, for example `#C5B3D3`."
    image_url, thumbnail_url, footer = image_url.strip(), thumbnail_url.strip(), footer.strip()
    for label, url in (("image", image_url), ("thumbnail", thumbnail_url)):
        if url and not url.lower().startswith(("https://", "http://")):
            return f"The {label} must be an `https://` or `http://` URL, or left blank."

    status, detail = await _ensure_panel(bot, category_key)
    if status == "error":
        return detail
    note = f"(The panel was missing, so I {detail}.) " if status in ("linked", "created") else ""
    try:
        _state, message = await _panel_state(bot, category_key)
        if message is None:
            return "I couldn't find my panel message for that category. Run `.setup_roles`."

        guild = message.guild
        embed = _make_embed(category_key)
        embed.title = title.strip()[:256] or category["title"]
        embed.color = discord.Color(color)
        if footer:
            embed.set_footer(text=footer[:2048])
        else:
            embed.set_footer(text="Click a button below or react with an emoji to toggle roles.")
        embed.set_image(url=image_url or None)
        embed.set_thumbnail(url=thumbnail_url or None)

        view = _make_view(category_key, guild)
        await message.edit(embed=embed, view=view)

        with _db() as db:
            db.execute(
                "UPDATE categories SET title = ?, color = ?, footer = ?, image_url = ?, thumbnail_url = ? WHERE key = ?",
                (embed.title, color, footer[:2048], image_url, thumbnail_url, category_key),
            )
        return f"{note}Updated the `{category_key}` panel's appearance."
    except discord.Forbidden:
        return _NO_PERMS
    except discord.HTTPException as exc:
        return f"Discord couldn't update the panel: {exc}"


async def _add_category(
    bot: commands.Bot,
    key: str,
    title: str,
    color_hex: str,
    channel_id: Optional[int] = None,
) -> str:
    """Create a new category and send a brand new message."""
    key = key.lower().strip()
    if not key or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in key):
        return "Category ID must use lowercase letters, numbers, hyphens, or underscores."
    if _category(key):
        return f"Category `{key}` already exists."
    color = _parse_color(color_hex)
    if color is None:
        return "Enter a valid hex color, for example `#C5B3D3`."

    target_channel_id = channel_id or ROLE_CHANNEL_ID
    channel = await _get_channel(bot, target_channel_id)
    if channel is None:
        return _NO_CHANNEL

    title = title.strip()[:256] or f"{key.upper()} ROLES"
    with _db() as db:
        db.execute("INSERT INTO categories(key, title, color) VALUES (?, ?, ?)", (key, title, color))
    try:
        message = await _post_panel(channel, key)
    except (discord.Forbidden, discord.HTTPException) as exc:
        if _panel(key) is None:
            with _db() as db:
                db.execute("DELETE FROM categories WHERE key = ?", (key,))
        return f"Couldn't post the category panel: {exc}. {_NO_PERMS}"

    return (
        f"✅ Created category `{key}` and sent a new message in {channel.mention}!\n"
        f"[Jump to Message]({message.jump_url})\n"
        f"Add roles to it with `.roleadd {key} 🎮 RoleName` or `/role-category add-role`."
    )


async def _delete_category(bot: commands.Bot, category_key: str, delete_message: bool = True) -> str:
    """Deletes a category and optionally removes the Discord message."""
    key = category_key.lower().strip()
    category = _category(key)
    if not category:
        return f"Category `{key}` not found."

    panel = _panel(key)
    if delete_message and panel:
        channel = await _get_channel(bot, int(panel["channel_id"]))
        if channel:
            try:
                msg = await channel.fetch_message(int(panel["message_id"]))
                await msg.delete()
            except Exception:
                pass

    with _db() as db:
        db.execute("DELETE FROM categories WHERE key = ?", (key,))

    return f"🗑️ Category `{key}` has been deleted from the database."


# ========================= DASHBOARD UI =========================

class _SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Role dashboard modal failed", exc_info=error)
        text = "Something went wrong while doing that. Check the bot log for details."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class AddRoleModal(_SafeModal, title="Add a role"):
    category_key = discord.ui.TextInput(label="Category ID", placeholder="games", max_length=40)
    emoji = discord.ui.TextInput(label="Reaction/Button emoji", placeholder="🎮", max_length=64)
    role_name = discord.ui.TextInput(label="Exact server role name or @Role", placeholder="Valorant", max_length=100)

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this inside your server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _add_role(
            self.bot, interaction.guild, str(self.category_key), str(self.emoji), str(self.role_name)
        )
        await interaction.followup.send(result, ephemeral=True)


class RemoveRoleModal(_SafeModal, title="Remove a role mapping"):
    category_key = discord.ui.TextInput(label="Category ID", placeholder="games", max_length=40)
    emoji_or_role = discord.ui.TextInput(label="Emoji or Role Name to remove", placeholder="🎮 or Valorant", max_length=100)

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _remove_role(self.bot, str(self.category_key), str(self.emoji_or_role))
        await interaction.followup.send(result, ephemeral=True)


class AddCategoryModal(_SafeModal, title="Create a role category"):
    key = discord.ui.TextInput(label="Category ID (no spaces)", placeholder="art", max_length=40)
    title_text = discord.ui.TextInput(label="Panel title", placeholder="🎨 ART ROLES", max_length=256)
    color = discord.ui.TextInput(label="Embed color (hex)", placeholder="#C5B3D3", default="#C5B3D3", max_length=7)

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _add_category(self.bot, str(self.key), str(self.title_text), str(self.color))
        await interaction.followup.send(result, ephemeral=True)


class PanelAppearanceModal(_SafeModal, title="Customize a role panel"):
    category_key = discord.ui.TextInput(label="Category ID", placeholder="games", max_length=40)
    title_text = discord.ui.TextInput(label="Embed title", placeholder="🎮 GAME ROLES", max_length=256)
    color = discord.ui.TextInput(label="Embed color (hex)", placeholder="#C5B3D3", default="#C5B3D3", max_length=7)
    footer = discord.ui.TextInput(label="Footer (optional)", placeholder="Leave blank for default", required=False, max_length=2048)
    image_url = discord.ui.TextInput(label="Image URL (optional)", placeholder="https://...", required=False, max_length=400)

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        existing = _category(str(self.category_key))
        thumbnail_url = existing["thumbnail_url"] if existing else ""
        result = await _edit_panel_appearance(
            self.bot, str(self.category_key), str(self.title_text), str(self.color),
            str(self.footer), str(self.image_url), thumbnail_url
        )
        await interaction.followup.send(result, ephemeral=True)


class RoleDashboardView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not (perms.manage_roles or perms.manage_guild or perms.administrator):
            await interaction.response.send_message("You need Manage Roles permission to use this dashboard.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add role", style=discord.ButtonStyle.success, emoji="➕")
    async def add_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddRoleModal(self.bot))

    @discord.ui.button(label="Remove role", style=discord.ButtonStyle.danger, emoji="➖")
    async def remove_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RemoveRoleModal(self.bot))

    @discord.ui.button(label="Add category", style=discord.ButtonStyle.primary, emoji="🗂️")
    async def add_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCategoryModal(self.bot))

    @discord.ui.button(label="Sync panels", style=discord.ButtonStyle.secondary, emoji="🔎")
    async def sync_panels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _setup_panels(self.bot)
        await interaction.followup.send(f"{result}\n\nCommand: `.setup_roles`"[:1900], ephemeral=True)

    @discord.ui.button(label="Customize panel", style=discord.ButtonStyle.primary, emoji="🎨")
    async def customize_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PanelAppearanceModal(self.bot))

    @discord.ui.button(label="Show categories", style=discord.ButtonStyle.secondary, emoji="📋")
    async def show_categories(self, interaction: discord.Interaction, button: discord.ui.Button):
        lines = []
        for row in _all_categories():
            status = "has panel" if _panel(row["key"]) else "no panel yet — press Sync panels"
            lines.append(f"**{row['title']}** · ID: `{row['key']}` · {len(_roles(row['key']))} roles · {status}")
        embed = discord.Embed(title="Role categories", description="\n".join(lines) or "No categories yet.", color=DEFAULT_COLOR)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ========================= SLASH COMMAND GROUP =========================

def _create_slash_commands(bot: commands.Bot) -> app_commands.Group:
    role_group = app_commands.Group(
        name="role-category",
        description="Create and manage interactive role category menus with buttons",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    async def category_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        current_lower = current.strip().lower()
        return [
            app_commands.Choice(name=f"{row['key']} ({row['title']})", value=row["key"])
            for row in _all_categories()
            if current_lower in row["key"].lower() or current_lower in row["title"].lower()
        ][:25]

    @role_group.command(name="create", description="Create a new category and send a new message")
    @app_commands.describe(
        name="Unique identifier (e.g., 'gaming', 'notifications', 'colors')",
        title="Title shown at top of the embed",
        channel="Optional channel to post in (defaults to configured role channel)",
        color="Optional embed color hex (e.g. #C5B3D3 or #5865F2)",
    )
    async def slash_create(
        interaction: discord.Interaction,
        name: str,
        title: str,
        channel: Optional[discord.TextChannel] = None,
        color: Optional[str] = "#C5B3D3",
    ):
        await interaction.response.defer(ephemeral=True)
        channel_id = channel.id if channel else ROLE_CHANNEL_ID
        result = await _add_category(bot, name, title, color or "#C5B3D3", channel_id=channel_id)
        await interaction.followup.send(result, ephemeral=True)

    @role_group.command(name="add-role", description="Add a role button to an existing category message")
    @app_commands.autocomplete(category=category_autocomplete)
    @app_commands.describe(
        category="The category to add this role to",
        role="The server role to assign when clicked",
        emoji="Emoji to display on the button and add as reaction (e.g. 🎮 or custom <:name:id>)",
        button_color="Button style color",
    )
    @app_commands.choices(
        button_color=[
            app_commands.Choice(name="Blurple (Primary)", value="primary"),
            app_commands.Choice(name="Grey (Secondary)", value="secondary"),
            app_commands.Choice(name="Green (Success)", value="success"),
            app_commands.Choice(name="Red (Danger)", value="danger"),
        ]
    )
    async def slash_add_role(
        interaction: discord.Interaction,
        category: str,
        role: discord.Role,
        emoji: str,
        button_color: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("❌ This command can only be used inside a server.", ephemeral=True)
            return
        style = button_color.value if button_color else "primary"
        result = await _add_role(bot, interaction.guild, category, emoji, role.name, style=style)
        await interaction.followup.send(result, ephemeral=True)

    @role_group.command(name="remove-role", description="Remove a role button from an existing category message")
    @app_commands.autocomplete(category=category_autocomplete)
    @app_commands.describe(
        category="The category to remove the role from",
        role="The role to remove",
    )
    async def slash_remove_role(
        interaction: discord.Interaction,
        category: str,
        role: discord.Role,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await _remove_role(bot, category, role.name)
        await interaction.followup.send(result, ephemeral=True)

    @role_group.command(name="list", description="List all configured role categories and panel links")
    async def slash_list(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        categories = _all_categories()
        if not categories:
            await interaction.followup.send("No categories configured yet.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Configured Role Categories", color=DEFAULT_COLOR)
        for cat in categories:
            panel = _panel(cat["key"])
            roles = _roles(cat["key"])
            roles_text = ", ".join([f"{r['emoji']} {r['role_name']}" for r in roles]) if roles else "*None*"
            msg_link = f"https://discord.com/channels/{interaction.guild_id}/{panel['channel_id']}/{panel['message_id']}" if panel else "*No panel yet*"
            embed.add_field(
                name=f"{cat['title']} (`{cat['key']}`)",
                value=f"• **Roles ({len(roles)}):** {roles_text}\n• **Panel:** {msg_link}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @role_group.command(name="resend", description="Re-post a category message to a channel")
    @app_commands.autocomplete(category=category_autocomplete)
    @app_commands.describe(
        category="The category to re-post",
        channel="The channel to post the message into",
    )
    async def slash_resend(
        interaction: discord.Interaction,
        category: str,
        channel: discord.TextChannel,
    ):
        await interaction.response.defer(ephemeral=True)
        clean_cat = category.strip().lower()
        if not _category(clean_cat):
            await interaction.followup.send(f"❌ Category `{category}` not found.", ephemeral=True)
            return
        try:
            msg = await _post_panel(channel, clean_cat)
            await interaction.followup.send(
                f"✅ Re-posted `{clean_cat}` in {channel.mention}!\n[Jump to Message]({msg.jump_url})",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Could not post message: {e}", ephemeral=True)

    @role_group.command(name="delete", description="Delete a category from the database")
    @app_commands.autocomplete(category=category_autocomplete)
    @app_commands.describe(
        category="Category to delete",
        delete_message="Whether to delete the Discord message",
    )
    async def slash_delete(
        interaction: discord.Interaction,
        category: str,
        delete_message: bool = True,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await _delete_category(bot, category, delete_message=delete_message)
        await interaction.followup.send(result, ephemeral=True)

    return role_group


# ========================= REACTION HANDLING =========================

async def _resolve_reaction(bot: commands.Bot, payload: discord.RawReactionActionEvent):
    """Turn a raw reaction event into (member, role), or None to ignore it."""
    if payload.guild_id is None or (bot.user and payload.user_id == bot.user.id):
        return None
    with _db() as db:
        panel = db.execute("SELECT category_key FROM role_panels WHERE message_id = ?", (payload.message_id,)).fetchone()
    if panel is None:
        return None
    mapping = _find_mapping(panel["category_key"], str(payload.emoji))
    if mapping is None:
        return None
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return None
    role = guild.get_role(int(mapping["role_id"])) if mapping["role_id"] else None
    if role is None:
        role = discord.utils.find(lambda r: r.name.casefold() == mapping["role_name"].casefold(), guild.roles)
    if role is None:
        log.warning("Role not found for %s: %s", mapping["emoji"], mapping["role_name"])
        return None
    if role.is_default() or role.managed or role >= guild.me.top_role:
        log.warning("Can't manage role %s (default, managed, or above top role)", role.name)
        return None
    member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    if member.bot:
        return None
    return member, role


# ========================= MAIN INSTALLER =========================

def install_role_manager(bot: commands.Bot) -> None:
    """Register role commands, dynamic persistent buttons, and reaction handlers."""
    _init_db()

    # Register the dynamic item template for interactive buttons
    bot.add_dynamic_items(RoleToggleButton)

    # Register slash command group
    role_group = _create_slash_commands(bot)
    try:
        bot.tree.add_command(role_group)
    except Exception as exc:
        log.warning("Could not add slash command group to tree: %s", exc)

    # Auto-sync slash commands on ready
    @bot.listen("on_ready")
    async def _on_role_manager_ready():
        try:
            synced = await bot.tree.sync()
            log.info("Role manager synced %d application commands.", len(synced))
        except Exception as exc:
            log.warning("Could not sync slash commands: %s", exc)

    # --- Prefix Commands ---

    @bot.command(name="roles", help="Open the interactive role-management dashboard.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def roles_dashboard(ctx: commands.Context):
        embed = discord.Embed(
            title="ChildLaborer · Role Control Centre",
            description=(
                "Manage role panels with interactive buttons and reactions.\n\n"
                "**Dashboard actions**\n"
                "➕ Add role · ➖ Remove role · 🗂️ Add category\n"
                "🎨 Customize panel · 🔎 Sync panels · 📋 Show categories\n\n"
                "**Prefix commands**\n"
                "`.setup_roles` — post or update all category panels with buttons\n"
                "`.rolecheck` — diagnose permissions and panels\n"
                "`.rolecategories` — list categories\n"
                "`.categoryadd <id> <#color> <title>` — add a new category (sends new message)\n"
                "`.roleadd <category> <emoji> <role>` — add role to category (edits existing message)\n"
                "`.roleremove <category> <emoji_or_role>` — remove role\n"
                "`.panelcolor <category> <hex>` — change panel embed color\n\n"
                "**Slash commands**\n"
                "Use `/role-category` for full autocomplete-enabled slash commands!\n"
            ),
            color=DEFAULT_COLOR,
        )
        await ctx.send(embed=embed, view=RoleDashboardView(bot))

    @bot.command(name="rolecheck", help="Diagnose the role panel setup: channel, permissions, saved panels.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def rolecheck(ctx: commands.Context):
        channel = await _get_channel(bot)
        if channel is None:
            await ctx.send(f"{_NO_CHANNEL} (ROLE_CHANNEL_ID is `{ROLE_CHANNEL_ID}`)")
            return
        lines = [f"**Role channel:** {channel.mention} (`{channel.id}`)"]
        perms = channel.permissions_for(channel.guild.me)
        needed = ("view_channel", "send_messages", "embed_links", "add_reactions", "read_message_history")
        missing = [name for name in needed if not getattr(perms, name)]
        lines.append("**Bot permissions in that channel:** " + ("all OK" if not missing else "MISSING " + ", ".join(missing)))
        if not perms.manage_messages:
            lines.append("Optional: give me **Manage Messages** so removing a role clears reactions.")
        lines.append("**Saved panels:**")
        labels = {
            "none": "no panel yet (run `.setup_roles`)",
            "missing": "the message was deleted (run `.setup_roles`)",
            "foreign": "an old message (run `.setup_roles` to re-post)",
            "ok": "OK, active and editable",
        }
        for row in _all_categories():
            try:
                state, message = await _panel_state(bot, row["key"])
                text = labels[state] + (f" - {message.jump_url}" if message else "")
            except (discord.Forbidden, discord.HTTPException) as exc:
                text = f"couldn't check ({exc})"
            lines.append(f"• `{row['key']}`: {text}")
        await ctx.send("\n".join(lines)[:1900])

    @bot.command(name="setup_roles", aliases=["migrate_role_panels"], help="Post or refresh role panels with buttons & reactions.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_roles(ctx: commands.Context):
        await ctx.send("Checking role panels. I'll refresh working ones with buttons, link existing ones, and post any that are missing…")
        await ctx.send((await _setup_panels(bot))[:1900])

    @bot.command(name="rolecategories", help="List configured role categories and their panel status.")
    @commands.guild_only()
    async def rolecategories(ctx: commands.Context):
        lines = []
        for row in _all_categories():
            panel = _panel(row["key"])
            lines.append(f"**{row['title']}** — `{row['key']}` — {len(_roles(row['key']))} roles — {'has panel' if panel else 'no panel yet'}")
        await ctx.send("\n".join(lines)[:1900] or "No categories configured.")

    @bot.command(name="categoryadd", help="Add a category: .categoryadd <id> <#color> <title>")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def categoryadd(ctx: commands.Context, key: str, color_hex: str, *, title: str):
        async with ctx.typing():
            result = await _add_category(bot, key, title, color_hex)
        await ctx.send(result)

    @bot.command(name="roleadd", help="Add a role to a category: .roleadd <category> <emoji> <exact role name or @Role>")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def roleadd(ctx: commands.Context, category_key: str, emoji: str, *, role_name: str):
        if ctx.guild is None:
            return
        async with ctx.typing():
            result = await _add_role(bot, ctx.guild, category_key, emoji, role_name)
        await ctx.send(result)

    @bot.command(name="roleremove", help="Remove a role from a category: .roleremove <category> <emoji or role name>")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def roleremove(ctx: commands.Context, category_key: str, *, emoji_or_role: str):
        async with ctx.typing():
            result = await _remove_role(bot, category_key, emoji_or_role)
        await ctx.send(result)

    @bot.command(name="panelcolor", help="Change a panel embed color: .panelcolor <category> <hex>")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def panelcolor(ctx: commands.Context, category_key: str, color_hex: str):
        category = _category(category_key)
        if category is None:
            await ctx.send(f"Unknown category `{category_key}`. Use `.rolecategories`.")
            return
        async with ctx.typing():
            result = await _edit_panel_appearance(
                bot, category_key, category["title"], color_hex, category["footer"],
                category["image_url"], category["thumbnail_url"]
            )
        await ctx.send(result)

    # --- Reaction Listeners ---

    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        result = await _resolve_reaction(bot, payload)
        if result is None:
            return
        member, role = result
        try:
            await member.add_roles(role, reason="Reaction role panel")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Couldn't give %s to %s: %s", role.name, member, exc)

    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        result = await _resolve_reaction(bot, payload)
        if result is None:
            return
        member, role = result
        try:
            await member.remove_roles(role, reason="Reaction role removed")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Couldn't remove %s from %s: %s", role.name, member, exc)

    bot.add_listener(on_raw_reaction_add, "on_raw_reaction_add")
    bot.add_listener(on_raw_reaction_remove, "on_raw_reaction_remove")

    print("Role manager loaded with Buttons, Slash Commands, and Reactions!")