# Deploy on Koyeb (primary)

Create a Koyeb Web Service from this GitHub repository. Leave Dockerfile detection enabled, set the health path to `/healthz`, and choose one always-on non-Free instance. Koyeb Free scale-to-zero can miss scheduled campaigns.

Set these Koyeb Secrets: `BOT_TOKEN`, `OWNER_USER_IDS`, `MONGODB_URI`, `WEBHOOK_PATH_SECRET`, and `WEBHOOK_SECRET_TOKEN`. Set `RUN_MODE=webhook` and `MONGODB_DB_NAME=telegram_campaign_orchestrator`. Do not set `PUBLIC_BASE_URL` unless using a custom domain: the app derives it from Koyeb's `KOYEB_PUBLIC_DOMAIN`.

Deploy, wait for `/readyz` to return 200, then use Telegram. Koyeb's UI is sufficient; no local shell deployment commands are required.
