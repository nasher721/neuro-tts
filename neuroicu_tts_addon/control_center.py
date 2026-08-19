"""Qt presentation for the read-only Control Center Overview."""

from __future__ import annotations

from html import escape
from typing import Callable

from .status import OverviewViewModel, StatusService

try:  # Keep pure status/tests importable on machines without Anki or Qt.
    from aqt.qt import (
        QDialog,
        QFormLayout,
        QGridLayout,
        QGroupBox,
        QLabel,
        QLineEdit,
        QPushButton,
        QTimer,
        QVBoxLayout,
    )
except ImportError:  # pragma: no cover - exercised by non-Anki imports
    QDialog = None


if QDialog is not None:

    class ControlCenter(QDialog):
        """Dashboard renderer; application callbacks own all side effects."""

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
        ) -> None:
            super().__init__(parent)
            self._status_service = status_service
            self._on_scan = on_scan
            self._on_current = on_current
            self._on_action = on_action
            self._on_pilot_tag = on_pilot_tag
            self._pilot_tag = pilot_tag
            self._closed = False
            self._recommended_action = ""
            self._unsubscribe = status_service.subscribe(self._status_changed)
            self.setWindowTitle("Neuro ICU TTS Control Center")
            self.setMinimumWidth(680)
            self.setAccessibleName("Neuro ICU TTS Control Center")

            layout = QVBoxLayout(self)
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
            self.tag = QLineEdit(pilot_tag)
            self.tag.setAccessibleName("Pilot tag")
            self.tag.setPlaceholderText("Optional Anki tag")
            self.tag.setToolTip("Anki tag used to select pilot notes for generation.")
            if on_pilot_tag:
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

            close = QPushButton("Close")
            close.setAccessibleName("Close Control Center")
            close.clicked.connect(self.close)
            layout.addWidget(close)
            self.refresh()

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
            for index, card in enumerate(view.cards):
                box = QGroupBox(card.title)
                box.setAccessibleName(f"{card.title} status card")
                box_layout = QVBoxLayout(box)
                value = QLabel(card.value)
                value.setStyleSheet("font-size: 18px; font-weight: bold;")
                value.setAccessibleName(f"{card.title} value")
                detail = QLabel(card.detail)
                detail.setWordWrap(True)
                detail.setAccessibleName(f"{card.title} details")
                box_layout.addWidget(value)
                box_layout.addWidget(detail)
                self.cards.addWidget(box, index // 2, index % 2)

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
