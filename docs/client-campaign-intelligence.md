# Client campaign intelligence

iHarvester has two web experiences:

- `https://<service-domain>/promote` is the public, aggregate-only media kit for prospective clients. It shows active placement inventory, subscriber memberships, and verified historical benchmarks without exposing channel identities. Creator clones receive isolated media-kit paths automatically.
- **Share client progress page** in a campaign creates a private bearer link. It shows that campaign's delivery, elapsed time, subscriber-membership inventory, live post views, timing benchmark, client request form, and links to currently live **public** placements. The public catalogue is ordered by known subscriber count and paginated; private placements never appear as links.

## What the figures mean

`Subscriber memberships` is the sum of the latest known subscriber count for every selected placement. One person may follow more than one channel, so it is not an audience total, unique-person reach figure, or outcome forecast.

`Verified views so far` on a private client page is an accumulating total from Telegram's actual post-view counters. The tracker refreshes each still-live placement and preserves its final sampled counter when a repost or cleanup removes it. It does not create a per-minute analytics collection, retain message content, or estimate views without a sample.

The public media kit deliberately does **not** show a raw average post-view figure. It presents accumulated verified post views only after a campaign has at least 50 placements observed for six or more hours. Fresh live counters remain visible to that campaign's client, but cannot become public performance evidence until they mature.

The time-of-day planner groups prior campaigns by the start hour selected in the campaign's owner timezone. It uses the measured post views per placement-hour and shows the number of campaign samples behind each number. A time without sufficient samples stays unscored rather than being assigned a made-up forecast.

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
