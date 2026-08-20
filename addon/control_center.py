"""Qt presentation for the tabbed Neuro ICU TTS Control Center.

The dialog is a pure renderer: every side effect lives in callbacks injected
by ``main.py``, and every callback runs through :meth:`ControlCenter._run_callback`
so a failure leaves the dashboard usable.
"""

from __future__ import annotations

from html import escape
from typing import Callable

from .status import OverviewViewModel, StatusService

# Rough per-note synthesis average used by the full-deck impact estimate.
DEFAULT_PER_NOTE_SECONDS = 30.0


def estimate_runtime(note_count: int, per_note_seconds: float = DEFAULT_PER_NOTE_SECONDS) -> float:
    """Pure ImpactDialog math: estimated total seconds for *note_count* notes."""
    return max(0, int(note_count)) * float(per_note_seconds)


try:  # Keep pure status/tests importable on machines without Anki or Qt.
    from aqt.qt import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QTabWidget,
        QTimer,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - exercised by non-Anki imports
    QDialog = None


if QDialog is not None:

    class ImpactDialog(QDialog):
        """G11 — confirmation with note count and estimated runtime."""

        def __init__(self, note_count: int, estimated_seconds: float, parent=None) -> None:
            super().__init__(parent)
            self.setWindowTitle("Full-deck conversion")
            self.setAccessibleName("Full-deck conversion impact")
            layout = QVBoxLayout(self)
            minutes = estimated_seconds / 60.0
            label = QLabel(
                f"This will queue TTS generation for <b>{note_count}</b> note(s).<br>"
                f"Estimated runtime: about {minutes:.0f} minute(s)."
            )
            label.setWordWrap(True)
            label.setAccessibleName("Full-deck conversion impact summary")
            layout.addWidget(label)
            row = QHBoxLayout()
            confirm = QPushButton("Confirm")
            confirm.setAccessibleName("Confirm full-deck conversion")
            confirm.setDefault(True)
            confirm.clicked.connect(self.accept)
            cancel = QPushButton("Cancel")
            cancel.setAccessibleName("Cancel full-deck conversion")
            cancel.clicked.connect(self.reject)
            row.addWidget(confirm)
            row.addWidget(cancel)
            layout.addLayout(row)

    class ControlCenter(QDialog):
        """Tabbed dashboard renderer; application callbacks own all side effects."""

        def __init__(
            self,
            parent=None,
            *,
            status_service: StatusService,
            on_scan: Callable[[], object],
            on_current: Callable[[], object],
            on_action: Callable[[str], object] | None = None,
            pilot_tag: str = "",
            on_pilot_tag: Callable[[str], bool] | None = None,
            on_settings_snapshot: Callable[[], dict] | None = None,
            on_save_settings: Callable[[dict], list] | None = None,
            on_reload_settings: Callable[[], bool] | None = None,
            on_queue_counts: Callable[[], dict] | None = None,
            on_engine_test: Callable[[], object] | None = None,
            on_log_tail: Callable[[], str] | None = None,
            on_storage_size: Callable[[], int] | None = None,
            on_clear_finished: Callable[[], int] | None = None,
            on_toggle_pilot: Callable[[bool], bool] | None = None,
            on_full_deck_info: Callable[[], tuple] | None = None,
            on_full_deck_convert: Callable[[], tuple] | None = None,
            on_upgrade_legacy: Callable[[], int] | None = None,
        ) -> None:
            super().__init__(parent)
            self._status_service = status_service
            self._on_scan = on_scan
            self._on_current = on_current
            self._on_action = on_action
            self._on_pilot_tag = on_pilot_tag
            self._on_settings_snapshot = on_settings_snapshot
            self._on_save_settings = on_save_settings
            self._on_reload_settings = on_reload_settings
            self._on_queue_counts = on_queue_counts
            self._on_engine_test = on_engine_test
            self._on_log_tail = on_log_tail
            self._on_storage_size = on_storage_size
            self._on_clear_finished = on_clear_finished
            self._on_toggle_pilot = on_toggle_pilot
            self._on_full_deck_info = on_full_deck_info
            self._on_full_deck_convert = on_full_deck_convert
            self._on_upgrade_legacy = on_upgrade_legacy
            self._pilot_tag = pilot_tag
            self._closed = False
            self._recommended_action = ""
            self._unsubscribe = status_service.subscribe(self._status_changed)
            self.setWindowTitle("Neuro ICU TTS Control Center")
            self.setMinimumWidth(680)
            self.setAccessibleName("Neuro ICU TTS Control Center")

            layout = QVBoxLayout(self)
            self.tabs = QTabWidget()
            self.tabs.setAccessibleName("Control Center sections")
            layout.addWidget(self.tabs)

            self.tabs.addTab(self._build_overview_tab(), "Overview")
            self.tabs.addTab(self._build_settings_tab(), "Settings")
            self.tabs.addTab(self._build_queue_tab(), "Queue")
            self.tabs.addTab(self._build_diagnostics_tab(), "Diagnostics")
            self.tabs.addTab(self._build_maintenance_tab(), "Maintenance")
            self.tabs.addTab(self._build_scope_tab(), "Scope")
            self.tabs.currentChanged.connect(self._tab_changed)

            close = QPushButton("Close")
            close.setAccessibleName("Close Control Center")
            close.clicked.connect(self.close)
            layout.addWidget(close)

            self._settings_populate()
            self._queue_refresh()
            self._diagnostics_refresh_log()
            self._maintenance_refresh()
            self._scope_refresh()
            self.refresh()

        # ── tab navigation ──────────────────────────────────────────────────
        def show_tab(self, name: str) -> bool:
            """Switch to the tab titled *name*; return False when unknown."""
            for index in range(self.tabs.count()):
                if self.tabs.tabText(index) == name:
                    self.tabs.setCurrentIndex(index)
                    return True
            return False

        def _tab_changed(self, index: int) -> None:
            title = self.tabs.tabText(index)
            if title == "Settings":
                self._settings_populate()
            elif title == "Queue":
                self._queue_refresh()
            elif title == "Diagnostics":
                self._diagnostics_refresh_log()
            elif title == "Maintenance":
                self._maintenance_refresh()
            elif title == "Scope":
                self._scope_refresh()

        # ── Overview tab (original dashboard, verbatim behavior) ────────────
        def _build_overview_tab(self) -> QWidget:
            tab = QWidget()
            tab.setAccessibleName("Overview tab")
            layout = QVBoxLayout(tab)

            self.banner = QLabel()
            self.banner.setWordWrap(True)
            self.banner.setAccessibleName("Control Center status")
            layout.addWidget(self.banner)

            self.refresh_button = QPushButton("Refresh status")
            self.refresh_button.setToolTip("Reload the dashboard status (Ctrl+R).")
            self.refresh_button.setAccessibleName("Refresh dashboard status")
            self.refresh_button.setShortcut("Ctrl+R")
            self.refresh_button.clicked.connect(self.refresh)
            layout.addWidget(self.refresh_button)

            self.cards = QGridLayout()
            layout.addLayout(self.cards)

            self.recommended = QGroupBox("Recommended next action")
            recommended_layout = QVBoxLayout(self.recommended)
            self.recommended_text = QLabel()
            self.recommended_text.setWordWrap(True)
            self.recommended_text.setAccessibleName("Recommended next action details")
            recommended_layout.addWidget(self.recommended_text)
            self.recommended_button = QPushButton()
            self.recommended_button.setAccessibleName("Run recommended next action")
            self.recommended_button.clicked.connect(self._recommended_clicked)
            recommended_layout.addWidget(self.recommended_button)
            layout.addWidget(self.recommended)

            self.advanced = QGroupBox("Advanced controls")
            self.advanced.setCheckable(True)
            self.advanced.setChecked(False)
            advanced_layout = QVBoxLayout(self.advanced)
            form = QFormLayout()
            self.tag = QLineEdit(self._pilot_tag)
            self.tag.setAccessibleName("Pilot tag")
            self.tag.setPlaceholderText("Optional Anki tag")
            self.tag.setToolTip("Anki tag used to select pilot notes for generation.")
            if self._on_pilot_tag:
                self.tag.editingFinished.connect(self._pilot_tag_finished)
            form.addRow("Pilot tag (editable)", self.tag)
            advanced_layout.addLayout(form)
            scan = QPushButton("Scan and queue changed notes")
            scan.setAccessibleName("Scan and queue changed notes")
            scan.clicked.connect(self._scan_clicked)
            advanced_layout.addWidget(scan)
            current = QPushButton("Generate current card")
            current.setAccessibleName("Generate audio for current card")
            current.clicked.connect(self._current_clicked)
            advanced_layout.addWidget(current)
            layout.addWidget(self.advanced)

            self.details_disclosure = QGroupBox("Advanced details")
            self.details_disclosure.setCheckable(True)
            self.details_disclosure.setChecked(False)
            details_layout = QVBoxLayout(self.details_disclosure)
            self.details_text = QLabel()
            self.details_text.setWordWrap(True)
            self.details_text.setAccessibleName("Advanced status details")
            self.details_text.setVisible(False)
            details_layout.addWidget(self.details_text)
            self.details_disclosure.toggled.connect(self.details_text.setVisible)
            layout.addWidget(self.details_disclosure)
            return tab

        # ── Settings tab (G1) ───────────────────────────────────────────────
        def _build_settings_tab(self) -> QWidget:
            tab = QWidget()
            tab.setAccessibleName("Settings tab")
            layout = QVBoxLayout(tab)
            form = QFormLayout()

            self.speed_edit = QLineEdit()
            self.speed_edit.setAccessibleName("TTS speed")
            self.speed_edit.setToolTip("Playback speed multiplier, between 0.5 and 2.0.")
            form.addRow("Speed", self.speed_edit)

            self.device_combo = QComboBox()
            self.device_combo.addItems(["cpu", "mps", "cuda"])
            self.device_combo.setAccessibleName("Inference device")
            self.device_combo.setToolTip("Compute device for F5-TTS inference.")
            form.addRow("Device", self.device_combo)

            self.ffmpeg_edit = QLineEdit()
            self.ffmpeg_edit.setAccessibleName("ffmpeg path")
            self.ffmpeg_edit.setToolTip("Path to the ffmpeg binary; leave empty to auto-detect.")
            form.addRow("ffmpeg path", self.ffmpeg_edit)

            self.settings_tag_edit = QLineEdit()
            self.settings_tag_edit.setAccessibleName("Settings pilot tag")
            self.settings_tag_edit.setToolTip("Anki tag used to select pilot notes; no whitespace.")
            form.addRow("Pilot tag", self.settings_tag_edit)
            layout.addLayout(form)

            locked = QGroupBox("Engine settings (read-only)")
            locked_layout = QFormLayout(locked)
            self.locked_fields: dict[str, QLineEdit] = {}
            for key, label in (
                ("f5_tts_repo", "F5-TTS repo"),
                ("f5_tts_python", "F5-TTS python"),
                ("f5_model", "Model"),
                ("f5_ref_audio", "Reference audio"),
                ("f5_ref_text", "Reference text"),
                ("f5_nfe_step", "NFE steps"),
            ):
                field = QLineEdit()
                field.setReadOnly(True)
                field.setAccessibleName(f"{label} (locked)")
                field.setToolTip("Engine-locked; edit config.json by hand to change.")
                self.locked_fields[key] = field
                locked_layout.addRow(label, field)
            layout.addWidget(locked)

            self.settings_error = QLabel()
            self.settings_error.setWordWrap(True)
            self.settings_error.setAccessibleName("Settings validation errors")
            layout.addWidget(self.settings_error)
            self.settings_status = QLabel()
            self.settings_status.setWordWrap(True)
            self.settings_status.setAccessibleName("Settings status")
            layout.addWidget(self.settings_status)

            row = QHBoxLayout()
            save = QPushButton("Save")
            save.setAccessibleName("Save settings")
            save.clicked.connect(self._settings_save_clicked)
            revert = QPushButton("Revert")
            revert.setAccessibleName("Revert settings")
            revert.clicked.connect(self._settings_revert_clicked)
            reload_button = QPushButton("Reload")
            reload_button.setAccessibleName("Reload settings from disk")
            reload_button.clicked.connect(self._settings_reload_clicked)
            row.addWidget(save)
            row.addWidget(revert)
            row.addWidget(reload_button)
            layout.addLayout(row)
            layout.addStretch(1)
            return tab

        def _settings_populate(self) -> None:
            if not self._on_settings_snapshot:
                return
            snapshot = self._on_settings_snapshot()
            self.speed_edit.setText(str(snapshot.get("f5_speed", "")))
            self.device_combo.setCurrentText(str(snapshot.get("f5_device", "cpu")))
            self.ffmpeg_edit.setText(str(snapshot.get("ffmpeg_path", "") or ""))
            self.settings_tag_edit.setText(str(snapshot.get("pilot_tag", "")))
            for key, field in self.locked_fields.items():
                field.setText(str(snapshot.get(key, "")))

        def _settings_values(self) -> dict:
            return {
                "f5_speed": self.speed_edit.text().strip(),
                "f5_device": self.device_combo.currentText(),
                "ffmpeg_path": self.ffmpeg_edit.text().strip(),
                "pilot_tag": self.settings_tag_edit.text().strip(),
            }

        def _settings_save_clicked(self) -> None:
            if not self._on_save_settings:
                return
            outcome: dict = {}

            def invoke() -> bool:
                outcome["errors"] = self._on_save_settings(self._settings_values())
                return True

            self._run_callback(invoke, "", "Settings save")
            errors = outcome.get("errors") or []
            if errors:
                # Invalid values stay in the fields until corrected; nothing was written.
                self.settings_error.setText("\n".join(escape(str(error)) for error in errors))
                self.settings_status.setText("")
            else:
                self.settings_error.setText("")
                self.settings_status.setText("Settings saved.")

        def _settings_revert_clicked(self) -> None:
            self._settings_populate()
            self.settings_error.setText("")
            self.settings_status.setText("Reverted to last saved values.")

        def _settings_reload_clicked(self) -> None:
            if not self._on_reload_settings:
                return
            self._run_callback(self._on_reload_settings, "", "Settings reload")
            self._settings_populate()
            self.settings_error.setText("")
            self.settings_status.setText("Settings reloaded from disk.")

        # ── Queue tab (G2, read-only) ───────────────────────────────────────
        _QUEUE_STATES = ("queued", "running", "staged", "failed_retryable", "failed_terminal", "succeeded", "stale")

        def _build_queue_tab(self) -> QWidget:
            tab = QWidget()
            tab.setAccessibleName("Queue tab")
            layout = QVBoxLayout(tab)
            self.queue_progress = QProgressBar()
            self.queue_progress.setAccessibleName("Queue progress bar")
            self.queue_progress.setRange(0, 100)
            self.queue_progress.setValue(0)
            layout.addWidget(self.queue_progress)
            self.queue_counts_label = QLabel()
            self.queue_counts_label.setWordWrap(True)
            self.queue_counts_label.setAccessibleName("Queue counts by status")
            layout.addWidget(self.queue_counts_label)
            refresh = QPushButton("Refresh queue")
            refresh.setAccessibleName("Refresh queue counts")
            refresh.clicked.connect(self._queue_refresh)
            layout.addWidget(refresh)
            layout.addStretch(1)
            return tab

        def _queue_refresh(self) -> None:
            counts = self._on_queue_counts() if self._on_queue_counts else {}
            lines = [f"{state}: {counts.get(state, 0)}" for state in self._QUEUE_STATES]
            self.queue_counts_label.setText("\n".join(lines))
            total = sum(counts.get(s, 0) for s in self._QUEUE_STATES)
            done = counts.get("succeeded", 0) + counts.get("failed_terminal", 0) + counts.get("stale", 0)
            if total > 0:
                pct = int((done / total) * 100)
                self.queue_progress.setValue(pct)
                self.queue_progress.setFormat(f"{done}/{total} jobs completed ({pct}%)")
            else:
                self.queue_progress.setValue(100)
                self.queue_progress.setFormat("Queue idle (0 pending)")

        # ── Diagnostics tab (G3, read-only) ─────────────────────────────────
        def _build_diagnostics_tab(self) -> QWidget:
            tab = QWidget()
            tab.setAccessibleName("Diagnostics tab")
            layout = QVBoxLayout(tab)
            self._raw_log_tail = ""
            btn_row = QHBoxLayout()
            test = QPushButton("Run Engine Test")
            test.setAccessibleName("Run engine test")
            test.clicked.connect(self._engine_test_clicked)
            btn_row.addWidget(test)
            refresh = QPushButton("Refresh Log")
            refresh.setAccessibleName("Refresh log tail")
            refresh.clicked.connect(self._diagnostics_refresh_log)
            btn_row.addWidget(refresh)
            copy_btn = QPushButton("Copy Log")
            copy_btn.setAccessibleName("Copy log tail")
            copy_btn.clicked.connect(self._diagnostics_copy_log)
            btn_row.addWidget(copy_btn)
            layout.addLayout(btn_row)
            self.log_filter = QLineEdit()
            self.log_filter.setPlaceholderText("Search / filter log lines...")
            self.log_filter.setAccessibleName("Log filter")
            self.log_filter.textChanged.connect(self._filter_log_display)
            layout.addWidget(self.log_filter)
            self.log_tail = QPlainTextEdit()
            self.log_tail.setReadOnly(True)
            self.log_tail.setAccessibleName("Add-on log tail")
            layout.addWidget(self.log_tail)
            return tab

        def _engine_test_clicked(self) -> None:
            if self._on_engine_test:
                self._run_callback(self._on_engine_test, "", "Engine test")

        def _diagnostics_refresh_log(self) -> None:
            if self._on_log_tail:
                self._raw_log_tail = self._on_log_tail() or ""
                self._raw_log_lines = self._raw_log_tail.splitlines()
                self._filter_log_display()

        def _filter_log_display(self) -> None:
            query = getattr(self, "log_filter", None)
            term = query.text().strip().lower() if query else ""
            raw = getattr(self, "_raw_log_tail", "")
            if not term:
                self.log_tail.setPlainText(raw)
            else:
                lines = getattr(self, "_raw_log_lines", None)
                if lines is None:
                    lines = raw.splitlines()
                    self._raw_log_lines = lines
                filtered = [line for line in lines if term in line.lower()]
                self.log_tail.setPlainText("\n".join(filtered))

        def _diagnostics_copy_log(self) -> None:
            text = self.log_tail.toPlainText()
            try:
                clip = QApplication.clipboard()
                if clip is not None:
                    clip.setText(text)
            except Exception:
                pass

        # ── Maintenance tab (G4, light) ─────────────────────────────────────
        def _build_maintenance_tab(self) -> QWidget:
            tab = QWidget()
            tab.setAccessibleName("Maintenance tab")
            layout = QVBoxLayout(tab)
            self.storage_label = QLabel()
            self.storage_label.setAccessibleName("Generated audio storage size")
            layout.addWidget(self.storage_label)
            self.maintenance_status = QLabel()
            self.maintenance_status.setWordWrap(True)
            self.maintenance_status.setAccessibleName("Maintenance status")
            layout.addWidget(self.maintenance_status)
            row = QHBoxLayout()
            refresh = QPushButton("Refresh")
            refresh.setAccessibleName("Refresh storage size")
            refresh.clicked.connect(self._maintenance_refresh)
            clear = QPushButton("Clear Finished")
            clear.setAccessibleName("Clear finished jobs")
            clear.clicked.connect(self._clear_finished_clicked)
            upgrade = QPushButton("Upgrade Legacy Markers")
            upgrade.setAccessibleName("Upgrade legacy audio markers to click-to-play")
            upgrade.setToolTip("Upgrade existing notes using [sound:...] to the click-to-play HTML player without re-generating audio.")
            upgrade.clicked.connect(self._upgrade_legacy_clicked)
            row.addWidget(refresh)
            row.addWidget(clear)
            row.addWidget(upgrade)
            layout.addLayout(row)
            layout.addStretch(1)
            return tab

        def _maintenance_refresh(self) -> None:
            if not self._on_storage_size:
                return
            size = self._on_storage_size()
            self.storage_label.setText(f"Generated audio storage: {self._format_bytes(size)}")

        @staticmethod
        def _format_bytes(size: int) -> str:
            value = float(size)
            for unit in ("B", "KB", "MB", "GB"):
                if value < 1024 or unit == "GB":
                    return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
                value /= 1024
            return f"{value:.1f} GB"

        def _clear_finished_clicked(self) -> None:
            if not self._on_clear_finished:
                return
            outcome: dict = {}

            def invoke() -> bool:
                outcome["removed"] = self._on_clear_finished()
                return True

            self._run_callback(invoke, "", "Clear finished")
            if "removed" in outcome:
                self.maintenance_status.setText(f"Cleared {outcome['removed']} finished job(s).")
                self._queue_refresh()

        def _upgrade_legacy_clicked(self) -> None:
            if not self._on_upgrade_legacy:
                return
            outcome: dict = {}

            def invoke() -> bool:
                outcome["upgraded"] = self._on_upgrade_legacy()
                return True

            self._run_callback(invoke, "", "Upgrade legacy markers")
            if "upgraded" in outcome:
                self.maintenance_status.setText(f"Upgraded {outcome['upgraded']} note(s) to click-to-play player.")

        # ── Scope tab (G5 + G11) ────────────────────────────────────────────
        def _build_scope_tab(self) -> QWidget:
            tab = QWidget()
            tab.setAccessibleName("Scope tab")
            layout = QVBoxLayout(tab)
            self.pilot_checkbox = QCheckBox("Limit generation to pilot-tagged notes")
            self.pilot_checkbox.setAccessibleName("Pilot-only scope toggle")
            self.pilot_checkbox.setToolTip("When enabled, only notes carrying the pilot tag are synthesized.")
            self.pilot_checkbox.toggled.connect(self._pilot_toggled)
            layout.addWidget(self.pilot_checkbox)
            full_deck = QPushButton("Full-Deck Convert")
            full_deck.setAccessibleName("Convert full deck")
            full_deck.setToolTip("Queue TTS generation for every eligible Neuro ICU note.")
            full_deck.clicked.connect(self._full_deck_clicked)
            layout.addWidget(full_deck)
            self.scope_status = QLabel()
            self.scope_status.setWordWrap(True)
            self.scope_status.setAccessibleName("Scope status")
            layout.addWidget(self.scope_status)
            layout.addStretch(1)
            return tab

        def _scope_refresh(self) -> None:
            if not self._on_settings_snapshot:
                return
            snapshot = self._on_settings_snapshot()
            self.pilot_checkbox.blockSignals(True)
            self.pilot_checkbox.setChecked(bool(snapshot.get("pilot_only", True)))
            self.pilot_checkbox.blockSignals(False)

        def _pilot_toggled(self, checked: bool) -> None:
            if self._on_toggle_pilot:
                self._run_callback(lambda: self._on_toggle_pilot(checked), "", "Scope toggle")

        def _full_deck_clicked(self) -> None:
            if not (self._on_full_deck_info and self._on_full_deck_convert):
                return
            try:
                note_count, estimated_seconds = self._on_full_deck_info()
            except Exception as exc:
                self.scope_status.setText(f"Could not estimate full-deck conversion: {exc}")
                return
            dialog = ImpactDialog(note_count, estimated_seconds, self)
            if dialog.exec() != QDialog.Accepted:
                return  # G11.3 — Cancel enqueues nothing.
            outcome: dict = {}

            def invoke() -> bool:
                outcome["result"] = self._on_full_deck_convert()
                return True

            self._run_callback(invoke, "", "Full-deck conversion")
            result = outcome.get("result")
            if result:
                enqueued, total = result
                if enqueued == total:
                    self.scope_status.setText(f"{enqueued} notes queued")
                else:
                    self.scope_status.setText(f"{enqueued} of {total} notes queued")

        # ── lifecycle ───────────────────────────────────────────────────────
        def closeEvent(self, event) -> None:
            if not self._closed:
                self._closed = True
                self._unsubscribe()
            super().closeEvent(event)

        def _status_changed(self, _snapshot) -> None:
            if self._closed:
                return
            QTimer.singleShot(0, self.refresh)

        def refresh(self) -> None:
            """Render a fresh snapshot without performing application work."""

            try:
                view = OverviewViewModel.from_snapshot(self._status_service.snapshot())
            except Exception as exc:  # UI must present adapter failures plainly.
                self._set_banner("Status temporarily unavailable", "Open Diagnostics or try refreshing again.", "degraded")
                self._clear_cards()
                self.recommended_text.setText(escape(str(exc)))
                self.recommended_button.setEnabled(False)
                self.details_text.setText("No advanced details available.")
                return

            if self.tag.text() != self._pilot_tag:
                self.tag.setText(self._pilot_tag)

            if view.banner:
                self._set_banner(view.banner.title, view.banner.message, view.banner.state)
            else:
                self._set_banner("", "", "")
            self._render_cards(view)
            self.recommended_text.setText(
                f"<b>{escape(view.recommended.title)}</b><br>{escape(view.recommended.message)}"
            )
            self.recommended_button.setText(view.recommended.action)
            self.recommended_button.setToolTip(view.recommended.message)
            self.recommended_button.setEnabled(self._on_action is not None)
            self._recommended_action = view.recommended.action
            self.details_text.setText(escape(view.advanced_details or "No advanced details available."))

        def _set_banner(self, title: str, message: str, state: str) -> None:
            if not title:
                self.banner.clear()
                self.banner.setStyleSheet("")
                return
            self.banner.setText(f"<b>{escape(title)}</b><br>{escape(message)}")
            # Avoid fixed foreground/background colors so Anki's palette remains
            # usable in dark mode and high-contrast themes.
            self.banner.setStyleSheet(
                "border: 1px solid palette(mid); padding: 8px;"
                if state in {"setup", "degraded", "unvalidated"}
                else "padding: 8px;"
            )

        def _clear_cards(self) -> None:
            while self.cards.count():
                item = self.cards.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        def _render_cards(self, view: OverviewViewModel) -> None:
            self._clear_cards()
            state_colors = {
                "ready": "#10b981",
                "setup": "#6366f1",
                "degraded": "#f59e0b",
                "unvalidated": "#f59e0b",
                "active": "#3b82f6",
                "idle": "#64748b",
            }
            for index, card in enumerate(view.cards):
                box = QGroupBox(card.title)
                box.setAccessibleName(f"{card.title} status card")
                box_layout = QVBoxLayout(box)
                value = QLabel(card.value)
                color = state_colors.get(card.state, "#2563eb")
                value.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {color};")
                value.setAccessibleName(f"{card.title} value")
                detail = QLabel(card.detail)
                detail.setWordWrap(True)
                detail.setAccessibleName(f"{card.title} details")
                box_layout.addWidget(value)
                box_layout.addWidget(detail)
                self.cards.addWidget(box, index // 2, index % 2)

        # ── callbacks ───────────────────────────────────────────────────────
        def _recommended_clicked(self) -> None:
            if self._on_action:
                self._run_callback(self._on_action, self._recommended_action, "Recommended action")

        def _scan_clicked(self) -> None:
            self._run_callback(self._on_scan, "", "Scan")

        def _current_clicked(self) -> None:
            self._run_callback(self._on_current, "", "Current-card generation")

        def _run_callback(self, callback: Callable, argument: str, label: str) -> None:
            """Contain adapter failures so a failed action leaves the dashboard usable."""
            self.refresh_button.setEnabled(False)
            self.recommended_button.setEnabled(False)
            failure: tuple[str, str] | None = None
            try:
                result = callback(argument) if argument else callback()
                if result is False:
                    failure = (f"{label} was not completed", "Try again or open Diagnostics for details.")
            except Exception as exc:
                failure = (f"{label} failed", str(exc))
            finally:
                self.refresh_button.setEnabled(True)
                self.refresh()
                if failure:
                    self._set_banner(*failure, "degraded")

        def _pilot_tag_finished(self) -> None:
            if not self._on_pilot_tag:
                return
            value = self.tag.text().strip()
            try:
                accepted = self._on_pilot_tag(value)
            except Exception as exc:
                accepted = False
                self._set_banner("Pilot tag could not be saved", str(exc), "degraded")
            if accepted:
                self._pilot_tag = value
                self.tag.setText(value)
                self.refresh()
            else:
                self.tag.setText(self._pilot_tag)
                self.tag.setToolTip("Pilot tag must be a non-empty Anki tag without whitespace.")

else:

    class ControlCenter:  # pragma: no cover - import-safe fallback only
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("The Neuro ICU TTS Control Center requires Anki/Qt")
