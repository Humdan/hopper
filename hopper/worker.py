"""`hop work`: run any agent command as a Hopper worker.

For each claimed job the runner writes the brief to a file, starts the command (brief on
stdin too), keeps the lease alive while it runs, and settles the job from the exit code
if the agent didn't report itself. Reserved slots take only urgent work, so a request
from a person never waits behind long background jobs.

Every run is recorded as usage: how long it took and, when the agent says, what it cost.
Cost comes from Claude Code's JSON result (`claude -p --output-format json`) or from a
JSON file the agent writes to $HOPPER_USAGE_FILE.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from .prompt import render
from .util import HopperError, expand, parse_priority


class _Locked:
    """Serialize calls on one Hopper across the runner's threads."""

    def __init__(self, hop):
        self._hop, self._lock = hop, threading.Lock()

    def __getattr__(self, name):
        fn = getattr(self._hop, name)
        if not callable(fn):
            return fn

        def call(*a, **kw):
            with self._lock:
                return fn(*a, **kw)
        return call


def _tail(path: str, limit: int = 3000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            text = fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return ("…" + text) if size > limit else text


_USAGE_KEYS = ("cost_usd", "input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "model")


def _read_tail(path: str, limit: int = 262144) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def agent_result(text: str) -> dict | None:
    """Find the agent's final JSON result in its output, if it printed one.

    Understands Claude Code's result object (`--output-format json`, or the last line of
    `stream-json`) and a plain {"cost_usd": ..., "input_tokens": ...} object. Returns
    {"usage": {...}, "text": final answer or None, "is_error": bool}, or None."""
    candidates = [text.strip()] + [ln.strip() for ln in reversed(text.splitlines())]
    for chunk in candidates:
        if not (chunk.startswith("{") and chunk.endswith("}")):
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result" or "total_cost_usd" in obj:   # Claude Code
            u = obj.get("usage") or {}
            models = list((obj.get("modelUsage") or {}).keys())
            usage = {"cost_usd": obj.get("total_cost_usd"),
                     "input_tokens": u.get("input_tokens"),
                     "output_tokens": u.get("output_tokens"),
                     "cache_read_tokens": u.get("cache_read_input_tokens"),
                     "cache_write_tokens": u.get("cache_creation_input_tokens"),
                     "model": ",".join(models) or None}
            result = obj.get("result")
            return {"usage": {k: v for k, v in usage.items() if v is not None},
                    "text": result if isinstance(result, str) else None,
                    "is_error": bool(obj.get("is_error"))}
        usage = {k: obj[k] for k in _USAGE_KEYS if obj.get(k) is not None}
        if usage:
            return {"usage": usage, "text": None, "is_error": False}
    return None


def _usage_file(path: str) -> dict:
    try:
        with open(path) as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    if "total_cost_usd" in obj and "cost_usd" not in obj:
        obj["cost_usd"] = obj["total_cost_usd"]
    return {k: obj[k] for k in _USAGE_KEYS if obj.get(k) is not None}


class Runner:
    def __init__(self, hop, command: str, *, name: str | None = None, shell: bool = False,
                 caps=None, queues=None, tags=None, slots: int = 1, reserve: int = 0,
                 reserve_min_priority="high", poll: float = 5.0, max_runtime: float = 3600,
                 once: bool = False, log_dir: str | None = None, env_extra: dict | None = None):
        if slots < 1 or not 0 <= reserve < slots + (1 if slots == 1 else 0):
            raise HopperError("need slots >= 1 and 0 <= reserve < slots (or reserve 1 of 1)")
        self.hop = _Locked(hop)
        self.command, self.shell = command, shell
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self.caps, self.queues, self.tags = caps, queues, tags
        self.slots, self.reserve = slots, reserve
        self.reserve_min = parse_priority(reserve_min_priority)
        self.poll, self.max_runtime, self.once = poll, max_runtime, once
        db_path = str(getattr(hop, "db_path", "") or "")
        local = db_path and not db_path.startswith(("http://", "https://")) and db_path != ":memory:"
        default_logs = (os.path.join(os.path.dirname(db_path), "logs") if local
                        else os.path.join(tempfile.gettempdir(), "hopper-logs"))
        self.log_dir = expand(log_dir or default_logs)
        os.makedirs(self.log_dir, exist_ok=True)
        self.env_extra = env_extra or {}
        self.running: dict[int, dict] = {}   # slot -> {job, proc, worker, ...}
        self.stopping = threading.Event()
        self.handled = 0

    # ── one job ────────────────────────────────────────────────────────────────

    def _argv(self, job: dict, prompt_file: str):
        values = {"prompt_file": prompt_file, "job_id": job["id"], "title": job["title"]}
        if self.shell:
            return self.command.format(**{k: shlex.quote(v) for k, v in values.items()})
        return [part.format(**values) for part in shlex.split(self.command)]

    def _start(self, slot: int, job: dict) -> None:
        worker = job["lease_owner"]
        brief = render(job, events=self.hop.events(job_id=job["id"]))
        fd, prompt_file = tempfile.mkstemp(prefix=f"hopper-{job['id']}-", suffix=".md")
        with os.fdopen(fd, "w") as fh:
            fh.write(brief)
        log_path = os.path.join(self.log_dir, f"{job['id']}.log")
        usage_file = prompt_file[:-3] + ".usage.json"
        env = dict(os.environ, **self.env_extra,
                   HOPPER_JOB_ID=job["id"], HOPPER_WORKER=worker, HOPPER_PROMPT_FILE=prompt_file,
                   HOPPER_USAGE_FILE=usage_file)
        if not os.environ.get("HOPPER_URL"):
            env["HOPPER_DB"] = self.hop.db_path  # the agent's `hop done` hits the same queue
        log = open(log_path, "ab")
        proc = subprocess.Popen(self._argv(job, prompt_file), shell=self.shell, env=env,
                                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            proc.stdin.write(brief.encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the command doesn't read stdin; it has the prompt file
        self.running[slot] = {"job": job, "proc": proc, "worker": worker, "log": log,
                              "log_path": log_path, "prompt_file": prompt_file,
                              "usage_file": usage_file, "log_start": os.path.getsize(log_path),
                              "started": time.time(), "beat": time.time()}
        print(f"[{self.name}] slot {slot}: started {job['id']} (p{job['priority']}) {job['title']}",
              flush=True)

    def _record(self, run, parsed: dict | None) -> None:
        """Log this run's time and cost, unless the agent already recorded this attempt."""
        job = run["job"]
        attempt = job["attempts"]
        try:
            if any(u["attempt"] == attempt for u in self.hop.usage(job_id=job["id"])):
                return
            usage = {**((parsed or {}).get("usage") or {}), **_usage_file(run["usage_file"]),
                     "duration_s": round(time.time() - run["started"], 3), "attempt": attempt}
            self.hop.record_usage(job_id=job["id"], worker=run["worker"], **usage)
        except HopperError as exc:
            print(f"[{self.name}] could not record usage for {job['id']}: {exc}",
                  file=sys.stderr, flush=True)

    def _settle(self, slot: int) -> None:
        run = self.running.pop(slot)
        run["log"].close()
        rc = run["proc"].returncode
        job_id, worker = run["job"]["id"], run["worker"]
        # The log is appended to across attempts; read only what this run wrote.
        this_run = os.path.getsize(run["log_path"]) - run["log_start"]
        parsed = agent_result(_read_tail(run["log_path"], limit=min(this_run, 262144)))
        self._record(run, parsed)
        try:
            job = self.hop.show(job_id=job_id)
            if job["status"] == "running" and job["lease_owner"] == worker:
                out = (parsed or {}).get("text") or _tail(run["log_path"])
                if parsed and parsed["is_error"]:
                    rc = rc or 1
                if rc == 0:
                    self.hop.complete(job_id=job_id, worker=worker,
                                      summary=out or "Finished (exit 0) with no output.",
                                      evidence=f"agent exited 0; full output in {run['log_path']}")
                    outcome = "done (from output)"
                else:
                    self.hop.fail(job_id=job_id, worker=worker, retry=True,
                                  reason=f"agent exited {rc}: {out[-800:] or 'no output'}")
                    outcome = f"failed with exit {rc}"
            else:
                outcome = f"reported {job['status']}"
        except HopperError as exc:
            outcome = f"could not settle: {exc}"
        finally:
            for path in (run["prompt_file"], run["usage_file"]):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self.handled += 1
        print(f"[{self.name}] slot {slot}: {job_id} {outcome}", flush=True)

    def _tend(self, slot: int) -> None:
        """Heartbeat, enforce max runtime, settle when the process ends."""
        run = self.running[slot]
        if run["proc"].poll() is not None:
            return self._settle(slot)
        t = time.time()
        if self.max_runtime and t - run["started"] > self.max_runtime:
            self._kill(run)
            run["proc"].wait()
            self._record(run, None)
            try:
                self.hop.fail(job_id=run["job"]["id"], worker=run["worker"], retry=True,
                              reason=f"ran longer than {int(self.max_runtime)}s and was stopped")
            except HopperError:
                pass
            self.running.pop(slot)["log"].close()
            print(f"[{self.name}] slot {slot}: {run['job']['id']} stopped at max runtime", flush=True)
            return
        if t - run["beat"] > min(60.0, run["job"]["timeout"] / 3):
            try:
                self.hop.heartbeat(job_id=run["job"]["id"], worker=run["worker"])
            except HopperError:
                pass  # the agent already reported (done/ask/split); nothing to keep alive
            run["beat"] = t

    @staticmethod
    def _kill(run) -> None:
        try:
            os.killpg(run["proc"].pid, signal.SIGTERM)
            run["proc"].wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(run["proc"].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # ── the loop ───────────────────────────────────────────────────────────────

    def _free_slot(self):
        """The next slot to fill and the minimum priority it accepts."""
        for slot in range(self.reserve, self.slots):
            if slot not in self.running:
                return slot, 0
        for slot in range(self.reserve):
            if slot not in self.running:
                return slot, self.reserve_min
        return None, None

    def run(self) -> int:
        def stop(*_):
            self.stopping.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        mode = f"{self.slots} slot(s)" + (f", {self.reserve} reserved for p>={self.reserve_min}"
                                          if self.reserve else "")
        print(f"[{self.name}] hopper worker: {mode}, caps={self.caps or '-'}, "
              f"queues={self.queues or 'default'}", flush=True)
        last_flush = 0.0
        while not self.stopping.is_set():
            for slot in list(self.running):
                self._tend(slot)
            if self.once and self.handled and not self.running:
                break
            slot, min_p = self._free_slot()
            claimed = False
            if slot is not None and not (self.once and (self.handled or self.running)):
                try:
                    job = self.hop.claim(worker=f"{self.name}/{slot}", caps=self.caps,
                                         queues=self.queues, tags=self.tags, min_priority=min_p)
                except HopperError as exc:
                    print(f"[{self.name}] claim failed: {exc}", file=sys.stderr, flush=True)
                    job = None
                if job:
                    self._start(slot, job)
                    claimed = True
            if self.once and not self.running and not self.handled and not claimed:
                break  # nothing to do
            if hasattr(self.hop._hop, "flush") and time.time() - last_flush > 10:
                try:
                    self.hop.flush()
                except HopperError:
                    pass
                last_flush = time.time()
            if not claimed:
                self.stopping.wait(self.poll if not self.running else min(self.poll, 1.0))
        # Shutting down: stop agents and hand their jobs back.
        for slot, run in list(self.running.items()):
            self._kill(run)
            try:
                self.hop.release(job_id=run["job"]["id"], worker=run["worker"],
                                 reason="worker shut down")
            except HopperError:
                pass
            run["log"].close()
        return 0
