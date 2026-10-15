"""Reaction-role panels (plain bot messages) and an interactive Discord admin dashboard.

Integrate into the existing bot with:
    from role_manager import install_role_manager
    install_role_manager(bot)

How it works
  * Every category is ONE normal message posted by the bot, with the roles listed in an embed.
  * Members react to the message to get a role and un-react to lose it.
  * Adding a role to a category EDITS that message and adds one reaction. Nothing is reposted,
    so the existing reactions stay.
  * Adding a category posts one new message.
  * No webhooks are used anywhere.

This module is the ONLY place that handles reaction roles. main.py must not define its own
on_raw_reaction_add / on_raw_reaction_remove handlers or panel commands.

Keep the database file on persistent hosting storage and do not commit it.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

# Child of the "discord" logger so messages end up in discord.log too.
log = logging.getLogger("discord.role_manager")

DB_PATH = Path(__file__).with_name("role_manager.sqlite3")
ROLE_CHANNEL_ID = 1550216770156232834
DEFAULT_COLOR = 0xC5B3D3
MAX_REACTIONS = 20  # Discord's limit of different reactions on one message

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
    # SQLite ignores FOREIGN KEY / ON DELETE CASCADE unless this is enabled per connection.
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
            role_id INTEGER, PRIMARY KEY (category_key, emoji),
            FOREIGN KEY(category_key) REFERENCES categories(key) ON DELETE CASCADE
        )""")
        panel_table = """CREATE TABLE IF NOT EXISTS role_panels (
            category_key TEXT PRIMARY KEY, channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            FOREIGN KEY(category_key) REFERENCES categories(key) ON DELETE CASCADE
        )"""
        # Upgrade from the old webhook version: that table had a NOT NULL webhook_id column.
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
        count = db.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"]
        # Seed only a genuinely new database. Removed roles will not reappear on restart.
        if count == 0:
            for key, category in INITIAL_CATEGORIES.items():
                db.execute(
                    "INSERT INTO categories(key, title, color) VALUES (?, ?, ?)",
                    (key, category["title"], category["color"]),
                )
                for emoji, role_name in category["roles"].items():
                    db.execute(
                        "INSERT INTO role_mappings(category_key, emoji, role_name) VALUES (?, ?, ?)",
                        (key, emoji, role_name),
                    )


def _category(key: str):
    with _db() as db:
        return db.execute("SELECT * FROM categories WHERE key = ?", (key.lower().strip(),)).fetchone()


