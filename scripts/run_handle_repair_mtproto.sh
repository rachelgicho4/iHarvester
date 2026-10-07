#!/usr/bin/env bash
# Run the audited MTProto handle-repair utility without writing credentials to disk.

set -euo pipefail

session_name="${1:-}"
if [[ ! "$session_name" =~ ^[A-Za-z0-9_-]{1,64}$ || "${2:-}" != "--" ]]; then
  echo "Usage: bash scripts/run_handle_repair_mtproto.sh <session-name> -- [repair options]" >&2
  echo "Example: bash scripts/run_handle_repair_mtproto.sh account-1 -- --scan search --max-candidates 10" >&2
  exit 2
fi
shift 2

read -r -p "Telegram API ID: " TELEGRAM_API_ID
read -r -s -p "Telegram API hash: " TELEGRAM_API_HASH
echo
if [[ ! "$TELEGRAM_API_ID" =~ ^[0-9]+$ ]]; then
  echo "Telegram API ID must contain digits only." >&2
  exit 2
fi
if [[ ! "$TELEGRAM_API_HASH" =~ ^[0-9A-Fa-f]{32}$ ]]; then
  echo "Telegram API hash must be a 32-character hexadecimal value." >&2
  exit 2
fi
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
session_dir="${IHARVESTER_RECOVERY_SESSION_DIR:-$HOME/iharvester-recovery/session}"
audit_dir="${IHARVESTER_HANDLE_REPAIR_AUDIT_DIR:-$HOME/iharvester-recovery/handle-repair-audit}"
install -d -m 700 "$session_dir" "$audit_dir"

export TELEGRAM_API_ID TELEGRAM_API_HASH
trap 'unset TELEGRAM_API_ID TELEGRAM_API_HASH' EXIT

docker run --rm -it \
  --mount "type=bind,src=$repo_dir,dst=/workspace/repo,readonly" \
  --mount "type=bind,src=$session_dir,dst=/recovery/session" \
  --mount "type=bind,src=$audit_dir,dst=/recovery/audit" \
  -w /workspace/repo \
  -e TELEGRAM_API_ID \
  -e TELEGRAM_API_HASH \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  python:3.12-slim \
  sh -ec 'session="$1"; audit="$2"; shift 2; python -m pip install --no-cache-dir -q -e . -r requirements-mtproto-recovery.txt && exec python scripts/repair_channel_handle_mtproto.py --session "$session" --audit-dir "$audit" "$@"' \
  sh "/recovery/session/$session_name" /recovery/audit "$@"
