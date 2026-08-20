"""Tests for worker retry/backoff, operator controls, and scope caching."""

import queue
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from . import main, tts_core
except ImportError:  # pragma: no cover - supports PYTHONPATH=addon discovery
    from addon import main, tts_core


def settings(**overrides):
    """Patch the numeric config accessors the worker reads per job."""
    ints = {"max_attempts": 3, "max_chunk_chars": 800}
    floats = {"retry_backoff_seconds": 0.05}
    ints.update({k: v for k, v in overrides.items() if k in ints})
    floats.update({k: v for k, v in overrides.items() if k in floats})
    return (
        patch.object(main.config, "get_int", side_effect=lambda key: ints[key]),
        patch.object(main.config, "get_float", side_effect=lambda key: floats[key]),
    )


class WorkerHarness:
    """Start a worker against a temporary store and shut it down cleanly."""

    def __init__(self, test, **overrides):
        self.test = test
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "jobs.sqlite3"
        self.results = queue.Queue()
        self.worker = main.TTSWorker(self.db, root / "media", self.results)
        self.patches = list(settings(**overrides))

    def start(self, tts):
        self.patches.append(patch.object(main, "_tts_file", side_effect=tts))
        for item in self.patches:
            item.start()
        self.worker.start()
        return self.worker

    def state(self, job_id):
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute("SELECT state, attempts FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row

    def close(self):
        self.worker.stop()
        self.worker.join(timeout=5)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()


class RetryTests(unittest.TestCase):
    def setUp(self):
        main._status_events = queue.Queue()
        main._status_service_instance = None

    def test_transient_failure_is_retried_and_then_succeeds(self):
        harness = WorkerHarness(self, max_attempts=3)
        attempts = []

        def flaky(_text, destination, _cancel=None):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("engine hiccup")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"mp3")

        try:
            worker = harness.start(flaky)
            worker.submit(main.Job("flaky", 7, "hello", "a" * 64))
            result = harness.results.get(timeout=10)
            self.assertIsNone(result.error)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(harness.state("flaky"), ("staged", 3))
        finally:
            harness.close()

    def test_failure_becomes_terminal_after_max_attempts(self):
        harness = WorkerHarness(self, max_attempts=2)
        attempts = []

        def always_fails(_text, _destination, _cancel=None):
            attempts.append(1)
            raise RuntimeError("engine is down")

        try:
            worker = harness.start(always_fails)
            worker.submit(main.Job("doomed", 8, "hello", "b" * 64))
            result = harness.results.get(timeout=10)
            self.assertIn("engine is down", result.error)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(harness.state("doomed"), ("failed_terminal", 2))
        finally:
            harness.close()

    def test_backoff_grows_with_each_attempt(self):
        worker = main.TTSWorker(Path(tempfile.gettempdir()) / "unused.sqlite3", Path("."), queue.Queue())
        with patch.object(main.config, "get_float", return_value=10.0):
            first = worker._schedule_retry(main.Job("a", 1, "t", "d" * 64), 1)
            second = worker._schedule_retry(main.Job("b", 2, "t", "e" * 64), 2)
        self.assertEqual((first, second), (10.0, 20.0))

    def test_cancellation_is_not_retried(self):
        harness = WorkerHarness(self, max_attempts=5)
        attempts = []

        def cancelled(_text, _destination, _cancel=None):
            attempts.append(1)
            raise RuntimeError("command cancelled: python")

        try:
            worker = harness.start(cancelled)
            worker.submit(main.Job("stopped", 9, "hello", "c" * 64))
            harness.results.get(timeout=10)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(harness.state("stopped")[0], "cancelled")
        finally:
            harness.close()


class OperatorControlTests(unittest.TestCase):
    def setUp(self):
        main._status_events = queue.Queue()
        main._status_service_instance = None

    def test_pause_holds_work_until_resumed(self):
        harness = WorkerHarness(self)
        started = queue.Queue()

        def record(_text, destination, _cancel=None):
            started.put(1)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"mp3")

        try:
            worker = harness.start(record)
            worker.pause()
            worker.submit(main.Job("held", 10, "hello", "d" * 64))
            with self.assertRaises(queue.Empty):
                started.get(timeout=0.5)
            self.assertTrue(worker.snapshot().paused)
            worker.resume()
            self.assertEqual(started.get(timeout=5), 1)
            self.assertIsNone(harness.results.get(timeout=5).error)
        finally:
            harness.close()

    def test_cancel_pending_drops_queued_and_held_jobs(self):
        harness = WorkerHarness(self)
        try:
            worker = harness.start(lambda *a, **k: None)
            worker.pause()
            for index in range(3):
                worker.submit(main.Job(f"job-{index}", index, "hello", chr(97 + index) * 64))
            deadline = time.time() + 3
            while len(worker._held) < 3 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(worker.cancel_pending(), 3)
            self.assertEqual(worker._held, [])
            # Cancelled note/digest pairs are released so they can be re-queued.
            self.assertEqual(worker.pending, set())
        finally:
            harness.close()

    def test_snapshot_reports_pause_state(self):
        worker = main.TTSWorker(Path(tempfile.gettempdir()) / "unused2.sqlite3", Path("."), queue.Queue())
        self.assertFalse(worker.snapshot().paused)
        worker.pause()
        self.assertTrue(worker.snapshot().paused)


