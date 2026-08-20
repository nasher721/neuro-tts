# Neuro ICU TTS Anki add-on (F5-TTS)

This add-on synchronizes spoken audio for the `Extra` field of
`Enhanced Cloze 2.1 v2-136cb (Neuro ICU Boards / arbornasher)` notes in
`Neuro ICU Boards (AnkiHub)`.

## Install

Copy the `neuroicu_tts_addon` directory into Anki Desktop's add-ons directory,
restart Anki, and add the tag `neuroicu-tts-pilot` to 10–20 pilot notes.

Keep `pilot_only` enabled while validating a small tagged sample. You can
toggle it from the Control Center's Scope tab once the pilot succeeds — no
restart or manual file edit needed.

## F5-TTS setup

The add-on runs F5-TTS locally through its CLI using Apple Silicon MPS.
F5-TTS is installed in the isolated environment configured in `config.json`.

Safe settings (`f5_speed`, `f5_device`, `ffmpeg_path`, `pilot_tag`,
`pilot_only`) are edited in the Control Center's Settings and Scope tabs with
inline validation and atomic saves. Engine-locked keys (`f5_tts_repo`,
`f5_tts_python`, `f5_model`, `f5_ref_audio`, `f5_ref_text`, `f5_nfe_step`) are
still edited in `config.json` by hand, or set via environment variables before
launching Anki:

```sh
export F5_TTS_REPO=/path/to/F5-TTS
export F5_TTS_PYTHON=/path/to/f5-tts-venv/bin/python
```

Set `f5_ref_audio` and `f5_ref_text` to a reference recording and its exact
transcript. The shipped configuration intentionally contains no machine-local
paths and stays unavailable until these values are supplied.
The add-on converts F5-TTS WAV output to MP3 and atomically imports it into
Anki media.

## Behavior

- A single background worker keeps F5-TTS and FFmpeg off Anki's UI thread.
- Note-save and sync hooks enqueue reconciliation; an immediate scan is also
  available from the Tools menu.
- Queue state is stored in the active Anki profile and interrupted work is
  recovered on the next profile open.
- `Tools → Neuro ICU TTS Control Center` provides status, pilot scope, and queue actions.
- `Ctrl+Alt+T` opens the Control Center directly.
- `Ctrl+Alt+V` toggles playback of the explanation audio in the reviewer.
- `Ctrl+Alt+G` (or `Tools → Generate Neuro ICU TTS for Current Card`) generates audio for the active card immediately.
- `Tools → Queue Neuro ICU TTS scan` runs an immediate scan.
- Audio is staged outside Anki media, revalidated on the main thread, and then
  stored as `neuroicu_tts_<note-id>-<sha256>.mp3`.
- The filename and v2 marker identify both source text and synthesis settings,
  so changing the voice/model configuration queues regeneration.
- The `Extra` field gets one managed HTML comment and an accessible, responsive
  `<div class="neuroicu-tts-player">` click-to-play HTML5 player widget.
- Audio **never** autoplays on the front side of the card or upon answering; it
  plays **only** when the user clicks the play button or uses the replay shortcut.
- Media files in `<audio src="...">` are fully recognized by Anki media sync (AnkiWeb, AnkiMobile iOS, AnkiDroid Android).
- Unrelated user audio tags (`[sound:...]`) and note HTML are strictly preserved.
- F5-TTS and FFmpeg calls have time limits and bounded diagnostic output.
- Closing or switching profiles cancels the active synthesis subprocess and
  leaves interrupted work retryable for the next profile open.
- Failed generation or note commit leaves the existing note content intact and
  writes details to `neuroicu_tts.log`.
- Ordinary generation never deletes existing media.
- The Maintenance tab includes an "Upgrade Legacy Markers" action to instantly
  upgrade existing cards to click-to-play without re-running synthesis.

## Control Center

`Tools → Neuro ICU TTS Control Center` (or `Ctrl+Alt+T`) opens six tabs:

- **Overview** — status dashboard, recommended next action, pilot-tag editing,
  current-card generation, and collection scans.
- **Settings** — edit speed, device, ffmpeg path, and pilot tag with inline
  validation; Save/Revert/Reload. Engine settings display read-only. Saved
  safe knobs apply at the next job start; running jobs are never interrupted.
- **Queue** — read-only job counts by status.
- **Diagnostics** — engine test and the tail of `neuroicu_tts.log`.
- **Maintenance** — generated-audio storage size and clearing finished jobs.
- **Scope** — pilot-only toggle and Full-Deck Convert, which shows an impact
  estimate (note count and runtime) before enqueueing deduplicated jobs.

Configuration is versioned (`schema_version` 2): old flat `config.json` files
migrate automatically, unknown keys are preserved, and a corrupt file falls
back to defaults with a logged warning instead of breaking the add-on. Reload
detection compares file mtimes, so two saves within the same filesystem
timestamp tick may require a second reload to be noticed.
