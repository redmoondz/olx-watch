#!/usr/bin/env bash
# Hourly OLX poll. Prints new ads to stdout, appends everything to logs/poll.log.
set -uo pipefail
cd /home/redmoon/projects/olx-watch || exit 1
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
extra=()
[ -f cookies.txt ] && extra+=(--cookies-file cookies.txt)
out="$(.venv/bin/python olx_watch.py --once --exclude-promoted --only-wooting-60he "${extra[@]}" 2>&1)"
code=$?
printf '%s [exit=%d]\n%s\n' "$ts" "$code" "$out" >> logs/poll.log
# keep log under ~2MB
if [ "$(stat -c%s logs/poll.log)" -gt 2000000 ]; then tail -c 1000000 logs/poll.log > logs/poll.log.tmp && mv logs/poll.log.tmp logs/poll.log; fi
# notify only when there is something new or a failure
if [ "$code" -ne 0 ] || printf '%s' "$out" | grep -q '^NEW:'; then
  # Deliver straight to Telegram via the channel API: no LLM call, no OpenRouter.
  .venv/bin/python - "$out" <<'PYEOF'
import json, shutil, subprocess, sys
text = sys.argv[1].strip()
if not text:
    sys.exit(0)
if len(text) > 3800:
    text = text[:3800] + "\n… (truncated)"
cli = shutil.which("openclaw") or "/home/redmoon/.npm-global/bin/openclaw"
res = subprocess.run(
    [cli, "message", "send", "--channel", "telegram",
     "--target", "536212014", "--message", text, "--json"],
    capture_output=True, text=True, timeout=120,
)
if res.returncode != 0:
    sys.stderr.write(res.stderr[-500:])
print(res.stdout.strip()[:200])
PYEOF
fi
exit 0
