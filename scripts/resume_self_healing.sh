#!/usr/bin/env bash
# Re-arms the Layer-3 self-healing trigger by deleting .self_healing_paused
# from master. /self-healing-audit banner returns to green.
set -euo pipefail
git rm .self_healing_paused 2>/dev/null && git commit -m "chore: resume self-healing" && git push
