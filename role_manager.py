"""Database-driven reaction-role panels for Child Labourer.

Uses ordinary bot-authored Discord messages (no webhooks). Requires discord.py 2.x.
Import install_role_manager(bot) from main.py and call it once before bot.run().
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional
from unicodedata import category

import discord
from discord import guild
from discord.ext import commands

ROLE_CHANNEL_ID = 1550216770156232834
PANEL_COLOR = 0xC5B3D3
DB_PATH = Path(__file__).resolve().parent / "role_manager.sqlite3"
log = logging.getLogger("discord.role_manager")

# These are the user's current panels. Seeded only when the database is empty.
DEFAULT_CATEGORIES = {
    "Music": {
        "title": "🎵 MUSIC ROLES",
        "description": "React below to choose your music roles.",
        "roles": {"🎧": "♩ Listener", "🎸": "♫ Musician", "🎛️": "𝄞 Producer", "🎤": "♬ Vocalist", "✍️": "♯ Songwriter", "📀": "♭ DJ"},
    },
    "Notifications": {
        "title": "🔔 NOTIFICATION ROLES",
        "description": "React to choose which notifications you want to receive.",
        "roles": {"🔔": "Upload Ping", "📢": "VC Ping"},
    },
    "Games": {
        "title": "🎮 GAME ROLES",
        "description": "React below to choose your game roles.",
        "roles": {"⚔️": "Valorant", "⛏️": "Minecraft"},
    },
    "Colors": {
        "title": "🎨 COLORS",
        "description": "React below to choose a color role.",
        "roles": {"🔴": "Red", "🟠": "Orange", "🟡": "Yellow", "🟢": "Green", "🔵": "Blue", "🟣": "Purple", "🩷": "Pink", "⚪": "White", "⚫": "Black"},
    },
}


def _connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _init_db():
    with _connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            color INTEGER NOT NULL DEFAULT 12956627,
            panel_message_id INTEGER,
            panel_channel_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS role_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            emoji TEXT NOT NULL,
            role_name TEXT NOT NULL,
            UNIQUE(category_id, emoji),
            UNIQUE(category_id, role_name COLLATE NOCASE)
        );
        """)
        count = con.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        if count == 0:
            for name, data in DEFAULT_CATEGORIES.items():
                cur = con.execute(
                    "INSERT INTO categories(name,title,description,color) VALUES(?,?,?,?)",
                    (name, data["title"], data["description"], PANEL_COLOR),
                )
                for emoji, role_name in data["roles"].items():
                    con.execute(
                        "INSERT INTO role_mappings(category_id,emoji,role_name) VALUES(?,?,?)",
                        (cur.lastrowid, emoji, role_name),
                    )


def _get_categories():
    with _connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM categories ORDER BY id")]


