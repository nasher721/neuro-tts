# Neuro ICU TTS Anki Project

This folder contains the complete working project for synchronized F5-TTS audio
on Neuro ICU Boards Enhanced Cloze cards.

## Contents

- `addon/` — current Anki add-on source, configuration, and tests
- `packages/neuroicu_tts_addon_f5_async_v2.zip` — installable add-on archive
- `packages/neuroicu_tts_addon_project.zip` — source-layout archive for development, not direct Anki installation
- `specs/` — UI/application plan and analysis
- `vendor/F5-TTS/` — F5-TTS source checkout used by the add-on

The F5-TTS Python environment remains at:

`/Users/Nash/Documents/Codex/2026-08-16/new-chat/work/f5-tts-venv`

The add-on is already installed in Anki at:

`/Users/Nash/Library/Application Support/Anki2/addons21/neuroicu_tts_addon`

Run the pure add-on tests with:

```sh
PYTHONPATH=addon python3 -m unittest discover -s addon -v
```

After restarting Anki, use `Tools → Neuro ICU TTS Control Center` to configure
the pilot scope and queue changed cards.
