# Neuro ICU TTS (`neuro-tts`)

Production-grade Anki add-on with asynchronous F5-TTS speech synthesis, in-card click-to-play HTML5 audio players with zero front-side autoplay, interactive playback speed control, animated equalizer soundwaves, broadcast audio loudness normalization, and a 6-tab Qt Control Center.

## Contents

- `addon/` — Core Anki add-on source, Qt Control Center UI, and automated test suite
- `neuroicu_tts_addon/` — Packaged add-on distribution directory for Anki Desktop (`addons21`)
- `packages/neuroicu_tts_addon_f5_async_v2.zip` — Installable `.ankiaddon` / `.zip` archive
- `specs/` — Architectural specifications, design plans, and verification records

## Key Features

- **Click-to-Play Audio & Zero Autoplay**: Embedded HTML5 player prevents cards from autoplaying explanations on the question or answer side while preserving AnkiWeb/iOS/Android media sync.
- **In-Card Speed Switcher**: Interactive `1.0x` → `2.0x` speed toggle on every flashcard.
- **Equalizer Soundwave**: Dynamic animated audio wave visualization during playback.
- **Broadcast Audio Normalization**: EBU R128 (`loudnorm`) volume leveling and instant zero-latency silence trimming via FFmpeg.
- **6-Tab Qt Control Center (`Ctrl+Alt+T`)**: Complete dashboard with Overview, Settings, Queue progress, Diagnostics (with log filter & 1-click clipboard export), Maintenance (storage stats & legacy migration), and Scope management.
- **Reviewer Shortcuts**:
  - `Ctrl+Alt+V`: Play/pause explanation audio
  - `Ctrl+Alt+G`: Generate/queue TTS for the active card immediately
  - `Ctrl+Alt+T`: Open Control Center

## Testing & Quality Gates

Run the test suite:

```sh
PYTHONPATH=addon python3 -m unittest discover -s addon -v
```
