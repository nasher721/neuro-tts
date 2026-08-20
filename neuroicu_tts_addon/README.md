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

Every setting is edited in the Control Center's Settings and Scope tabs with
inline validation and atomic saves — engine paths included, as of schema 3.
Saved engine values apply without restarting Anki. Environment variables still
take precedence when set, which keeps machine-local paths out of `config.json`:

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
- Note text is normalized for speech before synthesis: dose and unit shorthand,
  arrows and trend symbols, dosing intervals, numeric ranges and comparisons,
  and clinical abbreviations become spoken words. Cloze markup keeps the answer
  and drops the hint. See `text_normalize.py`; both passes are configurable.
- Notes longer than `max_chunk_chars` are split at sentence boundaries,
  synthesized in pieces, and concatenated with FFmpeg.
- Failed synthesis is retried with exponential backoff up to `max_attempts`
  before the note is marked `failed_terminal`; cancellations are never retried.
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
  writes details to `neuroicu_tts.log`, which rotates at 2 MB with three backups.
- Filenames parsed out of existing note HTML are validated against the managed
  filename pattern and HTML-escaped before being written back, so hand-edited
  or hostile markup cannot be reflected into a note.
- Ordinary generation never deletes existing media.
- The Maintenance tab includes an "Upgrade Legacy Markers" action to instantly
  upgrade existing cards to click-to-play without re-running synthesis.

## Control Center

`Tools → Neuro ICU TTS Control Center` (or `Ctrl+Alt+T`) opens six tabs:

- **Overview** — status dashboard, recommended next action, pilot-tag editing,
  current-card generation, and collection scans.
- **Settings** — edit speed, device, ffmpeg path, and pilot tag; the speech
  shaping and queue-reliability knobs; and the engine paths, model, reference
  voice, and NFE steps. Inline validation with Save/Revert/Reload. Saved values
  apply at the next job start; running jobs are never interrupted.
- **Queue** — job counts by status and progress, plus Pause/Resume (the running
  job finishes first), Retry failed (re-queues failed and cancelled notes from
  their current text), and Cancel pending.
- **Diagnostics** — engine test and the tail of `neuroicu_tts.log`.
- **Maintenance** — generated-audio storage size and clearing finished jobs.
- **Scope** — pilot-only toggle and Full-Deck Convert, which shows an impact
  estimate (note count and runtime) before enqueueing deduplicated jobs.

Configuration is versioned (`schema_version` 3): older `config.json` files
migrate automatically, unknown keys are preserved, and a corrupt file falls
back to defaults with a logged warning instead of breaking the add-on. Reload
detection compares file mtimes, so two saves within the same filesystem
timestamp tick may require a second reload to be noticed.
