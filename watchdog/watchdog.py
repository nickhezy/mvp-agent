#!/usr/bin/env python3
"""mvp-agent-mini watchdog — generic scheduler.

Knows nothing about training, evals, or any task. Only knows three things:
  1. how to spawn a worker/supervisor subprocess with a wake envelope,
  2. how to poll the worker's hooks for the first one to fire,
  3. how to apply a supervisor verdict (wake worker or terminate).

All semantics live in FILESYSTEM_CONTRACT.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --- Operator-tunable constants ------------------------------------------

TICK_SECONDS = 10
SANITY_CHECK_SECONDS = 3600
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 30
WORKER_TIMEOUT_SECONDS = 7200
SUPERVISOR_TIMEOUT_SECONDS = 600
MAX_MALFORMED_VERDICTS = 10

# Phases the watchdog reports in STATUS lines and exits with.
PHASE_RUNNING = "running"
PHASE_COMPLETED = "completed"
PHASE_ESCALATE = "escalate"


# --- Data classes --------------------------------------------------------

@dataclass
class Hook:
    """One entry in hooks.json after validation."""
    id: str
    after_seconds: int
    wake_message: dict
    # condition_script may be: None | "<path>" | "<path arg1 arg2 ...>" (shell-split)
    # | ["<path>", "arg1", "arg2", ...] (already parsed). Whichever form is
    # convenient — both end up as a list of args to bash.
    condition_script: Optional[list[str]] = None
    script_timeout_seconds: int = DEFAULT_SCRIPT_TIMEOUT_SECONDS
    registered_at: float = field(default_factory=time.time)

    def deadline_passed(self) -> bool:
        return (time.time() - self.registered_at) >= self.after_seconds


@dataclass
class WatchdogState:
    worker_invocation: int = 0
    supervisor_invocation: int = 0
    malformed_verdicts: int = 0
    tick: int = 0
    last_sanity_check: float = field(default_factory=time.time)


# --- Helpers -------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: Path, payload: dict | list) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def safe_read_json(path: Path):
    """Return parsed JSON, or None on missing/unparseable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# --- Hook parsing & evaluation ------------------------------------------

