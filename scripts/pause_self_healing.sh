#!/usr/bin/env bash
# Halts the Layer-3 self-healing trigger (trig_01EzPjGTvjG6cK9BW5NDY3bz) at
# its next 22:00 UTC run by committing .self_healing_paused to master.
# The dashboard /self-healing-audit page will turn its banner red.
# Run scripts/resume_self_healing.sh to re-arm.
set -euo pipefail
touch .self_healing_paused && git add .self_healing_paused && git commit -m "chore: pause self-healing" && git push
