# Codebase impact analysis: Neuro ICU TTS UI overhaul

## Current integration points

- `main.py`: Anki hooks, timer, menu action, shortcut, scan, F5-TTS subprocess, media/note commits, logging.
- `tts_core.py`: pure normalization, digest, managed marker, and filename helpers.
- `config.json`: current F5-TTS paths and synthesis settings.
- `test_tts_core.py`: only current automated coverage; three pure-function tests.

## Required change surface

- Split `main.py` into domain/application/adapters/infrastructure/UI boundaries.
- Add SQLite queue/state storage under Anki profile user data.
- Add F5-TTS engine adapter and worker process/thread.
- Add main-thread commit dispatcher with stale-source revalidation.
- Add schema-versioned runtime configuration and migrations.
- Add Qt Control Center and reviewer webview command/status bridge.
- Add managed-marker migration that preserves unrelated user audio.
- Expand unit, queue, adapter, Qt, Anki integration, and manual sync tests.

## Highest risks

1. Anki collection APIs must remain on the main thread.
2. Current sound-tag replacement can delete user-owned audio.
3. Current recursive retry can loop during repeated edits.
4. F5-TTS work can freeze Anki unless fully moved off the UI thread.
5. Reviewer webview and hook APIs vary across Anki versions.
6. Full-deck media mutation requires backup, pilot, and rollback gates.