def parse_hooks(raw, run_dir: Path) -> list[Hook] | str:
    """Validate raw hooks.json contents. Return list of Hook or error string."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return f"hooks.json must be a JSON array, got {type(raw).__name__}"
    hooks: list[Hook] = []
    seen_ids = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return f"hooks[{i}] must be an object"
        for required in ("id", "after_seconds", "wake_message"):
            if required not in entry:
                return f"hooks[{i}] missing required field: {required}"
        hid = entry["id"]
        if hid in seen_ids:
            return f"hooks[{i}] duplicate id: {hid}"
        seen_ids.add(hid)
        try:
            after = int(entry["after_seconds"])
        except (TypeError, ValueError):
            return f"hooks[{i}].after_seconds must be int"
        if not isinstance(entry["wake_message"], dict):
            return f"hooks[{i}].wake_message must be an object"
        cs_raw = entry.get("condition_script")
        if cs_raw is None:
            cs_argv: Optional[list[str]] = None
        elif isinstance(cs_raw, str):
            try:
                cs_argv = shlex.split(cs_raw)
            except ValueError as e:
                return f"hooks[{i}].condition_script (string) does not shell-split cleanly: {e}"
            if not cs_argv:
                return f"hooks[{i}].condition_script (string) is empty after split"
        elif isinstance(cs_raw, list):
            if not cs_raw or not all(isinstance(x, str) for x in cs_raw):
                return f"hooks[{i}].condition_script (list) must be a non-empty list of strings"
            cs_argv = list(cs_raw)
        else:
            return f"hooks[{i}].condition_script must be a string or list of strings"
        hooks.append(Hook(
            id=hid,
            after_seconds=after,
            wake_message=entry["wake_message"],
            condition_script=cs_argv,
            script_timeout_seconds=int(entry.get("script_timeout_seconds", DEFAULT_SCRIPT_TIMEOUT_SECONDS)),
        ))
    return hooks


def evaluate_condition_script(hook: Hook, run_dir: Path, log: logging.Logger) -> bool:
    """Run hook.condition_script. True if it exits 0 and stdout (trimmed) is 'true'."""
    if not hook.condition_script:
        return False
    # condition_script is now a list of argv. The first element is the script
    # path (resolved relative to run_dir); the rest are args. This lets the
    # agent parameterise a generic detector script via a single hook entry,
    # e.g. ["scripts/check_ckpt.sh", "50"].
    script_path = (run_dir / hook.condition_script[0]).resolve()
    argv = ["bash", str(script_path), *hook.condition_script[1:]]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=hook.script_timeout_seconds,
            env={**os.environ, "MVP_AGENT_RUN_DIR": str(run_dir)},
        )
    except subprocess.TimeoutExpired:
        log.warning("hook %s: condition_script timed out after %ds", hook.id, hook.script_timeout_seconds)
        return False
    except (OSError, FileNotFoundError) as e:
        log.warning("hook %s: condition_script failed to launch: %s", hook.id, e)
        return False
    if proc.returncode != 0:
        log.debug("hook %s: condition_script rc=%d stderr=%r", hook.id, proc.returncode, proc.stderr[:200])
        return False
    return proc.stdout.strip().lower() == "true"


def find_fired_hooks(hooks: list[Hook], run_dir: Path, log: logging.Logger) -> list[tuple[Hook, list[str]]]:
    """Return (hook, triggers) for each hook that fired this tick, in original order.
    triggers is a non-empty list with elements 'timer' and/or 'condition_script' —
    both may appear when the deadline and the script fire on the same tick.
    """
    fired: list[tuple[Hook, list[str]]] = []
    for h in hooks:
        triggers: list[str] = []
        if h.deadline_passed():
            triggers.append("timer")
        if evaluate_condition_script(h, run_dir, log):
            triggers.append("condition_script")
        if triggers:
            fired.append((h, triggers))
    return fired


# --- Subprocess wrapper --------------------------------------------------

@dataclass
class Subproc:
    """Tracks one running subprocess (worker or supervisor)."""
    name: str
    process: subprocess.Popen
    started_at: float
    stdout_path: Path
    stderr_path: Path
    timeout_seconds: int

    def poll(self) -> Optional[int]:
        return self.process.poll()

    def overdue(self) -> bool:
        return (time.time() - self.started_at) > self.timeout_seconds

    def stderr_tail(self, n_bytes: int = 2048) -> str:
        try:
            data = self.stderr_path.read_bytes()
            return data[-n_bytes:].decode("utf-8", errors="replace")
        except OSError:
            return ""

    def kill(self):
        try:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        except OSError:
            pass


def spawn(name: str, cmd: str, envelope: dict, run_dir: Path, invocation: int, timeout: int, log: logging.Logger) -> Subproc:
    """Spawn a worker or supervisor subprocess with the wake envelope in env."""
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / f"{name}.{invocation}.out"
    stderr_path = logs_dir / f"{name}.{invocation}.err"
    env = {**os.environ,
           "MVP_AGENT_RUN_DIR": str(run_dir),
           "MVP_AGENT_WAKE_ENVELOPE": json.dumps(envelope)}
    log.info("spawning %s invocation=%d cmd=%r wake_reason=%s next_state=%s",
             name, invocation, cmd, envelope.get("wake_reason"), envelope.get("next_state"))
    proc = subprocess.Popen(
        shlex.split(cmd),
        stdout=stdout_path.open("wb"),
        stderr=stderr_path.open("wb"),
        env=env,
        cwd=str(run_dir),
    )
    return Subproc(
        name=name,
        process=proc,
        started_at=time.time(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=timeout,
    )


# --- Watchdog main loop --------------------------------------------------

class Watchdog:
    def __init__(self, run_dir: Path, worker_cmd: str, supervisor_cmd: str,
                 initial_state: str, tick_seconds: int, sanity_check_seconds: int,
                 log: logging.Logger):
        self.run_dir = run_dir
        self.worker_cmd = worker_cmd
        self.supervisor_cmd = supervisor_cmd
        self.initial_state = initial_state
        self.tick_seconds = tick_seconds
        self.sanity_check_seconds = sanity_check_seconds
        self.log = log
        self.state = WatchdogState()
        self.hooks: list[Hook] = []
        self.worker: Optional[Subproc] = None
        self.supervisor: Optional[Subproc] = None
        self.pending_worker_envelope: Optional[dict] = None
        self.current_supervisor_reason: Optional[str] = None
        self.terminate_reason: Optional[str] = None
        self.escalate_reason: Optional[str] = None
        # Snapshot of the last completed worker invocation, kept until used for
        # the next dispatch's prior_exit. Cleared on consumption.
        self.last_worker_exit: Optional[dict] = None
        self.last_supervisor_exit: Optional[dict] = None

    # ----- top-level loop -----

    def run(self) -> str:
        self.bootstrap()
        try:
            while self.terminate_reason is None and self.escalate_reason is None:
                self.tick()
                time.sleep(self.tick_seconds)
        finally:
            self.shutdown()
        if self.escalate_reason:
            self.log.error("ESCALATE: %s", self.escalate_reason)
            return PHASE_ESCALATE
        self.log.info("COMPLETED: %s", self.terminate_reason)
        return PHASE_COMPLETED

    def bootstrap(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "workspace").mkdir(exist_ok=True)
        (self.run_dir / "logs").mkdir(exist_ok=True)
        self.clear_hooks_file()
        self.clear_verdict_dir()
        envelope = self.build_envelope(
            wake_reason="initial",
            next_state=self.initial_state,
        )
        self.dispatch_worker(envelope)

    def shutdown(self):
        for s in (self.worker, self.supervisor):
            if s and s.poll() is None:
                self.log.warning("shutting down: killing %s", s.name)
                s.kill()

    # ----- per-tick logic -----

    def tick(self):
        self.state.tick += 1
        # 1. If worker is running, check its status (exit or overdue).
        if self.worker:
            self.poll_worker()
        # 2. If supervisor is running, check its status.
        if self.supervisor:
            self.poll_supervisor()
        # If a verdict was applied this tick set terminate/escalate, stop dispatching new work.
        if self.terminate_reason or self.escalate_reason:
            return
        # 3. If a worker dispatch is pending (queued by supervisor verdict or hook), spawn it.
        if self.pending_worker_envelope and not self.worker:
            env = self.pending_worker_envelope
            self.pending_worker_envelope = None
            self.dispatch_worker(env)
        # 4. If waiting on hooks (worker sleeping, hooks present), poll them.
        if self.worker is None and self.hooks and self.supervisor is None:
            self.poll_hooks()
        # 5. Periodic sanity check (only if no supervisor running and worker is sleeping).
        if (self.worker is None
                and self.supervisor is None
                and (time.time() - self.state.last_sanity_check) >= self.sanity_check_seconds):
            envelope = self.build_envelope(
                wake_reason="sanity_check",
                next_state="sanity_check",
            )
            self.dispatch_supervisor(envelope)
            self.state.last_sanity_check = time.time()
        # 6. Status line every ~10 ticks.
        if self.state.tick % 10 == 0:
            self.log.info("STATUS tick=%d worker=%s supervisor=%s hooks=%d malformed=%d",
                          self.state.tick,
                          "busy" if self.worker else "sleeping",
                          "busy" if self.supervisor else "sleeping",
                          len(self.hooks),
                          self.state.malformed_verdicts)

    # ----- worker lifecycle -----

    def dispatch_worker(self, envelope: dict):
        if self.last_worker_exit:
            envelope.setdefault("prior_exit", self.last_worker_exit)
            self.last_worker_exit = None
        self.state.worker_invocation += 1
        envelope["tick"] = self.state.tick
        envelope["wall_clock"] = utc_now_iso()
        self.worker = spawn(
            "worker", self.worker_cmd, envelope,
            self.run_dir, self.state.worker_invocation,
            WORKER_TIMEOUT_SECONDS, self.log,
        )
        # Hooks from prior sleep are consumed.
        self.hooks = []
        self.clear_hooks_file()

    def poll_worker(self):
        assert self.worker
        rc = self.worker.poll()
        if rc is None:
            if self.worker.overdue():
                self.log.error("worker invocation %d overdue (>%ds), killing",
                               self.state.worker_invocation, self.worker.timeout_seconds)
                self.worker.kill()
                rc = self.worker.process.returncode
            else:
                return
        # Worker has exited.
        self.log.info("worker invocation %d exited rc=%d", self.state.worker_invocation, rc)
        self.last_worker_exit = {"rc": rc, "stderr_tail": self.worker.stderr_tail()}
        self.worker = None
        # Read and validate hooks.
        raw = safe_read_json(self.run_dir / "hooks.json")
        result = parse_hooks(raw, self.run_dir)
        if isinstance(result, str):
            self.log.warning("worker wrote malformed hooks.json: %s", result)
            # Treat as no-hook handoff; supervisor will see prior_exit and decide.
            self.last_worker_exit["malformed_hooks_error"] = result
            self.hooks = []
        else:
            self.hooks = result
        if not self.hooks:
            # No hook → dispatch supervisor for adjudication.
            envelope = self.build_envelope(
                wake_reason="worker_exit_no_hook",
                next_state="adjudicate",
            )
            self.dispatch_supervisor(envelope)

    def poll_hooks(self):
        fired = find_fired_hooks(self.hooks, self.run_dir, self.log)
        if not fired:
            return
        winner_hook, winner_triggers = fired[0]
        also = [{"id": h.id, "triggers": triggers} for h, triggers in fired[1:]]
        self.log.info("hook fired: %s triggers=%s (also_fired=%s)",
                      winner_hook.id, winner_triggers, [a["id"] for a in also])
        envelope = dict(winner_hook.wake_message)  # shallow copy
        envelope.setdefault("wake_reason", winner_hook.id)
        envelope["trigger"] = winner_triggers   # ["timer"], ["condition_script"], or both
        if also:
            envelope["also_fired"] = also
        # Fill standard fields.
        envelope["run_dir"] = str(self.run_dir)
        # Schedule the dispatch for next tick to keep loop step uniform.
        self.pending_worker_envelope = envelope

    # ----- supervisor lifecycle -----

    def dispatch_supervisor(self, envelope: dict):
        if self.last_supervisor_exit:
            envelope.setdefault("prior_exit", self.last_supervisor_exit)
            self.last_supervisor_exit = None
        self.state.supervisor_invocation += 1
        envelope["tick"] = self.state.tick
        envelope["wall_clock"] = utc_now_iso()
        self.clear_verdict_dir()
        # Remember the reason so apply_verdict can decide if "no verdict" is acceptable.
        self.current_supervisor_reason = envelope["wake_reason"]
        self.supervisor = spawn(
            "supervisor", self.supervisor_cmd, envelope,
            self.run_dir, self.state.supervisor_invocation,
            SUPERVISOR_TIMEOUT_SECONDS, self.log,
        )

    def poll_supervisor(self):
        assert self.supervisor
        rc = self.supervisor.poll()
        if rc is None:
            if self.supervisor.overdue():
                self.log.error("supervisor invocation %d overdue, killing",
                               self.state.supervisor_invocation)
                self.supervisor.kill()
                rc = self.supervisor.process.returncode
            else:
                return
        self.log.info("supervisor invocation %d exited rc=%d", self.state.supervisor_invocation, rc)
        self.last_supervisor_exit = {"rc": rc, "stderr_tail": self.supervisor.stderr_tail()}
        self.supervisor = None
        self.apply_verdict(rc)

    def apply_verdict(self, rc: int):
        verdict_dir = self.run_dir / "supervisor_verdict"
        wake_file = verdict_dir / "wake.json"
        term_file = verdict_dir / "terminate.json"
        wake = safe_read_json(wake_file)
        term = safe_read_json(term_file)
        # Noop is valid only when supervisor had no obligation to act
        # (i.e., dispatched as sanity_check). For other reasons, the
        # supervisor MUST resolve the situation.
        noop_allowed = self.current_supervisor_reason == "sanity_check"
        if rc != 0 or (wake is not None and term is not None):
            self.handle_malformed_verdict(rc, wake, term)
            return
        if wake is None and term is None:
            if noop_allowed:
                self.log.info("supervisor noop accepted (sanity_check)")
                self.state.malformed_verdicts = 0
                self.clear_verdict_dir()
                return
            self.handle_malformed_verdict(rc, wake, term)
            return
        if term is not None:
            reason = term.get("reason") if isinstance(term, dict) else None
            if not reason:
                self.handle_malformed_verdict(rc, wake, term)
                return
            self.terminate_reason = reason
            self.state.malformed_verdicts = 0
            self.clear_verdict_dir()
            return
        if not isinstance(wake, dict) or "wake_message" not in wake or not isinstance(wake["wake_message"], dict):
            self.handle_malformed_verdict(rc, wake, term)
            return
        env = wake["wake_message"]
        env.setdefault("wake_reason", "supervisor_resume")
        env["run_dir"] = str(self.run_dir)
        self.pending_worker_envelope = env
        self.state.malformed_verdicts = 0
        self.clear_verdict_dir()

    def handle_malformed_verdict(self, rc: int, wake, term):
        self.state.malformed_verdicts += 1
        self.log.warning("supervisor verdict malformed (count=%d): rc=%d wake_present=%s term_present=%s",
                         self.state.malformed_verdicts, rc, wake is not None, term is not None)
        self.clear_verdict_dir()
        if self.state.malformed_verdicts >= MAX_MALFORMED_VERDICTS:
            self.escalate_reason = f"supervisor produced malformed verdicts {self.state.malformed_verdicts} times"
            return
        envelope = self.build_envelope(
            wake_reason="supervisor_retry_malformed_output",
            next_state="adjudicate",
        )
        self.dispatch_supervisor(envelope)

    # ----- utilities -----

    def build_envelope(self, *, wake_reason: str, next_state: str, **extra) -> dict:
        env = {
            "wake_reason": wake_reason,
            "next_state": next_state,
            "run_dir": str(self.run_dir),
        }
        env.update(extra)
        return env

    def clear_hooks_file(self):
        p = self.run_dir / "hooks.json"
        if p.exists():
            p.unlink()

    def clear_verdict_dir(self):
        d = self.run_dir / "supervisor_verdict"
        d.mkdir(exist_ok=True)
        for f in d.iterdir():
            try:
                f.unlink()
            except OSError:
                pass


# --- CLI -----------------------------------------------------------------

def setup_logging(run_dir: Path) -> logging.Logger:
    log_path = run_dir / "logs" / "watchdog.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path)
    stream = logging.StreamHandler(sys.stderr)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(fmt)
    stream.setFormatter(fmt)
    log = logging.getLogger("mvp-agent-mini")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(stream)
    return log


def main():
    parser = argparse.ArgumentParser(description="mvp-agent-mini generic watchdog")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--worker-cmd", required=True,
                        help="Shell command to spawn the worker subprocess. Wake envelope passed via $MVP_AGENT_WAKE_ENVELOPE.")
    parser.add_argument("--supervisor-cmd", required=True,
                        help="Shell command to spawn the supervisor subprocess.")
    parser.add_argument("--initial-state", default="setup",
                        help="next_state value in the very first wake envelope.")
    parser.add_argument("--tick-seconds", type=int, default=TICK_SECONDS)
    parser.add_argument("--sanity-check-seconds", type=int, default=SANITY_CHECK_SECONDS)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    log = setup_logging(run_dir)
    log.info("mvp-agent-mini watchdog starting: run_dir=%s worker_cmd=%r supervisor_cmd=%r",
             run_dir, args.worker_cmd, args.supervisor_cmd)
    wd = Watchdog(
        run_dir=run_dir,
        worker_cmd=args.worker_cmd,
        supervisor_cmd=args.supervisor_cmd,
        initial_state=args.initial_state,
        tick_seconds=args.tick_seconds,
        sanity_check_seconds=args.sanity_check_seconds,
        log=log,
    )
    phase = wd.run()
    log.info("watchdog exit phase=%s", phase)
    sys.exit(0 if phase == PHASE_COMPLETED else 1)


if __name__ == "__main__":
    main()
