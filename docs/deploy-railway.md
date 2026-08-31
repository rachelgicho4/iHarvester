# Deploy on Railway

Create a project from this GitHub repository. Railway detects the Dockerfile; no deprecated new-service config-as-code is required. Add the required variables, make the service public, and set `RUN_MODE=webhook`, `PUBLIC_BASE_URL` to the generated Railway domain, and unique webhook secrets. Configure `/healthz` for health monitoring and use a restart policy.

Use MongoDB Atlas by default, or a Railway Mongo service if you explicitly want it. Keep one application replica.
