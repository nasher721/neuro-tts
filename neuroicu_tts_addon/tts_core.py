"""Pure functions for Neuro ICU card TTS synchronization."""

from __future__ import annotations

import functools
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
    r"\s*<!--\s*neuroicu-tts:(?:v1:([0-9a-f]{64})|v2:([0-9a-f]{64}):([0-9a-f]{64}))\s*-->"
    r"(?:\s*(?:\[sound:([^\]]+)\]|<div\b[^>]*\bclass=[\"']neuroicu-tts-player[\"'][^>]*>.*?<audio\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>.*?</audio>.*?</div>))?",
    re.I | re.DOTALL,
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
    if not extra:
        return ""
    value = extra
    if "<!--" in value:
        value = MANAGED_BLOCK_RE.sub("", value)
        if "<!--" in value:
            value = MARKER_RE.sub("", value)
    if "[sound:" in value:
        value = SOUND_RE.sub("", value)
    if "<img" in value or "<IMG" in value:
        value = IMG_RE.sub(" ", value)
    if "&" in value:
        value = html.unescape(value)
    if "<" in value:
        value = TAG_RE.sub(" ", value)
    if "\u00a0" in value:
        value = value.replace("\u00a0", " ")
    return SPACE_RE.sub(" ", value).strip()


@functools.lru_cache(maxsize=4096)
def digest_for(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=4096)
def generation_digest(source_digest: str, profile_digest: str) -> str:
    """Identify audio by both spoken content and synthesis configuration."""
    return digest_for(f"{source_digest}:{profile_digest}")


@functools.lru_cache(maxsize=4096)
def state_for(extra: str) -> TTSState:
    if not extra:
        return TTSState("", digest_for(""), None, None, None)
    text = source_text(extra)
    managed = MANAGED_BLOCK_RE.search(extra) if "<!--" in extra else None
    marker_digest = (managed.group(2) or managed.group(1)).lower() if managed else None
    profile_digest = managed.group(3).lower() if managed and managed.group(3) else None
    sound_filename = None
    if managed:
        raw_name = managed.group(5) or managed.group(4)
        if raw_name:
            sound_filename = raw_name.strip()
    return TTSState(text, digest_for(text), marker_digest, sound_filename, profile_digest)


def is_managed_filename(name: str | None) -> bool:
    """Return whether *name* is a filename this add-on generated.

    Used before reusing a name parsed out of note HTML, so that hand-edited or
    hostile ``src`` values are never written back into a note or resolved
    against the media folder.
    """
    return bool(name) and MANAGED_FILENAME_RE.fullmatch(str(name).strip()) is not None


def is_legacy_extra(extra: str) -> bool:
    """Return True if extra contains the legacy [sound:...] managed block instead of the click-to-play widget."""
    if not extra:
        return False
    managed = MANAGED_BLOCK_RE.search(extra)
    if not managed:
        return False
    return bool(managed.group(4))