class JobStoreTests(unittest.TestCase):
    def store(self, root):
        return main.JobStore(Path(root) / "jobs.sqlite3")

    def test_attempts_column_is_added_to_a_pre_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, note_id INTEGER, source TEXT,"
                    " digest TEXT, state TEXT, error TEXT, updated REAL)"
                )
                connection.execute(
                    "INSERT INTO jobs VALUES ('legacy', 1, 'text', 'x', 'queued', NULL, 0)"
                )
                connection.commit()
            store = main.JobStore(path)
            try:
                self.assertEqual(store.record_attempt("legacy"), 1)
                self.assertEqual(store.counts_by_status(), {"queued": 1})
            finally:
                store.close()

    def test_re_enqueueing_a_job_preserves_its_attempt_count(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            try:
                job = main.Job("j", 1, "text", "f" * 64)
                store.enqueue(job)
                store.record_attempt("j")
                store.record_attempt("j")
                store.enqueue(job)
                self.assertEqual(store.record_attempt("j"), 3)
            finally:
                store.close()

    def test_retryable_jobs_resets_state_and_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            try:
                store.enqueue(main.Job("bad", 1, "text", "g" * 64))
                store.record_attempt("bad")
                store.set_state("bad", "failed_terminal", "boom")
                rows = store.retryable_jobs()
                self.assertEqual([row[0] for row in rows], ["bad"])
                self.assertEqual(store.counts_by_status(), {"queued": 1})
                self.assertEqual(store.record_attempt("bad"), 1)
            finally:
                store.close()

    def test_cancel_pending_only_touches_unfinished_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            try:
                store.enqueue(main.Job("waiting", 1, "text", "h" * 64))
                store.enqueue(main.Job("done", 2, "text", "i" * 64))
                store.set_state("done", "succeeded")
                self.assertEqual(store.cancel_pending(), 1)
                self.assertEqual(store.counts_by_status(), {"cancelled": 1, "succeeded": 1})
            finally:
                store.close()

    def test_cancelled_digests_do_not_block_re_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            try:
                store.enqueue(main.Job("waiting", 1, "text", "j" * 64))
                store.cancel_pending()
                self.assertEqual(store.existing_digests(), set())
            finally:
                store.close()


class ScopeCacheTests(unittest.TestCase):
    def setUp(self):
        main._invalidate_scope_cache()
        self.addCleanup(main._invalidate_scope_cache)

    def collection(self, counter):
        class FakeCollection:
            def find_notes(self, _query):
                counter.append(1)
                return [1, 2, 3]

            def get_note(self, note_id):
                return SimpleNamespace(id=note_id)

        return SimpleNamespace(col=FakeCollection())

    def test_repeated_snapshots_do_not_rescan_the_collection(self):
        scans = []
        original = main.mw
        main.mw = self.collection(scans)
        try:
            with patch.object(main, "_eligible", return_value=True):
                first = main._scope_snapshot()
                for _ in range(20):
                    main._scope_snapshot()
            self.assertEqual(first.eligible_count, 3)
            self.assertEqual(len(scans), 1)
        finally:
            main.mw = original

    def test_invalidation_forces_a_fresh_count(self):
        scans = []
        original = main.mw
        main.mw = self.collection(scans)
        try:
            with patch.object(main, "_eligible", return_value=True):
                main._scope_snapshot()
                main._invalidate_scope_cache()
                main._scope_snapshot()
            self.assertEqual(len(scans), 2)
        finally:
            main.mw = original

    def test_a_broken_collection_reports_unknown_rather_than_raising(self):
        class BrokenCollection:
            def find_notes(self, _query):
                raise RuntimeError("collection is closed")

        original = main.mw
        main.mw = SimpleNamespace(col=BrokenCollection())
        try:
            snapshot = main._scope_snapshot()
        finally:
            main.mw = original
        self.assertIsNone(snapshot.eligible_count)

    def test_deck_eligibility_is_memoized_per_scan(self):
        lookups = []

        def fake_lookup(did):
            lookups.append(did)
            return True

        main._invalidate_scope_cache()
        with patch.object(main, "_deck_eligible_uncached", side_effect=fake_lookup):
            for _ in range(5):
                main._is_deck_eligible(1)
            main._is_deck_eligible(2)
        self.assertEqual(lookups, [1, 2])


class ManagedFilenameTests(unittest.TestCase):
    def test_generated_names_are_recognized(self):
        name = tts_core.filename(1234, "a" * 64)
        self.assertTrue(tts_core.is_managed_filename(name))

    def test_foreign_and_empty_names_are_rejected(self):
        for name in (None, "", "user-recording.mp3", "neuroicu_tts_1-short.mp3", "../evil.mp3"):
            self.assertFalse(tts_core.is_managed_filename(name), name)

    def test_player_markup_escapes_the_filename(self):
        markup = tts_core.player_html('evil.mp3" onerror="alert(1)')
        self.assertNotIn('onerror="alert(1)"', markup)
        self.assertIn("&quot;", markup)

    def test_generated_filenames_round_trip_through_the_player(self):
        name = tts_core.filename(42, "b" * 64)
        self.assertIn(f'src="{name}"', tts_core.player_html(name))


if __name__ == "__main__":
    unittest.main()
