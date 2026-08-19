"""Pure functions for Neuro ICU card TTS synchronization."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

V1_MARKER_RE = re.compile(r"<!--\s*neuroicu-tts:v1:([0-9a-f]{64})\s*-->", re.I)
V2_MARKER_RE = re.compile(
    r"<!--\s*neuroicu-tts:v2:([0-9a-f]{64}):([0-9a-f]{64})\s*-->", re.I
)
MARKER_RE = re.compile(
    r"<!--\s*neuroicu-tts:(?:v1:[0-9a-f]{64}|v2:[0-9a-f]{64}:[0-9a-f]{64})\s*-->",
    re.I,
)
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]", re.I)
MANAGED_BLOCK_RE = re.compile(
    r"\s*<!--\s*neuroicu-tts:(?:v1:([0-9a-f]{64})|v2:([0-9a-f]{64}):([0-9a-f]{64}))\s*-->\s*(?:\[sound:([^\]]+)\])?",
    re.I,
)
MANAGED_FILENAME_RE = re.compile(r"^neuroicu_tts_(\d+)-([0-9a-f]{64})\.mp3$", re.I)
IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class TTSState:
    text: str
    digest: str
    marker_digest: str | None
    sound_filename: str | None
    profile_digest: str | None = None


def source_text(extra: str) -> str:
    """Return stable speech text from Extra, excluding managed metadata."""
    value = MARKER_RE.sub("", extra or "")
    value = SOUND_RE.sub("", value)
    value = IMG_RE.sub(" ", value)
    value = html.unescape(value)
    value = TAG_RE.sub(" ", value)
    value = value.replace("\u00a0", " ")
    return SPACE_RE.sub(" ", value).strip()


def digest_for(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generation_digest(source_digest: str, profile_digest: str) -> str:
    """Identify audio by both spoken content and synthesis configuration."""
    return digest_for(f"{source_digest}:{profile_digest}")


def state_for(extra: str) -> TTSState:
    text = source_text(extra)
    managed = MANAGED_BLOCK_RE.search(extra or "")
    marker_digest = (managed.group(2) or managed.group(1)).lower() if managed else None
    profile_digest = managed.group(3).lower() if managed and managed.group(3) else None
    sound_filename = managed.group(4).strip() if managed and managed.group(4) else None
    return TTSState(text, digest_for(text), marker_digest, sound_filename, profile_digest)


def managed_extra(extra: str, filename: str, digest: str, profile_digest: str | None = None) -> str:
    """Replace only the add-on-owned marker/audio block.

    User-authored sound tags are deliberately preserved.
    """
    value = MANAGED_BLOCK_RE.sub("", extra or "").rstrip()
    marker = f"v2:{digest}:{profile_digest}" if profile_digest else f"v1:{digest}"
    suffix = f"\n\n<!-- neuroicu-tts:{marker} --> [sound:{filename}]"
    return value + suffix


def filename(note_id: int, digest: str) -> str:
    return f"neuroicu_tts_{note_id}-{digest}.mp3"


def needs_update(extra: str, profile_digest: str | None = None) -> bool:
    """Return whether managed audio is absent or no longer matches the text.

    The note id is intentionally not part of this decision: callers that only
    have card content must not treat an otherwise valid managed filename as
    stale merely because it belongs to a different note id.
    """
    state = state_for(extra)
    if not state.text:
        return False
    if state.marker_digest != state.digest or not state.sound_filename:
        return True
    if profile_digest is not None and state.profile_digest != profile_digest:
        return True
    match = MANAGED_FILENAME_RE.fullmatch(state.sound_filename)
    if not match:
        return True
    expected_digest = (
        generation_digest(state.digest, state.profile_digest)
        if state.profile_digest
        else state.digest
    )
    return match.group(2).lower() != expected_digest
