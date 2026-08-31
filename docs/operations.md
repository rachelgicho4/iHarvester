# Operations

`/healthz` confirms that the process is alive. `/readyz` additionally verifies MongoDB and startup initialization; it does not require an active campaign.

The scheduler uses one short MongoDB lease. A deployment overlap may run two processes briefly, but only the current lease holder creates campaign cycles or begins ending transitions. Delivery claims also have MongoDB leases; a crash simply makes expired work claimable again.

Delivery statuses distinguish permanent failures, retry waits, and `UNKNOWN_SEND_STATE`. The last is deliberately not auto-retried because a Telegram request timeout has no idempotency key. Correct permission problems by re-promoting the bot, then wait for the next campaign cycle or use the owner retry control where eligible.

Keep normal broadcast rates at or below the conservative free defaults. Koyeb Free reaches scale-to-zero after one hour without public Internet traffic; its health checks are not an uptime mechanism. An external request to `/` every 5–10 minutes can keep a hobby deployment awake, but an always-on instance is the reliable scheduler option.
