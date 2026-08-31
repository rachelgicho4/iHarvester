# Deploy on Heroku

Create an app, connect the GitHub repository, and set Config Vars for every required `.env.example` value. The included `Procfile` starts the one web process. Use `RUN_MODE=webhook`, `PUBLIC_BASE_URL=https://<app>.herokuapp.com`, plus unique webhook path and header secrets.

Run one web dyno for V1. MongoDB's lease protects brief release overlap but does not make multiple permanent workers useful.
