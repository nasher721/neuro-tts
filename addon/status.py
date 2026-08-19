"""Pure status contracts and presentation logic for the Control Center.

This module deliberately has no Anki or Qt imports.  Application adapters provide
the snapshots, while the view model turns them into copy that is safe to show to
people who do not need to read logs or configuration files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping


@dataclass(frozen=True)
class EngineSnapshot:
    """Read-only engine state supplied by the engine/configuration adapter."""

    configured: bool = False
    validated: bool = False
    test_succeeded: bool = False
    issue: str | None = None
    last_test: float | None = None
    details: str | None = None
    checks: Mapping[str, bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ScopeSnapshot:
    mode: str = "pilot"
    description: str = "Pilot notes"
    eligible_count: int | None = None
    selected_count: int | None = None
    last_scan: float | None = None


@dataclass(frozen=True)
class QueueSnapshot:
    counts: Mapping[str, int] = field(default_factory=dict)
    current_job: str | None = None
    current_note: int | None = None
    paused: bool = False
    latest_error: str | None = None

    def count(self, state: str) -> int:
        return int(self.counts.get(state, 0))


@dataclass(frozen=True)
class AttentionItem:
    category: str
    summary: str
    detail: str | None = None
    action: str | None = None
    priority: int = 0


@dataclass(frozen=True)
class ActivitySnapshot:
    last_scan: float | None = None
    last_generation: float | None = None
    recent_event: str | None = None


@dataclass(frozen=True)
class StatusSnapshot:
    engine: EngineSnapshot
    scope: ScopeSnapshot
    queue: QueueSnapshot
    attention: tuple[AttentionItem, ...] = ()
    activity: ActivitySnapshot = field(default_factory=ActivitySnapshot)


class StatusService:
    """Aggregate application-owned status adapters into one dashboard snapshot."""

    def __init__(
        self,
        engine: Callable[[], EngineSnapshot],
        scope: Callable[[], ScopeSnapshot],
        queue: Callable[[], QueueSnapshot],
        activity: Callable[[], ActivitySnapshot] | None = None,
    ) -> None:
        self._engine = engine
        self._scope = scope
        self._queue = queue
        self._activity = activity or ActivitySnapshot
        self._listeners: list[Callable[[StatusSnapshot], object]] = []

    def snapshot(self) -> StatusSnapshot:
        engine = self._engine()
        scope = self._scope()
        queue = self._queue()
        activity = self._activity()
        attention = _attention_for(engine, scope, queue)
        return StatusSnapshot(engine, scope, queue, attention, activity)

    def subscribe(self, listener: Callable[[StatusSnapshot], object]) -> Callable[[], None]:
        """Subscribe to refresh notifications and return a safe unsubscribe callback."""
        if listener not in self._listeners:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def notify(self) -> StatusSnapshot:
        """Re-read adapters and notify listeners after application activity."""
        snapshot = self.snapshot()
        for listener in tuple(self._listeners):
            listener(snapshot)
        return snapshot


@dataclass(frozen=True)
class StatusCard:
    title: str
    value: str
    detail: str
    state: str = "neutral"


@dataclass(frozen=True)
class Banner:
    title: str
    message: str
    action: str
    state: str


@dataclass(frozen=True)
class RecommendedAction:
    title: str
    message: str
    action: str
    state: str


@dataclass(frozen=True)
class OverviewViewModel:
    banner: Banner | None
    cards: tuple[StatusCard, ...]
    recommended: RecommendedAction
    advanced_details: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: StatusSnapshot) -> "OverviewViewModel":
        engine = snapshot.engine
        if not engine.configured:
            banner = Banner(
                "Set up the TTS engine",
                "Configure F5-TTS, a reference voice, and FFmpeg before generating audio.",
                "Run engine test",
                "setup",
            )
        elif engine.issue:
            banner = Banner(
                "Engine needs attention",
                plain_language_error(engine.issue),
                "Open Diagnostics",
                "degraded",
            )
        elif not engine.validated or not engine.test_succeeded:
            banner = Banner(
                "Finish the engine check",
                "The engine is configured but has not produced a successful test clip yet.",
                "Run engine test",
                "unvalidated",
            )
        else:
            banner = None

        cards = (
            _engine_card(snapshot),
            _scope_card(snapshot.scope),
            _queue_card(snapshot.queue),
            _attention_card(snapshot.attention),
            _activity_card(snapshot.activity),
        )
        recommended = recommended_action(snapshot)
        details = engine.details or engine.issue
        return cls(banner, cards, recommended, details)


def _attention_for(
    engine: EngineSnapshot, scope: ScopeSnapshot, queue: QueueSnapshot
) -> tuple[AttentionItem, ...]:
    items: list[AttentionItem] = []
    if engine.issue:
        items.append(
            AttentionItem("setup", plain_language_error(engine.issue), action="Open Diagnostics", priority=100)
        )
    if queue.latest_error:
        items.append(
            AttentionItem(
                "operational",
                plain_language_error(queue.latest_error),
                action="Review failures",
                priority=80,
            )
        )
    failed = queue.count("failed") + queue.count("failed_retryable") + queue.count("failed_terminal")
    if failed and not queue.latest_error:
        items.append(AttentionItem("operational", f"{failed} note(s) need review", action="Review failures", priority=70))
    skipped = queue.count("skipped")
    if skipped:
        items.append(AttentionItem("scope", f"{skipped} note(s) were skipped", action="Review scope", priority=40))
    if scope.eligible_count == 0:
        items.append(AttentionItem("scope", "No eligible notes are in the current scope", action="Review scope", priority=60))
    return tuple(sorted(items, key=lambda item: item.priority, reverse=True))


def recommended_action(snapshot: StatusSnapshot) -> RecommendedAction:
    engine = snapshot.engine
    if not engine.configured:
        return RecommendedAction("Configure the engine", "Add the required engine paths and reference voice, then run an engine test.", "Run engine test", "setup")
    if engine.issue:
        return RecommendedAction("Fix the engine setup", plain_language_error(engine.issue), "Open Diagnostics", "degraded")
    if not engine.validated or not engine.test_succeeded:
        return RecommendedAction("Run a test generation", "Confirm the configured engine can produce a short clip.", "Run engine test", "unvalidated")
    if snapshot.scope.mode == "full" and snapshot.scope.selected_count is None:
        return RecommendedAction("Choose a pilot scope", "Start with a small, explicit scope before scanning the full deck.", "Review scope", "scope")
    if snapshot.scope.eligible_count == 0:
        return RecommendedAction("Review the current scope", "No eligible notes are available in the current scope.", "Review scope", "scope")
    failures = sum(snapshot.queue.count(state) for state in ("failed", "failed_retryable", "failed_terminal"))
    if snapshot.queue.latest_error or failures or any(
        item.action == "Review failures" for item in snapshot.attention
    ):
        failure_count = failures or 1
        return RecommendedAction("Review failed notes", f"{failure_count} note(s) need attention before they can finish.", "Review failures", "degraded")
    queued = snapshot.queue.count("queued")
    if queued:
        return RecommendedAction("Review queued work", f"{queued} note(s) are waiting for generation.", "Open Queue", "active")
    return RecommendedAction("You are ready", "The engine and current scope are ready for normal operation.", "Open Queue", "ready")


def plain_language_error(error: str) -> str:
    """Map representative technical errors to actionable user-facing copy."""

    value = error.strip()
    lowered = value.lower()
    if "ffmpeg" in lowered:
        return "FFmpeg was not found. Install or configure FFmpeg, then run the engine test again."
    if "reference audio" in lowered:
        return "The reference voice file is missing. Choose a valid audio file, then test the engine again."
    if "cli" in lowered and "missing" in lowered:
        return "The F5-TTS CLI was not found. Check the repository path, then run the engine test again."
    if "timeout" in lowered:
        return "The engine test timed out. Check the device and model settings, then try again."
    if "python" in lowered or "executable" in lowered:
        return "The configured Python executable is unavailable. Check the engine path and try again."
    return value or "The TTS engine reported an unknown problem. Open Diagnostics for details."


def _engine_card(snapshot: StatusSnapshot) -> StatusCard:
    engine = snapshot.engine
    if engine.issue:
        return StatusCard("Engine", "Needs attention", plain_language_error(engine.issue), "degraded")
    if not engine.configured:
        return StatusCard("Engine", "Not configured", "Set up F5-TTS to enable generation.", "setup")
    if not engine.validated or not engine.test_succeeded:
        return StatusCard("Engine", "Not tested", "Run a test generation to confirm the setup.", "unvalidated")
    return StatusCard("Engine", "Ready", "The last engine test succeeded.", "ready")


def _scope_card(scope: ScopeSnapshot) -> StatusCard:
    count = "Unknown" if scope.eligible_count is None else str(scope.eligible_count)
    return StatusCard("Scope", scope.mode.title(), f"{scope.description} · {count} eligible note(s)", "neutral")


def _queue_card(queue: QueueSnapshot) -> StatusCard:
    queued = queue.count("queued")
    running = queue.count("running")
    completed = queue.count("succeeded") + queue.count("completed")
    paused = queue.count("paused") + (1 if queue.paused else 0)
    return StatusCard("Queue", f"{queued} queued", f"{running} generating · {completed} completed · {paused} paused", "active" if queued or running else "neutral")


def _attention_card(items: tuple[AttentionItem, ...]) -> StatusCard:
    if not items:
        return StatusCard("Attention needed", "None", "Nothing requires action right now.", "ready")
    return StatusCard("Attention needed", str(len(items)), items[0].summary, "degraded")


def _activity_card(activity: ActivitySnapshot) -> StatusCard:
    details = []
    if activity.last_scan is not None:
        details.append(f"Last scan: {_format_time(activity.last_scan)}")
    if activity.last_generation is not None:
        details.append(f"Last generation: {_format_time(activity.last_generation)}")
    if activity.recent_event:
        details.append(activity.recent_event)
    detail = " · ".join(details) or "No recent activity"
    return StatusCard("Activity", "Recent", detail, "neutral")


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
