import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import os
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from . import main
except ImportError:  # Supports PYTHONPATH=addon unittest discovery.
    from addon import main


class WorkerAndEngineTests(unittest.TestCase):
    def setUp(self):
        main._status_events = __import__("queue").Queue()
        main._status_service_instance = None

    def test_worker_processes_fake_job_without_sqlite_cross_thread_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = __import__("queue").Queue()

            def fake_tts(_text, destination, _cancel_event=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"fake mp3")

            worker = main.TTSWorker(root / "jobs.sqlite3", root / "media", results)
            with patch.object(main, "_tts_file", side_effect=fake_tts):
                worker.start()
                self.assertTrue(worker.submit(main.Job("job-1", 42, "hello", "d" * 64)))
                result = results.get(timeout=3)
                worker.mark_committed(result.job.job_id, True)
                worker.stop()
                worker.join(timeout=3)

            self.assertFalse(worker.is_alive())
            self.assertIsNone(result.error)
            self.assertTrue(result.artifact.exists())
            with closing(sqlite3.connect(root / "jobs.sqlite3")) as connection:
                state = connection.execute("SELECT state FROM jobs WHERE job_id='job-1'").fetchone()[0]
            self.assertEqual(state, "succeeded")

    def test_worker_rejects_submissions_after_stop_and_closes_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = main.TTSWorker(root / "jobs.sqlite3", root / "media", __import__("queue").Queue())
            worker.start()
            deadline = time.time() + 3
            while worker.store is None and time.time() < deadline:
                time.sleep(0.01)
            worker.stop()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(worker.stopped)
            self.assertFalse(worker.submit(main.Job("late", 1, "text", "e" * 64)))

    def test_worker_shutdown_does_not_start_queued_synthesis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = threading.Event()
            release = threading.Event()
            calls = []

            def blocking_tts(text, destination, _cancel_event=None):
                calls.append(text)
                started.set()
                release.wait(timeout=3)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"mp3")

            worker = main.TTSWorker(root / "jobs.sqlite3", root / "media", __import__("queue").Queue())
            with patch.object(main, "_tts_file", side_effect=blocking_tts):
                worker.start()
                self.assertTrue(worker.submit(main.Job("first", 1, "first", "a" * 64)))
                self.assertTrue(worker.submit(main.Job("second", 2, "second", "b" * 64)))
                self.assertTrue(started.wait(timeout=3))
                worker.stop()
                release.set()
                worker.join(timeout=3)

            self.assertEqual(calls, ["first"])
            with closing(sqlite3.connect(root / "jobs.sqlite3")) as connection:
                state = connection.execute("SELECT state FROM jobs WHERE job_id='second'").fetchone()[0]
            self.assertEqual(state, "queued")

    def test_worker_recovers_running_jobs_with_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "jobs.sqlite3")) as connection:
                connection.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, note_id INTEGER, source TEXT, digest TEXT, state TEXT, error TEXT, updated REAL)")
                connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, NULL, ?)", ("persisted", 7, "hello", "f" * 64, "running", time.time()))
                connection.commit()
            results = __import__("queue").Queue()
            worker = main.TTSWorker(root / "jobs.sqlite3", root / "media", results)
            with patch.object(main, "_tts_file", side_effect=AssertionError("recovery synthesized before revalidation")):
                worker.start()
                recovered = results.get(timeout=3)
                worker.stop()
                worker.join(timeout=3)
            self.assertIsInstance(recovered, main.RecoveredJob)
            self.assertEqual(recovered.job.job_id, "persisted")
            self.assertEqual(recovered.job.note_id, 7)

    def test_worker_recovers_retryable_commit_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "jobs.sqlite3")) as connection:
                connection.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, note_id INTEGER, source TEXT, digest TEXT, state TEXT, error TEXT, updated REAL)")
                connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)", ("retry", 8, "hello", "f" * 64, "failed_retryable", "write failed", time.time()))
                connection.commit()
            results = __import__("queue").Queue()
            worker = main.TTSWorker(root / "jobs.sqlite3", root / "media", results)
            with patch.object(main, "_tts_file", side_effect=AssertionError("retry synthesized before revalidation")):
                worker.start()
                recovered = results.get(timeout=3)
                worker.stop()
                worker.join(timeout=3)
            self.assertIsInstance(recovered, main.RecoveredJob)
            self.assertEqual(recovered.job.job_id, "retry")

    def test_subprocess_error_is_bounded_and_actionable(self):
        with self.assertRaisesRegex(RuntimeError, "exit 2") as raised:
            main._run_process([sys.executable, "-c", "import sys; print('x' * 2000, file=sys.stderr); raise SystemExit(2)"], timeout=2)
        self.assertLess(len(str(raised.exception)), 1500)

    def test_subprocess_can_be_cancelled(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            main._run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=20, cancel_event=cancelled)

    def test_mps_segmentation_fault_retries_inference_on_cpu(self):
        calls = []

        def fake_process(args, **_kwargs):
            calls.append(args[:])
            if len(calls) == 1:
                raise RuntimeError("command failed with exit -11: SIGSEGV")

        with patch.object(main, "F5_TTS_REPO", "/engine"), patch.object(main, "F5_TTS_PYTHON", "python"), patch.object(main, "F5_MODEL", "model"), patch.object(main, "F5_REF_AUDIO", "reference.wav"), patch.object(main, "F5_REF_TEXT", "reference"), patch.object(main.config, "get_device", return_value="mps"), patch.object(main, "_run_process", side_effect=fake_process):
            main._run_f5_inference("hello", Path("/tmp/wav"))

        self.assertEqual(len(calls), 2)
        first_device = calls[0][calls[0].index("--device") + 1]
        second_device = calls[1][calls[1].index("--device") + 1]
        self.assertEqual((first_device, second_device), ("mps", "cpu"))

    def test_non_mps_failure_is_not_retried(self):
        with patch.object(main.config, "get_device", return_value="mps"), patch.object(main, "_run_process", side_effect=RuntimeError("command failed with exit 2")) as run_process:
            with self.assertRaisesRegex(RuntimeError, "exit 2"):
                main._run_f5_inference("hello", Path("/tmp/wav"))
        run_process.assert_called_once()

    def test_ffmpeg_resolves_homebrew_path_when_gui_path_is_missing(self):
        with patch.object(main.shutil, "which", return_value=None), patch.object(main.Path, "is_file", return_value=True), patch.object(main.os, "access", return_value=True):
            self.assertEqual(main._ffmpeg_path(), "/opt/homebrew/bin/ffmpeg")

    def test_profile_shutdown_is_idempotent_and_clears_runtime(self):
        timer = SimpleNamespace(stop=unittest.mock.Mock())
        worker = SimpleNamespace(stop=unittest.mock.Mock(), join=unittest.mock.Mock())
        action = SimpleNamespace(deleteLater=unittest.mock.Mock())
        menu = SimpleNamespace(removeAction=unittest.mock.Mock())
        shortcut = SimpleNamespace(setEnabled=unittest.mock.Mock(), deleteLater=unittest.mock.Mock())
        engine_thread = SimpleNamespace(is_alive=unittest.mock.Mock(side_effect=[True, False]), join=unittest.mock.Mock())
        original_mw, original_engine_thread = main.mw, main._engine_test_thread
        main._engine_test_thread = engine_thread
        main.mw = SimpleNamespace(_neuroicu_tts_worker=worker, _neuroicu_tts_timer=timer, _neuroicu_tts_initialized=True, _neuroicu_tts_menu_actions=[action], _neuroicu_tts_shortcut=shortcut, form=SimpleNamespace(menuTools=menu))
        try:
            main._shutdown_profile_runtime()
            main._shutdown_profile_runtime()
            worker.stop.assert_called_once_with()
            worker.join.assert_called_once()
            timer.stop.assert_called_once_with()
            menu.removeAction.assert_called_once_with(action)
            action.deleteLater.assert_called_once_with()
            shortcut.setEnabled.assert_called_once_with(False)
            shortcut.deleteLater.assert_called_once_with()
            engine_thread.join.assert_called_once_with(timeout=5)
            self.assertFalse(main.mw._neuroicu_tts_initialized)
        finally:
            main.mw = original_mw
            main._engine_test_thread = original_engine_thread

    def test_runtime_hooks_are_not_duplicated_across_profile_reopens(self):
        hooks = SimpleNamespace(sync_did_finish=[], note_will_be_updated=[])
        original_gui_hooks = getattr(main, "gui_hooks", None)
        original_flags = main._sync_hook_registered, main._note_hook_registered
        try:
            main.gui_hooks = hooks
            main._sync_hook_registered = False
            main._note_hook_registered = False
            main._register_runtime_hooks()
            main._register_runtime_hooks()
            self.assertEqual(hooks.sync_did_finish, [main._after_sync])
            self.assertEqual(hooks.note_will_be_updated, [main._note_will_update])
        finally:
            if original_gui_hooks is not None:
                main.gui_hooks = original_gui_hooks
            else:
                del main.gui_hooks
            main._sync_hook_registered, main._note_hook_registered = original_flags

    def test_commit_failure_is_reported_without_escaping_drain_loop(self):
        class FakeNote(dict):
            id = 42

        source_digest = "a" * 64
        profile_digest = "b" * 64
        digest = main.generation_digest(source_digest, profile_digest)
        note = FakeNote(Extra="hello")
        worker = SimpleNamespace(mark_committed=unittest.mock.Mock())
        original_mw = main.mw
        main.mw = SimpleNamespace(
            col=SimpleNamespace(
                get_note=lambda _note_id: note,
                update_note=unittest.mock.Mock(side_effect=RuntimeError("write failed")),
            ),
            _neuroicu_tts_worker=worker,
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact = root / "staged.mp3"
                artifact.write_bytes(b"mp3")
                with patch.object(main, "state_for", return_value=SimpleNamespace(digest=source_digest)), patch.object(main, "_synthesis_profile_digest", return_value=profile_digest), patch.object(main, "_media_dir", return_value=root / "media"):
                    self.assertFalse(main._commit(main.Result(main.Job("job", 42, "hello", digest), artifact)))
            self.assertIn("write failed", main._last_event)
            worker.mark_committed.assert_called_once_with("job", False, "write failed")
        finally:
            main.mw = original_mw

    def test_successful_commit_moves_staged_audio_and_writes_v2_marker(self):
        class FakeNote(dict):
            id = 42

        source_digest, profile_digest = main.digest_for("hello"), "b" * 64
        artifact_digest = main.generation_digest(source_digest, profile_digest)
        note = FakeNote(Extra="hello")
        worker = SimpleNamespace(mark_committed=unittest.mock.Mock())
        original_mw = main.mw
        update_note = unittest.mock.Mock()
        main.mw = SimpleNamespace(col=SimpleNamespace(get_note=lambda _note_id: note, update_note=update_note), _neuroicu_tts_worker=worker)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact = root / "staged.mp3"
                artifact.write_bytes(b"mp3")
                with patch.object(main, "_synthesis_profile_digest", return_value=profile_digest), patch.object(main, "_media_dir", return_value=root / "media"):
                    self.assertTrue(main._commit(main.Result(main.Job("job", 42, "hello", artifact_digest), artifact)))
                expected = root / "media" / main.filename(42, artifact_digest)
                self.assertEqual(expected.read_bytes(), b"mp3")
            self.assertIn("neuroicu-tts:v2", note["Extra"])
            update_note.assert_called_once_with(note)
            worker.mark_committed.assert_called_once_with("job", True)
        finally:
            main.mw = original_mw

    def test_delayed_note_callback_is_rejected_after_profile_switch(self):
        original_mw, original_session = main.mw, main._profile_session_id
        submitted = []
        main.mw = SimpleNamespace(col=SimpleNamespace(get_note=lambda note_id: submitted.append(note_id)))
        main._profile_session_id = "new-profile"
        try:
            self.assertFalse(main._queue_note_if_current(42, "old-profile"))
            self.assertEqual(submitted, [])
        finally:
            main.mw, main._profile_session_id = original_mw, original_session

    def test_recovered_job_is_revalidated_against_current_scope(self):
        results = __import__("queue").Queue()
        recovered = main.RecoveredJob(main.Job("old", 42, "hello", "a" * 64))
        results.put(recovered)
        worker = SimpleNamespace(results=results, mark_committed=unittest.mock.Mock())
        original_mw = main.mw
        main.mw = SimpleNamespace(
            col=SimpleNamespace(get_note=lambda _note_id: {"Extra": "hello"}),
            _neuroicu_tts_worker=worker,
            reset=unittest.mock.Mock(),
        )
        try:
            with patch.object(main, "_eligible", return_value=False), patch.object(main, "_submit") as submit:
                main._drain()
            submit.assert_not_called()
            worker.mark_committed.assert_called_once_with("old", False)
            main.mw.reset.assert_not_called()
        finally:
            main.mw = original_mw

    def test_fake_engine_test_persists_ready_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "src" / "f5_tts" / "infer" / "infer_cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text("# fake cli", encoding="utf-8")
            reference = root / "reference.wav"
            reference.write_bytes(b"fake")

            def fake_tts(_text, destination, _cancel_event=None):
                destination.write_bytes(b"fake mp3")

            values = {
                "F5_TTS_REPO": str(root),
                "F5_TTS_PYTHON": sys.executable,
                "F5_REF_AUDIO": str(reference),
            }
            patches = [patch.object(main, name, value) for name, value in values.items()]
            patches.append(patch.object(main.config, "get_device", return_value="cpu"))
            patches.append(patch.object(main.shutil, "which", return_value="/usr/bin/ffmpeg"))
            with ExitStack() as stack:
                stack.enter_context(patch.object(main, "_tts_file", side_effect=fake_tts))
                for item in patches:
                    stack.enter_context(item)
                main._engine_session = main.EngineSession()
                self.assertTrue(main._run_engine_test())
                snapshot = main._engine_snapshot()

            self.assertTrue(snapshot.validated)
            self.assertTrue(snapshot.test_succeeded)
            self.assertIsNone(snapshot.issue)
            self.assertTrue(snapshot.checks["ffmpeg"])

    def test_fake_engine_generation_failure_is_retained(self):
        main._engine_session = main.EngineSession()
        with patch.object(main, "F5_TTS_REPO", ""), patch.object(main, "F5_REF_AUDIO", ""):
            self.assertFalse(main._run_engine_test())
            snapshot = main._engine_snapshot()
        self.assertFalse(snapshot.test_succeeded)
        self.assertIsNotNone(snapshot.issue)

    def test_stale_profile_engine_test_is_ignored(self):
        original_session = main._profile_session_id
        main._profile_session_id = "current"
        try:
            with patch.object(main, "_tts_file", side_effect=AssertionError("stale test ran")):
                self.assertFalse(main._run_engine_test("previous"))
        finally:
            main._profile_session_id = original_session

    def test_dashboard_snapshot_does_not_run_device_probe_on_ui_thread(self):
        with patch.object(main, "F5_TTS_REPO", "configured"), patch.object(main, "F5_REF_AUDIO", "voice.wav"), patch.object(main, "F5_TTS_PYTHON", sys.executable), patch.object(main, "_device_available", side_effect=AssertionError("device probe ran on UI thread")):
            main._engine_session = main.EngineSession()
            with patch.object(main.Path, "exists", return_value=True), patch.object(main.shutil, "which", return_value="/usr/bin/ffmpeg"):
                snapshot = main._engine_snapshot()
        self.assertIsNone(snapshot.checks["device"])

    def test_engine_session_snapshot_copies_shared_state(self):
        main._engine_session = main.EngineSession(checks={"device": True})
        first = main._engine_session_snapshot()
        main._engine_session.checks["device"] = False
        self.assertTrue(first.checks["device"])

    def test_dashboard_actions_dispatch_to_real_application_callbacks(self):
        with patch.object(main, "_start_engine_test", return_value=True) as engine_test:
            self.assertTrue(main._dashboard_action("Run engine test"))
            engine_test.assert_called_once_with()
        # Navigation actions switch tabs on the live dialog (G1/G2/G5 surfaces).
        dialog = SimpleNamespace(show_tab=unittest.mock.Mock(return_value=True))
        original_mw = main.mw
        try:
            main.mw = SimpleNamespace(_neuroicu_tts_dialog=dialog)
            for action in ("Open Queue", "Review failures", "Review scope", "Open Voice & engine"):
                self.assertTrue(main._dashboard_action(action))
            dialog.show_tab.assert_any_call("Queue")
            dialog.show_tab.assert_any_call("Scope")
            dialog.show_tab.assert_any_call("Settings")
        finally:
            main.mw = original_mw
        # Without a live dialog the navigation actions report failure.
        with patch.object(main, "scan_collection", side_effect=AssertionError("dashboard route scanned collection")):
            for action in ("Open Queue", "Review failures", "Review scope", "Open Voice & engine"):
                self.assertFalse(main._dashboard_action(action))
        diagnostics = main.EngineSnapshot(configured=True, issue="fixture")
        with patch.object(main, "_engine_snapshot", return_value=diagnostics):
            self.assertIs(main._dashboard_action("Open Diagnostics"), diagnostics)
        self.assertFalse(main._dashboard_action("unknown action"))

    def test_worker_status_events_do_not_evaluate_collection_off_thread(self):
        notifications = []
        collection_threads = []

        class FakeCollection:
            def find_notes(self, _query):
                collection_threads.append(threading.current_thread())
                return []

        service = main.StatusService(
            lambda: main.EngineSnapshot(configured=True),
            main._scope_snapshot,
            lambda: main.QueueSnapshot(),
        )
        main._status_service_instance = service
        service.subscribe(lambda snapshot: notifications.append(threading.current_thread()))

        original_mw = main.mw
        main.mw = SimpleNamespace(col=FakeCollection())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = __import__("queue").Queue()
            worker = main.TTSWorker(root / "jobs.sqlite3", root / "media", results)
            with patch.object(main, "_tts_file", side_effect=lambda _text, destination, _cancel_event=None: destination.write_bytes(b"mp3")):
                worker.start()
                self.assertTrue(worker.submit(main.Job("boundary", 1, "hello", "a" * 64)))
                results.get(timeout=3)
                worker.stop()
                worker.join(timeout=3)
        self.assertEqual(notifications, [])
        self.assertTrue(main._drain_status_events())
        self.assertTrue(notifications)
        self.assertEqual(collection_threads, [threading.current_thread()])
        main.mw = original_mw

    def test_scan_collection_reports_collection_error_without_crashing(self):
        class BrokenCollection:
            def find_notes(self, _query):
                raise RuntimeError("database disk image is malformed")

        original_mw = main.mw
        main.mw = SimpleNamespace(col=BrokenCollection())
        try:
            with patch.object(main, "_emit_status") as emit_status:
                self.assertEqual(main.scan_collection(), 0)
            self.assertIn("database disk image is malformed", main._last_event)
            emit_status.assert_called_once_with()
        finally:
            main.mw = original_mw

    def test_submit_reports_worker_deduplication(self):
        class FakeNote(dict):
            id = 42

        note = FakeNote(Extra="hello")
        worker = SimpleNamespace(submit=lambda _job: False)
        original_mw = main.mw
        main.mw = SimpleNamespace(_neuroicu_tts_worker=worker)
        state = SimpleNamespace(digest="d" * 64, marker_digest=None, sound_filename=None, profile_digest=None, text="hello")
        try:
            with patch.object(main, "_eligible", return_value=True), patch.object(main, "state_for", return_value=state):
                self.assertFalse(main._submit(note))
        finally:
            main.mw = original_mw

    def test_submit_regenerates_when_managed_media_is_missing(self):
        class FakeNote(dict):
            id = 42

        note = FakeNote(Extra="hello")
        source_digest = "d" * 64
        profile_digest = "e" * 64
        digest = main.generation_digest(source_digest, profile_digest)
        expected = main.filename(note.id, digest)
        worker = SimpleNamespace(submit=unittest.mock.Mock(return_value=True))
        original_mw = main.mw
        main.mw = SimpleNamespace(_neuroicu_tts_worker=worker)
        state = SimpleNamespace(digest=source_digest, marker_digest=source_digest, sound_filename=expected, profile_digest=profile_digest, text="hello")
        try:
            with patch.object(main, "_eligible", return_value=True), patch.object(main, "state_for", return_value=state), patch.object(main, "_synthesis_profile_digest", return_value=profile_digest), patch.object(main, "_managed_media_exists", return_value=False):
                self.assertTrue(main._submit(note))
            worker.submit.assert_called_once()
        finally:
            main.mw = original_mw

    def test_submit_regenerates_when_synthesis_profile_changes(self):
        class FakeNote(dict):
            id = 42

        note = FakeNote(Extra="hello")
        source_digest, old_profile, new_profile = "d" * 64, "e" * 64, "f" * 64
        old_filename = main.filename(note.id, main.generation_digest(source_digest, old_profile))
        state = SimpleNamespace(digest=source_digest, marker_digest=source_digest, sound_filename=old_filename, profile_digest=old_profile, text="hello")
        worker = SimpleNamespace(submit=unittest.mock.Mock(return_value=True))
        original_mw = main.mw
        main.mw = SimpleNamespace(_neuroicu_tts_worker=worker)
        try:
            with patch.object(main, "_eligible", return_value=True), patch.object(main, "state_for", return_value=state), patch.object(main, "_synthesis_profile_digest", return_value=new_profile), patch.object(main, "_managed_media_exists", return_value=True):
                self.assertTrue(main._submit(note))
            submitted = worker.submit.call_args.args[0]
            self.assertEqual(submitted.digest, main.generation_digest(source_digest, new_profile))
        finally:
            main.mw = original_mw

    def test_synthesis_profile_includes_engine_and_python_identity(self):
        with patch.object(main, "F5_TTS_REPO", "/engine/a"), patch.object(main, "F5_TTS_PYTHON", "/python/a"):
            first = main._synthesis_profile_digest()
        with patch.object(main, "F5_TTS_REPO", "/engine/b"), patch.object(main, "F5_TTS_PYTHON", "/python/b"):
            second = main._synthesis_profile_digest()
        self.assertNotEqual(first, second)

    def test_successful_generation_clears_stale_worker_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = __import__("queue").Queue()
            calls = [RuntimeError("old failure"), None]

            def fake_tts(_text, destination, _cancel_event=None):
                failure = calls.pop(0)
                if failure:
                    raise failure
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"mp3")

            worker = main.TTSWorker(root / "jobs.sqlite3", root / "media", results)
            with patch.object(main, "_tts_file", side_effect=fake_tts):
                worker.start()
                first = main.Job("failed", 1, "hello", "b" * 64)
                second = main.Job("succeeded", 2, "hello", "c" * 64)
                self.assertTrue(worker.submit(first))
                self.assertIsNotNone(results.get(timeout=3).error)
                self.assertTrue(worker.submit(second))
                self.assertIsNone(results.get(timeout=3).error)
                worker.stop()
                worker.join(timeout=3)

            self.assertIsNone(worker.snapshot().latest_error)

    def test_pilot_tag_update_validates_updates_scope_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            original_path, original_tag, original_pilot_only = main.config.CONFIG_PATH, main.PILOT_TAG, main.PILOT_ONLY
            try:
                main.config.CONFIG_PATH = config_path
                main.config.load()
                main.PILOT_ONLY = True
                self.assertFalse(main._update_pilot_tag("bad tag"))
                self.assertTrue(main._update_pilot_tag("new-pilot"))
                self.assertEqual(main.PILOT_TAG, "new-pilot")
                self.assertIn('"pilot_tag": "new-pilot"', config_path.read_text(encoding="utf-8"))
                self.assertIn("new-pilot", main._scope_snapshot().description)
            finally:
                main.config.CONFIG_PATH = original_path
                main.config.load()
                main.PILOT_TAG, main.PILOT_ONLY = original_tag, original_pilot_only

    def test_pilot_tag_write_failure_does_not_mutate_live_configuration(self):
        original_path, original_tag = main.config.CONFIG_PATH, main.PILOT_TAG
        try:
            main.config.CONFIG_PATH = Path("/path/that/does/not/exist/config.json")
            main.PILOT_TAG = "original"
            self.assertFalse(main._update_pilot_tag("replacement"))
            self.assertEqual(main.PILOT_TAG, "original")
            self.assertNotEqual(main.config.get("pilot_tag"), "replacement")
        finally:
            main.config.CONFIG_PATH = original_path
            main.config.load()
            main.PILOT_TAG = original_tag

    def test_inference_reads_safe_knobs_live_from_config(self):
        # Step 3 success criterion: speed/device changes reach the worker at next job start.
        captured = []
        with patch.object(main.config, "get_device", return_value="cuda"), patch.object(main.config, "get_speed", return_value="1.75"), patch.object(main, "_run_process", side_effect=lambda args, **_kw: captured.append(args)):
            main._run_f5_inference("hello", Path("/tmp/wav"))
        args = captured[0]
        self.assertEqual(args[args.index("--speed") + 1], "1.75")
        self.assertEqual(args[args.index("--device") + 1], "cuda")


