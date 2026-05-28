# mvp-agent-mini

A minimal, self-contained harness pair for the OpenBee stage1 demo. Two
files of agent prose plus the generic watchdog and supporting files —
no external code dependencies. Runs as a Slurm job, drives `claude -p`
sessions for the worker and supervisor.

## Layout

```
watchdog/watchdog.py                        # generic scheduler (Python stdlib only)
FILESYSTEM_CONTRACT.md                      # hook / wake envelope / verdict schemas + tick ordering
worker/task-overview.md                     # the worker (one prose file)
supervisor/task-overview.md                 # the supervisor (one prose file)
examples/
├── claude_wrapper.sh                       # bridge: claude -p, session capture, --resume
├── stage1_demo.sbatch.template             # training sbatch template
└── watchdog.sbatch                         # Slurm job wiring the watchdog at this harness
README.md
```

## Run

```bash
sbatch --export=ALL,MVP_AGENT_RUN_DIR=/abs/path/to/run examples/watchdog.sbatch
```

If this repo lives somewhere other than the hardcoded default in
`examples/watchdog.sbatch`, also export `MVP_AGENT_MINI_ROOT`.

## Browser view (optional)

[`fastharness-web`](https://github.com/nickhezy/FastHarness/tree/main/fastharness-web)
is a small Python tool that renders a harness's task-overview as a
Mermaid diagram and tails the run log. Install once
(`pip install --user fastharness-web` or from source), then:

```bash
fastharness-web /abs/path/to/mvp-agent-mini/worker            # :8765
fastharness-web /abs/path/to/mvp-agent-mini/supervisor --port 8766
```

It's a standalone external tool, not a code dependency of this repo.
The live-run tab won't auto-find activity (the watchdog owns the run
dir externally); use `<run_root>/workspace/work_log.md` and `tail -F
<run_root>/logs/watchdog.log` instead.
