# mvp-agent

A self-contained agent harness for long-horizon experiments. The bundled
example runs the OpenBee stage1 demo (train Qwen3-VL-8B-stage0 → save
checkpoints → evaluate each on VStarBench → write a final report), but
the watchdog and the contract are generic — only the prose in
`worker/task-overview.md` is task-specific. No external code dependencies.

## Components

Three components communicate through files in a single run directory.

### 1. Watchdog (`watchdog/watchdog.py`)

A single-file Python program, stdlib only. Pure scheduler with zero
task knowledge. Each tick (default 10 s) it:

1. Polls subprocess status (worker / supervisor).
2. Applies any supervisor verdict that just landed.
3. Spawns a pending worker (if any).
4. Evaluates the worker's hook conditions and dispatches the first that fires.
5. Optionally dispatches a periodic sanity-check supervisor.

It enforces three schemas (full definitions in `FILESYSTEM_CONTRACT.md`):

- **Hook** — `<run_dir>/hooks.json`, an array of `{id, after_seconds,
  condition_script?, wake_message}`. The worker writes this on every
  sleep; the watchdog clears it on every dispatch.
- **Wake envelope** — JSON delivered to each spawned agent via the
  `MVP_AGENT_WAKE_ENVELOPE` env var. Carries `wake_reason`, `trigger`
  (`["timer"]` / `["condition_script"]`), `tick`, `prior_exit`, plus
  anything the worker put in the hook's `wake_message`.
- **Supervisor verdict** — `<run_dir>/supervisor_verdict/{wake.json|terminate.json}`.
  `wake.json` re-activates the worker with a custom envelope;
  `terminate.json` ends the run.

The watchdog also enforces tick-ordering guarantees: sanity-check fires
only when both the worker and the supervisor are idle, hook conditions
are not evaluated while the supervisor is running, supervisor verdicts
win over concurrent hook fires, and `terminate.json` stops the loop
immediately.

### 2. Worker (`worker/task-overview.md`)

The agent that does the actual work. One prose file — no `states/`
subdirectory, no `skills/` tree. It is invoked via `claude -p` (the
bundled `examples/claude_wrapper.sh` handles session capture on the
first invocation and `--resume <session_id>` thereafter, so the
conversation context persists across wakes).

On each wake the worker reads its envelope at
`<run_dir>/wake_envelopes/worker.json`, does some work
(sets up the env, submits the training job, evaluates a stable
checkpoint, etc.), and on sleep writes a fresh `hooks.json` describing
the conditions under which it wants to be woken next. If it exits
without writing any hooks, the watchdog hands off to the supervisor.

### 3. Supervisor (`supervisor/task-overview.md`)

The agent that decides termination. Also a single prose file, also
`claude -p`-driven. Woken in three situations:

- `worker_exit_no_hook` — the worker has nothing left to register
  (run finished cleanly OR worker gave up).
- `sanity_check` — periodic heartbeat (default 60 min) to catch
  silent stalls. Writes no verdict if everything looks fine.
- `supervisor_retry_malformed_output` — its previous verdict was
  unparseable; retry with `prior_exit.stderr_tail` for context.

It writes either `wake.json` (re-activate worker, optionally with a
`supervisor_note` hint) or `terminate.json` (`reason: "completed: ..."`
or `reason: "escalated: ..."`).

## Layout

```
watchdog/watchdog.py
FILESYSTEM_CONTRACT.md
worker/task-overview.md
supervisor/task-overview.md
examples/
├── claude_wrapper.sh             # claude -p bridge: session capture + --resume
├── stage1_demo.sbatch.template   # training sbatch template (used by worker)
└── watchdog.sbatch               # convenience: run watchdog as a Slurm job
README.md
```

## Run

The watchdog is a regular Python program. Run it in the foreground:

```bash
python3 watchdog/watchdog.py \
    --run-dir /abs/path/to/run \
    --worker-cmd     "bash $(pwd)/examples/claude_wrapper.sh $(pwd)/worker" \
    --supervisor-cmd "bash $(pwd)/examples/claude_wrapper.sh $(pwd)/supervisor"
```

It will exit when the supervisor writes `terminate.json`. The worker
and supervisor subprocesses are spawned on demand; their stdout/stderr
land under `<run_dir>/logs/`, the per-component session ids under
`<run_dir>/session_ids/`, and the worker's own work log at
`<run_dir>/workspace/work_log.md`.

For long-running experiments where you don't want the watchdog tied
to your shell, a Slurm wrapper is bundled:

```bash
sbatch --export=ALL,MVP_AGENT_RUN_DIR=/abs/path/to/run examples/watchdog.sbatch
```

If this repo lives somewhere other than the hardcoded default in
`examples/watchdog.sbatch`, also export `MVP_AGENT_MINI_ROOT`.
