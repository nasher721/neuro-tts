import unittest

try:
    from .status import (
        ActivitySnapshot,
        AttentionItem,
        EngineSnapshot,
        QueueSnapshot,
        ScopeSnapshot,
        StatusService,
        OverviewViewModel,
        StatusSnapshot,
        plain_language_error,
        recommended_action,
    )
except ImportError:  # Supports running this file from the add-on directory.
    from status import (
        ActivitySnapshot,
        AttentionItem,
        EngineSnapshot,
        QueueSnapshot,
        ScopeSnapshot,
        StatusService,
        OverviewViewModel,
        StatusSnapshot,
        plain_language_error,
        recommended_action,
    )


class StatusFixturesTests(unittest.TestCase):
    def snapshot(self, *, engine=None, scope=None, queue=None):
        return StatusSnapshot(
            engine or EngineSnapshot(),
            scope or ScopeSnapshot(),
            queue or QueueSnapshot(),
            activity=ActivitySnapshot(recent_event="fixture event"),
        )

    def test_service_aggregates_mixed_state_and_prioritizes_attention(self):
        service = StatusService(
            lambda: EngineSnapshot(configured=True, validated=True, test_succeeded=True),
            lambda: ScopeSnapshot(eligible_count=12, selected_count=12),
            lambda: QueueSnapshot({"queued": 3, "running": 1, "failed_retryable": 2}, latest_error="ffmpeg is required"),
        )

        result = service.snapshot()

        self.assertEqual(result.queue.count("queued"), 3)
        self.assertEqual(result.queue.count("running"), 1)
        self.assertEqual(result.attention[0].category, "operational")
        self.assertIn("FFmpeg was not found", result.attention[0].summary)

    def test_recommended_actions_cover_setup_validation_failure_and_ready_states(self):
        self.assertEqual(recommended_action(self.snapshot()).action, "Run engine test")
        self.assertEqual(
            recommended_action(self.snapshot(engine=EngineSnapshot(configured=True))).action,
            "Run engine test",
        )
        self.assertEqual(
            recommended_action(self.snapshot(engine=EngineSnapshot(configured=True, issue="ffmpeg missing"))).action,
            "Open Diagnostics",
        )
        self.assertEqual(
            recommended_action(
                self.snapshot(
                    engine=EngineSnapshot(configured=True, validated=True, test_succeeded=True),
                    queue=QueueSnapshot({"queued": 2}),
                )
            ).action,
            "Open Queue",
        )

    def test_recommended_action_prioritizes_queue_failure_over_queued_work(self):
        snapshot = self.snapshot(
            engine=EngineSnapshot(configured=True, validated=True, test_succeeded=True),
            queue=QueueSnapshot({"queued": 3}, latest_error="generation failed"),
        )
        self.assertEqual(recommended_action(snapshot).action, "Review failures")

    def test_recommended_action_prioritizes_attention_failure_over_queued_work(self):
        snapshot = self.snapshot(
            engine=EngineSnapshot(configured=True, validated=True, test_succeeded=True),
            queue=QueueSnapshot({"queued": 3}),
        )
        snapshot = StatusSnapshot(
            snapshot.engine,
            snapshot.scope,
            snapshot.queue,
            attention=(AttentionItem("operational", "failure", action="Review failures"),),
            activity=snapshot.activity,
        )
        self.assertEqual(recommended_action(snapshot).action, "Review failures")

    def test_recommended_action_flags_empty_scope_before_reporting_ready(self):
        snapshot = self.snapshot(
            engine=EngineSnapshot(configured=True, validated=True, test_succeeded=True),
            scope=ScopeSnapshot(eligible_count=0, selected_count=0),
        )
        self.assertEqual(recommended_action(snapshot).action, "Review scope")

    def test_subscribe_is_idempotent_for_same_listener(self):
        events = []
        service = StatusService(
            lambda: EngineSnapshot(), lambda: ScopeSnapshot(), lambda: QueueSnapshot()
        )
        def listener(snapshot):
            events.append(snapshot)

        service.subscribe(listener)
        service.subscribe(listener)
        service.notify()
        self.assertEqual(len(events), 1)

    def test_view_model_transitions_from_setup_to_ready_and_degraded(self):
        setup = OverviewViewModel.from_snapshot(self.snapshot())
        self.assertEqual(setup.banner.state, "setup")
        self.assertEqual(setup.cards[0].value, "Not configured")

        ready = OverviewViewModel.from_snapshot(
            self.snapshot(engine=EngineSnapshot(configured=True, validated=True, test_succeeded=True))
        )
        self.assertIsNone(ready.banner)
        self.assertEqual(ready.cards[0].value, "Ready")

        degraded = OverviewViewModel.from_snapshot(
            self.snapshot(engine=EngineSnapshot(configured=True, issue="FFmpeg was not found"))
        )
        self.assertEqual(degraded.banner.state, "degraded")
        self.assertIn("FFmpeg was not found", degraded.banner.message)

    def test_errors_are_presented_as_actionable_plain_language(self):
        self.assertIn("reference voice file is missing", plain_language_error("reference audio missing").lower())
        self.assertIn("timed out", plain_language_error("engine timeout").lower())
        self.assertEqual(plain_language_error("custom failure"), "custom failure")

    def test_status_events_refresh_listeners_from_fake_adapters(self):
        events = []
        queue_state = [QueueSnapshot({"queued": 1}), QueueSnapshot({"succeeded": 1})]
        service = StatusService(
            lambda: EngineSnapshot(configured=True, validated=True, test_succeeded=True),
            lambda: ScopeSnapshot(eligible_count=1, selected_count=1),
            lambda: queue_state[-1],
        )
        unsubscribe = service.subscribe(lambda snapshot: events.append(snapshot.queue.count("succeeded")))

        queue_state.append(QueueSnapshot({"succeeded": 2}))
        service.notify()
        unsubscribe()
        service.notify()

        self.assertEqual(events, [2])


if __name__ == "__main__":
    unittest.main()