class JobStoreExtensionTests(unittest.TestCase):
    """Step 2 — counts_by_status / clear_finished / existing_digests (G2, G4, G11)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = main.JobStore(Path(self._tmp.name) / "jobs.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _add(self, job_id, state, digest=None):
        self.store.enqueue(main.Job(job_id, 1, "source text", digest or f"digest-{job_id}"))
        self.store.set_state(job_id, state)

    def test_counts_by_status_groups_every_state(self):
        self._add("a", "queued")
        self._add("b", "queued")
        self._add("c", "running")
        self._add("d", "succeeded")
        self._add("e", "failed_retryable")
        counts = self.store.counts_by_status()
        self.assertEqual(counts, {"queued": 2, "running": 1, "succeeded": 1, "failed_retryable": 1})

    def test_counts_by_status_empty_store(self):
        self.assertEqual(self.store.counts_by_status(), {})

    def test_clear_finished_removes_only_terminal_states(self):
        for job_id, state in [("ok", "succeeded"), ("dead", "failed_terminal"), ("old", "stale"),
                              ("wait", "queued"), ("run", "running"), ("retry", "failed_retryable")]:
            self._add(job_id, state)
        removed = self.store.clear_finished()
        self.assertEqual(removed, 3)
        self.assertEqual(self.store.counts_by_status(),
                         {"queued": 1, "running": 1, "failed_retryable": 1})

    def test_clear_finished_returns_zero_when_nothing_terminal(self):
        self._add("a", "queued")
        self.assertEqual(self.store.clear_finished(), 0)

    def test_existing_digests_returns_all_stored_digests(self):
        self._add("a", "queued", digest="d1")
        self._add("b", "succeeded", digest="d2")
        self.assertEqual(self.store.existing_digests(), {"d1", "d2"})


class ConfigCenterCallbackTests(unittest.TestCase):
    """Wave 3 — tab callback glue in main.py (G1/G2/G3/G4/G5/G11)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig_path = main.config.CONFIG_PATH
        self._orig_pilot_only, self._orig_pilot_tag = main.PILOT_ONLY, main.PILOT_TAG
        self._orig_mw = main.mw
        main.config.CONFIG_PATH = self.root / "config.json"
        main.config.load()

    def tearDown(self):
        main.config.CONFIG_PATH = self._orig_path
        main.config.load()
        main.PILOT_ONLY, main.PILOT_TAG = self._orig_pilot_only, self._orig_pilot_tag
        main.mw = self._orig_mw
        self._tmp.cleanup()

    # ── G1 Settings ─────────────────────────────────────────────────────────
    def test_save_settings_valid_persists_and_returns_no_errors(self):
        errors = main._save_settings({"f5_speed": "1.5", "f5_device": "cpu", "ffmpeg_path": "", "pilot_tag": "icu-pilot"})
        self.assertEqual(errors, [])
        self.assertEqual(main.config.get_speed(), "1.5")
        self.assertEqual(main.PILOT_TAG, "icu-pilot")
        on_disk = main.config.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn('"f5_speed": 1.5', on_disk)

    def test_save_settings_invalid_returns_errors_and_writes_nothing(self):
        main._save_settings({"f5_speed": "1.0", "pilot_tag": "good-tag"})
        before = main.config.CONFIG_PATH.read_text(encoding="utf-8")
        errors = main._save_settings({"f5_speed": "5.0", "pilot_tag": "bad tag"})
        self.assertEqual(len(errors), 2)
        self.assertEqual(main.config.CONFIG_PATH.read_text(encoding="utf-8"), before)
        self.assertEqual(main.config.get_speed(), "1.0")

    def test_reload_settings_picks_up_external_edit(self):
        main._save_settings({"f5_speed": "1.0"})
        data = main.config.CONFIG_PATH.read_text(encoding="utf-8").replace("1.0", "1.75")
        main.config.CONFIG_PATH.write_text(data, encoding="utf-8")
        stat = main.config.CONFIG_PATH.stat()
        os.utime(main.config.CONFIG_PATH, (stat.st_atime, stat.st_mtime + 2))
        self.assertTrue(main._reload_settings())
        self.assertEqual(main.config.get_speed(), "1.75")

    # ── G5 Scope toggle ─────────────────────────────────────────────────────
    def test_toggle_pilot_persists_and_updates_global(self):
        self.assertTrue(main._toggle_pilot(False))
        self.assertFalse(main.PILOT_ONLY)
        self.assertFalse(main.config.get("pilot_only"))
        self.assertTrue(main._toggle_pilot(True))
        self.assertTrue(main.PILOT_ONLY)

    # ── G2/G4 JobStore-backed callbacks ─────────────────────────────────────
    def _fake_mw_with_profile(self):
        main.mw = SimpleNamespace(pm=SimpleNamespace(profileFolder=lambda: str(self.root)))

    def test_queue_counts_reads_persistent_store(self):
        self._fake_mw_with_profile()
        store = main.JobStore(self.root / "neuroicu_tts.sqlite3")
        store.enqueue(main.Job("a", 1, "text", "d1"))
        store.enqueue(main.Job("b", 2, "text", "d2"))
        store.set_state("b", "succeeded")
        store.close()
        self.assertEqual(main._queue_counts(), {"queued": 1, "succeeded": 1})

    def test_clear_finished_removes_terminal_jobs(self):
        self._fake_mw_with_profile()
        store = main.JobStore(self.root / "neuroicu_tts.sqlite3")
        store.enqueue(main.Job("a", 1, "text", "d1"))
        store.set_state("a", "succeeded")
        store.close()
        self.assertEqual(main._clear_finished(), 1)
        self.assertEqual(main._queue_counts(), {})

    # ── G3/G4 diagnostics & maintenance ─────────────────────────────────────
    def test_log_tail_returns_last_lines(self):
        original_dir = main.CONFIG_DIR
        try:
            main.CONFIG_DIR = self.root
            (self.root / "neuroicu_tts.log").write_text("\n".join(f"line {i}" for i in range(300)), encoding="utf-8")
            tail = main._log_tail(lines=50)
            self.assertEqual(len(tail.splitlines()), 50)
            self.assertIn("line 299", tail)
            self.assertNotIn("line 200", tail)
        finally:
            main.CONFIG_DIR = original_dir

    def test_storage_size_sums_only_managed_audio(self):
        media = self.root / "media"
        media.mkdir()
        (media / "neuroicu_tts_1-abc.mp3").write_bytes(b"x" * 10)
        (media / "neuroicu_tts_2-def.mp3").write_bytes(b"x" * 5)
        (media / "unrelated.mp3").write_bytes(b"x" * 999)
        main.mw = SimpleNamespace(col=SimpleNamespace(media=SimpleNamespace(dir=lambda: str(media))))
        self.assertEqual(main._storage_size(), 15)

    # ── G11 full-deck conversion ────────────────────────────────────────────
    def test_full_deck_info_counts_eligible_notes(self):
        class FakeNote(dict):
            def __init__(self, note_id, extra):
                super().__init__(Extra=extra)
                self.id = note_id

        notes = {1: FakeNote(1, "alpha"), 2: FakeNote(2, "beta")}
        col = SimpleNamespace(find_notes=lambda _q: [1, 2], get_note=lambda nid: notes[nid])
        main.mw = SimpleNamespace(col=col)
        with patch.object(main, "_eligible", return_value=True):
            count, seconds = main._full_deck_info()
        self.assertEqual(count, 2)
        self.assertEqual(seconds, main.estimate_runtime(2))

    def test_full_deck_convert_enqueues_deduped_jobs(self):
        class FakeNote(dict):
            def __init__(self, note_id, extra):
                super().__init__(Extra=extra)
                self.id = note_id

        notes = {1: FakeNote(1, "alpha"), 2: FakeNote(2, "beta"), 3: FakeNote(3, "gamma")}
        profile_digest = "e" * 64

        def fake_state(extra):
            return SimpleNamespace(digest=main.digest_for(extra), marker_digest=None, sound_filename=None, profile_digest=None, text=extra)

        # "beta" is already queued in the persistent store -> must be skipped (G11.5).
        store = main.JobStore(self.root / "neuroicu_tts.sqlite3")
        store.enqueue(main.Job("seed", 99, "beta", main.generation_digest(main.digest_for("beta"), profile_digest)))
        store.close()

        worker = SimpleNamespace(submit=unittest.mock.Mock(return_value=True))
        col = SimpleNamespace(find_notes=lambda _q: [1, 2, 3], get_note=lambda nid: notes[nid])
        main.mw = SimpleNamespace(col=col, pm=SimpleNamespace(profileFolder=lambda: str(self.root)), _neuroicu_tts_worker=worker)
        with patch.object(main, "_eligible", return_value=True), patch.object(main, "state_for", side_effect=fake_state), patch.object(main, "_synthesis_profile_digest", return_value=profile_digest), patch.object(main, "_managed_media_exists", return_value=False):
            enqueued, total = main._full_deck_convert()
        self.assertEqual((enqueued, total), (2, 3))
        self.assertEqual(worker.submit.call_count, 2)

    def test_full_deck_convert_without_worker_enqueues_nothing(self):
        main.mw = SimpleNamespace()
        self.assertEqual(main._full_deck_convert(), (0, 0))


if __name__ == "__main__":
    unittest.main()
