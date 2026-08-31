# iHarvester

iHarvester is a Telegram-native campaign orchestrator for an owner-operated network of channels. It registers channels when the bot becomes an administrator, stores durable state in MongoDB, and runs rate-limited, restart-safe campaign cycles with real Telegram buttons.

It deliberately remains one Python service plus MongoDB: no Redis, Celery, dashboard, redirect tracker, user-account login, or separate worker deployment.

## The quickest production path: Koyeb

1. Create a MongoDB Atlas database and copy its connection URI.
2. Push this repository to GitHub, then create a **Web Service** in the Koyeb UI from the repository. Koyeb will detect the Dockerfile.
3. Under **Exposed ports**, select `8000` with protocol `HTTP`; Koyeb supplies that value as `PORT`. Choose an always-on paid instance with one minimum instance. Koyeb Free scales to zero after idle time, which is unsuitable for a campaign scheduler.
4. Add `BOT_TOKEN`, `OWNER_USER_IDS`, `MONGODB_URI`, `WEBHOOK_PATH_SECRET`, and `WEBHOOK_SECRET_TOKEN` as Koyeb Secrets. Set `RUN_MODE=webhook` and `MONGODB_DB_NAME=telegram_campaign_orchestrator`.
5. Koyeb supplies `KOYEB_PUBLIC_DOMAIN`; iHarvester derives its public webhook base URL from it. Deploy and check `/readyz`.
6. Open the bot in Telegram, press **Start**, and promote it to administrator in the source channels.

Koyeb's default TCP health check is sufficient; an HTTP `/healthz` check is optional if the port settings expose the customization control. The service sets its Telegram webhook on startup. Keep `WEB_CONCURRENCY=1` / one application worker; the MongoDB scheduler lease remains a safety net during deployment overlaps.

If you choose a Free Instance, Koyeb scales it to zero after one hour without **public** traffic. Its internal health probes do not prevent this. Configure an external monitor to request `https://<your-koyeb-domain>/` every 5–10 minutes, or use an always-on instance for reliable scheduling.

## Local or VPS polling mode

```text
cp .env.example .env
# fill BOT_TOKEN, OWNER_USER_IDS, MONGODB_URI
docker compose up -d
```

With `RUN_MODE=polling`, no domain or TLS certificate is required. The container exposes `/healthz` and `/readyz` at port 8000.

## Owner workflow

Everything is in the bot's private chat with an allowed owner ID:

1. Press **Create Campaign**, name the draft, and capture formatted text or a supported media post.
2. Choose **Text**, **Photo**, **Photo + caption**, **Video**, **Video + caption**, **Album**, or **Forward ready post**. Forwarded content retains its Telegram formatting and media IDs.
3. Add a CTA by entering its label and URL in separate prompts. Use **Add beside last** for a horizontal button or **Add new row** for a vertical one; labels are never rewritten or truncated by iHarvester.
4. Add destinations, source targets, mode, and schedule through their own guided controls. The Network screen shows active, unavailable, paused, and attention-required channel counts, and accepts a forwarded channel post for manual repair/registration.
5. Choose **Send campaign**, then pick a duration preset or enter a custom duration such as `45m`, `2h`, `3d`, or `1mo` (30 days). Repost choices automatically divide that campaign duration exactly; custom repost periods use the same units. iHarvester renders a real preview and presents the final launch confirmation with the exact planned targets and protected destination exclusions.
6. **Launch** freezes the active target snapshot and automatically excludes all known destination channel IDs.

For fallback channel registration, forward a post from a source channel to the bot and select **Register/Refresh**. `/backup` creates a compressed core export; `/restore` validates an attached export and asks for confirmation before upserting it.

## Safety behavior worth knowing

- Every cycle gets a new deterministic HMAC dispatch order. A crash resumes the same cycle order; registration order is never broadcast order.
- Mix + Rotate freezes count-balanced cohorts, rotates variants by cohort, and independently reshuffles physical delivery order each cycle.
- Reposts delete the known previous campaign message for that channel immediately before sending a replacement.
- End time/early end prevents future sends, cleans known live posts, then archives immutable results. An archive is repeated only by creating a new draft.
- A message-send timeout is recorded as `UNKNOWN_SEND_STATE`, not blindly retried, because Telegram can have accepted the send before a timeout reached the bot.
- Deletion cleanup uses a conservative 47-hour validation rule. A long campaign must repost within 47 hours if it promises cleanup.

## Configuration

See [.env.example](.env.example). Secrets stay in the environment and are never placed into MongoDB or backups. Default delivery rates are 20 sends/sec and 25 combined API mutations/sec, below the normal free-broadcast ceiling.

## Development

```text
uv sync --locked --all-groups
uv run ruff check .
uv run pytest
uv run uvicorn app.main:app --reload
```

CI runs linting, tests, and a Docker image build. See [docs/operations.md](docs/operations.md) for operational behavior and the deployment guides for platform-specific steps.

## Deployment guides

- [Koyeb](docs/deploy-koyeb.md) (primary)
- [Render](docs/deploy-render.md)
- [Heroku](docs/deploy-heroku.md)
- [Railway](docs/deploy-railway.md)
- [VPS / Docker](docs/deploy-vps.md)
- [Backup and restore](docs/backup-restore.md)
