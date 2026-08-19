# Neuro ICU TTS Anki add-on (F5-TTS)

This add-on synchronizes spoken audio for the `Extra` field of
`Enhanced Cloze 2.1 v2-136cb (Neuro ICU Boards / arbornasher)` notes in
`Neuro ICU Boards (AnkiHub)`.

## Install

Copy the `neuroicu_tts_addon` directory into Anki Desktop's add-ons directory,
restart Anki, and add the tag `neuroicu-tts-pilot` to 10–20 pilot notes.

Keep `pilot_only` set to `true` while validating a small tagged sample. Change
it to `false` only after the pilot succeeds, then restart Anki before a
full-deck scan.

## F5-TTS setup

The add-on runs F5-TTS locally through its CLI using Apple Silicon MPS.
F5-TTS is installed in the isolated environment configured in `config.json`.

Edit `config.json` in this add-on directory, or set these environment
variables before launching Anki:

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
- `Tools → Queue Neuro ICU TTS scan` runs an immediate scan.
- Audio is staged outside Anki media, revalidated on the main thread, and then
  stored as `neuroicu_tts_<note-id>-<sha256>.mp3`.
- The filename and v2 marker identify both source text and synthesis settings,
  so changing the voice/model configuration queues regeneration.
- The `Extra` field gets one managed HTML comment and one add-on-owned `[sound:...]` tag;
  unrelated user audio tags are preserved.
- F5-TTS and FFmpeg calls have time limits and bounded diagnostic output.
- Closing or switching profiles cancels the active synthesis subprocess and
  leaves interrupted work retryable for the next profile open.
- Failed generation or note commit leaves the existing note content intact and
  writes details to `neuroicu_tts.log`.
- Ordinary generation never deletes existing media. Automated cleanup is not
  part of this release.

## Current Control Center scope

The Overview supports engine testing, status refresh, pilot-tag editing,
current-card generation, and collection scans. Detailed queue browsing,
in-app engine configuration, and media cleanup remain future surfaces; the
dashboard reports these unavailable actions instead of silently doing work.
