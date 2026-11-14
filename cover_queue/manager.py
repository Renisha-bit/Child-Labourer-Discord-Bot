
import asyncio
import logging

from datetime import datetime, timezone
from typing import Any

import discord

from .detector import detect_recommendation
from .storage import CoverQueueStorage
from .verifier import MusicVerifier


SCAN_INTERVAL = 1800  # 30 minutes
MAX_MESSAGE_LENGTH = 1900


class CoverQueueManager:
    """Coordinates the Cover Queue system."""

    def __init__(
        self,
        bot: discord.Client,
        channel_id: int,
        singer_role_id: int,
        storage: CoverQueueStorage | None = None,
    ):
        self.bot = bot
        self.channel_id = channel_id
        self.singer_role_id = singer_role_id

        self.storage = storage or CoverQueueStorage()
        self.verifier = MusicVerifier()

        self._scan_lock = asyncio.Lock()
        self._background_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the verifier and background scanning task."""

        await self.verifier.start()

        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(
                self._background_scanner()
            )

        logging.info("Cover Queue manager started.")

    async def close(self) -> None:
        """Stop background work and close the verifier."""

        if self._background_task and not self._background_task.done():
            self._background_task.cancel()

            try:
                await self._background_task
            except asyncio.CancelledError:
                pass

        await self.verifier.close()

    async def get_channel(self) -> discord.TextChannel | None:
        """Retrieve the configured recommendation channel."""

        channel = self.bot.get_channel(self.channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except discord.NotFound:
                logging.error("Cover Queue channel was not found.")
                return None
            except discord.Forbidden:
                logging.error("Cannot access Cover Queue channel.")
                return None
            except discord.HTTPException:
                logging.exception("Could not fetch Cover Queue channel.")
                return None

        if not isinstance(channel, discord.TextChannel):
            logging.error("Cover Queue destination is not a text channel.")
            return None

        return channel

    def get_numbered_songs(
        self,
        status: str,
    ) -> list[dict[str, Any]]:
        """Return songs with their current visible numbering."""

        songs = self.storage.get_songs(status)

        return [
            {"number": index, "song": song}
            for index, song in enumerate(songs, start=1)
        ]

    async def on_message(self, message: discord.Message) -> None:
        """Process new messages in the recommendation channel."""

        if message.author.bot:
            return

        if message.channel.id != self.channel_id:
            return

        if message.content.strip().startswith("."):
            return

        await self.process_message(message)

    async def process_message(
        self,
        message: discord.Message,
    ) -> None:
        """Detect and process one recommendation message."""

        async with self._scan_lock:
            try:
                await self._process_message(message)
            except Exception:
                logging.exception(
                    "Could not process cover recommendation message %s.",
                    message.id,
                )

    async def _process_message(
        self,
        message: discord.Message,
        refresh: bool = True,
    ) -> None:
        candidate = detect_recommendation(message.content)

        if candidate is None:
            return

        # Link-only submissions need title/artist extraction.
        # Preserve them for the later AI/moderator review workflow.
        if not candidate.title or not candidate.artist:
            self._save_review_submission(message, candidate.reason)

            if refresh:
                await self.refresh_pinned_message()
            return

        result = await self.verifier.verify(candidate)

        status = "to_cover" if result.verified else "review"

        song, is_new = self.storage.add_song(
            title=result.title or candidate.title,
            artist=result.artist or candidate.artist,
            user_id=message.author.id,
            display_name=message.author.display_name,
            source=candidate.source or "text",
            source_url=candidate.source_url,
            canonical_id=result.canonical_id,
            message_id=message.id,
            status=status,
        )

        # A previously uncertain song can become confirmed
        # if a later submission produces a strong match.
        if result.verified and song.get("status") == "review":
            song["status"] = "to_cover"
            self.storage.save()

        if not result.verified:
            logging.info(
                "Song submitted for review: %s - %s (score: %s)",
                result.title,
                result.artist,
                result.score,
            )

        elif is_new:
            logging.info(
                "Verified song added: %s - %s",
                result.title,
                result.artist,
            )
        if refresh:
            await self.refresh_pinned_message()

    def _save_review_submission(
        self,
        message: discord.Message,
        reason: str,
    ) -> None:
        """Preserve submissions that still need song identification."""

        submissions = self.storage.data.setdefault(
            "review_submissions",
            [],
        )

        if any(
            item.get("message_id") == message.id
            for item in submissions
        ):
            return

        submissions.append({
            "message_id": message.id,
            "user_id": message.author.id,
            "display_name": message.author.display_name,
            "content": message.content,
            "reason": reason,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "status": "review",
        })

        self.storage.save()

        logging.info(
            "Unidentified song submission saved for review: %s",
            message.id,
        )

    async def initial_scan(self) -> None:
        """Scan existing channel history on first installation."""

        if self.storage.data.get("initialized"):
            return

        channel = await self.get_channel()

        if channel is None:
            return

        logging.info("Starting initial Cover Queue history scan.")

        async with self._scan_lock:
            try:
                async for message in channel.history(
                    limit=None,
                    oldest_first=True,
                ):
                    if not message.author.bot:
                        await self._process_message(message, refresh=False)

                    self.storage.set_scan_progress(message.id)

                self.storage.set_initialized()

                logging.info("Initial Cover Queue scan completed.")

            except discord.Forbidden:
                logging.error(
                    "Missing permission to read Cover Queue channel history."
                )
                return

            except discord.HTTPException:
                logging.exception("Initial Cover Queue scan failed.")
                return

        await self.refresh_pinned_message()

    async def backup_scan(self) -> None:
        """Scan messages newer than the last saved message ID."""

        channel = await self.get_channel()

        if channel is None:
            return

        last_id = int(
            self.storage.data.get("last_scanned_message_id", 0)
        )

        if last_id <= 0:
            await self.initial_scan()
            return

        async with self._scan_lock:
            try:
                async for message in channel.history(
                    limit=None,
                    after=discord.Object(id=last_id),
                    oldest_first=True,
                ):
                    if not message.author.bot:
                        await self._process_message(message, refresh=False)

                    self.storage.set_scan_progress(message.id)

            except discord.Forbidden:
                logging.error(
                    "Missing permission to scan Cover Queue history."
                )

            except discord.HTTPException:
                logging.exception("Cover Queue backup scan failed.")

        await self.refresh_pinned_message()

    async def _background_scanner(self) -> None:
        """Run initial and periodic scans."""

        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                if not self.storage.data.get("initialized"):
                    await self.initial_scan()
                else:
                    await self.backup_scan()

            except asyncio.CancelledError:
                raise

            except Exception:
                logging.exception(
                    "Cover Queue background scanner encountered an error."
                )

            await asyncio.sleep(SCAN_INTERVAL)

    @staticmethod
    def _recommender_text(song: dict[str, Any]) -> str:
        recommenders = song.get("recommenders", [])

        mentions = [
            f"<@{item['user_id']}>"
            for item in recommenders
        ]

        if not mentions:
            return "Recommended by: Unknown"

        return "Recommended by " + " & ".join(mentions)

    @staticmethod
    def _completion_text(song: dict[str, Any]) -> list[str]:
        lines = []

        for completion in song.get("completions", []):
            timestamp = completion.get("completed_at", "")
            cover_url = completion.get("cover_url")

            try:
                date = datetime.fromisoformat(
                    timestamp
                ).strftime("%d %b %Y")
            except (ValueError, TypeError):
                date = "Date unavailable"

            line = (
                f"• <@{completion['user_id']}> — {date}"
            )

            if cover_url:
                line += f" · [Listen]({cover_url})"

            lines.append(line)

        return lines

    def _build_queue_pages(self) -> list[str]:
        """Build pinned message content without exceeding Discord limits."""

        to_cover = self.get_numbered_songs("to_cover")
        completed = self.get_numbered_songs("completed")

        lines = [
            "✦ TO BE COVERED",
            "",
        ]

        if not to_cover:
            lines.append("No songs in the queue yet.")
            lines.append("")

        for item in to_cover:
            song = item["song"]

            lines.append(
                f"**{item['number']}. {song['title']} — {song['artist']}**"
            )
            lines.append(self._recommender_text(song))
            lines.append("")

        lines.extend([
            "✦ ───────────────────────── ✦",
            "",
            "✦ COMPLETED COVERS",
            "",
        ])

        if not completed:
            lines.append("No completed covers yet.")
            lines.append("")

        for item in completed:
            song = item["song"]

            lines.append(
                f"**{item['number']}. {song['title']} — {song['artist']}**"
            )

            lines.extend(self._completion_text(song))
            lines.append(self._recommender_text(song))
            lines.append("")

        # Split long queues into multiple messages.
        pages = []
        current_page = ""

        for line in lines:
            addition = line + "\n"

            if (
                current_page
                and len(current_page) + len(addition) > MAX_MESSAGE_LENGTH
            ):
                pages.append(current_page.rstrip())
                current_page = ""

            current_page += addition

        if current_page:
            pages.append(current_page.rstrip())

        return pages or ["🎶 The Cover Queue is currently empty."]

    async def refresh_pinned_message(self) -> None:
        """Update the maintained pinned queue messages."""

        channel = await self.get_channel()

        if channel is None:
            return

        pages = self._build_queue_pages()
        saved_ids = list(
            self.storage.data.get("pinned_message_ids", [])
        )

        updated_ids = []

        for index, content in enumerate(pages):
            message = None

            if index < len(saved_ids):
                try:
                    message = await channel.fetch_message(saved_ids[index])
                except discord.NotFound:
                    message = None
                except discord.Forbidden:
                    logging.error(
                        "Cannot access the existing pinned queue message."
                    )
                    return
                except discord.HTTPException:
                    logging.exception(
                        "Could not retrieve pinned queue message."
                    )
                    return

            try:
                if message is not None:
                    await message.edit(
                        content=content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    message = await channel.send(
                        content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )

                    await message.pin(
                        reason="Maintained Cover Queue message."
                    )

                updated_ids.append(message.id)

            except discord.Forbidden:
                logging.error(
                    "Bot needs Send Messages and Manage Messages "
                    "permissions to maintain the Cover Queue."
                )
                return

            except discord.HTTPException:
                logging.exception(
                    "Could not update the pinned Cover Queue."
                )
                return

        # Remove old extra pages if the queue becomes shorter.
        for old_id in saved_ids[len(pages):]:
            try:
                old_message = await channel.fetch_message(old_id)
                await old_message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logging.exception(
                    "Could not remove an outdated queue page."
                )

        self.storage.set_pinned_message_ids(updated_ids)

        logging.info("Pinned Cover Queue message refreshed.")
