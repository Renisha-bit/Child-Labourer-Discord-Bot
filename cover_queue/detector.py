
import re
from dataclasses import dataclass
from urllib.parse import urlparse


SUPPORTED_DOMAINS = {
    "open.spotify.com": "spotify",
    "spotify.com": "spotify",
    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "youtu.be": "youtube",
    "m.youtube.com": "youtube",
    "soundcloud.com": "soundcloud",
    "www.soundcloud.com": "soundcloud",
    "on.soundcloud.com": "soundcloud",
}

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

# Examples:
# Song Title - Artist
# Song Title by Artist
# Artist - Song Title
TITLE_ARTIST_PATTERNS = [
    re.compile(
        r"^\s*(?P<title>.+?)\s+-\s+(?P<artist>.+?)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?P<title>.+?)\s+by\s+(?P<artist>.+?)\s*$",
        re.IGNORECASE,
    ),
]

COMMAND_PATTERN = re.compile(r"^\s*\.")


@dataclass
class SongCandidate:
    title: str | None
    artist: str | None
    source_url: str | None
    source: str | None
    confidence: str
    needs_review: bool
    reason: str


def extract_urls(content: str) -> list[str]:
    """Extract HTTP(S) URLs from a message."""

    return URL_PATTERN.findall(content)


def identify_source(url: str) -> str | None:
    """Identify supported music platforms."""

    try:
        hostname = urlparse(url).hostname

        if not hostname:
            return None

        hostname = hostname.lower()

        for domain, platform in SUPPORTED_DOMAINS.items():
            if hostname == domain or hostname.endswith("." + domain):
                return platform

    except ValueError:
        return None

    return None


def extract_plain_text_song(content: str) -> tuple[str, str] | None:
    """Extract a title and artist from simple text formats."""

    cleaned = content.strip()

    for pattern in TITLE_ARTIST_PATTERNS:
        match = pattern.match(cleaned)

        if match:
            title = match.group("title").strip()
            artist = match.group("artist").strip()

            if title and artist:
                return title, artist

    return None


def detect_recommendation(content: str) -> SongCandidate | None:
    """
    Detect a possible song recommendation.

    This is a lightweight first-pass detector.
    Actual song verification and AI extraction happen separately.
    """

    content = content.strip()

    if not content:
        return None

    # Ignore bot commands.
    if COMMAND_PATTERN.match(content):
        return None

    urls = extract_urls(content)

    # Check for supported music links.
    supported_links = [
        (url, identify_source(url))
        for url in urls
    ]

    supported_links = [
        (url, source)
        for url, source in supported_links
        if source is not None
    ]

    if supported_links:
        url, source = supported_links[0]

        # Links alone do not reliably provide title/artist information.
        # The verifier/AI extraction stage will handle that.
        return SongCandidate(
            title=None,
            artist=None,
            source_url=url,
            source=source,
            confidence="low",
            needs_review=True,
            reason="Music link detected; track details need extraction.",
        )

    # Remove unrelated URLs before trying plain-text extraction.
    text_without_urls = URL_PATTERN.sub("", content).strip()

    if not text_without_urls:
        return None

    # Ignore messages containing unrelated URLs.
    if urls:
        return None

    song = extract_plain_text_song(text_without_urls)

    if song:
        title, artist = song

        return SongCandidate(
            title=title,
            artist=artist,
            source_url=None,
            source="text",
            confidence="medium",
            needs_review=True,
            reason="Possible title and artist match; metadata verification required.",
        )

    return None