def player_html(filename: str) -> str:
    """Return the accessible, responsive click-to-play HTML player widget with animated equalizer and speed switcher.

    *filename* is escaped because the legacy-upgrade path reuses a name parsed
    out of existing note HTML rather than one this add-on generated.
    """
    filename = html.escape(str(filename), quote=True)
    return (
        f'<div class="neuroicu-tts-player" style="margin-top: 10px; margin-bottom: 6px; display: inline-flex; align-items: center; gap: 8px; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif;">'
        f'<audio class="neuroicu-audio" src="{filename}" preload="none" '
        f'onended="var p=this.closest(\'.neuroicu-tts-player\'); if(p){{var b=p.querySelector(\'.neuroicu-play-btn\'); if(b){{b.classList.remove(\'playing\'); b.querySelector(\'.neuroicu-btn-label\').textContent=\'Play Explanation\'; var eq=p.querySelector(\'.neuroicu-eq\'); if(eq)eq.style.display=\'none\'; var ic=p.querySelector(\'.neuroicu-icon\'); if(ic)ic.textContent=\'&#9654;\';}}}}" '
        f'onplay="var p=this.closest(\'.neuroicu-tts-player\'); if(p){{var b=p.querySelector(\'.neuroicu-play-btn\'); if(b){{b.classList.add(\'playing\'); b.querySelector(\'.neuroicu-btn-label\').textContent=\'Pause\'; var eq=p.querySelector(\'.neuroicu-eq\'); if(eq)eq.style.display=\'inline-flex\'; var ic=p.querySelector(\'.neuroicu-icon\'); if(ic)ic.textContent=\'&#9208;\';}}}}" '
        f'onpause="var p=this.closest(\'.neuroicu-tts-player\'); if(p){{var b=p.querySelector(\'.neuroicu-play-btn\'); if(b){{b.classList.remove(\'playing\'); b.querySelector(\'.neuroicu-btn-label\').textContent=\'Play Explanation\'; var eq=p.querySelector(\'.neuroicu-eq\'); if(eq)eq.style.display=\'none\'; var ic=p.querySelector(\'.neuroicu-icon\'); if(ic)ic.textContent=\'&#9654;\';}}}}"></audio>'
        f'<button type="button" class="neuroicu-play-btn" aria-label="Play Explanation Audio" onclick="var a=this.parentElement.querySelector(\'audio\'); if(!a)return false; if(a.paused){{a.play();}}else{{a.pause(); a.currentTime=0;}} return false;" style="cursor: pointer; padding: 7px 15px; border-radius: 20px; border: 1px solid rgba(59,130,246,0.35); background: rgba(59,130,246,0.14); color: inherit; font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 7px; transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1); box-shadow: 0 1px 3px rgba(0,0,0,0.06); user-select: none; -webkit-user-select: none;">'
        f'<span class="neuroicu-icon" style="font-size: 1.1em; line-height: 1;">&#9654;</span>'
        f'<span class="neuroicu-btn-label">Play Explanation</span>'
        f'<span class="neuroicu-eq" style="display: none; align-items: flex-end; gap: 2px; height: 12px; margin-left: 2px;">'
        f'<span style="display: inline-block; width: 2px; height: 100%; background: currentColor; border-radius: 1px; animation: neuroicu-wave 0.8s ease-in-out infinite alternate;"></span>'
        f'<span style="display: inline-block; width: 2px; height: 60%; background: currentColor; border-radius: 1px; animation: neuroicu-wave 0.8s ease-in-out infinite alternate 0.2s;"></span>'
        f'<span style="display: inline-block; width: 2px; height: 80%; background: currentColor; border-radius: 1px; animation: neuroicu-wave 0.8s ease-in-out infinite alternate 0.4s;"></span>'
        f'</span>'
        f'</button>'
        f'<button type="button" class="neuroicu-speed-btn" title="Change Playback Speed" aria-label="Change Playback Speed" onclick="var a=this.parentElement.querySelector(\'audio\'); if(!a)return false; var speeds=[1.0, 1.25, 1.5, 1.75, 2.0]; var cur=parseFloat(this.getAttribute(\'data-speed\')||\'1.0\'); var next=speeds[(speeds.indexOf(cur)+1)%speeds.length]; a.playbackRate=next; this.setAttribute(\'data-speed\', next); this.textContent=next+\'x\'; return false;" style="cursor: pointer; padding: 7px 11px; border-radius: 20px; border: 1px solid rgba(148,163,184,0.3); background: rgba(148,163,184,0.12); color: inherit; font-size: 12px; font-weight: 600; user-select: none; -webkit-user-select: none; transition: all 0.2s ease;">1.0x</button>'
        f'<style>@keyframes neuroicu-wave {{ 0% {{ height: 25%; opacity: 0.6; }} 100% {{ height: 100%; opacity: 1; }} }} .neuroicu-play-btn:hover {{ background: rgba(59,130,246,0.25) !important; transform: translateY(-1px); }} .neuroicu-speed-btn:hover {{ background: rgba(148,163,184,0.22) !important; transform: translateY(-1px); }} .neuroicu-play-btn:active, .neuroicu-speed-btn:active {{ transform: translateY(0); }}</style>'
        f'</div>'
    )


def managed_extra(extra: str, filename: str, digest: str, profile_digest: str | None = None, legacy_sound_tag: bool = False) -> str:
    """Replace only the add-on-owned marker/audio block.

    User-authored sound tags are deliberately preserved.
    """
    value = MANAGED_BLOCK_RE.sub("", extra or "").rstrip()
    marker = f"v2:{digest}:{profile_digest}" if profile_digest else f"v1:{digest}"
    suffix = (
        f"\n\n<!-- neuroicu-tts:{marker} --> [sound:{filename}]"
        if legacy_sound_tag
        else f"\n\n<!-- neuroicu-tts:{marker} -->\n{player_html(filename)}"
    )
    return value + suffix


def filename(note_id: int, digest: str) -> str:
    return f"neuroicu_tts_{note_id}-{digest}.mp3"


def needs_update(extra: str, profile_digest: str | None = None) -> bool:
    """Return whether managed audio is absent, stale, or uses the legacy autoplay format."""
    state = state_for(extra)
    if not state.text:
        return False
    if state.marker_digest != state.digest or not state.sound_filename:
        return True
    if profile_digest is not None and state.profile_digest != profile_digest:
        return True
    if is_legacy_extra(extra):
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
