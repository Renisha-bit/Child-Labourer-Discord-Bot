from unittest import result

import discord

from discord.ext import commands

from .detector import detect_recommendation
from .manager import CoverQueueManager

def register_cover_commands(
    bot: commands.Bot,
    manager: CoverQueueManager,
) -> None:
    """Register all Cover Queue commands."""

    def is_moderator(ctx: commands.Context) -> bool:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False

        permissions = ctx.author.guild_permissions

        return (
            permissions.administrator
            or permissions.manage_messages
        )

    def is_singer(ctx: commands.Context) -> bool:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False

        if is_moderator(ctx):
            return True

        if manager.singer_role_id == 0:
            return False

        return any(
            role.id == manager.singer_role_id
            for role in ctx.author.roles
        )

    def get_song_by_number(
        status: str,
        number: int,
    ) -> dict | None:
        songs = manager.storage.get_songs(status)

        if number < 1 or number > len(songs):
            return None

        return songs[number - 1]

    @bot.command(name="coverqueue")
    async def coverqueue(ctx: commands.Context):
        """Display songs waiting to be covered."""

        songs = manager.get_numbered_songs("to_cover")

        if not songs:
            await ctx.send("🎶 The cover queue is currently empty.")
            return

        lines = ["**🎶 TO BE COVERED**", ""]

        for item in songs:
            song = item["song"]

            recommenders = " & ".join(
                f"<@{person['user_id']}>"
                for person in song.get("recommenders", [])
            )

            lines.append(
                f"**{item['number']}. {song['title']} — {song['artist']}**"
            )
            lines.append(
                f"Recommended by {recommenders or 'Unknown'}"
            )
            lines.append("")

        # Keep Discord messages within the character limit.
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                await ctx.send(
                    current,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                current = ""

            current += line + "\n"

        if current.strip():
            await ctx.send(
                current,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @bot.command(name="coverarchive")
    async def coverarchive(ctx: commands.Context):
        """Display completed covers."""

        songs = manager.get_numbered_songs("completed")

        if not songs:
            await ctx.send("🎙️ There are no completed covers yet.")
            return

        lines = ["**🎙️ COMPLETED COVERS**", ""]

        for item in songs:
            song = item["song"]

            lines.append(
                f"**{item['number']}. {song['title']} — {song['artist']}**"
            )

            for completion in song.get("completions", []):
                cover_url = completion.get("cover_url")

                line = f"• <@{completion['user_id']}>"

                if cover_url:
                    line += f" — [Listen]({cover_url})"

                lines.append(line)

            lines.append("")

        current = ""

        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                await ctx.send(
                    current,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                current = ""

            current += line + "\n"

        if current.strip():
            await ctx.send(
                current,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @bot.command(name="coveradd")
    async def coveradd(ctx: commands.Context, *, song_details: str):
        """
        Manually submit a song.

        Example: .coveradd Numb - Linkin Park
        """

        candidate = detect_recommendation(song_details)

        if (
            candidate is None
            or not candidate.title
            or not candidate.artist
        ):
            await ctx.send(
                "Please provide a song in this format:\n"
                "`.coveradd Song Title - Artist Name`"
            )
            return

        result = await manager.verifier.verify(candidate)

        song, is_new = manager.storage.add_song(
            title=result.title or candidate.title,
            artist=result.artist or candidate.artist,
            user_id=ctx.author.id,
            display_name=ctx.author.display_name,
            source="manual",
            canonical_id=result.canonical_id,
            status="to_cover" if result.verified else "review",
        )

        if result.verified:
            if not is_new and song.get("status") == "review":
                song["status"] = "to_cover"
                manager.storage.save()

            message = (
                f"🎶 **{song['title']} — {song['artist']}** "
                "has been added to the queue!"
                if is_new
                else "This song was already recommended. "
                "Your recommendation credit has been added."
            )
        else:
            message = (
                f"Your submission **{candidate.title} — "
                f"{candidate.artist}** needs moderator review.\n"
                f"Reason: {result.reason}"
            )

        await manager.refresh_pinned_message()
        await ctx.send(message)

    @bot.command(name="coverdone")
    async def coverdone(
        ctx: commands.Context,
        song_number: str,
        cover_url: str | None = None,
    ):
        """
        Mark a song as covered.

        Examples:
        .coverdone 3
        .coverdone 3 https://youtube.com/...
        .coverdone c3 https://soundcloud.com/...
        """

        if not is_singer(ctx):
            await ctx.send(
                "You need the configured singer role to use this command."
            )
            return

        try:
            if song_number.lower().startswith("c"):
                number = int(song_number[1:])
                song = get_song_by_number("completed", number)
            else:
                number = int(song_number)
                song = get_song_by_number("to_cover", number)

        except ValueError:
            await ctx.send(
                "Invalid song number. Example: `.coverdone 3`"
            )
            return

        if not song:
            await ctx.send("I couldn't find that song in the selected list.")
            return

        if cover_url:
            if not cover_url.startswith(("https://", "http://")):
                await ctx.send("Please provide a valid HTTP(S) cover link.")
                return

        # Prevent duplicate credits from the same singer.
        if any(
            item.get("user_id") == ctx.author.id
            for item in song.get("completions", [])
        ):
            await ctx.send(
                "You've already been credited for this song!"
            )

            return

        result = manager.storage.mark_completed(
            song_id=song["id"],
            user_id=ctx.author.id,
            display_name=ctx.author.display_name,
            cover_url=cover_url,
        )

        if result is None:
            await ctx.send("This song cannot be marked as completed.")
            return

        if not any(
            item.get("user_id") == ctx.author.id
            for item in result.get("completions", [])
        ):
            await ctx.send("Your cover was not recorded.")
            return

        await manager.refresh_pinned_message()

        await ctx.send(
            f"🎙️ Your cover credit for **{song['title']} — "
            f"{song['artist']}** has been recorded!"
        )

    @bot.command(name="coverremove")
    async def coverremove(
        ctx: commands.Context,
        number: int,
    ):
        """Remove a song from the active queue."""

        if not is_moderator(ctx):
            await ctx.send("You need moderator permissions to do that.")
            return

        song = get_song_by_number("to_cover", number)

        if not song:
            await ctx.send("That queue number doesn't exist.")
            return

        manager.storage.remove_song(song["id"])
        await manager.refresh_pinned_message()

        await ctx.send(
            f"Removed **{song['title']} — {song['artist']}** "
            "from the active queue."
        )

    @bot.command(name="coverrestore")
    async def coverrestore(
        ctx: commands.Context,
        number: int,
    ):
        """Restore a completed song to the active queue."""

        if not is_moderator(ctx):
            await ctx.send("You need moderator permissions to do that.")
            return

        song = get_song_by_number("completed", number)

        if not song:
            await ctx.send("That completed song number doesn't exist.")
            return

        manager.storage.restore_song(song["id"])
        await manager.refresh_pinned_message()

        await ctx.send(
            f"Restored **{song['title']} — {song['artist']}** "
            "to To Be Covered."
        )

    @bot.command(name="coverapprove")
    async def coverapprove(
        ctx: commands.Context,
        number: int,
    ):
        """Approve a submission awaiting moderator review."""

        if not is_moderator(ctx):
            await ctx.send("You need moderator permissions to do that.")
            return

        # First, check songs already identified but awaiting review.
        pending_songs = manager.storage.get_songs("review")

        # Then check submissions that still need song identification.
        submissions = manager.storage.data.get(
            "review_submissions",
            [],
        )

        pending_submissions = [
            item for item in submissions
            if item.get("status") == "review"
        ]

        total_pending = len(pending_songs) + len(pending_submissions)

        if number < 1 or number > total_pending:
            await ctx.send(
                f"Invalid review number. There are {total_pending} submissions awaiting review."
            )
            return

        # Approve an identified song.
        if number <= len(pending_songs):
            song = pending_songs[number - 1]

            song["status"] = "to_cover"
            manager.storage.save()

            await manager.refresh_pinned_message()

            await ctx.send(
                f"Approved **{song['title']} — {song['artist']}**."
            )
            return

        # Handle submissions that still need song identification.
        submission_number = number - len(pending_songs)
        submission = pending_submissions[submission_number - 1]

        candidate = detect_recommendation(submission["content"])

        if (
            candidate is None
            or not candidate.title
            or not candidate.artist
        ):
            await ctx.send(
                "This submission still needs song identification. "
                "AI extraction will be added in a later step."
            )
            return

        result = await manager.verifier.verify(candidate)

        song, _ = manager.storage.add_song(
            title=result.title or candidate.title,
            artist=result.artist or candidate.artist,
            user_id=submission["user_id"],
            display_name=submission["display_name"],
            source=candidate.source or "text",
            source_url=candidate.source_url,
            canonical_id=result.canonical_id,
            message_id=submission["message_id"],
            status="to_cover",
        )

        song["status"] = "to_cover"

        submission["status"] = "approved"
        submission["approved_song_id"] = song["id"]

        manager.storage.save()
        await manager.refresh_pinned_message()

        await ctx.send(
            f"Approved **{song['title']} — {song['artist']}**."
        )

    @bot.command(name="coverreject")
    async def coverreject(
        ctx: commands.Context,
        number: int,
    ):
        """Reject a submission awaiting review."""

        if not is_moderator(ctx):
            await ctx.send("You need moderator permissions to do that.")
            return

        pending_songs = manager.storage.get_songs("review")

        submissions = manager.storage.data.get(
            "review_submissions",
            [],
        )

        pending_submissions = [
            item for item in submissions
            if item.get("status") == "review"
        ]

        total_pending = len(pending_songs) + len(pending_submissions)

        if number < 1 or number > total_pending:
            await ctx.send(
                f"Invalid review number. There are {total_pending} submissions awaiting review."
            )
            return

        if number <= len(pending_songs):
            song = pending_songs[number - 1]
            song["status"] = "rejected"
        else:
            submission_number = number - len(pending_songs)
            submission = pending_submissions[submission_number - 1]
            submission["status"] = "rejected"

        manager.storage.save()

        await ctx.send("Submission rejected.")

    @bot.command(name="coverrefresh")
    async def coverrefresh(ctx: commands.Context):
        """Rebuild the pinned queue message."""

        if not is_moderator(ctx):
            await ctx.send("You need moderator permissions to do that.")
            return

        await manager.refresh_pinned_message()
        await ctx.send("The pinned Cover Queue has been refreshed.")

    @bot.command(name="coverstats")
    async def coverstats(ctx: commands.Context):
        """Display Cover Queue statistics."""

        if not is_moderator(ctx):
            await ctx.send("You need moderator permissions to do that.")
            return

        to_cover = manager.storage.get_songs("to_cover")
        completed = manager.storage.get_songs("completed")
        removed = manager.storage.get_songs("removed")
        
        review_songs = manager.storage.get_songs("review")

        review_submissions = [
            item for item in manager.storage.data.get(
                "review_submissions",
                [],
            )
            if item.get("status") == "review"
        ]

        review_count = len(review_songs) + len(review_submissions)

        embed = discord.Embed(
            title="Cover Queue Statistics",
            color=discord.Color.from_str("#C033DD"),
        )

        embed.add_field(
            name="To Be Covered",
            value=str(len(to_cover)),
            inline=True,
        )

        embed.add_field(
            name="Completed",
            value=str(len(completed)),
            inline=True,
        )

        embed.add_field(
            name="Needs Review",
            value=str(review_count),
            inline=True,
        )

        embed.add_field(
            name="Removed",
            value=str(len(removed)),
            inline=True,
        )

        await ctx.send(embed=embed)

    print("Cover Queue commands registered.")
