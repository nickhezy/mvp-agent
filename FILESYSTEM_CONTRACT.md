# mvp-agent-mini — Filesystem Contract

The watchdog has zero task-specific knowledge. All semantics live in
three small schemas: hooks, wake envelope, and supervisor verdict.
This file is the source of truth; every component implements against it.

## Run directory layout

```
<run_dir>/
├── workspace/                # worker's scratch (worker manages — watchdog does not read)
├── hooks.json                # array of hook objects; written atomically by worker on sleep
├── supervisor_verdict/       # supervisor writes ONE of these per invocation
│   ├── wake.json             # presence ⇒ wake worker with this envelope
│   └── terminate.json        # presence ⇒ stop the run
├── logs/
│   ├── watchdog.log
│   ├── worker.<n>.{out,err}
│   └── supervisor.<n>.{out,err}
└── state.json                # watchdog's own state (ticks, retry counters)
```

## Schema 1 — Hook (`<run_dir>/hooks.json`)

Array of zero or more hook objects. Empty array OR missing file means
"no hook, hand off to supervisor for finish-check". Written atomically
(`hooks.tmp` then `mv`).

```json
[
  {
    "id": "warmup_check",
    "after_seconds": 600,
    "wake_message": { ... wake envelope ... }
  },
  {
    "id": "ckpt_appeared",
    "after_seconds": 7200,
    "condition_script": "workspace/scripts/check_ckpts.sh",
    "script_timeout_seconds": 30,
    "wake_message": { ... wake envelope ... }
  }
]
```

Fields:

- `id` (string, required): unique within this hook set; used in logs and
  the wake envelope's `wake_reason`.
- `after_seconds` (int, required): fallback timer. Hook fires no later
  than this many seconds after the watchdog reads the hooks file.
- `condition_script` (string OR list of strings, optional): script to
  run via `bash`, plus optional args. Path is resolved relative to
  `<run_dir>`. Three equivalent forms:
  - `"scripts/check_ckpts.sh"` — just a path, no args.
  - `"scripts/check_ckpt.sh 50"` — string is shell-split (`shlex.split`)
    so spaces work; trailing tokens become args to the script.
  - `["scripts/check_ckpt.sh", "50"]` — already a list of argv.
  Hook also fires if this script exits 0 and prints `true` (any case,
  whitespace-trimmed). Anything else = condition false, keep waiting.
- `script_timeout_seconds` (int, optional, default 30): kill the
  condition script if it runs longer than this. A killed/timed-out
  script counts as "condition false".
- `wake_message` (object, required): the wake envelope (Schema 2) that
  the worker will receive when this hook fires.

Multiple hooks fire on the same tick → first by array order wins; the
others go into `also_fired` on the wake envelope so the worker can
react if it wants.

## Schema 2 — Wake envelope

Delivered to the agent on every wake via the `MVP_AGENT_WAKE_ENVELOPE`
environment variable (JSON-serialized). The agent reads it on startup
to know why it was woken.

```json
{
  "wake_reason": "ckpt_appeared",
  "trigger": ["condition_script"],
  "run_dir": "/abs/path/to/run/dir",
  "tick": 142,
  "wall_clock": "2026-05-27T17:42:50Z",
  "prior_exit": {
    "rc": 0,
    "stderr_tail": "..."
  },
  "supervisor_note": "...",
  "also_fired": [{"id": "max_wait", "triggers": ["timer"]}]
}
```

Fields:

- `wake_reason` (string, required): one of the hook `id`s; or
  `"initial"` (first wake), `"sanity_check"` (supervisor periodic),
  `"worker_exit_no_hook"` (supervisor adjudicating worker no-hook
  exit), or `"supervisor_resume"` (worker wake after supervisor
  handed back).
- `trigger` (array of strings, required when waking from a hook):
  why this hook fired. Each element is `"timer"` (deadline
  `after_seconds` elapsed) or `"condition_script"` (the script
  returned truthy). Both may appear when they happen on the same
  tick. Absent for `initial` / `sanity_check` / supervisor-induced
  wakes.
