import json
import logging
import os
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
QUEUE_FILE = DATA_DIR / "cover_queue.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CoverQueueStorage:
    """Handles persistent storage for the Cover Queue."""

    def __init__(self, file_path: Path = QUEUE_FILE):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "initialized": False,
            "last_scanned_message_id": 0,
            "pinned_message_ids": [],
            "songs": [],
            "next_song_id": 1,
        }

    def _load(self) -> dict[str, Any]:
        if not self.file_path.exists():
            return self._default_data()

        try:
            with self.file_path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)

            if not isinstance(loaded, dict):
                raise ValueError("Cover queue data must be a JSON object.")

            defaults = self._default_data()
            defaults.update(loaded)

            if not isinstance(defaults["songs"], list):
                raise ValueError("Cover queue songs must be a list.")

            if not isinstance(defaults["pinned_message_ids"], list):
                raise ValueError("Pinned message IDs must be a list.")

            return defaults

        except (json.JSONDecodeError, OSError, ValueError):
            logging.exception("Could not load cover queue data.")

            backup_path = self.file_path.with_suffix(".corrupt.json")

            try:
                if self.file_path.exists():
                    self.file_path.replace(backup_path)
                    logging.warning(
                        "Corrupted queue data backed up to %s",
                        backup_path,
                    )
            except OSError:
                logging.exception("Could not back up corrupted queue data.")

            return self._default_data()

    def save(self) -> None:
        """Atomically save queue data to JSON."""

        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.file_path.parent,
                delete=False,
                suffix=".tmp",
            ) as file:
                json.dump(
                    self.data,
                    file,
                    indent=4,
                    ensure_ascii=False,
                )
                file.flush()
                os.fsync(file.fileno())
                temp_path = Path(file.name)

            temp_path.replace(self.file_path)

        except OSError:
            logging.exception("Could not save cover queue data.")

            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

            raise

    def get_songs(self, status: str | None = None) -> list[dict[str, Any]]:
        songs = self.data["songs"]

        if status is None:
            return songs

        return [
            song for song in songs
            if song.get("status") == status
        ]

    def get_song(self, song_id: int) -> dict[str, Any] | None:
        for song in self.data["songs"]:
            if song.get("id") == song_id:
                return song

        return None

    def find_duplicate(
        self,
        canonical_id: str | None = None,
        title: str | None = None,
        artist: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an existing song using its verified ID or metadata."""

        normalized_title = (title or "").strip().casefold()
        normalized_artist = (artist or "").strip().casefold()

        for song in self.data["songs"]:
            if song.get("status") == "removed":
                continue

            if canonical_id and song.get("canonical_id") == canonical_id:
                return song

            if (
                normalized_title
                and normalized_artist
                and song.get("title", "").strip().casefold() == normalized_title
                and song.get("artist", "").strip().casefold() == normalized_artist
            ):
                return song

        return None

    def add_recommender(
        self,
        song: dict[str, Any],
        user_id: int,
        display_name: str,
        message_id: int | None = None,
    ) -> bool:
        """Add a recommender without duplicating their credit."""

        recommenders = song.setdefault("recommenders", [])

        if any(
            recommender.get("user_id") == user_id
            for recommender in recommenders
        ):
            return False

        recommenders.append({
            "user_id": user_id,
            "display_name": display_name,
            "message_id": message_id,
            "timestamp": utc_now(),
        })

        self.save()
        return True

    def add_song(
        self,
        title: str,
        artist: str,
        user_id: int,
        display_name: str,
        source: str = "manual",
        source_url: str | None = None,
        canonical_id: str | None = None,
        message_id: int | None = None,
        status: str = "review",
    ) -> tuple[dict[str, Any], bool]:
        """Add a song or credit an existing recommendation."""

        duplicate = self.find_duplicate(
            canonical_id=canonical_id,
            title=title,
            artist=artist,
        )

        if duplicate:
            self.add_recommender(
                duplicate,
                user_id,
                display_name,
                message_id,
            )

            if source_url:
                links = duplicate.setdefault("source_links", [])

                if source_url not in links:
                    links.append(source_url)
                    self.save()

            return duplicate, False

        song = {
            "id": self.data["next_song_id"],
            "title": title.strip(),
            "artist": artist.strip(),
            "canonical_id": canonical_id,
            "source": source,
            "source_links": [source_url] if source_url else [],
            "first_submitted_at": utc_now(),
            "first_message_id": message_id,
            "recommenders": [{
                "user_id": user_id,
                "display_name": display_name,
                "message_id": message_id,
                "timestamp": utc_now(),
            }],
            "status": status,
            "completions": [],
        }

        self.data["songs"].append(song)
        self.data["next_song_id"] += 1
        self.save()

        return song, True

    def mark_completed(
        self,
        song_id: int,
        user_id: int,
        display_name: str,
        cover_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a singer's cover without duplicating that singer's entry."""

        song = self.get_song(song_id)

        if not song:
            return None

        if song.get("status") not in ("to_cover", "completed"):
            return None

        completions = song.setdefault("completions", [])

        if any(
            completion.get("user_id") == user_id
            for completion in completions
        ):
            return song

        completions.append({
            "user_id": user_id,
            "display_name": display_name,
            "cover_url": cover_url,
            "completed_at": utc_now(),
        })

        song["status"] = "completed"
        self.save()

        return song

    def restore_song(self, song_id: int) -> dict[str, Any] | None:
        song = self.get_song(song_id)

        if not song or song.get("status") != "completed":
            return None

        song["status"] = "to_cover"
        self.save()

        return song

    def remove_song(self, song_id: int) -> dict[str, Any] | None:
        song = self.get_song(song_id)

        if not song:
            return None

        song["status"] = "removed"
        self.save()

        return song

    def set_scan_progress(self, message_id: int) -> None:
        self.data["last_scanned_message_id"] = message_id
        self.save()

    def set_initialized(self) -> None:
        self.data["initialized"] = True
        self.save()

    def set_pinned_message_ids(self, message_ids: list[int]) -> None:
        self.data["pinned_message_ids"] = message_ids
        self.save()

    def get_next_song_id(self) -> int:
        return int(self.data["next_song_id"])
