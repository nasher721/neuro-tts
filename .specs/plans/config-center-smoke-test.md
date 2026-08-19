# Config Center — In-Anki Smoke Test Checklist

**Setup:** quit Anki fully, restart it, then open `Tools → Neuro ICU TTS Control Center` (or `Ctrl+Alt+T`).
Expected duration: ~10 minutes. Check each box as it passes; note failures with the step number.

> **Executed by agent via KimiCU computer-use on 2026-08-18 ~17:05–17:40, Anki profile "User 1", pid 99860.**
> Legend: ✅ verified in live GUI · ⏭️ deliberately not run (reason noted) · unit-covered items reference the 86/86 passing suite.

---

## 0. First run / migration (G8)

- [x] **0.1** ✅ Add-on loads without error dialog; `addon/config.json` now contains `"schema_version": 2` and your previous values are intact.
  - Log: `17:05:32 INFO Neuro ICU TTS loaded with asynchronous F5-TTS worker`, no errors. All pre-migration values preserved verbatim.
  - Note: two *unrelated* add-ons fail at startup (Anki Prettify, Anki Excel Sync) — pre-existing, not caused by this change.
- [ ] **0.2** ⏭️ (Optional) Corrupt-config fallback — not run against the live profile; covered by unit tests (defaults + warning path).

## 1. Structure (Step 4)

- [x] **1.1** ✅ Six tabs in order: Overview · Settings · Queue · Diagnostics · Maintenance · Scope (AX tree + pixel-verified).
- [x] **1.2** ✅ Overview renders live status cards (Engine "Not tested", Scope "Pilot · 1 eligible note(s)", Queue "0 queued", Attention "None", Activity with real scan timestamp). "Refresh status" clicked, re-renders without error.
  - Recommended-action button ("Run engine test") present but not clicked — it would start a real F5-TTS synthesis on CPU.
- [ ] **1.3** ⏭️ No tab-jump recommendation exists in the current state (the only recommended action is the engine test). Tab-jump wiring is unit-tested.

## 2. Settings tab (G1)

- [x] **2.1** ✅ Fields show current values; engine fields (repo, python, model, ref audio/text, NFE) render read-only/grayed.
- [x] **2.2** ✅ Speed `1.5` → Save → `config.json` contained `"f5_speed": 1.5`; schema migrated to v2 with all values intact.
- [x] **2.3** ✅ Revert after edits (`5.0`, then `x`) → field returned to last-saved `1.5`; no file write (no save log line).
- [x] **2.4** ✅ External edit (speed → `1.25`) → Reload → form showed `1.25`; log: `config.json reloaded (mtime changed)`. Restored to `1.0` afterwards.
- [x] **2.5** ✅ Speed `5.0` → Save → inline error "speed must be between 0.5 and 2.0"; file not written; field kept `5.0` for correction. Log: `WARNING config save rejected: speed must be between 0.5 and 2.0`.
- [x] **2.6** ✅ ffmpeg path `/no/such/ffmpeg` → Save → rejected; file not written. Log: `WARNING config save rejected: ffmpeg_path does not exist: /no/such/ffmpeg`.
- [x] **2.7** ✅ Pilot tag `bad tag` (with space) → Save → inline error "pilot_tag must be non-empty with no whitespace"; file not written.
- [ ] **2.8** ⏭️ Generate-with-new-speed — requires a real synthesis job; deferred (job argument passing is unit-tested).

## 3. Queue tab (G2)

- [x] **3.1** ✅ Counts displayed from the real JobStore: queued 0 · running 0 · staged 0 · failed_retryable 0 · failed_terminal 0 · succeeded 2 · stale 17.
- [x] **3.2** ✅ No edit/delete controls on the tab; "Refresh queue" clicked, counts re-rendered without error.

## 4. Diagnostics tab (G3)

- [ ] **4.1** ⏭️ "Run engine test" not clicked — starts real F5-TTS synthesis (CPU, minutes). Button present and wired.
- [x] **4.2** ✅ Log tail populated with real `neuroicu_tts.log` lines on tab open.
  - Minor doc drift: there is no separate "Refresh log" button; the tail loads when the tab opens.

## 5. Maintenance tab (G4)

- [x] **5.1** ✅ Storage size "Generated audio storage: 706.3 KB" — cross-checked against `collection.media/neuroicu_tts_*.mp3` (~712 KB in du blocks). "Refresh" clicked without error.
- [ ] **5.2** ⏭️ "Clear Finished" not clicked — would delete your 2 real succeeded job records. Covered by unit tests.

## 6. Scope tab (G5 + G11)

- [x] **6.1** ✅ Checkbox reflects config; toggle OFF → `"pilot_only": false` + Overview Scope card flipped to "Full · 2872 eligible note(s)" + Activity logged "Pilot-only scope disabled"; toggle ON → back to true / "Pilot · 1 eligible note(s)".
- [x] **6.2** ✅ Full-Deck Convert → ImpactDialog: "This will queue TTS generation for **2872** note(s). Estimated runtime: about **1436** minute(s)." (Count matches the Overview Full-scope eligible count.)
- [x] **6.3** ✅ Cancel → dialog dismissed; nothing enqueued (no queue log lines, counts unchanged).
- [ ] **6.4** ⏭️ Confirm path not run — would enqueue 2872 real synthesis jobs. Unit-tested (including (2,3) dedupe proof).
- [ ] **6.5** ⏭️ Re-run dedupe — unit-tested.

---

## Result: PASS (16 verified live, 0 failures, 7 deliberately deferred to unit coverage)

**Integrity restored:** live `config.json` ends with your exact original values (speed 1.0, pilot_only true, pilot tag, all engine paths) in schema v2 form; no jobs enqueued; no notes touched.

**Tooling note for future GUI runs (not product bugs):**
- KimiCU `AXPress` index clicks intermittently fail to deliver to Qt push buttons (observed on About dialog Close, ImpactDialog Cancel, Settings Reload). Coordinate clicks (window-local, after an image-mode snapshot) were 100% reliable. Tab radio buttons, checkboxes, and Save/Revert worked via AXPress.
- The Control Center lives on an inactive macOS Space, so its pixel buffer goes stale after tab switches; a 1-px window resize forces a repaint for screenshots. AX tree always reflects live state.
- A 22×90 "(untitled)" orange-sliver window (from another add-on) accompanies Anki; harmless, pre-existing.
