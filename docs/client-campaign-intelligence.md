# Client campaign intelligence

iHarvester has two web experiences:

- `https://<service-domain>/promote` is the public, aggregate-only media kit for prospective clients. It shows active placement inventory, combined subscriber slots, and verified historical benchmarks without exposing channel identities.
- **Share client progress page** in a campaign creates a private bearer link. It shows that campaign's delivery, elapsed time, subscriber-slot audience, live post views, timing benchmark, client request form, and links to any currently live **public** placements. Private placements never appear as links.

## What the figures mean

`Combined subscriber audience` is the sum of the latest known subscriber count for every selected placement. One person may follow more than one channel, so it is not a unique-person reach figure.

`Views` comes only from Telegram's actual post-view counters. The tracker stores the latest counter on the existing delivery record and one compact total on the campaign. It does not create a per-minute analytics collection, retain message content, or estimate views without a sample.

The time-of-day planner groups prior campaigns by the start hour selected in the campaign's owner timezone. It uses the measured views per placement-hour and shows the number of campaign samples behind each number. A time without sufficient samples stays unscored rather than being assigned a made-up forecast.

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

Run it roughly every 10-15 minutes using a conservative cron job or systemd timer. It loads the authorised accounts' dialog directories once to obtain access hashes, then requests the counter for exact live message IDs. If one account receives a flood wait, the sampler skips it temporarily and attempts another already-authorised account; it never signs in again or sends a message.

Do not place `TELEGRAM_API_HASH`, Mongo credentials, or session files in Git, Koyeb variables, public logs, or client links. Keep the existing session files until the owner explicitly retires them.