def _roles(category_key: str):
    with _db() as db:
        return db.execute(
            "SELECT emoji, role_name, role_id FROM role_mappings WHERE category_key = ? ORDER BY rowid",
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


# ========================= HELPERS =========================

def _norm_emoji(value: str) -> str:
    """Ignore the invisible variation selector so '⚔' and '⚔️' count as the same emoji."""
    return value.replace("\ufe0f", "").strip()


def _find_mapping(category_key: str, emoji: str):
    wanted = _norm_emoji(emoji)
    for row in _roles(category_key):
        if _norm_emoji(row["emoji"]) == wanted:
            return row
    return None


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


def _role_description(category_key: str) -> str:
    rows = _roles(category_key)
    if not rows:
        return "No roles have been added to this category yet."
    return "\n".join(f"{row['emoji']} — {row['role_name']}" for row in rows)


def _make_embed(category_key: str) -> discord.Embed:
    category = _category(category_key)
    embed = discord.Embed(
        title=category["title"],
        description=_role_description(category_key),
        color=int(category["color"]),
    )
    if category["footer"]:
        embed.set_footer(text=category["footer"])
    if category["image_url"]:
        embed.set_image(url=category["image_url"])
    if category["thumbnail_url"]:
        embed.set_thumbnail(url=category["thumbnail_url"])
    return embed


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


# ========================= PANEL MESSAGES =========================

async def _panel_state(bot: commands.Bot, key: str):
    """Look at a category's saved panel message.

    Returns (state, message) where state is:
      "none"    no panel saved for this category
      "missing" saved, but the message (or its channel) no longer exists
      "foreign" the message exists but wasn't posted by this bot (e.g. an old webhook panel),
                so it can't be edited
      "ok"      the bot's own message; it can be edited
    """
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
    """Discover panels already in the channel, by embed title. Never changes anything.

    Returns (found, old, notes):
      found -> {category_key: message} bot-posted panels that can be edited
      old   -> {category_key: message} panels posted by something else (old webhook panels)
      notes -> human-readable remarks
    """
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
                notes.append(f"{key}: found another copy (message `{message.id}`); left it untouched")
            else:
                found[key] = message
        else:
            old.setdefault(key, message)
    return found, old, notes


async def _post_panel(channel: discord.TextChannel, key: str) -> discord.Message:
    """Post a new panel message for a category, register it, and add one reaction per role."""
    message = await channel.send(embed=_make_embed(key))
    # Save immediately so a failure while adding reactions can't cause duplicates on re-run.
    _save_panel(key, channel.id, message.id)
    for row in _roles(key):
        await message.add_reaction(row["emoji"])
        await asyncio.sleep(0.3)  # stay under reaction rate limits
    return message


async def _ensure_panel(bot: commands.Bot, key: str, scan: Optional[dict] = None) -> tuple[str, str]:
    """Make sure a category has a working panel message.

    Order: use the saved panel if it still works -> otherwise link a matching bot-posted panel
    already in the channel -> otherwise post a new one.

    Returns (status, detail). status is one of:
      "ok"      already working, nothing was changed
      "linked"  found an existing panel in the channel and linked it
      "created" posted a new panel (already built from the database)
      "error"   couldn't do it; detail says why
    `scan` is an optional dict used to share one channel scan across several categories.
    """
    channel = await _get_channel(bot)
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
            return "linked", f"linked the existing panel (message `{found[key].id}`)"
        message = await _post_panel(channel, key)
        if key in old:
            return "created", (
                f"posted a new panel (message `{message.id}`). The older panel {old[key].jump_url} "
                "wasn't posted by me (it's an old webhook panel), so I can't edit it - delete it when you're ready"
            )
        return "created", f"posted a new panel (message `{message.id}`)"
    except discord.Forbidden:
        return "error", _NO_PERMS
    except discord.HTTPException as exc:
        return "error", f"Discord error: {exc}"


async def _update_existing_panel(bot: commands.Bot, key: str) -> tuple[bool, str]:
    """Edit the panel message so its embed matches the database. Keeps title, color, images."""
    try:
        state, message = await _panel_state(bot, key)
        if state != "ok" or message is None:
            return False, "I couldn't find my panel message for that category. Run `.setup_roles`."
        # Preserve the existing embed's title, color, footer, image, and thumbnail.
        # Only replace its role-list description.
        if message.embeds:
            embed = discord.Embed.from_dict(message.embeds[0].to_dict())
            embed.description = _role_description(key)
        else:
            embed = _make_embed(key)
        await message.edit(embed=embed)
        return True, "Updated the panel message."
    except discord.Forbidden:
        return False, _NO_PERMS
    except discord.HTTPException as exc:
        return False, f"Discord couldn't update the panel: {exc}"


async def _sync_panel(bot: commands.Bot, key: str) -> tuple[bool, str, bool]:
    """Ensure the panel exists, then bring its embed up to date with the database.

    Returns (ok, message, rebuilt). rebuilt=True means a fresh panel was posted, which already
    includes everything (roles and reactions), so there is nothing left to edit.
    """
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
    """Safe, repeatable: keep working panels, link ones that exist, create any that are missing."""
    scan: dict = {}
    working, linked, created, problems = [], [], [], []
    for category in _all_categories():
        key = category["key"]
        status, detail = await _ensure_panel(bot, key, scan)
        if status == "error" and detail == _NO_CHANNEL:
            return detail
        if status == "ok":
            working.append(key)
        elif status == "linked":
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

async def _add_role(bot: commands.Bot, guild: discord.Guild, category_key: str, emoji: str, role_name: str) -> str:
    """Add a role to a category: edit the existing panel message and add one reaction."""
    category_key = category_key.lower().strip()
    if _category(category_key) is None:
        return f"Unknown category `{category_key}`. Use `.rolecategories` to see categories."
    emoji = emoji.strip()
    if not emoji or len(emoji) > 32:
        return "Please provide one emoji (custom emoji are not supported by this first version)."
    role = discord.utils.find(lambda r: r.name.casefold() == role_name.strip().casefold(), guild.roles)
    if role is None:
        return f"I couldn't find a server role named `{role_name}`. Create that role in Discord first."
    if role.is_default() or role.managed:
        return f"`{role.name}` can't be handed out by reactions (it's @everyone or managed by an integration)."
    if role >= guild.me.top_role:
        return f"`{role.name}` is above my highest role, so I couldn't assign it. Move my role higher first."
    if _find_mapping(category_key, emoji) is not None:
        return f"Emoji {emoji} is already used in `{category_key}`. Remove or change that mapping first."
    if len(_roles(category_key)) >= MAX_REACTIONS:
        return f"Discord allows at most {MAX_REACTIONS} different reactions on one message. Create another category instead."

    def undo() -> None:
        with _db() as db:
            db.execute("DELETE FROM role_mappings WHERE category_key = ? AND emoji = ?", (category_key, emoji))

    with _db() as db:
        db.execute(
            "INSERT INTO role_mappings(category_key, emoji, role_name, role_id) VALUES (?, ?, ?, ?)",
            (category_key, emoji, role.name, role.id),
        )
    # 1) edit the panel message (or rebuild it if it's gone)
    ok, message, rebuilt = await _sync_panel(bot, category_key)
    if not ok:
        undo()
        return message
    if rebuilt:  # a new panel was posted with every role and reaction already on it
        return f"Added {emoji} → **{role.name}** to `{category_key}`. {message}"
    # 2) add the one new reaction to that same message
    problem = ""
    try:
        _state, panel_message = await _panel_state(bot, category_key)
        if panel_message is None:
            problem = "I couldn't find the panel message"
        else:
            await panel_message.add_reaction(emoji)
    except (discord.Forbidden, discord.HTTPException) as exc:
        problem = str(exc)
    if problem:
        # Reaction refused (bad emoji or no permission): undo so the embed and reactions agree.
        undo()
        await _sync_panel(bot, category_key)
        return f"I couldn't add the reaction {emoji} ({problem}), so I undid the change."
    return f"Added {emoji} → **{role.name}** to `{category_key}`. I edited the panel message and added the reaction."


async def _remove_role(bot: commands.Bot, category_key: str, emoji: str) -> str:
    """Remove a role from a category: edit the panel message and clear that reaction."""
    category_key = category_key.lower().strip()
    if _category(category_key) is None:
        return f"Unknown category `{category_key}`. Use `.rolecategories` to see categories."
    old = _find_mapping(category_key, emoji)
    if old is None:
        return f"No mapping for {emoji} exists in `{category_key}`."
    with _db() as db:
        db.execute("DELETE FROM role_mappings WHERE category_key = ? AND emoji = ?", (category_key, old["emoji"]))
    ok, message, rebuilt = await _sync_panel(bot, category_key)
    if not ok:
        with _db() as db:
            db.execute(
                "INSERT INTO role_mappings(category_key, emoji, role_name, role_id) VALUES (?, ?, ?, ?)",
                (category_key, old["emoji"], old["role_name"], old["role_id"]),
            )
        return message
    if rebuilt:
        return f"Removed {old['emoji']} from `{category_key}`. {message}"
    try:
        _state, panel_message = await _panel_state(bot, category_key)
        if panel_message is not None:
            try:
                await panel_message.clear_reaction(old["emoji"])
            except discord.Forbidden:  # no Manage Messages: at least remove my own reaction
                if bot.user:
                    await panel_message.remove_reaction(old["emoji"], bot.user)
    except (discord.Forbidden, discord.HTTPException):
        pass
    return (
        f"Removed {old['emoji']} from `{category_key}` and updated the panel. "
        "Members who already have that role keep it."
    )


async def _edit_panel_appearance(bot: commands.Bot, category_key: str, title: str, color_hex: str, footer: str, image_url: str, thumbnail_url: str) -> str:
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
    # Make sure a panel exists (link or post it) before editing it.
    status, detail = await _ensure_panel(bot, category_key)
    if status == "error":
        return detail
    note = f"(The panel was missing, so I {detail}.) " if status in ("linked", "created") else ""
    try:
        _state, message = await _panel_state(bot, category_key)
        if message is None:
            return "I couldn't find my panel message for that category. Run `.setup_roles`."
        if message.embeds:
            embed = discord.Embed.from_dict(message.embeds[0].to_dict())
        else:
            embed = _make_embed(category_key)
        embed.title = title.strip()[:256] or category["title"]
        embed.color = discord.Color(color)
        # discord.py has no remove_image()/remove_thumbnail(); passing url=None clears them.
        if footer:
            embed.set_footer(text=footer[:2048])
        else:
            embed.remove_footer()
        embed.set_image(url=image_url or None)
        embed.set_thumbnail(url=thumbnail_url or None)
        await message.edit(embed=embed)
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


async def _add_category(bot: commands.Bot, key: str, title: str, color_hex: str) -> str:
    """Create a new category and post its panel message."""
    key = key.lower().strip()
    if not key or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in key):
        return "Category ID must use lowercase letters, numbers, hyphens, or underscores."
    if _category(key):
        return f"Category `{key}` already exists."
    color = _parse_color(color_hex)
    if color is None:
        return "Enter a valid hex color, for example `#C5B3D3`."
    channel = await _get_channel(bot)
    if channel is None:
        return _NO_CHANNEL
    title = title.strip()[:256] or f"{key.upper()} ROLES"
    with _db() as db:
        db.execute("INSERT INTO categories(key, title, color) VALUES (?, ?, ?)", (key, title, color))
    try:
        await _post_panel(channel, key)
    except (discord.Forbidden, discord.HTTPException) as exc:
        if _panel(key) is None:  # nothing was posted, so don't leave a half-made category behind
            with _db() as db:
                db.execute("DELETE FROM categories WHERE key = ?", (key,))
        return f"Couldn't post the category panel: {exc}. {_NO_PERMS}"
    return f"Created category `{key}` and posted its panel. Add roles with `.roleadd {key} 🎮 RoleName`."


