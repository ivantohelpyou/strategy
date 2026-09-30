#!/usr/bin/env bash
# The admin surface — `launch strategy`. Read-only; binds to loopback.
# The store is whatever STRATEGY_DB_URL resolves to (see `strategy doctor`).
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --extra web strategy web --host "${STRATEGY_WEB_HOST:-127.0.0.1}" \
                                     --port "${STRATEGY_WEB_PORT:-8021}" "$@"
