# Client campaign intelligence

iHarvester has two web experiences:

- `https://<service-domain>/promote` is the public media kit for prospective clients. It shows active placement inventory, subscriber memberships, and a paginated browse of active **public** channels ordered by their current subscriber count. Creator clones receive isolated media-kit paths automatically.
- **Share client progress page** in a campaign creates a private bearer link. It shows that campaign's delivery, elapsed time, subscriber-membership inventory, client request form, public placement catalogue, and live placement highlights. The public catalogue is ordered by known subscriber count and paginated; private placements never appear as links.

## What the figures mean

`Subscriber memberships` is the sum of the latest known subscriber count for every selected placement. One person may follow more than one channel, so it is not an audience total, unique-person reach figure, or outcome forecast.

The optional view sampler reads Telegram's actual post-view counters only for exact live campaign posts. It preserves a final sampled counter when a repost or cleanup removes a post, but these internal measurements are **not** presented as a network-wide average or a sales forecast.

The public media kit deliberately does **not** show post-view averages or forecasts. It instead lets prospects browse the public portion of the live network. Private channels contribute only to aggregate inventory totals.

For an active campaign, the client page shows up to three **live campaign highlights**. Where a public post has both a real sampled counter and at least six hours of life, the highlights are ordered by that verified post counter. Until then, the page honestly falls back to the largest currently public campaign placements. It never labels a fresh or sparsely sampled post as best-performing.

## Contact target

The public media kit never points prospects to the bot. By default it links to the first authorised owner using a Telegram `tg://user?id=...` link. On the primary deployment, set the optional environment variable below to use a public username instead:

```env
PUBLIC_CONTACT_USERNAME=iamskyking2
```

Creator clones intentionally do not inherit the primary username. Their media kits link to their own first authorised creator, so each creator receives an isolated contact flow without a separate deployment.

## Enable live view sampling on the recovery VPS

Telegram's Bot API delivery result does not provide a general read API for existing channel-post counters. Run the sampler on the private recovery VPS with the already-authorised **human admin** sessions; do not add Telethon or any human session to Koyeb.

The sampler reads only exact live post IDs from `campaign_channel_state`, uses Telegram's `messages.getMessagesViews` with `increment=False`, and does not inspect channel history or change any Telegram message.

From the checked-out repository on the recovery VPS:

```bash
cd ~/iharvester-recovery/repo
python -m pip install --no-cache-dir -q -e . -r requirements-mtproto-recovery.txt

export TELEGRAM_API_ID='your-api-id'
export TELEGRAM_API_HASH='your-api-hash'
export MONGODB_URI='your-mongodb-uri'
export MONGODB_DB_NAME='telegram_campaign_orchestrator'

python scripts/track_campaign_views_mtproto.py \
  --session ~/iharvester-recovery/session/account-1 \
  --session ~/iharvester-recovery/session/account-2 \
  --session ~/iharvester-recovery/session/account-3 \
  --limit 30 --dry-run
```

Confirm the dry-run lists only expected campaign targets. Then remove `--dry-run` and the small test limit:

```bash
python scripts/track_campaign_views_mtproto.py \
  --session ~/iharvester-recovery/session/account-1 \
  --session ~/iharvester-recovery/session/account-2 \
  --session ~/iharvester-recovery/session/account-3
```

Run it roughly every 15-30 minutes using a conservative cron job or systemd timer. The normal run samples only active, paused, and ending campaigns; use `--include-archived` only for a deliberate one-off history pass. It loads the authorised accounts' dialog directories once to obtain access hashes, then routes each exact post to the account that already lists that channel and rotates between accounts where more than one can read it. If one account receives a flood wait, the sampler skips it temporarily and attempts another already-authorised account; it never signs in again or sends a message.

Do not place `TELEGRAM_API_HASH`, Mongo credentials, or session files in Git, Koyeb variables, public logs, or client links. Keep the existing session files until the owner explicitly retires them.