# ========================= DASHBOARD UI =========================

class _SafeModal(discord.ui.Modal):
    """Modal whose failures are logged and reported instead of showing 'interaction failed'."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Role dashboard modal failed", exc_info=error)
        text = "Something went wrong while doing that. Check the bot log for details."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class AddRoleModal(_SafeModal, title="Add a role"):
    category_key = discord.ui.TextInput(label="Category ID", placeholder="games", max_length=40)
    emoji = discord.ui.TextInput(label="Reaction emoji", placeholder="🎮", max_length=32)
    role_name = discord.ui.TextInput(label="Exact server role name", placeholder="Terraria", max_length=100)

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this inside your server.", ephemeral=True)
            return
        # Editing a panel takes a few API calls; defer so Discord's 3-second limit isn't hit.
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _add_role(self.bot, interaction.guild, str(self.category_key), str(self.emoji), str(self.role_name))
        await interaction.followup.send(result, ephemeral=True)


class RemoveRoleModal(_SafeModal, title="Remove a role mapping"):
    category_key = discord.ui.TextInput(label="Category ID", placeholder="games", max_length=40)
    emoji = discord.ui.TextInput(label="Reaction emoji to remove", placeholder="🎮", max_length=32)

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await _remove_role(self.bot, str(self.category_key), str(self.emoji))
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
    footer = discord.ui.TextInput(label="Footer (optional)", placeholder="Leave blank for no footer", required=False, max_length=2048)
    image_url = discord.ui.TextInput(label="Image URL (optional)", placeholder="https://...", required=False, max_length=400)

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Discord modals allow only 5 inputs, so there's no thumbnail field.
        # Keep whatever thumbnail is already stored instead of silently wiping it.
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
        if perms is None or not (perms.manage_guild or perms.administrator):
            await interaction.response.send_message("You need Manage Server or Administrator permission to use this dashboard.", ephemeral=True)
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


# ========================= REACTION HANDLING =========================

async def _resolve_reaction(bot: commands.Bot, payload: discord.RawReactionActionEvent):
    """Turn a raw reaction event on a registered panel into (member, role), or None to ignore it."""
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
        log.warning("Can't manage role %s (default, managed, or above my top role)", role.name)
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


def install_role_manager(bot: commands.Bot) -> None:
    """Register role commands and reaction handlers on an already-created bot."""
    _init_db()

    @bot.command(name="roles", help="Open the interactive role-management dashboard.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def roles_dashboard(ctx: commands.Context):
        embed = discord.Embed(
            title="ChildLaborer · Role Control Centre",
            description=(
                "Manage reaction-role panels without editing Python.\n\n"
                "**Dashboard actions**\n"
                "➕ Add role · ➖ Remove role · 🗂️ Add category\n"
                "🎨 Customize panel · 🔎 Sync panels · 📋 Show categories\n\n"
                "**Copyable commands**\n"
                "`.setup_roles` — post any missing panels (safe to re-run)\n"
                "`.rolecheck` — diagnose permissions and panels\n"
                "`.rolecategories` — list categories\n"
                "`.categoryadd art #C5B3D3 🎨 ART ROLES` — add a category\n"
                "`.roleadd games 🎮 Terraria` — add a role\n"
                "`.roleremove games 🎮` — remove a role\n"
                "`.panelcolor games #FFAA00` — change a panel color\n\n"
                "Members use reactions on the panel messages."
            ),
            color=DEFAULT_COLOR,
        )
        await ctx.send(embed=embed, view=RoleDashboardView(bot))

    @bot.command(name="rolecheck", help="Diagnose the role panel setup: channel, permissions, saved panels.")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
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
            lines.append("Optional: give me **Manage Messages** so removing a role also clears its reactions.")
        lines.append("**Saved panels:**")
        labels = {
            "none": "no panel yet (run `.setup_roles`)",
            "missing": "the message was deleted (run `.setup_roles`)",
            "foreign": "an old webhook message I can't edit (run `.setup_roles` to post a new one)",
            "ok": "OK, editable",
        }
        for row in _all_categories():
            try:
                state, message = await _panel_state(bot, row["key"])
                text = labels[state] + (f" - {message.jump_url}" if message else "")
            except (discord.Forbidden, discord.HTTPException) as exc:
                text = f"couldn't check ({exc})"
            lines.append(f"• `{row['key']}`: {text}")
        await ctx.send("\n".join(lines)[:1900])

    @bot.command(name="setup_roles", aliases=["migrate_role_panels"], help="Post any role panels that are missing (safe to re-run).")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setup_roles(ctx: commands.Context):
        await ctx.send("Checking the role panels. I'll keep working ones, link existing ones, and post any that are missing…")
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
    @commands.has_guild_permissions(manage_guild=True)
    async def categoryadd(ctx: commands.Context, key: str, color_hex: str, *, title: str):
        async with ctx.typing():
            result = await _add_category(bot, key, title, color_hex)
        await ctx.send(result)

    @bot.command(name="roleadd", help="Add a role to a category: .roleadd <category> <emoji> <exact role name>")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def roleadd(ctx: commands.Context, category_key: str, emoji: str, *, role_name: str):
        if ctx.guild is None:
            return
        async with ctx.typing():
            result = await _add_role(bot, ctx.guild, category_key, emoji, role_name)
        await ctx.send(result)

    @bot.command(name="roleremove", help="Remove a role from a category: .roleremove <category> <emoji>")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def roleremove(ctx: commands.Context, category_key: str, emoji: str):
        async with ctx.typing():
            result = await _remove_role(bot, category_key, emoji)
        await ctx.send(result)

    @bot.command(name="panelcolor", help="Change a panel embed color: .panelcolor <category> <hex>")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
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

    # add_listener (not @bot.event) so these never replace any handler defined elsewhere.
    bot.add_listener(on_raw_reaction_add, "on_raw_reaction_add")
    bot.add_listener(on_raw_reaction_remove, "on_raw_reaction_remove")
    print("Role manager loaded: .roles, .rolecheck, .setup_roles, .rolecategories, .categoryadd, .roleadd, .roleremove, .panelcolor")
 