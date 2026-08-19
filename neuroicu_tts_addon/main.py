"""Neuro ICU TTS add-on with an asynchronous F5-TTS queue."""
from __future__ import annotations

import json, logging, os, queue, shutil, sqlite3, subprocess, tempfile, threading, time, uuid
from dataclasses import dataclass, field
from pathlib import Path

from .tts_core import digest_for, filename, generation_digest, managed_extra, state_for
from .control_center import ControlCenter
from .status import ActivitySnapshot, EngineSnapshot, QueueSnapshot, ScopeSnapshot, StatusService

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

def _config():
    try:
        value = json.loads((CONFIG_DIR / "config.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError): return {}
CFG = _config()
F5_TTS_REPO = os.environ.get("F5_TTS_REPO", CFG.get("f5_tts_repo", ""))
F5_TTS_PYTHON = os.environ.get("F5_TTS_PYTHON", CFG.get("f5_tts_python", "python3"))
F5_MODEL, F5_REF_AUDIO = CFG.get("f5_model", "F5TTS_v1_Base"), CFG.get("f5_ref_audio", "")
F5_REF_TEXT, F5_DEVICE = CFG.get("f5_ref_text", ""), CFG.get("f5_device", "cpu")
F5_NFE_STEP = str(CFG.get("f5_nfe_step", 16))
FFMPEG_CANDIDATES = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
PILOT_ONLY, PILOT_TAG = bool(CFG.get("pilot_only", True)), CFG.get("pilot_tag", "neuroicu-tts-pilot")
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

def _configure_logging():
    with _logging_lock:
        if LOGGER.handlers: return
        try: h = logging.FileHandler(CONFIG_DIR / "neuroicu_tts.log", encoding="utf-8")
        except OSError:
            LOGGER.warning("Could not create Neuro ICU TTS log file", exc_info=True)
            return
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s")); LOGGER.addHandler(h); LOGGER.setLevel(logging.INFO)

@dataclass(frozen=True)
class Job:
    job_id: str; note_id: int; source: str; digest: str; force: bool = False

@dataclass(frozen=True)
class Result:
    job: Job; artifact: Path | None; error: str | None = None


@dataclass(frozen=True)
class RecoveredJob:
    job: Job


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
        self.conn.execute("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, note_id INTEGER, source TEXT, digest TEXT, state TEXT, error TEXT, updated REAL)"); self.conn.commit()
    def enqueue(self, j):
        self.conn.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, 'queued', NULL, ?)", (j.job_id, j.note_id, j.source, j.digest, time.time())); self.conn.commit()
    def set_state(self, jid, state, error=None):
        self.conn.execute("UPDATE jobs SET state=?, error=?, updated=? WHERE job_id=?", (state, error, time.time(), jid)); self.conn.commit()
    def close(self): self.conn.close()
    def recoverable(self):
        return self.conn.execute("SELECT note_id, source, digest FROM jobs WHERE state IN ('queued', 'running', 'staged', 'failed', 'failed_retryable')").fetchall()

    def recoverable_jobs(self):
        rows = self.conn.execute("SELECT job_id, note_id, source, digest FROM jobs WHERE state IN ('queued', 'running', 'staged', 'failed', 'failed_retryable')").fetchall()
        self.conn.execute("UPDATE jobs SET state='queued', error=NULL, updated=? WHERE state IN ('running', 'staged', 'failed', 'failed_retryable')", (time.time(),))
        self.conn.commit()
        return rows

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
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
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
    configured = CFG.get("ffmpeg_path")
    candidates = ([configured] if configured else []) + [shutil.which("ffmpeg")] + list(FFMPEG_CANDIDATES)
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run_f5_inference(text: str, wav_dir: Path, cancel_event=None):
    args = [F5_TTS_PYTHON, str(Path(F5_TTS_REPO) / "src" / "f5_tts" / "infer" / "infer_cli.py"), "--model", F5_MODEL, "--ref_audio", str(Path(F5_REF_AUDIO) if Path(F5_REF_AUDIO).is_absolute() else Path(F5_TTS_REPO) / F5_REF_AUDIO), "--ref_text", F5_REF_TEXT, "--gen_text", text, "--output_dir", str(wav_dir), "--output_file", "speech.wav", "--device", F5_DEVICE, "--nfe_step", F5_NFE_STEP]
    try:
        _run_process(args, cwd=F5_TTS_REPO, cancel_event=cancel_event)
    except RuntimeError as exc:
        if F5_DEVICE.lower() != "mps" or not _is_mps_crash(exc):
            raise
        LOGGER.warning("F5-TTS MPS inference crashed; retrying note on CPU")
        args[args.index("--device") + 1] = "cpu"
        _run_process(args, cwd=F5_TTS_REPO, cancel_event=cancel_event)


def _tts_file(text: str, destination: Path, cancel_event=None):
    if not F5_TTS_REPO: raise RuntimeError("Set f5_tts_repo in config.json")
    cli = Path(F5_TTS_REPO) / "src" / "f5_tts" / "infer" / "infer_cli.py"
    ref = Path(F5_REF_AUDIO); ref = ref if ref.is_absolute() else Path(F5_TTS_REPO) / ref
    ffmpeg = _ffmpeg_path()
    if not cli.exists() or not ref.exists(): raise RuntimeError("F5-TTS CLI or reference audio is missing")
    if not ffmpeg: raise RuntimeError("ffmpeg is required to convert F5-TTS WAV output to MP3")
    with tempfile.TemporaryDirectory(prefix="neuroicu-tts-") as tmp:
        wav_dir = Path(tmp) / "wav"; wav_dir.mkdir()
        _run_f5_inference(text, wav_dir, cancel_event)
        wav = wav_dir / "speech.wav"
        if not wav.exists(): raise RuntimeError("F5-TTS completed without producing a WAV")
        staged = Path(tmp) / "speech.mp3"
        _run_process([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-codec:a", "libmp3lame", "-q:a", "4", str(staged)], timeout=120, cancel_event=cancel_event)
        if not staged.exists() or staged.stat().st_size == 0: raise RuntimeError("ffmpeg completed without producing an MP3")
        destination.parent.mkdir(parents=True, exist_ok=True); os.replace(staged, destination)

class TTSWorker(threading.Thread):
    def __init__(self, db_path, media_dir, results, session_id=None):
        super().__init__(name="neuroicu-tts-worker", daemon=True); self.jobs = queue.Queue(); self.results = results; self.db_path = Path(db_path); self.media_dir = Path(media_dir); self.staging_dir = self.db_path.parent / "neuroicu_tts_staging"; self.session_id = session_id; self.store = None; self.stopping = threading.Event(); self.stopped = False; self.pending = set(); self.pending_lock = threading.Lock(); self.status_lock = threading.Lock(); self.statuses = {}; self.current_job = None; self.current_note = None; self.latest_error = None
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
            return QueueSnapshot(counts, self.current_job, self.current_note, latest_error=getattr(self, "latest_error", None))
    def mark_committed(self, job_id, success, error=None):
        state = "succeeded" if success else ("failed_retryable" if error else "stale")
        with self.status_lock:
            self.statuses[job_id] = state
            if error: self.latest_error = error
        if not self.stopping.is_set():
            self.jobs.put(JobStateUpdate(job_id, state, error))
    def stop(self):
        if not self.stopping.is_set():
            self.stopping.set(); self.jobs.put(None)
    def run(self):
        # SQLite is created and used only by the worker thread.
        self.store = JobStore(self.db_path)
        for job_id, note_id, source, digest in self.store.recoverable_jobs():
            with self.status_lock: self.statuses[job_id] = "queued"
            self.results.put(RecoveredJob(Job(job_id, note_id, source, digest)))
        if self.statuses: _emit_status()
        try:
            while True:
                j = self.jobs.get()
                if j is None: break
                if isinstance(j, JobStateUpdate):
                    self.store.set_state(j.job_id, j.state, j.error)
                    continue
                self.store.enqueue(j)
                if self.stopping.is_set():
                    with self.pending_lock: self.pending.discard((j.note_id, j.digest))
                    continue
                self.store.set_state(j.job_id, "running")
                with self.status_lock: self.statuses[j.job_id] = "running"; self.current_job = j.job_id; self.current_note = j.note_id
                _emit_status()
                dest = self.staging_dir / f"{j.job_id}.mp3"
                try:
                    _tts_file(j.source, dest, self.stopping)
                    self.store.set_state(j.job_id, "staged")
                    with self.status_lock: self.latest_error = None
                    self.results.put(Result(j, dest))
                except Exception as exc:
                    self.store.set_state(j.job_id, "failed_retryable", str(exc))
                    with self.status_lock: self.statuses[j.job_id] = "failed_retryable"; self.latest_error = str(exc)
                    self.results.put(Result(j, None, str(exc)))
                finally:
                    with self.status_lock: self.current_job = None; self.current_note = None
                    with self.pending_lock: self.pending.discard((j.note_id, j.digest))
                    _emit_status()
        finally:
            self.store.close()
            self.stopped = True

def _media_dir(): return Path(mw.col.media.dir())


def _managed_media_exists(sound_filename):
    try:
        return bool(sound_filename and (_media_dir() / sound_filename).is_file())
    except (AttributeError, OSError, TypeError):
        return False


def _path_identity(path):
    if not path:
        return None
    value = Path(path)
    try:
        resolved = value.resolve()
        stat = resolved.stat()
        identity = {"path": str(resolved), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"path": str(value)}
    if resolved.is_dir():
        head = resolved / ".git" / "HEAD"
        try:
            head_value = head.read_text(encoding="utf-8").strip()
            identity["git_head"] = head_value
            if head_value.startswith("ref: "):
                identity["git_revision"] = (resolved / ".git" / head_value[5:]).read_text(encoding="utf-8").strip()
        except OSError:
            pass
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
        "device": F5_DEVICE,
        "nfe_step": F5_NFE_STEP,
    }
    return digest_for(json.dumps(payload, sort_keys=True, separators=(",", ":")))

def _eligible(note):
    if note.model()["name"] != MODEL or not note["Extra"].strip(): return False
    if PILOT_ONLY and PILOT_TAG not in note.tags: return False
    decks = {mw.col.decks.get(c.did)["name"] for c in note.cards()}
    return any(n == DECK_PREFIX or n.startswith(DECK_PREFIX + "::") for n in decks)

def _submit(note, force=False):
    if not _eligible(note): return False
    s = state_for(note["Extra"])
    profile_digest = _synthesis_profile_digest()
    artifact_digest = generation_digest(s.digest, profile_digest)
    expected = filename(note.id, artifact_digest)
    if not force and s.marker_digest == s.digest and getattr(s, "profile_digest", None) == profile_digest and s.sound_filename == expected and _managed_media_exists(expected): return False
    worker = getattr(mw, "_neuroicu_tts_worker", None)
    if not worker: return False
    return worker.submit(Job(str(uuid.uuid4()), note.id, s.text, artifact_digest, force))

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
        _submit(note)
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
    try:
        count = sum(_submit(mw.col.get_note(nid)) for nid in mw.col.find_notes(f'note:"{MODEL}"'))
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
    if web: web.eval("document.querySelector('.replay-button')?.click()")

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
    return EngineSnapshot(configured=configured, validated=session.validated, test_succeeded=session.test_succeeded, issue=issue, last_test=session.last_test, details=session.details or f"Model: {F5_MODEL}; device: {F5_DEVICE}", checks=checks | {"device": session.checks.get("device")})


def _engine_checks(repo, cli, ref, python_available):
    configured = bool(F5_TTS_REPO and F5_TTS_PYTHON and F5_REF_AUDIO and F5_MODEL)
    return {"configured": configured, "python": python_available, "cli": bool(cli and cli.exists()), "reference_audio": bool(ref and ref.exists()), "ffmpeg": bool(_ffmpeg_path()), "device": None}


def _device_available():
    if not F5_DEVICE or F5_DEVICE.lower() in {"cpu", "cuda", "auto"}:
        return True
    try:
        device = F5_DEVICE.lower()
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
            _engine_session.details = f"Checks passed; model: {F5_MODEL}; device: {F5_DEVICE}"
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


def _scope_snapshot():
    description = f"Pilot tag {PILOT_TAG}" if PILOT_ONLY else "Eligible Neuro ICU notes"
    if mw is None:
        return ScopeSnapshot("pilot" if PILOT_ONLY else "full", description)
    try:
        note_ids = mw.col.find_notes(f'note:"{MODEL}"')
        eligible = sum(_eligible(mw.col.get_note(note_id)) for note_id in note_ids)
        return ScopeSnapshot("pilot" if PILOT_ONLY else "full", description, eligible, eligible, _last_scan_at)
    except Exception:
        return ScopeSnapshot("pilot" if PILOT_ONLY else "full", description, None, None, _last_scan_at)


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


def _dashboard_action(action):
    """Dispatch dashboard labels to existing application-layer operations."""
    if action == "Run engine test":
        return _start_engine_test()
    if action in {"Open Voice & engine", "Open Queue", "Review failures", "Review scope"}:
        return _dashboard_unavailable(action)
    if action == "Open Diagnostics":
        snapshot = _engine_snapshot()
        LOGGER.warning("Control Center diagnostics requested: %s", snapshot.details or snapshot.issue or "no current issue")
        global _last_event
        _last_event = "Diagnostics requested"
        _emit_status()
        return snapshot
    LOGGER.warning("Unknown Control Center action ignored: %s", action)
    return False


def _dashboard_unavailable(action):
    """Report a dashboard surface that this slice does not implement."""
    global _last_event
    messages = {
        "Open Voice & engine": "Voice and engine configuration surface is not available here; edit config.json, then run the engine test.",
        "Open Queue": "Queue browsing is not available in this dashboard slice.",
        "Review failures": "Failure review is not available in this dashboard slice; see neuroicu_tts.log.",
        "Review scope": "Scope review is not available in this dashboard slice; use the pilot tag field and scan control.",
    }
    message = messages.get(action, f"{action} is unavailable")
    LOGGER.warning("Control Center action unavailable: %s", message)
    _last_event = message
    _emit_status()
    return False


def _update_pilot_tag(value):
    """Validate and persist the pilot tag used by scope selection."""
    global PILOT_TAG, _last_event
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        return False
    updated = dict(CFG)
    updated["pilot_tag"] = value
    config_path = CONFIG_DIR / "config.json"
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        temporary_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_path, config_path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        LOGGER.error("Could not save pilot tag: %s", exc)
        return False
    CFG.clear()
    CFG.update(updated)
    PILOT_TAG = value
    _last_event = f"Pilot tag updated to {value}"
    _emit_status()
    return True

def _show_center():
    refresh_status()
    d = ControlCenter(mw, status_service=_status_service(), on_scan=scan_collection, on_current=_queue_current, on_action=_dashboard_action, pilot_tag=PILOT_TAG, on_pilot_tag=_update_pilot_tag)
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
    mw._neuroicu_tts_menu_actions = [menu, scan]
    shortcut = QShortcut(QKeySequence("Ctrl+Alt+V"), mw); shortcut.activated.connect(_replay); mw._neuroicu_tts_shortcut = shortcut
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