def _get_category(name: str):
    with _connect() as con:
        row = con.execute("SELECT * FROM categories WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
        return dict(row) if row else None


def _get_mappings(category_id: int):
    with _connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM role_mappings WHERE category_id=? ORDER BY id", (category_id,))]


def _set_panel(category_id: int, channel_id: int, message_id: int):
    with _connect() as con:
        con.execute("UPDATE categories SET panel_channel_id=?, panel_message_id=? WHERE id=?", (channel_id, message_id, category_id))


def _valid_emoji(emoji: str) -> bool:
    # Custom emoji notation, unicode emoji and composed emoji sequences are accepted.
    return bool(emoji and emoji.strip() and len(emoji) <= 100)


def _panel_embed(category: dict, mappings: list[dict]) -> discord.Embed:
    lines = []
    for item in mappings:
        lines.append(f"{item['emoji']} — **{discord.utils.escape_markdown(item['role_name'])}**")
    description = category["description"].strip()
    if lines:
        description = (description + "\n\n" if description else "") + "\n".join(lines)
    else:
        description = (description + "\n\n" if description else "") + "*No roles have been added to this category yet.*"
    embed = discord.Embed(title=category["title"], description=description, color=int(category["color"]))
    embed.set_footer(text="React to add or remove a role.")
    return embed


async def _find_panel(
    bot: commands.Bot, category: dict
) -> tuple[Optional[discord.TextChannel], Optional[discord.Message]]:
    """Fetch a saved panel only from a normal guild text channel."""
    channel_id = category.get("panel_channel_id")
    message_id = category.get("panel_message_id")
    if not channel_id or not message_id:
        return None, None

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            fetched = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None, None
        if not isinstance(fetched, discord.TextChannel):
            return None, None
        channel = fetched

    # A saved panel should be in a TextChannel; ForumChannel and category
    # objects do not expose fetch_message().
    if not isinstance(channel, discord.TextChannel):
        return None, None

    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return channel, None
    return channel, message


async def _sync_panel(
    bot: commands.Bot,
    category: Optional[dict],
    *,
    create_if_missing: bool = True,
) -> tuple[bool, str]:
    if category is None:
        return False, "I couldn't find that category in the saved configuration."

    channel, message = await _find_panel(bot, category)

    if channel is None:
        candidate = bot.get_channel(ROLE_CHANNEL_ID)
        if candidate is None:
            try:
                candidate = await bot.fetch_channel(ROLE_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False, (
                    f"I can't access the configured role channel (`{ROLE_CHANNEL_ID}`). "
                    "Check the channel ID and my permissions."
                )
        if not isinstance(candidate, discord.TextChannel):
            return False, "The configured role channel isn't a regular text channel."
        channel = candidate

    if not isinstance(channel, discord.TextChannel):
        return False, "The saved panel channel isn't a regular text channel."

    me = channel.guild.me
    if me is None and bot.user is not None:
        me = channel.guild.get_member(bot.user.id)
    if me is None:
        return False, "I couldn't identify my bot member in this server. Try again after the bot reconnects."

    perms = channel.permissions_for(me)
    missing = [
        name
        for name, okay in (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Add Reactions", perms.add_reactions),
            ("Read Message History", perms.read_message_history),
        )
        if not okay
    ]
    if missing:
        return False, "Missing permissions in the role channel: " + ", ".join(missing) + "."

    embed = _panel_embed(category, _get_mappings(category["id"]))

    # Webhook-authored messages cannot be edited by the bot account.
    # Leave them untouched and create a normal bot-authored replacement.
    if message is not None and message.webhook_id is not None:
        log.info(
            "Saved panel for %s is webhook-authored; creating a normal bot message instead.",
            category["name"],
        )
        message = None

    if message is not None:
        try:
            await message.edit(embed=embed)
        except discord.Forbidden:
            return False, "I can see the existing panel but can't edit it. Check message permissions."
        except discord.HTTPException as exc:
            return False, f"Discord wouldn't let me edit the panel: {exc}"
    elif create_if_missing:
        try:
            message = await channel.send(embed=embed)
            _set_panel(category["id"], channel.id, message.id)
        except discord.Forbidden:
            return False, "I don't have permission to send the role panel in the role channel."
        except discord.HTTPException as exc:
            return False, f"Discord wouldn't let me create the panel: {exc}"
    else:
        return False, "The saved panel message is missing. Run `.rolesetup` to create a replacement."

    
    # Get the current valid emoji mappings for this category.
    mappings = _get_mappings(category["id"])
    valid_emojis = {item["emoji"] for item in mappings}

    # Remove reactions that no longer have a role mapping.
    try:
        message = await channel.fetch_message(message.id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.error("Couldn't refresh role panel before reaction cleanup: %s", exc)
        return False, f"Couldn't refresh the role panel: {exc}"

    mappings = _get_mappings(category["id"])
    valid_emojis = {item["emoji"] for item in mappings}
    cleanup_failed = []

    # Remove reactions whose emoji no longer has a role mapping.
    for reaction in list(message.reactions):
        emoji = str(reaction.emoji)

        if emoji in valid_emojis:
            continue

        try:
            await message.clear_reaction(reaction.emoji)
            log.info(
                "Cleared obsolete reaction %s from category %s.",
                emoji,
                category["name"],
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            cleanup_failed.append(emoji)
            log.error(
                "Failed to clear obsolete reaction %s from category %s: %s",
                emoji,
                category["name"],
                exc,
            )

    if cleanup_failed:
        return True, (
            "Panel updated, but Discord couldn't remove these obsolete "
            "reactions: " + ", ".join(cleanup_failed)
            + ". Check the bot's channel permissions and logs."
        )

    # Add reactions for mappings that are still valid.
    try:
        message = await channel.fetch_message(message.id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.error("Couldn't refresh role panel before adding reactions: %s", exc)
        return True, f"Panel updated and cleanup attempted, but couldn't refresh reactions: {exc}"

    existing_emojis = {str(reaction.emoji) for reaction in message.reactions}

    for item in mappings:
        if item["emoji"] in existing_emojis:
            continue

        try:
            await message.add_reaction(item["emoji"])
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Panel updated but couldn't add reaction %s: %s",
                item["emoji"],
                exc,
            )
            return True, (
                f"Panel updated, but I couldn't add reaction {item['emoji']}. "
                "Check that emoji and Add Reactions permission."
            )

    return True, "Panel updated successfully."

def _admin_check():
    return commands.has_guild_permissions(manage_roles=True)


def install_role_manager(bot: commands.Bot):
    """Register the dynamic reaction-role system on the supplied bot."""
    _init_db()

    def admin_only():
        return commands.has_guild_permissions(manage_roles=True)

    @bot.command(name="roles")
    @commands.guild_only()
    @admin_only()
    async def roles_dashboard(ctx: commands.Context):
        cats = _get_categories()
        embed = discord.Embed(title="Role Manager", description="Manage reaction-role categories and mappings with commands below. Panels are ordinary bot messages—no webhooks.", color=PANEL_COLOR)
        embed.add_field(name="Categories", value="\n".join(f"• **{c['name']}** — {len(_get_mappings(c['id']))} roles" for c in cats) or "None", inline=False)
        embed.add_field(name="Setup / repair", value="`.rolesetup` — create missing panels and sync reactions\n`.rolepanel update <category>` — update one panel", inline=False)
        embed.add_field(name="Categories", value="`.rolecategory add <name>`\n`.rolecategory rename <old> | <new>`\n`.rolecategory remove <name>`", inline=False)
        embed.add_field(name="Roles", value="`.roleadd <category> | <emoji> | <role name>`\n`.roleremove <category> | <emoji>`\n`.roleedit <category> | <old emoji> | <new emoji> | <role name>`", inline=False)
        embed.add_field(name="Appearance", value="`.rolepanel title <category> | <new title>`\n`.rolepanel description <category> | <description>`\n`.rolepanel color <category> | #C5B3D3`", inline=False)
        embed.set_footer(text="Manage Roles permission required. The bot role must be above roles it assigns.")
        await ctx.send(embed=embed)

    @bot.command(name="rolesetup")
    @commands.guild_only()
    @admin_only()
    async def rolesetup(ctx: commands.Context):
        await ctx.send("Starting role-panel setup. I'll create any missing category panels and sync their reactions; existing saved panels will be edited in place.")
        results = []
        for category in _get_categories():
            okay, message = await _sync_panel(bot, category, create_if_missing=True)
            results.append(f"{'✅' if okay else '❌'} **{category['name']}**: {message}")
        await ctx.send("\n".join(results))

    @bot.command(name="rolecategory")
    @commands.guild_only()
    @admin_only()
    async def rolecategory(ctx: commands.Context, action: str, *, details: str):
        action = action.lower().strip()
        if action == "add":
            name = details.strip()
            if not name or len(name) > 80:
                await ctx.send("❌ Category name must be between 1 and 80 characters.")
                return
            if _get_category(name):
                await ctx.send(f"❌ Category **{name}** already exists.")
                return
            title = name.upper()
            with _connect() as con:
                cur = con.execute("INSERT INTO categories(name,title,description,color) VALUES(?,?,?,?)", (name, title, "React below to choose your roles.", PANEL_COLOR))
                category_id = cur.lastrowid
            category = next(c for c in _get_categories() if c["id"] == category_id)
            okay, message = await _sync_panel(bot, category, create_if_missing=True)
            await ctx.send(("✅" if okay else "⚠️") + f" Category **{name}** saved. {message}")
            return
        if action == "rename":
            parts = [p.strip() for p in details.split("|", 1)]
            if len(parts) != 2 or not all(parts):
                await ctx.send("Usage: `.rolecategory rename <old name> | <new name>`")
                return
            old, new = parts
            category = _get_category(old)
            if not category:
                await ctx.send(f"❌ I couldn't find category **{old}**.")
                return
            if _get_category(new):
                await ctx.send(f"❌ Category **{new}** already exists.")
                return
            with _connect() as con:
                con.execute("UPDATE categories SET name=?, title=? WHERE id=?", (new, new.upper(), category["id"]))
            updated = _get_category(new)
            okay, message = await _sync_panel(bot, updated)
            await ctx.send(("✅" if okay else "⚠️") + f" Category renamed to **{new}**. {message}")
            return
        if action == "remove":
            name = details.strip()
            category = _get_category(name)
            if not category:
                await ctx.send(f"❌ I couldn't find category **{name}**.")
                return
            with _connect() as con:
                con.execute("DELETE FROM categories WHERE id=?", (category["id"],))
            # Intentionally leave the old message in place rather than deleting user-visible history.
            await ctx.send(f"✅ Category **{name}** and its role mappings were removed from configuration. Its old panel message was left in the channel; delete it manually if you no longer want it visible. Server roles were not deleted.")
            return
        await ctx.send("❌ Unknown action. Use `add`, `rename`, or `remove`.")

    @bot.command(name="roleadd")
    @commands.guild_only()
    @admin_only()
    async def roleadd(ctx: commands.Context, *, details: str):
        parts = [p.strip() for p in details.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            await ctx.send("Usage: `.roleadd <category> | <emoji> | <role name>`\nExample: `.roleadd Music | 🎧 | Listener`\nI'll create the server role automatically if it doesn't exist.")
            return
        category_name, emoji, role_name = parts
        category = _get_category(category_name)
        if not category:
            await ctx.send(f"❌ I couldn't find category **{category_name}**. Create it with `.rolecategory add {category_name}` first.")
            return
        if not _valid_emoji(emoji):
            await ctx.send("❌ That emoji is empty or too long.")
            return
        mappings = _get_mappings(category["id"])
        if any(m["emoji"] == emoji for m in mappings):
            await ctx.send(f"❌ Emoji {emoji} is already used in **{category_name}**. Remove or edit that mapping first.")
            return
        if any(m["role_name"].casefold() == role_name.casefold() for m in mappings):
            await ctx.send(f"❌ Role **{role_name}** is already mapped in **{category_name}**.")
            return
        guild = ctx.guild
        if guild is None:
            await ctx.send("❌ This command only works inside a server.")
            return
        role = discord.utils.get(guild.roles, name=role_name)
        created = False
        if role is None:
            me = guild.me
            if not (me.guild_permissions.manage_roles or me.guild_permissions.administrator):
                await ctx.send("❌ I can't create roles because I lack **Manage Roles** permission.")
                return
            try:
                role = await guild.create_role(name=role_name, reason=f"Reaction role added by {ctx.author}")
                created = True
            except discord.Forbidden:
                await ctx.send("❌ Discord denied role creation. Check my Manage Roles permission and role hierarchy.")
                return
            except discord.HTTPException as exc:
                await ctx.send(f"❌ Discord couldn't create the role: {exc}")
                return
        with _connect() as con:
            try:
                con.execute("INSERT INTO role_mappings(category_id,emoji,role_name) VALUES(?,?,?)", (category["id"], emoji, role.name))
            except sqlite3.IntegrityError:
                await ctx.send("❌ This emoji or role is already mapped in that category.")
                if created:
                    try:
                        await role.delete(reason="Undo failed reaction-role mapping")
                    except discord.HTTPException:
                        pass
                return
        okay, message = await _sync_panel(bot, _get_category(category_name))
        if not okay:
            with _connect() as con:
                con.execute("DELETE FROM role_mappings WHERE category_id=? AND emoji=?", (category["id"], emoji))
            if created:
                try:
                    await role.delete(reason="Undo failed reaction-role panel update")
                except discord.HTTPException:
                    pass
            await ctx.send(f"❌ I couldn't finish adding {emoji} → **{role.name}**. Mapping rolled back. {message}")
            return
        prefix = "Created the role and " if created else "Reused the existing role and "
        await ctx.send(f"✅ {prefix}added {emoji} → **{role.name}** to **{category_name}**. {message}")

    
    @bot.command(name="roleremove")
    @commands.guild_only()
    @admin_only()
    async def roleremove(ctx: commands.Context, *, details: str):
        guild = ctx.guild
        if guild is None:
            await ctx.send("❌ This command only works inside a server.")
            return

        parts = [p.strip() for p in details.split("|", 1)]
        if len(parts) != 2 or not all(parts):
            await ctx.send("Usage: `.roleremove <category> | <emoji>`")
            return

        category_name, emoji = parts
        category = _get_category(category_name)

        if not category:
            await ctx.send(
            f"❌ I couldn't find category **{category_name}**.")
            return

        role_name = None
        other_mapping = False

        with _connect() as con:
            row = con.execute(
                """
                SELECT role_name FROM role_mappings
                WHERE category_id = ? AND emoji = ?
                """,
                (category["id"], emoji),
            ).fetchone()

            if row:
                role_name = row[0]

                other_mapping = con.execute(
                    """
                    SELECT 1 FROM role_mappings
                    WHERE role_name = ?
                    AND NOT (category_id = ? AND emoji = ?)
                    LIMIT 1
                   """,
                    (role_name, category["id"], emoji),
                ).fetchone() is not None

                con.execute(
                    """
                    DELETE FROM role_mappings
                    WHERE category_id = ? AND emoji = ?
                    """,
                    (category["id"], emoji),
                )

    # Always synchronize the panel, even if the mapping was
    # already deleted during an earlier attempt.
        okay, sync_message = await _sync_panel(
            bot, _get_category(category_name)
        )

        if not okay:
            await ctx.send(
                f"⚠️ Couldn't synchronize **{category_name}**: "
                f"{sync_message}"
            )
            return

        # Mapping was already absent; this attempt was just cleanup.
        if role_name is None:
            await ctx.send(
                f"✅ Checked **{category_name}** and synchronized its panel. "
                f"{sync_message}"
            )
            return

        if other_mapping:
            await ctx.send(
                f"✅ Removed {emoji} from **{category_name}**. "
                f"Role **{role_name}** was kept because another category "
                f"uses it. {sync_message}"
            )
            return

        role = discord.utils.get(guild.roles, name=role_name)

        if role is None:
            await ctx.send(
                f"✅ Mapping removed. Role **{role_name}** was already "
                f"missing. {sync_message}"
            )
            return

        me = guild.me
        if me is None:
            await ctx.send(
                "⚠️ Mapping and panel updated, but I couldn't verify the "
                "bot's role hierarchy. Delete the role manually if needed."
            )
            return

        if role.managed or role >= me.top_role:
            await ctx.send(
                f"⚠️ Mapping and panel updated, but I couldn't delete "
                f"**{role_name}**. Check the bot's role hierarchy."
            )
            return

        try:
            await role.delete(
                reason=f"Role mapping removed by {ctx.author}"
            )
            await ctx.send(
                f"✅ Removed {emoji} from **{category_name}**.\n"
               f"🗑️ Deleted server role **{role_name}**.\n"
               f"✅ Panel synchronized successfully."
            )
        except discord.Forbidden:
            await ctx.send(
                f"⚠️ Panel updated, but Discord denied permission to delete "
                f"**{role_name}**. Check Manage Roles and role hierarchy."
            )
        except discord.HTTPException as error:
            await ctx.send(
                f"⚠️ Panel updated, but deleting **{role_name}** failed: "
                f"{error}"
            )

    @bot.command(name="roleedit")
    @commands.guild_only()
    @admin_only()
    async def roleedit(ctx: commands.Context, *, details: str):
        parts = [p.strip() for p in details.split("|", 3)]
        if len(parts) != 4 or not all(parts):
            await ctx.send("Usage: `.roleedit <category> | <old emoji> | <new emoji> | <role name>`")
            return
        cat_name, old_emoji, new_emoji, role_name = parts
        category = _get_category(cat_name)
        if not category:
            await ctx.send(f"❌ I couldn't find category **{cat_name}**.")
            return
        mappings = _get_mappings(category["id"])
        old = next((m for m in mappings if m["emoji"] == old_emoji), None)
        if not old:
            await ctx.send(f"❌ Emoji {old_emoji} isn't mapped in **{cat_name}**.")
            return
        if any(m["emoji"] == new_emoji and m["emoji"] != old_emoji for m in mappings):
            await ctx.send(f"❌ Emoji {new_emoji} is already in use in **{cat_name}**.")
            return
        if any(m["role_name"].casefold() == role_name.casefold() and m["id"] != old["id"] for m in mappings):
            await ctx.send(f"❌ Role **{role_name}** is already mapped in **{cat_name}**.")
            return
        guild = ctx.guild
        if guild is None:
            await ctx.send("❌ This command only works inside a server.")
            return
        role = discord.utils.get(guild.roles, name=role_name)
        created = False
        if role is None:
            try:
                role = await guild.create_role(name=role_name, reason=f"Reaction role edited by {ctx.author}")
                created = True
            except (discord.Forbidden, discord.HTTPException) as exc:
                await ctx.send(f"❌ Couldn't create role **{role_name}**: {exc}")
                return
        with _connect() as con:
            con.execute("UPDATE role_mappings SET emoji=?, role_name=? WHERE id=?", (new_emoji, role.name, old["id"]))
        okay, message = await _sync_panel(bot, _get_category(cat_name))
        if not okay:
            await ctx.send(f"⚠️ Saved the mapping but couldn't update the panel. Run `.rolepanel update {cat_name}`. {message}")
            return
        await ctx.send(f"✅ Updated {old_emoji} → {new_emoji} and set the mapped role to **{role.name}** in **{cat_name}**. {'Created the role. ' if created else ''}{message}")

    @bot.command(name="rolecreate")
    @commands.guild_only()
    @admin_only()
    async def rolecreate(ctx: commands.Context, *, name: str):
        name = name.strip()
        if not name:
            await ctx.send("Usage: `.rolecreate <role name>`")
            return
        guild = ctx.guild
        if guild is None:
            await ctx.send("❌ This command only works inside a server.")
            return
        if discord.utils.get(guild.roles, name=name):
            await ctx.send(f"❌ Role **{name}** already exists.")
            return
        try:
            role = await guild.create_role(name=name, reason=f"Role created by {ctx.author}")
        except discord.Forbidden:
            await ctx.send("❌ I can't create roles. Give me Manage Roles and move my bot role above the roles I need to manage.")
        except discord.HTTPException as exc:
            await ctx.send(f"❌ Discord couldn't create the role: {exc}")
        else:
            await ctx.send(f"✅ Created server role **{role.name}**.")

    @bot.command(name="roledelete")
    @commands.guild_only()
    @admin_only()
    async def roledelete(ctx: commands.Context, *, name: str):
        guild = ctx.guild
        if guild is None:
            await ctx.send("❌ This command only works inside a server.")
            return
        role = discord.utils.get(guild.roles, name=name.strip())
        if role is None:
            await ctx.send(f"❌ I couldn't find server role **{name}**.")
            return
        if role == guild.default_role or role.managed:
            await ctx.send("❌ That role is managed by Discord or is @everyone and cannot be deleted by this command.")
            return
        bot_member = guild.me
        if bot_member is None and bot.user is not None:
            bot_member = guild.get_member(bot.user.id)
        if bot_member is None:
            await ctx.send("❌ I couldn't identify my bot member in this server. Try again after reconnecting.")
            return
        if role >= bot_member.top_role:
            await ctx.send("❌ I can't delete that role because it is at or above my highest role. Move my bot role higher.")
            return
        try:
            await role.delete(reason=f"Role deleted by {ctx.author}")
        except discord.Forbidden:
            await ctx.send("❌ Discord denied role deletion. Check Manage Roles and role hierarchy.")
        except discord.HTTPException as exc:
            await ctx.send(f"❌ Discord couldn't delete that role: {exc}")
        else:
            # Remove any mappings to that role from all categories, then refresh their panels.
            with _connect() as con:
                con.execute("DELETE FROM role_mappings WHERE role_name=? COLLATE NOCASE", (name.strip(),))
            for category in _get_categories():
                await _sync_panel(bot, category)
            await ctx.send(f"✅ Deleted role **{name}** and removed its configured reaction-role mappings.")

    @bot.command(name="rolepanel")
    @commands.guild_only()
    @admin_only()
    async def rolepanel(ctx: commands.Context, action: str, *, details: str):
        action = action.lower().strip()
        if action == "update":
            category = _get_category(details.strip())
            if not category:
                await ctx.send(f"❌ I couldn't find category **{details.strip()}**.")
                return
            okay, message = await _sync_panel(bot, category, create_if_missing=True)
            await ctx.send(("✅ " if okay else "❌ ") + message)
            return
        if action not in {"title", "description", "color"}:
            await ctx.send("Usage: `.rolepanel update <category>` or `.rolepanel title|description|color <category> | <value>`")
            return
        parts = [p.strip() for p in details.split("|", 1)]
        if len(parts) != 2 or not all(parts):
            await ctx.send(f"Usage: `.rolepanel {action} <category> | <value>`")
            return
        cat_name, value = parts
        category = _get_category(cat_name)
        if not category:
            await ctx.send(f"❌ I couldn't find category **{cat_name}**.")
            return
        with _connect() as con:
            if action == "title":
                if len(value) > 256:
                    await ctx.send("❌ Embed titles must be 256 characters or fewer.")
                    return
                con.execute("UPDATE categories SET title=? WHERE id=?", (value, category["id"]))
            elif action == "description":
                if len(value) > 1500:
                    await ctx.send("❌ Panel descriptions must be 1,500 characters or fewer.")
                    return
                con.execute("UPDATE categories SET description=? WHERE id=?", (value, category["id"]))
            else:
                color = value.lstrip("#")
                if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
                    await ctx.send("❌ Invalid color. Use a six-digit hex color, such as `#C5B3D3`.")
                    return
                con.execute("UPDATE categories SET color=? WHERE id=?", (int(color, 16), category["id"]))
        okay, message = await _sync_panel(bot, _get_category(cat_name))
        await ctx.send(("✅ " if okay else "⚠️ ") + f"Saved {action} for **{cat_name}**. {message}")

    # Raw reaction events work even when the panel message isn't cached.
    @bot.listen("on_raw_reaction_add")
    async def role_reaction_add(payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or bot.user is None or payload.user_id == bot.user.id:
            return
        category = next((c for c in _get_categories() if c.get("panel_message_id") == payload.message_id and c.get("panel_channel_id") == payload.channel_id), None)
        if category is None:
            return
        emoji = str(payload.emoji)
        mapping = next((m for m in _get_mappings(category["id"]) if m["emoji"] == emoji), None)
        if mapping is None:
            return
        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = discord.utils.get(guild.roles, name=mapping["role_name"])
        if role is None:
            log.warning("Role '%s' configured in category '%s' no longer exists", mapping["role_name"], category["name"])
            return
        try:
            member = payload.member or await guild.fetch_member(payload.user_id)
            bot_member = guild.me
            if bot_member is None and bot.user is not None:
                bot_member = guild.get_member(bot.user.id)
            if bot_member is None or role >= bot_member.top_role:
                log.warning("Cannot assign role '%s': bot member is missing or role is at/above its highest role", role.name)
                return
            await member.add_roles(role, reason=f"Reaction role: {category['name']} / {emoji}")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not assign reaction role: %s", exc)

    @bot.listen("on_raw_reaction_remove")
    async def role_reaction_remove(payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or bot.user is None or payload.user_id == bot.user.id:
            return
        category = next((c for c in _get_categories() if c.get("panel_message_id") == payload.message_id and c.get("panel_channel_id") == payload.channel_id), None)
        if category is None:
            return
        mapping = next((m for m in _get_mappings(category["id"]) if m["emoji"] == str(payload.emoji)), None)
        if mapping is None:
            return
        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = discord.utils.get(guild.roles, name=mapping["role_name"])
        if role is None:
            return
        try:
            member = await guild.fetch_member(payload.user_id)
            await member.remove_roles(role, reason=f"Reaction role removed: {category['name']} / {payload.emoji}")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not remove reaction role: %s", exc)

    log.info("Database-driven reaction-role system registered (normal bot messages, no webhooks).")
