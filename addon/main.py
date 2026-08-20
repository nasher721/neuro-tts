"""Neuro ICU TTS add-on with an asynchronous F5-TTS queue."""
from __future__ import annotations

import heapq, json, logging, logging.handlers, os, queue, shutil, sqlite3, subprocess, tempfile, threading, time, uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .control_center import ControlCenter, estimate_runtime
from .tts_core import digest_for, filename, generation_digest, is_legacy_extra, is_managed_filename, managed_extra, state_for
from .status import ActivitySnapshot, EngineSnapshot, QueueSnapshot, ScopeSnapshot, StatusService
from .text_normalize import normalize_for_speech, split_for_synthesis

try:
    from aqt import gui_hooks, mw
    from aqt.qt import QAction, QKeySequence, QShortcut, QTimer
    from anki.hooks import addHook
except ImportError:  # pragma: no cover
    mw = None

MODEL = "Enhanced Cloze 2.1 v2-136cb (Neuro ICU Boards / arbornasher)"
DECK_PREFIX = "Neuro ICU Boards (AnkiHub)"
CONFIG_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("neuroicu_tts")

# Engine settings mirror addon.config into module globals so that the worker
# reads them without touching the config lock on every job.  They are refreshed
# by _refresh_engine_globals() whenever the Control Center saves or reloads, so
# editing engine paths no longer requires restarting Anki.  Environment
# variables still win, which keeps machine-local paths out of config.json.
F5_TTS_REPO = ""
F5_TTS_PYTHON = "python3"
F5_MODEL = ""
F5_REF_AUDIO = ""
F5_REF_TEXT = ""
F5_NFE_STEP = "16"
PILOT_ONLY, PILOT_TAG = True, "neuroicu-tts-pilot"


# Scope counting walks the whole collection, and status is re-emitted on every
# queue transition, so the result is cached and invalidated explicitly rather
# than recomputed per event.
SCOPE_CACHE_SECONDS = 30.0
_scope_cache: tuple[float, ScopeSnapshot] | None = None
_scope_cache_lock = threading.Lock()
_deck_eligibility: dict[int, bool] = {}


def _invalidate_scope_cache():
    """Drop cached scope counts after anything that can change eligibility."""
    global _scope_cache
    with _scope_cache_lock:
        _scope_cache = None
    _deck_eligibility.clear()


def _refresh_engine_globals():
    """Re-read engine settings from addon.config (environment overrides win)."""
    global F5_TTS_REPO, F5_TTS_PYTHON, F5_MODEL, F5_REF_AUDIO, F5_REF_TEXT, F5_NFE_STEP
    global PILOT_ONLY, PILOT_TAG
    F5_TTS_REPO = os.environ.get("F5_TTS_REPO", config.get("f5_tts_repo", "")) or ""
    F5_TTS_PYTHON = os.environ.get("F5_TTS_PYTHON", config.get("f5_tts_python", "python3"))
    F5_MODEL = config.get("f5_model") or ""
    F5_REF_AUDIO = config.get("f5_ref_audio") or ""
    F5_REF_TEXT = config.get("f5_ref_text") or ""
    F5_NFE_STEP = str(config.get("f5_nfe_step"))
    PILOT_ONLY = bool(config.get("pilot_only", True))
    PILOT_TAG = config.get("pilot_tag", "neuroicu-tts-pilot")
    _invalidate_scope_cache()


_refresh_engine_globals()
_last_scan_at = None
_last_generation_at = None
_last_event = None
_status_service_instance = None
_engine_test_thread = None
_engine_test_cancel = threading.Event()
_engine_test_lock = threading.Lock()
_engine_session_lock = threading.Lock()
_status_events = queue.Queue()
_profile_hook_registered = False
_profile_close_hook_registered = False
_sync_hook_registered = False
_note_hook_registered = False
_profile_session_id = None
_logging_lock = threading.Lock()
_SUBPROCESS_OUTPUT_LIMIT = 1000
_SUBPROCESS_TIMEOUT = 15 * 60


@dataclass
class EngineSession:
    validated: bool = False
    test_succeeded: bool = False
    issue: str | None = None
    last_test: float | None = None
    details: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)


_engine_session = EngineSession()


