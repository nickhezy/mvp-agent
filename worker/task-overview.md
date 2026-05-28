# OpenBee Stage1 Demo Training and VStar Evaluation — Mini Worker

Train OpenBee Stage1 for **1000 steps with a checkpoint every 100 steps** (10
ckpts saved), evaluate **the subset at multiples of 250 steps** (4 evals: at
iter_250, iter_500, iter_750, iter_1000) on `VStarBench` using VLMEvalKit, and
produce a final report. The intermediate saved ckpts (iter_100, _200, _300,
_400, _600, _700, _800, _900) exist on disk but are NOT evaluated.

This is the worker half of a tiny two-harness agent system driven by the bundled
generic watchdog. The supervisor (`../supervisor/task-overview.md`) only
adjudicates if goal is not met — you handle all problems yourself.

## Resources

- Dataset: [`mvp-lab/mvp-engine-openbee-stage1-demo-5k`](https://huggingface.co/datasets/mvp-lab/mvp-engine-openbee-stage1-demo-5k)
- Stage0 model: [`mvp-lab/Qwen3-VL-8B-Base-woDS-stage0`](https://huggingface.co/mvp-lab/Qwen3-VL-8B-Base-woDS-stage0)
- Training config: `recipes/basic_vlm/configs/stage1.yaml`
- Bundled sbatch template: `../examples/stage1_demo.sbatch.template`
  (uses `__PLACEHOLDER__` markers — fill them in, write to
  `<run_root>/training/run.sbatch`, submit that)

## How you're driven

A generic watchdog (`../watchdog/watchdog.py`) spawns this session. On every
wake, read your wake envelope at `<run_root>/wake_envelopes/worker.json`. On
every sleep, write `<run_root>/hooks.json` describing when you want to be
woken next, then exit. **The watchdog deletes `hooks.json` on every
dispatch, so you must write a fresh one each wake — even if your intent is
"keep the same hooks". Not re-writing it sends you straight to the
supervisor's no-hook handoff.**

**Read `../FILESYSTEM_CONTRACT.md` once** for the formal schemas of
the wake envelope and `hooks.json`. The mechanics:

- A hook is `{id, after_seconds, condition_script?, wake_message}`. It fires when
  the timer expires or the condition script (a bash file you write) prints `true`.
- `wake_message` becomes the wake envelope on fire — you choose `wake_reason` and
  whatever fields you want the next wake to see.
- Whether and when to register a hook is your call. Use one when you're genuinely
  waiting (training progressing, a new ckpt to appear) to help saving tokens.

## 1. Set up the mvp-engine environment

From the mvp-engine repo root:

```bash
uv venv --python=3.12
source .venv/bin/activate
uv sync --inexact
# the recipe needs flash_attn; prebuilt wheel works
pip install --no-build-isolation \
    "flash-attn @ https://github.com/Dao-AILab/flash-attention/releases/download/v2.7.4.post1/flash_attn-2.7.4.post1+cu12torch2.5cxx11abiFALSE-cp312-cp312-linux_x86_64.whl"
```

Install `https://github.com/mvp-ai-lab/mvp-dataset` if not already pulled in.

## 2. Download the demo dataset and stage0 model

Into the paths `stage1.yaml` reads by default:

```bash
hf download mvp-lab/mvp-engine-openbee-stage1-demo-5k \
  --repo-type dataset --local-dir ./data/Open-Bee-Lance/stage1
hf download mvp-lab/Qwen3-VL-8B-Base-woDS-stage0 \
  --local-dir ./pretrained/Qwen3-VL-8B-Base-woDS-stage0
```

Skip downloads if the target already has the expected files at non-zero size
(idempotent re-entry after a transient failure).

## 3. Submit the training sbatch (1000 steps, checkpoint every 100)

Fill the template at `../examples/stage1_demo.sbatch.template`:
`__TOTAL_TRAINING_STEPS__=1000`, `__EVAL_CADENCE_STEPS__=100` (this placeholder
name is a historical misnomer — it maps to the recipe's `checkpoint.interval`,
which is the *save* cadence; the eval cadence is independent and is described
above). The template's hardcoded `checkpoint.keep_n=__TOTAL_TRAINING_STEPS__`
ensures all 10 saved ckpts survive. Plus the other placeholders (paths, env
vars). Write to `<run_root>/training/run.sbatch` and submit via `sbatch --parsable`.

**Important: only iter_250, iter_500, iter_750, iter_1000 get evaluated.** Your
ckpt-discovery hook condition_script should filter for `step % 250 == 0` (don't
fire on intermediate saved ckpts) — otherwise you'll waste GPU time on evals
you don't need.

**Before declaring training "started", verify it is actually making progress.**
Submitting an sbatch is not the same as training running. Confirm:

- `sacct -j $JOBID` is in `RUNNING` (not stuck `PENDING` for an unexpectedly long
  time), and
- the recipe log shows actual forward progress — at least one `Step N` line has
  appeared.

The first step on this recipe takes ~14 min (torch.compile + inductor warmup) so
give it a real warmup budget (30–45 min before suspecting a true hang). If sacct
says RUNNING for much longer than that with no `Step N`, training is hung — fix
it inline (`scancel` + re-submit with `model.compile=false` is a known recovery
for compile inductor hangs).

Record the jobid + submission timestamp + your chosen warmup budget at
`<run_root>/workspace/training_kickoff.md`.

## 4. Evaluate each checkpoint as it appears

The recipe exports HF-format ckpts under
`outputs/basic_vlm-qwen3vl_8b-alignment*/checkpoints/iter_<step>/hf_model/`.
After a ckpt directory is stable (no file modified within the last ~60 s), you
can evaluate it.

For each ckpt:

1. Merge it: copy the pretrained dir's non-weight files
   (`config.json`, tokenizer, processor configs, chat template, etc.) into the
   ckpt's `hf_model/` so it becomes a complete HF model. Skip `*.safetensors`
   and `*.safetensors.index.json` (those came from the recipe).
2. Run VLMEvalKit on `VStarBench` per its official configuration. Use multi-GPU
   (`torchrun --nproc-per-node=N run.py ...`) if available — the model is 8B
   params; single-GPU HF transformers inference can take many hours.

**Before recording the metric, verify the eval is actually exercising the model.**
A working eval pipeline can still produce a meaningless number if:

- the judge silently fell back (no API key, missing dependency),
- the model's output format doesn't match what the scorer expects (early ckpts
  often emit captions rather than MCQ letters),
- predictions are running to the token cap with repetitive garbage.

Spot-check a few predictions vs gold. A real low score on an early ckpt is fine;
a number produced by a silently broken pipeline is not. Fix inline (alternative
judge, different `max_new_tokens`, etc.) before recording.

Also peek **during** inference (as soon as a handful of predictions exist on
disk) — don't wait for a multi-hour run to finish before noticing it's broken.

Write per-checkpoint deliverables atomically (`tmp + rename`):

- `<run_root>/workspace/eval_results/ckpt_<step>.json`:
  ```json
  {"schema_version": 1, "ckpt_step": <int>, "ckpt_path": "<merged dir>",
   "benchmark": "VStarBench", "metrics": {"accuracy": <float>},
   "vlmeval_command": "<exact cmd>", "evaluated_at": "<UTC iso>"}
  ```
- `<run_root>/workspace/cycles/cycle_<step>.md`: human-readable narrative —
  sections `## Trigger`, `## Action`, `## Notes` (observations, anomalies,
  what you spot-checked).

## 5. Maintain a work log

Append one entry to `<run_root>/workspace/work_log.md` on every wake describing:
- when you woke and why (wake_reason from the envelope),
- what you did,
- what you decided to do next.

This is your memory across wakes for the operator (and for your own future
self) to read. Keep it terse — bullets, not essays.

## 6. Finalize

When all 4 evaluated checkpoints (iter_250, _500, _750, _1000) have results
AND training is `COMPLETED`, write `<run_root>/workspace/final_report.md`
(in the same wake, no need for a hook):

- `## Job summary` — config, total steps, eval cadence, count evaluated,
  start/finish timestamps.
- `## Per-cycle results` — table of step, headline metric, short note column
  for any anomalies from the cycle file.
- `## Artifacts` — bulleted absolute paths to the final ckpt, sbatch logs,
  `cycles/` dir, `eval_results/` dir, the work log.
- `## Conclusion` — one paragraph on the metric trajectory in plain language.
  Don't invent verdicts like "converged"; report what the numbers show.

Then exit with `<run_root>/hooks.json = []` (empty). The supervisor sees
`final_report.md` and terminates the run.

## Handling problems

Fix any problem inline. Do not escalate to the user. Try alternatives, retry
transient errors, look at the relevant file or log.

If you genuinely cannot fix something after exhausting your options, exit with
empty `hooks.json` (without writing `final_report.md`). The supervisor will
either wake you to try again with hints, or terminate the run if it agrees
the situation is unrecoverable.
