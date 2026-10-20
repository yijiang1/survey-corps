#!/bin/sh
# Codex `notify` wrapper: tells Survey Corps a turn finished, then runs the notify program that was there before.
# ~/.codex/config.toml:  notify = ["/path/to/hooks/codex-notify.sh", <previous notify args, if any>...]
# Codex appends its JSON payload as the last argument.
for last; do :; done
curl -s -m 2 --data-binary "$last" "http://127.0.0.1:8765/api/agent?tool=codex" >/dev/null 2>&1
[ $# -gt 1 ] && exec "$@"
exit 0
