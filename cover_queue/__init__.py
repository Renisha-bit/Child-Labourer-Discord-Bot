from .manager import CoverQueueManager
from .commands import register_cover_commands
from .storage import CoverQueueStorage
from .detector import detect_recommendation, SongCandidate
from .verifier import MusicVerifier, VerificationResult

__all__ = [
    "CoverQueueManager",
    "register_cover_commands",
    "CoverQueueStorage",
    "detect_recommendation",
    "SongCandidate",
    "MusicVerifier",
    "VerificationResult",
]