def _engine_session_snapshot() -> EngineSession:
    """Return an immutable-by-convention copy of the worker-owned session state."""
    with _engine_session_lock:
        session = _engine_session
        return EngineSession(
            validated=session.validated,
            test_succeeded=session.test_succeeded,
            issue=session.issue,
            last_test=session.last_test,
            details=session.details,
            checks=dict(session.checks),
        )

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def _configure_logging():
    """Attach a size-capped rotating file handler exactly once per process."""
    with _logging_lock:
        if LOGGER.handlers:
            _apply_log_level()
            return
        try:
            handler = logging.handlers.RotatingFileHandler(
                CONFIG_DIR / "neuroicu_tts.log",
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError:
            LOGGER.warning("Could not create Neuro ICU TTS log file", exc_info=True)
            return
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
        LOGGER.addHandler(handler)
        _apply_log_level()


def _apply_log_level():
    """Honour the configured log_level; unknown values fall back to INFO."""
    level = logging.getLevelName(str(config.get("log_level", "INFO")).upper())
    LOGGER.setLevel(level if isinstance(level, int) else logging.INFO)

@dataclass(frozen=True)
class Job:
    job_id: str; note_id: int; source: str; digest: str; force: bool = False

@dataclass(frozen=True)
class Result:
    job: Job; artifact: Path | None; error: str | None = None


@dataclass(frozen=True)
class RecoveredJob:
    job: Job


class _Wake:
    """Sentinel that releases a blocking queue read without carrying work."""

    __slots__ = ()


_WAKE = _Wake()


@dataclass(frozen=True)
class JobStateUpdate:
    job_id: str
    state: str
    error: str | None = None

class JobStore:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-2000")
        self.conn.execute("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, note_id INTEGER, source TEXT, digest TEXT, state TEXT, error TEXT, updated REAL)")
        self._migrate_schema()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_digest ON jobs(digest)")
        self.conn.commit()

    def _migrate_schema(self):
        """Add columns introduced after the original schema, in place."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        if "attempts" not in existing:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")

    def enqueue(self, j):
        self.conn.execute(
            "INSERT INTO jobs (job_id, note_id, source, digest, state, error, updated, attempts)"
            " VALUES (?, ?, ?, ?, 'queued', NULL, ?, 0)"
            " ON CONFLICT(job_id) DO UPDATE SET state='queued', error=NULL, updated=excluded.updated",
            (j.job_id, j.note_id, j.source, j.digest, time.time()),
        )
        self.conn.commit()

    def enqueue_many(self, jobs):
        now = time.time()
        data = [(j.job_id, j.note_id, j.source, j.digest, "queued", None, now, 0) for j in jobs]
        self.conn.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", data)
        self.conn.commit()

    def set_state(self, jid, state, error=None):
        self.conn.execute("UPDATE jobs SET state=?, error=?, updated=? WHERE job_id=?", (state, error, time.time(), jid))
        self.conn.commit()

    def record_attempt(self, jid):
        """Increment and return the attempt count for *jid*."""
        self.conn.execute("UPDATE jobs SET attempts = attempts + 1, updated=? WHERE job_id=?", (time.time(), jid))
        self.conn.commit()
        row = self.conn.execute("SELECT attempts FROM jobs WHERE job_id=?", (jid,)).fetchone()
        return int(row[0]) if row else 1

    def retryable_jobs(self):
        """Failed jobs the user asked to run again, reset to a fresh attempt count."""
        rows = self.conn.execute(
            "SELECT job_id, note_id, source, digest FROM jobs WHERE state IN ('failed_terminal', 'failed_retryable', 'cancelled')"
        ).fetchall()
        if rows:
            self.conn.execute(
                "UPDATE jobs SET state='queued', error=NULL, attempts=0, updated=?"
                " WHERE state IN ('failed_terminal', 'failed_retryable', 'cancelled')",
                (time.time(),),
            )
            self.conn.commit()
        return rows

    def cancel_pending(self):
        """Mark every not-yet-finished job cancelled; return how many changed."""
        cur = self.conn.execute(
            "UPDATE jobs SET state='cancelled', updated=? WHERE state IN ('queued', 'running', 'staged')",
            (time.time(),),
        )
        self.conn.commit()
        return cur.rowcount

    def close(self):
        self.conn.close()

    def recoverable(self):
        return self.conn.execute("SELECT note_id, source, digest FROM jobs WHERE state IN ('queued', 'running', 'staged', 'failed', 'failed_retryable')").fetchall()

    def recoverable_jobs(self):
        rows = self.conn.execute("SELECT job_id, note_id, source, digest FROM jobs WHERE state IN ('queued', 'running', 'staged', 'failed', 'failed_retryable')").fetchall()
        if rows:
            self.conn.execute("UPDATE jobs SET state='queued', error=NULL, updated=? WHERE state IN ('running', 'staged', 'failed', 'failed_retryable')", (time.time(),))
            self.conn.commit()
        return rows

    def counts_by_status(self):
        """G2 — job counts grouped by state for the read-only Queue tab."""
        rows = self.conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()
        return dict(rows)

    def clear_finished(self):
        """G4 — remove terminal jobs; return the number of rows deleted."""
        cur = self.conn.execute("DELETE FROM jobs WHERE state IN ('succeeded', 'failed_terminal', 'stale', 'cancelled')")
        self.conn.commit()
        return cur.rowcount

    def existing_digests(self):
        """G11 — all stored digests, for dedupe during full-deck enqueue."""
        rows = self.conn.execute("SELECT digest FROM jobs WHERE state != 'cancelled'").fetchall()
        return {row[0] for row in rows}

def _run_process(args, *, timeout=_SUBPROCESS_TIMEOUT, cwd=None, cancel_event=None):
    process = None
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try: process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate()
                raise RuntimeError(f"command cancelled: {args[0]}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill(); process.communicate()
                raise RuntimeError(f"command timed out after {timeout}s: {args[0]}")
            try:
                poll_timeout = min(0.2, remaining) if cancel_event is not None else remaining
                stdout, stderr = process.communicate(timeout=poll_timeout)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            output = "\n".join(part for part in (stdout, stderr) if part)[-_SUBPROCESS_OUTPUT_LIMIT:]
            raise RuntimeError(f"command failed with exit {process.returncode}: {args[0]}{': ' + output if output else ''}")
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not start command {args[0]}: {exc}") from exc


def _is_mps_crash(error: RuntimeError) -> bool:
    """Recognize the fatal process exit emitted by the broken MPS path."""
    message = str(error).lower()
    return "sigsegv" in message or "exit -11" in message or "signal 11" in message


def _ffmpeg_path() -> str | None:
    return config.get_ffmpeg_path()


def _run_f5_inference(text: str, wav_dir: Path, cancel_event=None, output_file: str = "speech.wav"):
    # Safe knobs are re-read per job so Config Center saves apply at next job start.
    device, speed = config.get_device(), config.get_speed()
    args = [F5_TTS_PYTHON, str(Path(F5_TTS_REPO) / "src" / "f5_tts" / "infer" / "infer_cli.py"), "--model", F5_MODEL, "--ref_audio", str(Path(F5_REF_AUDIO) if Path(F5_REF_AUDIO).is_absolute() else Path(F5_TTS_REPO) / F5_REF_AUDIO), "--ref_text", F5_REF_TEXT, "--gen_text", text, "--output_dir", str(wav_dir), "--output_file", output_file, "--device", device, "--nfe_step", F5_NFE_STEP, "--speed", speed]
    try:
        _run_process(args, cwd=F5_TTS_REPO, cancel_event=cancel_event)
    except RuntimeError as exc:
        if device.lower() != "mps" or not _is_mps_crash(exc):
            raise
        LOGGER.warning("F5-TTS MPS inference crashed; retrying note on CPU")
        args[args.index("--device") + 1] = "cpu"
        _run_process(args, cwd=F5_TTS_REPO, cancel_event=cancel_event)


def speech_text(text: str) -> str:
    """Apply the configured speech normalization to raw note text."""
    if not config.get_bool("normalize_speech"):
        return text
    return normalize_for_speech(
        text,
        expand_abbreviations=config.get_bool("expand_abbreviations"),
        extra_replacements=config.get_speech_replacements(),
    ) or text


def _synthesize_chunks(text: str, wav_dir: Path, ffmpeg: str, cancel_event=None) -> Path:
    """Render *text* to a single WAV, splitting long input into joined chunks.

    F5-TTS degrades badly on very long single utterances, so anything over
    ``max_chunk_chars`` is synthesized in sentence-aligned pieces and stitched
    back together with ffmpeg's concat demuxer.
    """
    chunks = split_for_synthesis(text, config.get_int("max_chunk_chars"))
    if len(chunks) <= 1:
        _run_f5_inference(chunks[0] if chunks else text, wav_dir, cancel_event)
        return wav_dir / "speech.wav"

    LOGGER.info("Synthesizing %d chunks for a %d character note", len(chunks), len(text))
    parts = []
    for index, chunk in enumerate(chunks):
        name = f"part-{index:03d}.wav"
        _run_f5_inference(chunk, wav_dir, cancel_event, output_file=name)
        part = wav_dir / name
        if not part.exists():
            raise RuntimeError(f"F5-TTS produced no audio for chunk {index + 1} of {len(chunks)}")
        parts.append(part)

    listing = wav_dir / "parts.txt"
    # The concat demuxer reads single-quoted paths with embedded quotes escaped.
    listing.write_text(
        "\n".join("file '{}'".format(str(part).replace("'", "'\\''")) for part in parts) + "\n",
        encoding="utf-8",
    )
    joined = wav_dir / "speech.wav"
    _run_process(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)],
        timeout=300,
        cancel_event=cancel_event,
    )
    return joined


def _tts_file(text: str, destination: Path, cancel_event=None):
    if not F5_TTS_REPO: raise RuntimeError("Set f5_tts_repo in config.json")
    cli = Path(F5_TTS_REPO) / "src" / "f5_tts" / "infer" / "infer_cli.py"
    ref = Path(F5_REF_AUDIO); ref = ref if ref.is_absolute() else Path(F5_TTS_REPO) / ref
    ffmpeg = _ffmpeg_path()
    if not cli.exists() or not ref.exists(): raise RuntimeError("F5-TTS CLI or reference audio is missing")
    if not ffmpeg: raise RuntimeError("ffmpeg is required to convert F5-TTS WAV output to MP3")
    spoken = speech_text(text)
    if not spoken.strip():
        raise RuntimeError("There is nothing speakable in this note after normalization")
    with tempfile.TemporaryDirectory(prefix="neuroicu-tts-") as tmp:
        wav_dir = Path(tmp) / "wav"; wav_dir.mkdir()
        wav = _synthesize_chunks(spoken, wav_dir, ffmpeg, cancel_event)
        if not wav.exists(): raise RuntimeError("F5-TTS completed without producing a WAV")
        staged = Path(tmp) / "speech.mp3"
        audio_filter = "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-50dB:stop_periods=1:stop_duration=0.1:stop_threshold=-50dB,loudnorm=I=-16:TP=-1.5:LRA=11"
        try:
            _run_process([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-af", audio_filter, "-codec:a", "libmp3lame", "-q:a", "3", str(staged)], timeout=120, cancel_event=cancel_event)
        except Exception:
            LOGGER.warning("FFmpeg audio filter normalization failed, falling back to direct encoding")
            _run_process([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-codec:a", "libmp3lame", "-q:a", "4", str(staged)], timeout=120, cancel_event=cancel_event)
        if not staged.exists() or staged.stat().st_size == 0: raise RuntimeError("ffmpeg completed without producing an MP3")
        destination.parent.mkdir(parents=True, exist_ok=True); os.replace(staged, destination)

class TTSWorker(threading.Thread):
    """Single background synthesis thread with retry, pause, and cancel support.

    Ownership rules that the rest of the add-on depends on:

    * The SQLite store is created *and* only ever touched on this thread.
    * ``results`` is drained on Anki's UI thread, which owns the collection.
    * Failed synthesis is retried in-process with exponential backoff up to
      ``max_attempts``; only then does a job become ``failed_terminal``.
    """

    def __init__(self, db_path, media_dir, results, session_id=None):
        super().__init__(name="neuroicu-tts-worker", daemon=True)
        self.jobs = queue.Queue()
        self.results = results
        self.db_path = Path(db_path)
        self.media_dir = Path(media_dir)
        self.staging_dir = self.db_path.parent / "neuroicu_tts_staging"
        self.session_id = session_id
        self.store = None
        self.stopping = threading.Event()
        self.paused = threading.Event()
        self.stopped = False
        self.pending = set()
        self.pending_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.statuses = {}
        self.current_job = None
        self.current_note = None
        self.latest_error = None
        # Deferred work is mutated by the worker thread and read/cleared by the
        # UI thread through cancel_pending(), so it lives behind its own lock.
        self._schedule_lock = threading.Lock()
        self._retries: list[tuple[float, int, Job]] = []
        self._held: list[Job] = []
        self._sequence = 0

    # ── submission ──────────────────────────────────────────────────────────
    def submit(self, j):
        if self.stopping.is_set(): return False
        key = (j.note_id, j.digest)
        with self.pending_lock:
            if key in self.pending: return False
            self.pending.add(key)
        with self.status_lock: self.statuses[j.job_id] = "queued"
        self.jobs.put(j); return True

    def snapshot(self):
        with self.status_lock:
            counts = {}
            for state in self.statuses.values(): counts[state] = counts.get(state, 0) + 1
            return QueueSnapshot(counts, self.current_job, self.current_note, paused=self.paused.is_set(), latest_error=self.latest_error)

    def mark_committed(self, job_id, success, error=None):
        state = "succeeded" if success else ("failed_retryable" if error else "stale")
        with self.status_lock:
            self.statuses[job_id] = state
            if error: self.latest_error = error
        if not self.stopping.is_set():
            self.jobs.put(JobStateUpdate(job_id, state, error))

    def stop(self):
        if not self.stopping.is_set():
            self.stopping.set()
            self.paused.clear()
            self.jobs.put(None)

    # ── operator controls ───────────────────────────────────────────────────
    def pause(self):
        """Hold new jobs after the running one finishes; state updates still land."""
        self.paused.set()
        return True

    def resume(self):
        self.paused.clear()
        self.jobs.put(_WAKE)  # release the blocking get() so held work restarts
        return True

    def cancel_pending(self):
        """Drop every queued, held, and scheduled-retry job; return how many."""
        cancelled = []
        while True:
            try:
                item = self.jobs.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Job):
                cancelled.append(item)
            elif item is None:
                self.jobs.put(None)  # never swallow the shutdown sentinel
                break
        with self._schedule_lock:
            cancelled.extend(self._held)
            self._held = []
            cancelled.extend(job for _, _, job in self._retries)
            self._retries = []
        with self.status_lock:
            for job in cancelled:
                self.statuses[job.job_id] = "cancelled"
        with self.pending_lock:
            for job in cancelled:
                self.pending.discard((job.note_id, job.digest))
        for job in cancelled:
            self.jobs.put(JobStateUpdate(job.job_id, "cancelled"))
        return len(cancelled)

    # ── retry scheduling ────────────────────────────────────────────────────
    def _schedule_retry(self, job, attempts):
        """Queue *job* to run again after an exponential backoff delay."""
        backoff = config.get_float("retry_backoff_seconds") * (2 ** (attempts - 1))
        with self._schedule_lock:
            self._sequence += 1
            heapq.heappush(self._retries, (time.monotonic() + backoff, self._sequence, job))
        with self.status_lock:
            self.statuses[job.job_id] = "failed_retryable"
        LOGGER.info("Retrying note %s in %.0fs (attempt %d)", job.note_id, backoff, attempts + 1)
        return backoff

    def _due_retry(self):
        with self._schedule_lock:
            if self._retries and self._retries[0][0] <= time.monotonic():
                return heapq.heappop(self._retries)[2]
        return None

    def _next_wait(self):
        """Seconds to block on the queue before the earliest retry comes due."""
        with self._schedule_lock:
            if not self._retries:
                return None
            return max(0.05, self._retries[0][0] - time.monotonic())

    def _next_item(self):
        """Return the next queue item, or a retry that has come due."""
        due = self._due_retry()
        if due is not None:
            return due
        timeout = self._next_wait()
        try:
            return self.jobs.get(timeout=timeout) if timeout is not None else self.jobs.get()
        except queue.Empty:
            return _WAKE

    # ── main loop ───────────────────────────────────────────────────────────
    def run(self):
        # SQLite is created and used only by the worker thread.
        self.store = JobStore(self.db_path)
        for job_id, note_id, source, digest in self.store.recoverable_jobs():
            with self.status_lock: self.statuses[job_id] = "queued"
            self.results.put(RecoveredJob(Job(job_id, note_id, source, digest)))
        if self.statuses: _emit_status()
        try:
            while True:
                j = self._next_item()
                if j is None: break
                if j is _WAKE:
                    if not self.paused.is_set():
                        with self._schedule_lock:
                            held, self._held = self._held, []
                        for job in held:
                            self.jobs.put(job)
                    continue
                if isinstance(j, JobStateUpdate):
                    self.store.set_state(j.job_id, j.state, j.error)
                    continue
                self.store.enqueue(j)
                if self.stopping.is_set():
                    with self.pending_lock: self.pending.discard((j.note_id, j.digest))
                    continue
                if self.paused.is_set():
                    # Hold the job rather than sleeping, so commits and state
                    # updates queued behind it keep being recorded while paused.
                    with self._schedule_lock: self._held.append(j)
                    with self.status_lock: self.statuses[j.job_id] = "paused"
                    _emit_status()
                    continue
                self._process(j)
        finally:
            self.store.close()
            self.stopped = True

    def _process(self, j):
        attempts = self.store.record_attempt(j.job_id)
        self.store.set_state(j.job_id, "running")
        with self.status_lock: self.statuses[j.job_id] = "running"; self.current_job = j.job_id; self.current_note = j.note_id
        _emit_status()
        dest = self.staging_dir / f"{j.job_id}.mp3"
        retrying = False
        try:
            self.staging_dir.mkdir(parents=True, exist_ok=True)
            _tts_file(j.source, dest, self.stopping)
            self.store.set_state(j.job_id, "staged")
            with self.status_lock: self.latest_error = None
            self.results.put(Result(j, dest))
        except Exception as exc:
            retrying = self._handle_failure(j, exc, attempts)
        finally:
            with self.status_lock: self.current_job = None; self.current_note = None
            # A job awaiting retry keeps its dedupe key, so an identical submit
            # does not slip a second copy of the same work into the queue.
            if not retrying:
                with self.pending_lock: self.pending.discard((j.note_id, j.digest))
            _emit_status()

    def _handle_failure(self, j, exc, attempts):
        """Retry transient synthesis failures; surface the rest to the UI.

        Returns True when a retry was scheduled and the job still owns its
        dedupe key.
        """
        message = str(exc)
        with self.status_lock: self.latest_error = message
        cancelled = self.stopping.is_set() or "cancelled" in message.lower()
        if not cancelled and attempts < max(1, config.get_int("max_attempts")):
            self.store.set_state(j.job_id, "failed_retryable", message)
            self._schedule_retry(j, attempts)
            return True
        state = "cancelled" if cancelled else "failed_terminal"
        self.store.set_state(j.job_id, state, message)
        with self.status_lock: self.statuses[j.job_id] = state
        if not cancelled:
            LOGGER.error("Note %s failed after %d attempt(s): %s", j.note_id, attempts, message)
        self.results.put(Result(j, None, message))
        return False


def _media_dir(): return Path(mw.col.media.dir())


def _managed_media_exists(sound_filename):
    if not sound_filename:
        return False
    try:
        media_path = str(_media_dir())
        return os.path.isfile(os.path.join(media_path, str(sound_filename)))
    except (AttributeError, OSError, TypeError):
        return False


_PATH_IDENTITY_CACHE_LIMIT = 64
_path_identity_cache: dict[str, tuple[int, int, dict]] = {}


def _path_identity(path):
    if not path:
        return None
    p_str = str(path)
    try:
        resolved = str(Path(path).resolve())
        stat_res = os.stat(resolved)
        mtime = stat_res.st_mtime_ns
        size = stat_res.st_size
    except OSError:
        return {"path": p_str}

    cached = _path_identity_cache.get(resolved)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]

    identity = {"path": resolved, "size": size, "mtime_ns": mtime}
    if os.path.isdir(resolved):
        head = os.path.join(resolved, ".git", "HEAD")
        try:
            with open(head, encoding="utf-8") as f:
                head_value = f.read().strip()
            identity["git_head"] = head_value
            if head_value.startswith("ref: "):
                ref_file = os.path.join(resolved, ".git", head_value[5:])
                with open(ref_file, encoding="utf-8") as f:
                    identity["git_revision"] = f.read().strip()
        except OSError:
            pass
    if len(_path_identity_cache) >= _PATH_IDENTITY_CACHE_LIMIT:
        _path_identity_cache.clear()
    _path_identity_cache[resolved] = (mtime, size, identity)
    return identity


def _synthesis_profile_digest():
    """Return a stable identity for settings that can change generated audio."""
    reference = Path(F5_REF_AUDIO) if F5_REF_AUDIO else None
    repository = Path(F5_TTS_REPO) if F5_TTS_REPO else None
    if reference and repository and not reference.is_absolute():
        reference = repository / reference
    cli = repository / "src" / "f5_tts" / "infer" / "infer_cli.py" if repository else None
    python_path = shutil.which(F5_TTS_PYTHON) or F5_TTS_PYTHON
    payload = {
        "model": F5_MODEL,
        "repository": _path_identity(repository),
        "engine_cli": _path_identity(cli),
        "python": _path_identity(python_path),
        "reference": _path_identity(reference),
        "reference_text": F5_REF_TEXT,
        "device": config.get_device(),
        "nfe_step": F5_NFE_STEP,
        "speed": config.get_speed(),
        "speech": config.speech_profile(),
    }
    return digest_for(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _is_deck_eligible(did):
    cached = _deck_eligibility.get(did)
    if cached is not None:
        return cached
    result = _deck_eligible_uncached(did)
    _deck_eligibility[did] = result
    return result


def _deck_eligible_uncached(did):
    try:
        deck = mw.col.decks.get(did, default=False)
        if not deck:
            return False
        name = deck.get("name", "") if isinstance(deck, dict) else getattr(deck, "name", "")
        return name == DECK_PREFIX or name.startswith(DECK_PREFIX + "::")
    except Exception:
        return False


def _eligible(note, ignore_pilot=False):
    extra = note.get("Extra") if hasattr(note, "get") else (note["Extra"] if "Extra" in note else None)
    if not extra or not str(extra).strip():
        return False
    try:
        model = note.model()
        model_name = model.get("name") if isinstance(model, dict) else getattr(model, "name", None)
        if model_name != MODEL:
            return False
    except Exception:
        return False
    if not ignore_pilot and PILOT_ONLY and PILOT_TAG not in note.tags:
        return False
    try:
        return any(_is_deck_eligible(c.did) for c in note.cards())
    except Exception:
        try:
            decks = {mw.col.decks.get(c.did)["name"] for c in note.cards()}
            return any(n == DECK_PREFIX or n.startswith(DECK_PREFIX + "::") for n in decks)
        except Exception:
            return False


def _submit(note, force=False, profile_digest=None):
    if not _eligible(note): return False
    s = state_for(note["Extra"])
    if profile_digest is None:
        profile_digest = _synthesis_profile_digest()
    artifact_digest = generation_digest(s.digest, profile_digest)
    expected = filename(note.id, artifact_digest)
    if not force and s.marker_digest == s.digest and getattr(s, "profile_digest", None) == profile_digest and s.sound_filename == expected and _managed_media_exists(expected):
        if is_legacy_extra(note["Extra"]):
            try:
                note["Extra"] = managed_extra(note["Extra"], expected, s.digest, profile_digest)
                mw.col.update_note(note)
                LOGGER.info("Upgraded note %s to click-to-play player", note.id)
            except Exception:
                LOGGER.exception("Could not upgrade note %s", note.id)
        return False
    worker = getattr(mw, "_neuroicu_tts_worker", None)
    if not worker: return False
    return worker.submit(Job(str(uuid.uuid4()), note.id, s.text, artifact_digest, force))


def upgrade_legacy_notes():
    """Upgrade notes using legacy [sound:...] managed tags to the click-to-play player."""
    if mw is None:
        return 0
    upgraded = 0
    profile_digest = _synthesis_profile_digest()
    try:
        for nid in mw.col.find_notes(f'note:"{MODEL}"'):
            try:
                note = mw.col.get_note(nid)
                extra = note.get("Extra") if hasattr(note, "get") else (note["Extra"] if "Extra" in note else "")
                if not extra or not str(extra).strip():
                    continue
                if is_legacy_extra(extra):
                    s = state_for(extra)
                    artifact_digest = generation_digest(s.digest, profile_digest)
                    expected = filename(note.id, artifact_digest)
                    reusable = is_managed_filename(s.sound_filename) and _managed_media_exists(s.sound_filename)
                    if _managed_media_exists(expected) or reusable:
                        actual_file = expected if _managed_media_exists(expected) else s.sound_filename
                        note["Extra"] = managed_extra(extra, actual_file, s.digest, profile_digest)
                        mw.col.update_note(note)
                        upgraded += 1
            except Exception:
                LOGGER.exception("Failed upgrading note %s", nid)
        if upgraded:
            LOGGER.info("Upgraded %d note(s) to click-to-play player", upgraded)
            _last_event_set(f"Upgraded {upgraded} note(s) to click-to-play player")
    except Exception as exc:
        LOGGER.exception("Upgrade legacy notes scan failed: %s", exc)
    return upgraded


def _commit(r: Result):
    global _last_generation_at, _last_event
    worker = getattr(mw, "_neuroicu_tts_worker", None)
    if r.error:
        LOGGER.error("TTS failed for note %s: %s", r.job.note_id, r.error)
        if worker: worker.mark_committed(r.job.job_id, False, r.error)
        _emit_status()
        return False
    try:
        note = mw.col.get_note(r.job.note_id)
        current = state_for(note["Extra"])
    except Exception as exc:
        LOGGER.exception("Could not revalidate note %s", r.job.note_id)
        if worker: worker.mark_committed(r.job.job_id, False, str(exc))
        _last_event = f"Could not commit note {r.job.note_id}: {exc}"
        _emit_status()
        return False
    profile_digest = _synthesis_profile_digest()
    artifact_digest = generation_digest(current.digest, profile_digest)
    if artifact_digest != r.job.digest:
        r.artifact.unlink(missing_ok=True)
        if worker: worker.mark_committed(r.job.job_id, False)
        _submit(note, profile_digest=profile_digest)
        _emit_status()
        return False
    expected = filename(note.id, artifact_digest)
    try:
        destination = _media_dir() / expected
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(r.artifact, destination)
        note["Extra"] = managed_extra(note["Extra"], expected, current.digest, profile_digest)
        mw.col.update_note(note)
    except Exception as exc:
        LOGGER.exception("Could not update note %s", r.job.note_id)
        if worker: worker.mark_committed(r.job.job_id, False, str(exc))
        _last_event = f"Could not commit note {r.job.note_id}: {exc}"
        _emit_status()
        return False
    LOGGER.info("Updated note %s -> %s", note.id, expected)
    if worker: worker.mark_committed(r.job.job_id, True)
    _last_generation_at, _last_event = time.time(), f"Generated note {note.id}"
    _emit_status()
    return True


def scan_collection():
    global _last_scan_at, _last_event
    _invalidate_scope_cache()
    try:
        upgrade_legacy_notes()
        profile_digest = _synthesis_profile_digest()
        count = sum(_submit(mw.col.get_note(nid), profile_digest=profile_digest) for nid in mw.col.find_notes(f'note:"{MODEL}"'))
    except Exception as exc:
        _last_scan_at, _last_event = time.time(), f"Scan unavailable: {exc}"
        LOGGER.exception("Collection scan failed")
        _emit_status()
        return 0
    _last_scan_at, _last_event = time.time(), f"Scan queued {count} note(s)"
    LOGGER.info("Queued %d note(s)", count)
    _emit_status()
    return count

def _drain():
    _drain_status_events()
    worker = getattr(mw, "_neuroicu_tts_worker", None)
    if not worker: return
    changed = False
    while True:
        try:
            item = worker.results.get_nowait()
            if isinstance(item, RecoveredJob):
                worker.mark_committed(item.job.job_id, False)
                try:
                    note = mw.col.get_note(item.job.note_id)
                    if _eligible(note): _submit(note)
                except Exception:
                    LOGGER.exception("Could not revalidate recovered note %s", item.job.note_id)
                continue
            changed = _commit(item) or changed
        except queue.Empty: break
    if changed: mw.reset()

def _replay():
    web = getattr(getattr(mw, "reviewer", None), "web", None)
    if web:
        js = (
            "(function() {"
            "  var btn = document.querySelector('.neuroicu-play-btn');"
            "  if (btn) { btn.click(); return; }"
            "  var audio = document.querySelector('.neuroicu-audio');"
            "  if (audio) {"
            "    if (audio.paused) { audio.play(); } else { audio.pause(); audio.currentTime = 0; }"
            "    return;"
            "  }"
            "  var fallback = document.querySelector('.replay-button');"
            "  if (fallback) fallback.click();"
            "})()"
        )
        web.eval(js)

def _engine_snapshot():
    repo = Path(F5_TTS_REPO) if F5_TTS_REPO else None
    ref = Path(F5_REF_AUDIO) if F5_REF_AUDIO else None
    if repo and ref and not ref.is_absolute(): ref = repo / ref
    configured = bool(F5_TTS_REPO and F5_TTS_PYTHON and F5_REF_AUDIO and F5_MODEL)
    session = _engine_session_snapshot()
    cli = repo / "src" / "f5_tts" / "infer" / "infer_cli.py" if repo else None
    python_available = bool(shutil.which(F5_TTS_PYTHON) or Path(F5_TTS_PYTHON).exists())
    checks = _engine_checks(repo, cli, ref, python_available)
    issue = session.issue
    if issue is None:
        if not checks["configured"]: issue = "TTS engine configuration is incomplete"
        elif not checks["python"]: issue = "Python executable is unavailable"
        elif not checks["cli"] or not checks["reference_audio"]: issue = "F5-TTS CLI or reference audio is missing"
        elif not checks["ffmpeg"]: issue = "ffmpeg is required to convert F5-TTS WAV output to MP3"
    return EngineSnapshot(configured=configured, validated=session.validated, test_succeeded=session.test_succeeded, issue=issue, last_test=session.last_test, details=session.details or f"Model: {F5_MODEL}; device: {config.get_device()}", checks=checks | {"device": session.checks.get("device")})


def _engine_checks(repo, cli, ref, python_available):
    configured = bool(F5_TTS_REPO and F5_TTS_PYTHON and F5_REF_AUDIO and F5_MODEL)
    return {"configured": configured, "python": python_available, "cli": bool(cli and cli.exists()), "reference_audio": bool(ref and ref.exists()), "ffmpeg": bool(_ffmpeg_path()), "device": None}


def _device_available():
    device = config.get_device().lower()
    if not device or device in {"cpu", "cuda", "auto"}:
        return True
    try:
        if device.startswith("cuda"):
            expression = "import torch; print(torch.cuda.is_available())"
        elif device.startswith("mps"):
            expression = "import torch; print(torch.backends.mps.is_available())"
        else:
            return True
        result = subprocess.run([F5_TTS_PYTHON, "-c", expression], capture_output=True, text=True, timeout=5, check=False)
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _run_engine_test(session_id=None, cancel_event=None):
    """Validate dependencies and produce a disposable clip for the dashboard."""
    global _last_event
    if session_id is not None and session_id != _profile_session_id:
        return False
    repo = Path(F5_TTS_REPO) if F5_TTS_REPO else None
    ref = Path(F5_REF_AUDIO) if F5_REF_AUDIO else None
    if repo and ref and not ref.is_absolute(): ref = repo / ref
    cli = repo / "src" / "f5_tts" / "infer" / "infer_cli.py" if repo else None
    python_available = bool(F5_TTS_PYTHON and (shutil.which(F5_TTS_PYTHON) or Path(F5_TTS_PYTHON).exists()))
    checks = _engine_checks(repo, cli, ref, python_available)
    checks["device"] = _device_available() if checks["configured"] and checks["python"] else False
    if session_id is not None and session_id != _profile_session_id:
        return False
    with _engine_session_lock:
        _engine_session.validated = all(checks.values())
        _engine_session.test_succeeded = False
        _engine_session.checks = dict(checks)
        _engine_session.last_test = time.time()
        validated = _engine_session.validated
    if not validated:
        failed = next(name for name, passed in checks.items() if not passed)
        messages = {"reference_audio": "reference audio is missing", "ffmpeg": "ffmpeg is required", "cli": "F5-TTS CLI is missing", "device": "configured device is unavailable"}
        issue = messages.get(failed, f"{failed} check failed")
        with _engine_session_lock:
            _engine_session.issue = issue
            _engine_session.details = "Checks: " + ", ".join(f"{name}={'ok' if value else 'failed'}" for name, value in checks.items())
        _last_event = f"Engine test failed: {issue}"
        _emit_status()
        return False

    try:
        with tempfile.TemporaryDirectory(prefix="neuroicu-engine-test-") as tmp:
            _tts_file("Neuro ICU TTS engine test", Path(tmp) / "engine-test.mp3", cancel_event)
        if session_id is not None and session_id != _profile_session_id:
            return False
        with _engine_session_lock:
            _engine_session.issue = None
            _engine_session.test_succeeded = True
            _engine_session.details = f"Checks passed; model: {F5_MODEL}; device: {config.get_device()}"
        _last_event = "Engine test succeeded"
        _emit_status()
        return True
    except Exception as exc:
        if session_id is not None and session_id != _profile_session_id:
            return False
        with _engine_session_lock:
            _engine_session.issue = str(exc)
            _engine_session.details = "Engine validation passed, but test generation failed."
        _last_event = f"Engine test failed: {exc}"
        _emit_status()
        return False


def _start_engine_test():
    """Start validation/test generation away from the Anki/Qt UI thread."""
    global _engine_test_thread, _last_event
    with _engine_test_lock:
        if _engine_test_thread and _engine_test_thread.is_alive():
            return False
        _engine_test_cancel.clear()
        _last_event = "Engine test started"
        _engine_test_thread = threading.Thread(target=_run_engine_test, args=(_profile_session_id, _engine_test_cancel), name="neuroicu-engine-test", daemon=True)
        _engine_test_thread.start()
    _emit_status()
    return True


def _scope_snapshot(force=False):
    """Return scope counts, reusing a recent result unless *force* is set.

    Counting eligibility opens every candidate note, which is far too expensive
    to repeat on each queue transition, so results are cached for
    SCOPE_CACHE_SECONDS and invalidated whenever scope inputs change.
    """
    global _scope_cache
    description = f"Pilot tag {PILOT_TAG}" if PILOT_ONLY else "Eligible Neuro ICU notes"
    mode = "pilot" if PILOT_ONLY else "full"
    if mw is None:
        return ScopeSnapshot(mode, description)
    if not force:
        with _scope_cache_lock:
            cached = _scope_cache
        if cached is not None and time.monotonic() - cached[0] < SCOPE_CACHE_SECONDS:
            counted = cached[1]
            return ScopeSnapshot(mode, description, counted.eligible_count, counted.selected_count, _last_scan_at)
    try:
        note_ids = mw.col.find_notes(f'note:"{MODEL}"')
        eligible = sum(_eligible(mw.col.get_note(note_id)) for note_id in note_ids)
        snapshot = ScopeSnapshot(mode, description, eligible, eligible, _last_scan_at)
    except Exception:
        LOGGER.debug("Scope count unavailable", exc_info=True)
        return ScopeSnapshot(mode, description, None, None, _last_scan_at)
    with _scope_cache_lock:
        _scope_cache = (time.monotonic(), snapshot)
    return snapshot


def _queue_snapshot():
    worker = getattr(mw, "_neuroicu_tts_worker", None) if mw is not None else None
    return worker.snapshot() if worker else QueueSnapshot()


def _activity_snapshot():
    return ActivitySnapshot(_last_scan_at, _last_generation_at, _last_event)


def _status_service():
    global _status_service_instance
    if _status_service_instance is None:
        _status_service_instance = StatusService(_engine_snapshot, _scope_snapshot, _queue_snapshot, _activity_snapshot)
    return _status_service_instance


def _emit_status():
    """Publish a worker-safe event; snapshot adapters run on the main thread."""
    if _status_service_instance is not None:
        _status_events.put_nowait(object())


def _drain_status_events():
    """Recompute status on the Anki/Qt thread after worker notifications."""
    if _status_service_instance is None:
        return False
    pending = False
    while True:
        try:
            _status_events.get_nowait()
            pending = True
        except queue.Empty:
            break
    if pending:
        _status_service_instance.notify()
    return pending


def refresh_status():
    """Refresh the dashboard snapshot after configuration or external activity."""
    return _status_service().notify()


def _queue_current():
    card = getattr(getattr(mw, "reviewer", None), "card", None)
    return bool(card and _submit(card.note(), force=True))


_TAB_BY_ACTION = {
    "Open Voice & engine": "Settings",
    "Open Queue": "Queue",
    "Review failures": "Queue",
    "Review scope": "Scope",
}


def _dashboard_action(action):
    """Dispatch dashboard labels to application-layer operations."""
    global _last_event
    if action == "Run engine test":
        return _start_engine_test()
    if action in _TAB_BY_ACTION:
        dialog = getattr(mw, "_neuroicu_tts_dialog", None)
        if dialog is None:
            return False
        return bool(dialog.show_tab(_TAB_BY_ACTION[action]))
    if action == "Open Diagnostics":
        dialog = getattr(mw, "_neuroicu_tts_dialog", None)
        if dialog is not None:
            dialog.show_tab("Diagnostics")
        snapshot = _engine_snapshot()
        LOGGER.warning("Control Center diagnostics requested: %s", snapshot.details or snapshot.issue or "no current issue")
        _last_event = "Diagnostics requested"
        _emit_status()
        return snapshot
    LOGGER.warning("Unknown Control Center action ignored: %s", action)
    return False


# ── Config Center tab callbacks ─────────────────────────────────────────────

def _jobs_db_path():
    return Path(mw.pm.profileFolder()) / "neuroicu_tts.sqlite3"


def _settings_snapshot():
    """G1 — current config values for the Settings form."""
    return config.as_dict()


def _save_settings(values):
    """G1 — validate and persist Settings-tab values; return error strings."""
    updates = _coerce_settings(values)
    try:
        errors = config.save(updates)
    except OSError as exc:
        LOGGER.error("Could not save settings: %s", exc)
        return [str(exc)]
    if not errors:
        _refresh_engine_globals()
        _apply_log_level()
        _last_event_set("Settings saved")
    return errors


_NUMERIC_SETTINGS = {
    "f5_speed": float,
    "retry_backoff_seconds": float,
    "f5_nfe_step": int,
    "max_chunk_chars": int,
    "max_attempts": int,
}


def _coerce_settings(values):
    """Turn Settings-tab strings into the types the config schema expects.

    Values that will not convert are passed through untouched so that
    ``config.validate`` reports one clear range error instead of a TypeError.
    """
    updates = dict(values)
    for key, caster in _NUMERIC_SETTINGS.items():
        raw = updates.get(key)
        if isinstance(raw, str):
            try:
                updates[key] = caster(raw)
            except ValueError:
                pass
    return updates


def _reload_settings():
    """G1 — re-read config.json when changed and refresh live scope values."""
    config.reload_config_if_changed()
    _refresh_engine_globals()
    _apply_log_level()
    _emit_status()
    return True


def _last_event_set(message):
    global _last_event
    _last_event = message
    _emit_status()


def _queue_counts():
    """G2 — persistent job counts by status for the read-only Queue tab."""
    try:
        store = JobStore(_jobs_db_path())
        try:
            return store.counts_by_status()
        finally:
            store.close()
    except Exception as exc:
        LOGGER.warning("Queue counts unavailable: %s", exc)
        return {}


def _queue_pause(paused):
    """Pause or resume the worker; the running job always finishes first."""
    worker = getattr(mw, "_neuroicu_tts_worker", None) if mw is not None else None
    if worker is None:
        return False
    worker.pause() if paused else worker.resume()
    _last_event_set("Queue paused" if paused else "Queue resumed")
    return True


def _queue_cancel():
    """Cancel everything not yet synthesized; return how many jobs were dropped."""
    worker = getattr(mw, "_neuroicu_tts_worker", None) if mw is not None else None
    in_memory = worker.cancel_pending() if worker is not None else 0
    try:
        store = JobStore(_jobs_db_path())
        try:
            persisted = store.cancel_pending()
        finally:
            store.close()
    except Exception:
        LOGGER.exception("Could not cancel persisted jobs")
        persisted = 0
    cancelled = max(in_memory, persisted)
    _last_event_set(f"Cancelled {cancelled} pending job(s)")
    return cancelled


def _queue_retry_failed():
    """Re-queue failed and cancelled jobs against the current note content."""
    worker = getattr(mw, "_neuroicu_tts_worker", None) if mw is not None else None
    if worker is None:
        return 0
    try:
        store = JobStore(_jobs_db_path())
        try:
            rows = store.retryable_jobs()
        finally:
            store.close()
    except Exception:
        LOGGER.exception("Could not read retryable jobs")
        return 0
    requeued = 0
    profile_digest = _synthesis_profile_digest()
    for _job_id, note_id, _source, _digest in rows:
        try:
            # Re-derive from the note so a retry never replays stale text.
            if _submit(mw.col.get_note(note_id), force=True, profile_digest=profile_digest):
                requeued += 1
        except Exception:
            LOGGER.exception("Could not re-queue note %s", note_id)
    _last_event_set(f"Re-queued {requeued} of {len(rows)} failed job(s)")
    return requeued


def _log_tail(lines=200):
    """G3 — last N lines of the add-on log."""
    try:
        content = (CONFIG_DIR / "neuroicu_tts.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Log unavailable: {exc}"
    return "\n".join(content[-lines:]) if content else "Log is empty."


def _storage_size():
    """G4 — total bytes of add-on-managed audio in the collection media folder."""
    try:
        media_path = str(_media_dir())
        if not os.path.isdir(media_path):
            return 0
        total = 0
        with os.scandir(media_path) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith("neuroicu_tts_") and name.endswith(".mp3") and entry.is_file():
                    total += entry.stat().st_size
        return total
    except Exception as exc:
        LOGGER.warning("Storage size unavailable: %s", exc)
        return 0


def _clear_finished():
    """G4 — remove terminal jobs; return how many were cleared."""
    store = JobStore(_jobs_db_path())
    try:
        removed = store.clear_finished()
    finally:
        store.close()
    _last_event_set(f"Cleared {removed} finished job(s)")
    return removed


def _toggle_pilot(enabled):
    """G5 — persist the pilot-only scope toggle."""
    global PILOT_ONLY
    try:
        errors = config.save({"pilot_only": bool(enabled)})
    except OSError as exc:
        LOGGER.error("Could not save pilot scope: %s", exc)
        return False
    if errors:
        return False
    PILOT_ONLY = bool(enabled)
    _invalidate_scope_cache()
    _last_event_set(f"Pilot-only scope {'enabled' if PILOT_ONLY else 'disabled'}")
    return True


def _full_deck_info():
    """G11 — (eligible note count, estimated seconds) for the ImpactDialog."""
    note_ids = mw.col.find_notes(f'note:"{MODEL}"')
    count = sum(1 for nid in note_ids if _eligible(mw.col.get_note(nid), ignore_pilot=True))
    return count, estimate_runtime(count)


def _full_deck_convert():
    """G11 — enqueue one job per eligible note; return (enqueued, total)."""
    worker = getattr(mw, "_neuroicu_tts_worker", None)
    if worker is None:
        return (0, 0)
    try:
        store = JobStore(_jobs_db_path())
        try:
            digests = store.existing_digests()
        finally:
            store.close()
    except Exception:
        LOGGER.exception("Could not read job digests; proceeding without dedupe")
        digests = set()
    total = 0
    enqueued = 0
    profile_digest = _synthesis_profile_digest()
    for nid in mw.col.find_notes(f'note:"{MODEL}"'):
        try:
            note = mw.col.get_note(nid)
            if not _eligible(note, ignore_pilot=True):
                continue
            total += 1
            state = state_for(note["Extra"])
            artifact_digest = generation_digest(state.digest, profile_digest)
            if artifact_digest in digests:
                continue  # G11.5 — already queued/stored for this exact content+profile
            if _submit(note, profile_digest=profile_digest):
                enqueued += 1
                digests.add(artifact_digest)
        except Exception:
            LOGGER.exception("Full-deck enqueue failed for note %s", nid)  # G11.4 — keep going
    _last_event_set(f"Full-deck conversion queued {enqueued} of {total} note(s)")
    LOGGER.info("Full-deck conversion queued %d of %d note(s)", enqueued, total)
    return (enqueued, total)


def _update_pilot_tag(value):
    """Validate and persist the pilot tag used by scope selection."""
    global PILOT_TAG, _last_event
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        return False
    try:
        errors = config.save({"pilot_tag": value})
    except OSError as exc:
        LOGGER.error("Could not save pilot tag: %s", exc)
        return False
    if errors:
        LOGGER.error("Could not save pilot tag: %s", "; ".join(errors))
        return False
    PILOT_TAG = value
    _invalidate_scope_cache()
    _last_event = f"Pilot tag updated to {value}"
    _emit_status()
    return True

def _show_center():
    refresh_status()
    d = ControlCenter(
        mw,
        status_service=_status_service(),
        on_scan=scan_collection,
        on_current=_queue_current,
        on_action=_dashboard_action,
        pilot_tag=PILOT_TAG,
        on_pilot_tag=_update_pilot_tag,
        on_settings_snapshot=_settings_snapshot,
        on_save_settings=_save_settings,
        on_reload_settings=_reload_settings,
        on_queue_counts=_queue_counts,
        on_engine_test=_start_engine_test,
        on_log_tail=_log_tail,
        on_storage_size=_storage_size,
        on_clear_finished=_clear_finished,
        on_toggle_pilot=_toggle_pilot,
        on_full_deck_info=_full_deck_info,
        on_full_deck_convert=_full_deck_convert,
        on_upgrade_legacy=upgrade_legacy_notes,
        on_queue_pause=_queue_pause,
        on_queue_cancel=_queue_cancel,
        on_queue_retry=_queue_retry_failed,
    )
    mw._neuroicu_tts_dialog = d
    d.show()

def _after_sync(*_):
    global _last_event
    _last_event = "Sync completed; refreshing scope"
    _emit_status()
    session_id = _profile_session_id
    QTimer.singleShot(1000, lambda: _scan_if_current(session_id))


def _scan_if_current(session_id):
    if session_id != _profile_session_id or mw is None:
        return False
    scan_collection()
    return True

def _note_will_update(note, *_):
    """Save hook: enqueue only; synthesis remains outside Anki's UI thread."""
    if mw is not None:
        note_id, session_id = note.id, _profile_session_id
        QTimer.singleShot(0, lambda: _queue_note_if_current(note_id, session_id))


def _queue_note_if_current(note_id, session_id):
    """Resolve a saved note only if its originating profile is still active."""
    if session_id != _profile_session_id or mw is None:
        return False
    try:
        note = mw.col.get_note(note_id)
    except Exception:
        LOGGER.exception("Could not reload updated note %s", note_id)
        return False
    return _submit(note)


def _register_runtime_hooks():
    """Register process-wide sync/edit hooks once across profile reopen cycles."""
    global _sync_hook_registered, _note_hook_registered
    if not _sync_hook_registered:
        try:
            gui_hooks.sync_did_finish.append(_after_sync)
            _sync_hook_registered = True
        except AttributeError:
            addHook("syncFinished", _after_sync)
            _sync_hook_registered = True
    if not _note_hook_registered:
        try:
            gui_hooks.note_will_be_updated.append(_note_will_update)
            _note_hook_registered = True
        except AttributeError:
            # Older Anki versions have no equivalent supported edit hook.
            _note_hook_registered = True


def _shutdown_profile_runtime(*_):
    """Stop profile-owned resources before Anki changes or closes the profile."""
    global _profile_session_id, _engine_test_thread
    if mw is None: return
    _profile_session_id = None
    _engine_test_cancel.set()
    engine_thread = _engine_test_thread
    if engine_thread is not None and engine_thread.is_alive():
        engine_thread.join(timeout=5)
    if engine_thread is not None and engine_thread.is_alive():
        LOGGER.error("Engine test did not stop within five seconds")
    else:
        _engine_test_thread = None
    worker_alive = False
    worker = getattr(mw, "_neuroicu_tts_worker", None)
    if worker is not None:
        try: worker.stop()
        except Exception: LOGGER.exception("Could not request TTS worker shutdown")
        try: worker.join(timeout=5)
        except Exception: LOGGER.exception("Could not join TTS worker")
        worker_alive = worker.is_alive() if hasattr(worker, "is_alive") else False
        if not worker_alive:
            mw._neuroicu_tts_worker = None
        else:
            LOGGER.error("TTS worker did not stop within five seconds; restart Anki before opening another profile")
    timer = getattr(mw, "_neuroicu_tts_timer", None)
    if timer is not None:
        try: timer.stop()
        except Exception: LOGGER.exception("Could not stop TTS timer")
        mw._neuroicu_tts_timer = None
    menu = getattr(getattr(mw, "form", None), "menuTools", None)
    for action in getattr(mw, "_neuroicu_tts_menu_actions", ()):
        try:
            if menu is not None: menu.removeAction(action)
            if hasattr(action, "deleteLater"): action.deleteLater()
        except Exception: LOGGER.exception("Could not remove Neuro ICU TTS menu action")
    mw._neuroicu_tts_menu_actions = []
    shortcut = getattr(mw, "_neuroicu_tts_shortcut", None)
    if shortcut is not None:
        try:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        except Exception: LOGGER.exception("Could not remove Neuro ICU TTS shortcut")
        mw._neuroicu_tts_shortcut = None
    shortcut_gen = getattr(mw, "_neuroicu_tts_shortcut_gen", None)
    if shortcut_gen is not None:
        try:
            shortcut_gen.setEnabled(False)
            shortcut_gen.deleteLater()
        except Exception: LOGGER.exception("Could not remove Neuro ICU TTS generate shortcut")
        mw._neuroicu_tts_shortcut_gen = None
    mw._neuroicu_tts_initialized = worker_alive

def _init_profile_runtime():
    global _profile_session_id
    if mw is None or getattr(mw, "_neuroicu_tts_initialized", False):
        return
    _configure_logging()
    results = queue.Queue()
    profile = Path(mw.pm.profileFolder())
    _profile_session_id = str(uuid.uuid4())
    worker = TTSWorker(profile / "neuroicu_tts.sqlite3", _media_dir(), results, _profile_session_id)
    worker.start()
    mw._neuroicu_tts_worker = worker
    menu = QAction("Neuro ICU TTS Control Center", mw); menu.setShortcut(QKeySequence("Ctrl+Alt+T")); menu.triggered.connect(_show_center); mw.form.menuTools.addAction(menu)
    scan = QAction("Queue Neuro ICU TTS scan", mw); scan.triggered.connect(scan_collection); mw.form.menuTools.addAction(scan)
    generate = QAction("Generate Neuro ICU TTS for Current Card", mw); generate.setShortcut(QKeySequence("Ctrl+Alt+G")); generate.triggered.connect(_queue_current); mw.form.menuTools.addAction(generate)
    mw._neuroicu_tts_menu_actions = [menu, scan, generate]
    shortcut = QShortcut(QKeySequence("Ctrl+Alt+V"), mw); shortcut.activated.connect(_replay); mw._neuroicu_tts_shortcut = shortcut
    shortcut_gen = QShortcut(QKeySequence("Ctrl+Alt+G"), mw); shortcut_gen.activated.connect(_queue_current); mw._neuroicu_tts_shortcut_gen = shortcut_gen
    timer = QTimer(mw); timer.timeout.connect(_drain); timer.start(250); mw._neuroicu_tts_timer = timer
    _register_runtime_hooks()
    mw._neuroicu_tts_initialized = True
    LOGGER.info("Neuro ICU TTS loaded with asynchronous F5-TTS worker")
    scan_collection()


def _profile_did_open(*_):
    QTimer.singleShot(0, _init_profile_runtime)


def init_addon():
    global _profile_hook_registered, _profile_close_hook_registered
    if mw is None:
        return
    if not _profile_hook_registered:
        try:
            gui_hooks.profile_did_open.append(_profile_did_open)
            _profile_hook_registered = True
        except AttributeError:
            LOGGER.warning("Profile-open hook unavailable; Neuro ICU TTS will initialize after profile selection")
    if not _profile_close_hook_registered:
        try:
            gui_hooks.profile_will_close.append(_shutdown_profile_runtime)
            _profile_close_hook_registered = True
        except AttributeError:
            LOGGER.warning("Profile-close hook unavailable; worker shutdown will rely on Anki exit")
    if getattr(mw.pm, "name", None) is not None:
        _init_profile_runtime()
