# Deploy on Koyeb (primary)

Create a Koyeb Web Service from this GitHub repository and choose the **Dockerfile** builder. In **Exposed ports**, use `8000` with protocol `HTTP` (Koyeb then supplies `PORT=8000`). The image can also safely bind Koyeb's legacy/default `PORT=80` setting. Leave Koyeb's default TCP health check in place. If your port settings show **Customize health check**, you may optionally set an HTTP `GET /healthz` check, but it is not required.

Set these Koyeb Secrets: `BOT_TOKEN`, `OWNER_USER_IDS`, `MONGODB_URI`, `WEBHOOK_PATH_SECRET`, and `WEBHOOK_SECRET_TOKEN`. Set `RUN_MODE=webhook` and `MONGODB_DB_NAME=telegram_campaign_orchestrator`. Do not set `PUBLIC_BASE_URL` unless using a custom domain: the app derives it from Koyeb's `KOYEB_PUBLIC_DOMAIN`.

The provided Dockerfile installs a hash-locked `requirements.txt` and a current CA bundle. If you deliberately deploy with Koyeb's **Buildpack** rather than the Dockerfile, the same requirements file is available; set the run command to `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log`. Disabling the generic access log prevents the secret webhook path from being written to deployment logs.

Deploy, wait for `/readyz` to return 200, then use Telegram. Koyeb's UI is sufficient; no local shell deployment commands are required.

## Free Instance note

Koyeb Free always enters scale-to-zero after one hour without public Internet traffic. The service's own internal health checks do not count as public traffic. For testing/hobby use, configure cron-job.org (or another external monitor) to request `https://<your-koyeb-domain>/` every 5–10 minutes. The root route deliberately returns HTTP 200 for this purpose. For campaign scheduling that must run without an external keepalive dependency, choose an always-on instance.
