# Deploy on Render

Connect the repository as a Docker Web Service or use `render.yaml`. Set all required secrets in Render's Environment view and set `RUN_MODE=webhook`, `PUBLIC_BASE_URL` to the service's HTTPS address, and unique webhook secrets. Use an always-on paid service in production: Render Free spins down when idle and has ephemeral local storage.

Use one web service / one process. MongoDB remains the only durable state and the scheduler runs in that process.
