# Neuro ICU TTS (`neuro-tts`)

Anki add-on that generates spoken explanations for Neuro ICU flashcards with
F5-TTS, off the UI thread, and embeds them as click-to-play HTML5 players that
never autoplay.

## Contents

| Path | Purpose |
| --- | --- |
| `addon/` | **The source tree.** Add-on code, Qt Control Center, and the test suite. Edit here. |
| `neuroicu_tts_addon/` | Generated Anki `addons21` package. Built by `tools/package.py` — never edit by hand. |
| `tools/package.py` | Builds and verifies the package; `--check` fails when it is stale. |
| `packages/` | Installable `.ankiaddon` archives. |
| `specs/`, `docs/` | Design specifications and verification records. |

## Key features

- **Click-to-play audio, zero autoplay.** An embedded HTML5 player keeps cards
  silent on both sides until the user presses play, while remaining a real
  media reference so AnkiWeb, AnkiMobile, and AnkiDroid still sync the file.
- **Speech normalization for clinical text.** Written shorthand is rewritten
  before synthesis so the engine reads it the way a clinician would: `10mg` →
  "10 milligrams", `q4-6h` → "every 4 to 6 hours", `SAH` → "subarachnoid
  hemorrhage", `↑ICP` → "increased intracranial pressure", `SBP <140` →
  "systolic blood pressure less than 140". Cloze markup keeps the answer and
  drops the hint. Both the symbol pass and abbreviation expansion are
  independently toggleable, and `speech_replacements` lets you override or
  extend the built-in table without touching the code.
- **Long notes are chunked, not truncated.** Explanations over
  `max_chunk_chars` are split at sentence boundaries, synthesized separately,
  and concatenated with FFmpeg, avoiding the quality collapse F5-TTS shows on
  very long single utterances.
- **Retrying queue.** Failed synthesis is retried in-process with exponential
  backoff up to `max_attempts` before a note is marked `failed_terminal`.
  Interrupted work is recovered when the profile reopens.
- **Operator controls.** Pause and resume the queue (the running job always
  finishes first), cancel everything still pending, or re-queue every failed
  and cancelled note against its current text.
- **Broadcast loudness normalization.** EBU R128 `loudnorm` leveling and
  leading-silence trimming through FFmpeg, with a plain-encode fallback.
- **6-tab Qt Control Center (`Ctrl+Alt+T`)** — Overview, Settings, Queue,
  Diagnostics, Maintenance, and Scope. Engine paths are editable in the UI and
  apply without restarting Anki.
- **Reviewer shortcuts** — `Ctrl+Alt+V` play/pause, `Ctrl+Alt+G` generate for
  the current card, `Ctrl+Alt+T` open the Control Center.

## Development

`addon/` is the single source of truth. `neuroicu_tts_addon/` is generated from
it, and `addon/test_packaging.py` fails the suite whenever the two drift, so a
change is never half-shipped.

```sh
make test           # PYTHONPATH=addon python3 -m unittest discover -s addon -v
make lint           # ruff check .
make package        # regenerate neuroicu_tts_addon/ from addon/
make package-check  # fail if the generated package is stale
make dist           # build packages/neuroicu_tts_addon.ankiaddon
make check          # everything CI runs
```

Every push and pull request runs the suite on Python 3.9, 3.11, and 3.13, plus
the package-parity and lint jobs (`.github/workflows/ci.yml`).

## Configuration

Settings live in `addon/config.json`, are versioned (`schema_version` 3), and
migrate forward automatically: missing keys are filled from defaults, unknown
keys are preserved, and a corrupt file falls back to defaults with a logged
warning rather than breaking the add-on.

| Key | Default | Meaning |
| --- | --- | --- |
| `f5_tts_repo` | `""` | F5-TTS checkout containing `src/f5_tts/infer/infer_cli.py`. |
| `f5_tts_python` | `"python3"` | Interpreter of the F5-TTS environment. |
| `f5_model` | `"F5TTS_v1_Base"` | F5-TTS model name. |
| `f5_ref_audio` / `f5_ref_text` | `""` | Reference recording and its exact transcript. |
| `f5_device` | `"cpu"` | `cpu`, `mps`, or `cuda`. An MPS segfault falls back to CPU for that note. |
| `f5_nfe_step` | `16` | Denoising steps (4–128). |
| `f5_speed` | `1.0` | Synthesis speed (0.5–2.0). |
| `ffmpeg_path` | `""` | Explicit FFmpeg binary; empty auto-detects. |
| `pilot_only` / `pilot_tag` | `true` / `neuroicu-tts-pilot` | Restrict generation to tagged pilot notes. |
| `normalize_speech` | `true` | Rewrite symbols, doses, and units before synthesis. |
| `expand_abbreviations` | `true` | Expand clinical abbreviations to full words. |
| `speech_replacements` | `{}` | Your own text→speech overrides, applied before the built-ins. |
| `max_chunk_chars` | `800` | Split threshold for long notes (100–5000). |
| `max_attempts` | `3` | Synthesis attempts before a note fails terminally (1–10). |
| `retry_backoff_seconds` | `30.0` | Base retry delay; doubles per attempt. |
| `log_level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

The shipped file intentionally contains no machine-local paths. Supply them in
the Control Center's Settings tab, or export `F5_TTS_REPO` / `F5_TTS_PYTHON`
before launching Anki — environment variables win over saved values, and the
Settings tab says so when one is set.

Changing anything that affects the audio — engine paths, device, speed, or the
speech-shaping settings — changes the synthesis profile digest, so affected
notes are re-queued on the next scan instead of keeping stale audio.

Logs rotate at 2 MB with three backups (`addon/neuroicu_tts.log`).
