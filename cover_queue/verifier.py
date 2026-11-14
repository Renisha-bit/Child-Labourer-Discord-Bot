
import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .detector import SongCandidate


MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording/"

# MusicBrainz requests should be limited to approximately one per second.
REQUEST_INTERVAL = 1.1

# Require a strong metadata match before automatic confirmation.
MINIMUM_MATCH_SCORE = 90


@dataclass
class VerificationResult:
    verified: bool
    title: str | None
    artist: str | None
    canonical_id: str | None
    score: int
    needs_review: bool
    reason: str


class MusicVerifier:
    """Verifies song candidates using MusicBrainz metadata."""

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self._request_lock = asyncio.Lock()
        self._last_request_time = 0.0

    async def start(self) -> None:
        """Create the HTTP session."""

        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=15)

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "ChildLabourerDiscordBot/1.0 "
                    "(Discord music recommendation queue)"
                },
            )

    async def close(self) -> None:
        """Close the HTTP session."""

        if self.session and not self.session.closed:
            await self.session.close()

    async def _rate_limit(self) -> None:
        """Ensure requests are spaced out."""

        async with self._request_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()

            elapsed = now - self._last_request_time
            wait_time = REQUEST_INTERVAL - elapsed

            if wait_time > 0:
                await asyncio.sleep(wait_time)

            self._last_request_time = loop.time()

    @staticmethod
    def _artist_name(recording: dict[str, Any]) -> str:
        """Extract artist names from MusicBrainz artist credits."""

        credits = recording.get("artist-credit", [])
        artists = []

        for credit in credits:
            if isinstance(credit, dict):
                artist = credit.get("artist", {})
                name = artist.get("name")

                if name:
                    artists.append(name)

        return ", ".join(artists)

    async def _search_recording(
        self,
        title: str,
        artist: str,
    ) -> dict[str, Any] | None:
        """Search MusicBrainz for a recording."""

        if self.session is None or self.session.closed:
            await self.start()

        session = self.session

        if session is None or session.closed:
            logging.error("MusicBrainz HTTP session could not be initialized.")
            return None

        await self._rate_limit()

        params = {
            "query": f'recording:"{title}" AND artist:"{artist}"',
            "fmt": "json",
            "limit": "5",
        }

        try:
            async with session.get(
                MUSICBRAINZ_URL,
                    params=params,
            ) as response:

                if response.status == 503:
                    logging.warning(
                        "MusicBrainz rate limit reached."
                    )
                    return None

                response.raise_for_status()
                data = await response.json()

                recordings = data.get("recordings", [])

                if not recordings:
                    return None

                return recordings[0]

        except (aiohttp.ClientError, asyncio.TimeoutError):
            logging.exception(
                "MusicBrainz recording search failed."
            )
            return None

        except (ValueError, TypeError):
            logging.exception(
                "MusicBrainz returned invalid JSON."
            )
            return None

    async def verify(
        self,
        candidate: SongCandidate,
    ) -> VerificationResult:
        """
        Verify a detected song candidate.

        Music links without extracted metadata remain unverified.
        """

        if not candidate.title or not candidate.artist:
            return VerificationResult(
                verified=False,
                title=candidate.title,
                artist=candidate.artist,
                canonical_id=None,
                score=0,
                needs_review=True,
                reason=(
                    "Song details could not be extracted from the "
                    "submission. AI or moderator review is required."
                ),
            )

        recording = await self._search_recording(
            candidate.title,
            candidate.artist,
        )

        if recording is None:
            return VerificationResult(
                verified=False,
                title=candidate.title,
                artist=candidate.artist,
                canonical_id=None,
                score=0,
                needs_review=True,
                reason=(
                    "No reliable MusicBrainz match was found, "
                    "or the metadata service was unavailable."
                ),
            )

        score = int(recording.get("score", 0))
        verified_title = recording.get("title")
        verified_artist = self._artist_name(recording)
        canonical_id = recording.get("id")

        if score < MINIMUM_MATCH_SCORE:
            return VerificationResult(
                verified=False,
                title=verified_title or candidate.title,
                artist=verified_artist or candidate.artist,
                canonical_id=canonical_id,
                score=score,
                needs_review=True,
                reason=(
                    f"Metadata match score was {score}; "
                    "moderator approval is required."
                ),
            )

        return VerificationResult(
            verified=True,
            title=verified_title or candidate.title,
            artist=verified_artist or candidate.artist,
            canonical_id=canonical_id,
            score=score,
            needs_review=False,
            reason="Strong MusicBrainz metadata match.",
        )