- `run_dir` (string, required): absolute path to the run root.
- `tick` (int, required): watchdog tick counter.
- `wall_clock` (string, required): ISO 8601 UTC, watchdog's wall
  clock at dispatch.
- `prior_exit` (object, optional): present if the prior session
  exited non-zero (or for context after the first invocation).
  `rc` is the exit code; `stderr_tail` is the last ~2KB of stderr.
- `supervisor_note` (string, optional): present when the supervisor
  produced this envelope. Free-form message for the worker.
- `also_fired` (array, optional): other hooks whose condition was
  also true on this tick. Each entry: `{"id": "...", "triggers": [...]}`.

**Free-form fields are allowed.** Anything the agent put into the
hook's `wake_message` (e.g., a `jobid`, a self-note, a state hint)
is preserved as-is in the envelope. There is no agent-defined
mandatory field — the agent is one prose program, not a
state-machine that needs a `next_state` selector.

## Schema 3 — Supervisor verdict

After each supervisor invocation the watchdog inspects
`<run_dir>/supervisor_verdict/`. Zero or one of two files may exist:

**No file** ⇒ noop. The supervisor decided no action is needed — the
worker stays asleep with its existing hooks, the run continues. This
is the expected outcome of a healthy sanity-check.

**`wake.json`** — supervisor wants the worker re-activated:

```json
{
  "wake_message": { ... wake envelope ... }
}
```

The watchdog will deliver `wake_message` to the worker on the next
dispatch. `wake_reason` should typically be `"supervisor_resume"`.

**`terminate.json`** — supervisor decided the run is done:

```json
{
  "reason": "All checkpoints evaluated and final report written."
}
```

The watchdog will stop the loop and exit `phase=completed`.

**Malformed**: both files present, unparseable JSON, missing required
field, OR no verdict file written but supervisor was dispatched for a
reason that requires action (e.g., `worker_exit_no_hook` — leaving the
worker stalled with no hooks and no resolution). Watchdog deletes the
verdict dir, increments a malformed counter, and dispatches the
supervisor again with `wake_reason: "supervisor_retry_malformed_output"`
and the prior attempt's stderr in `prior_exit`. After 10 consecutive
malformed attempts the watchdog writes an alert and exits
`phase=escalate`.

After applying (or discarding) a verdict the watchdog clears the
`supervisor_verdict/` directory.

## Watchdog scheduling

Each tick (default 10 s) the watchdog does, in order: (a) poll subprocess
exits, (b) apply any landed supervisor verdict, (c) spawn the pending
worker if any, (d) evaluate hook conditions, (e) maybe dispatch a
`sanity_check`. Consequences worth knowing:

- **`sanity_check` is deferred while worker OR supervisor is busy.** The
  interval timer doesn't reset on a skipped tick; dispatch fires on the
  first tick both are idle.
- **Hook conditions are not evaluated while the supervisor is running.** A
  `condition_script` whose result flips to `true` during a supervisor
  wake doesn't fire until that supervisor exits.
- **A supervisor verdict wins over any concurrent hook fire.** If the
  supervisor writes `wake.json` and a hook would also be eligible on the
  next tick, the supervisor-issued envelope dispatches first; the
  worker's prior `hooks.json` is discarded (the watchdog clears it on
  every worker dispatch).
- **`terminate.json` or escalation stops the loop immediately.** Any
  pending hook or worker dispatch on the same tick is discarded.

## Atomicity

All writes (hooks, verdict, watchdog state) use the `tmp + rename`
pattern. Readers that find a missing file simply retry next tick;
readers that find a partial parse should treat it as missing.

## What the watchdog does NOT do

- Inspect or interpret the worker's `workspace/` contents.
- Track checkpoints, training jobs, or any task-specific concept.
- Decide what `next_state` means or validate it.
- Re-run scripts the worker didn't register as a hook condition.

All of those are worker/supervisor responsibilities.
