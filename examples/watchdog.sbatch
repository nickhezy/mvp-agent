#!/bin/bash
# mvp-agent-mini watchdog as a Slurm job.
#
# Submit:
#   sbatch --export=ALL,MVP_AGENT_RUN_DIR=/abs/path/to/run watchdog.sbatch
#
# Override the location of this repo by exporting MVP_AGENT_MINI_ROOT before
# submitting (the default below assumes the standard checkout location).

#SBATCH --job-name=mvp-agent-mini-watchdog
#SBATCH --partition=cpu
#SBATCH --account=proj_agent
#SBATCH --qos=high
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=3-00:00:00
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err
#SBATCH --requeue

set -euo pipefail

RUN_DIR="${MVP_AGENT_RUN_DIR:?must set MVP_AGENT_RUN_DIR}"
MINI_ROOT="${MVP_AGENT_MINI_ROOT:-/home/z84415624/mvp-agent/mvp-engine/mvp-agent-mini}"

exec python3 "${MINI_ROOT}/watchdog/watchdog.py" \
    --run-dir "${RUN_DIR}" \
    --worker-cmd     "bash ${MINI_ROOT}/examples/claude_wrapper.sh ${MINI_ROOT}/worker" \
    --supervisor-cmd "bash ${MINI_ROOT}/examples/claude_wrapper.sh ${MINI_ROOT}/supervisor" \
    --initial-state begin
