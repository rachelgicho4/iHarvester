# Repairing an obsolete Telegram handle in existing channel posts

Use this one-off operator tool when a Telegram handle has moved and old posts
in the channel network must point at the replacement. It is intentionally
separate from the hosted bot: it uses already-authorised **human** Telegram
admin sessions and never sends phone numbers, login codes, session files, or
API credentials to Koyeb or to the repository.

The current target is the exact replacement:

```text
@i_BOXTV / t.me/i_BOXTV  ->  @i_BOX_TV / t.me/i_BOX_TV
```

## What it changes

- Plain text and media captions containing the old `@i_BOXTV`, `t.me/i_BOXTV`,
  `https://t.me/i_BOXTV`, or `tg://resolve?domain=i_BOXTV` references.
- Existing text-link URLs and URL buttons pointing to the old handle.
- It preserves the saved text entities (formatting, links, spoilers, emoji)
  and the complete inline keyboard while replacing only matching URL values.
- It writes an append-only JSONL audit for every candidate, skip, successful
  edit, and Telegram error. The audit records metadata and counts, not post
  body text.

It never changes a similar name, a different handle, or any message outside
the iHarvester channel registry. Messages forwarded from elsewhere are listed
but intentionally skipped. A handle burned into an image/video thumbnail or
watermark is media pixels, not editable Telegram text; it must be replaced by
posting a corrected media asset separately.

## Safe run order

Run the tool once per existing human admin session. Its session loads the
account's channel directory, skips channels it cannot resolve or lacks the
**Edit messages** privilege for, and leaves them for the next account.

1. QR-authorise the three accounts on the temporary VPS using the existing
   `scripts/authorize_owner_qr.sh account-1` helper. Sessions stay under the
   VPS operator's local `iharvester-recovery/session` directory.
2. Run a 10-channel, read-only pilot with `--scan search`.
3. Inspect the JSONL audit. The `would_edit` records must only identify old
   `i_BOXTV` references.
4. Apply that 10-channel pilot and verify the posts in Telegram.
5. Run a full `search` pass for all three accounts.
6. If an audit still shows old links inside button-only messages, run a full
   history pass for those accounts. This is deliberately slower because it
   reads every historical message; Telegram's pacing is respected.

The convenience launcher prompts invisibly for the private Telegram API hash
and Mongo connection value, passes them only to its short-lived container, and
unsets them when it exits. It does not create an `.env` file. Start with:

```bash
cd /root/iharvester-handle-repair/repo

# Run this once per account and scan the terminal QR in Telegram:
# Settings > Devices > Link Desktop Device.
bash scripts/authorize_owner_qr.sh account-1

# Read-only pilot. It creates a JSONL audit on the VPS outside this repo.
bash scripts/run_handle_repair_mtproto.sh account-1 -- --scan search --limit-channels 10
```

The matching apply pilot adds the deliberate confirmation pair:

```bash
bash scripts/run_handle_repair_mtproto.sh account-1 -- \
  --scan search --limit-channels 10 --apply --confirm-new-handle i_BOX_TV
```

The launcher is preferable to constructing a Docker command by hand. The
equivalent low-level command below is retained for advanced operators who
already use exported values. Do not paste those values in command history or
source them from a tracked file.

```bash
cd /root/iharvester-handle-repair/repo

# Read-only pilot. The audit path is on the VPS disk, outside the repo.
docker run --rm -it \
  --mount type=bind,src="$PWD",dst=/workspace/repo,readonly \
  --mount type=bind,src="$HOME/iharvester-recovery/session",dst=/recovery/session \
  --mount type=bind,src="$HOME/iharvester-recovery/handle-repair-audit",dst=/recovery/audit \
  -w /workspace/repo \
  -e MONGODB_URI -e MONGODB_DB_NAME -e TELEGRAM_API_ID -e TELEGRAM_API_HASH \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  python:3.12-slim sh -ec \
  'python -m pip install --no-cache-dir -q -e . -r requirements-mtproto-recovery.txt && \
   python scripts/repair_channel_handle_mtproto.py \
     --session /recovery/session/account-1 \
     --audit-dir /recovery/audit \
     --scan search --limit-channels 10'
```

After all three `search` passes finish, use this only if the audit identifies
messages that contain the old URL solely in inline buttons (no searchable
caption/text):

```bash
bash scripts/run_handle_repair_mtproto.sh account-1 -- \
  --scan full --history-wait 1 --apply --confirm-new-handle i_BOX_TV
```

`--scan full` has no history cap by default. For a test on very old, busy
channels, add `--history-limit 500` first; remove it only after reviewing the
audit. The utility throttles edits to 0.8 per second and honours Telegram flood
waits. Do not run the same account/session twice at once.

## Recovery and reporting

Each audit file is named with its session label and a UTC timestamp. Useful
events are:

- `would_edit` — dry-run candidate.
- `edited` / `already_repaired` — successful outcome.
- `channel_unavailable` or `channel_skipped_no_edit_right` — leave it for the
  account that administers that channel.
- `candidate_skipped_forwarded` — Telegram cannot safely edit that post in
  place.
- `edit_failed` — inspect the Telegram error before deciding whether an
  individual manual repair is appropriate.

Keep the audit files until every account has completed its pass. They provide
a deterministic list of exceptions without retaining Telegram session secrets.
