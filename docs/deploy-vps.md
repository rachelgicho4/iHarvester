# Deploy on a VPS

Install Docker and Docker Compose, create an external MongoDB Atlas database, then:

```text
cp .env.example .env
# fill the three required values and keep RUN_MODE=polling
docker compose up -d
```

The compose service has `restart: unless-stopped`. Polling does not require a reverse proxy. If you choose webhook mode later, add a public HTTPS domain/reverse proxy and configure `PUBLIC_BASE_URL` plus both webhook secrets.
