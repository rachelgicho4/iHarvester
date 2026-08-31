# Deploy on Koyeb (primary)

Create a Koyeb Web Service from this GitHub repository and choose the **Dockerfile** builder. Expose the application as an HTTP Web Service; the container listens on Koyeb's supplied `$PORT`. Leave Koyeb's default TCP health check in place. If your port settings show **Customize health check**, you may optionally set an HTTP `GET /healthz` check, but it is not required.

Set these Koyeb Secrets: `BOT_TOKEN`, `OWNER_USER_IDS`, `MONGODB_URI`, `WEBHOOK_PATH_SECRET`, and `WEBHOOK_SECRET_TOKEN`. Set `RUN_MODE=webhook` and `MONGODB_DB_NAME=telegram_campaign_orchestrator`. Do not set `PUBLIC_BASE_URL` unless using a custom domain: the app derives it from Koyeb's `KOYEB_PUBLIC_DOMAIN`.

Deploy, wait for `/readyz` to return 200, then use Telegram. Koyeb's UI is sufficient; no local shell deployment commands are required.

## Free Instance note

Koyeb Free always enters scale-to-zero after one hour without public Internet traffic. The service's own internal health checks do not count as public traffic. For testing/hobby use, configure cron-job.org (or another external monitor) to request `https://<your-koyeb-domain>/` every 5–10 minutes. The root route deliberately returns HTTP 200 for this purpose. For campaign scheduling that must run without an external keepalive dependency, choose an always-on instance.
