# Operations

`/healthz` confirms that the process is alive. `/readyz` additionally verifies MongoDB and startup initialization; it does not require an active campaign.

The scheduler uses one short MongoDB lease. A deployment overlap may run two processes briefly, but only the current lease holder creates campaign cycles or begins ending transitions. Delivery claims also have MongoDB leases; a crash simply makes expired work claimable again.

Delivery statuses distinguish permanent failures, retry waits, and `UNKNOWN_SEND_STATE`. The last is deliberately not auto-retried because a Telegram request timeout has no idempotency key. Correct permission problems by re-promoting the bot, then wait for the next campaign cycle or use the owner retry control where eligible.

Keep normal broadcast rates at or below the conservative free defaults. Koyeb Free reaches scale-to-zero after one hour without public Internet traffic; its health checks are not an uptime mechanism. An external request to `/` every 5–10 minutes can keep a hobby deployment awake, but an always-on instance is the reliable scheduler option.

Manual variant sharing requires inline mode to be enabled once through `@BotFather` with `/setinline`. The application logs a warning at startup when this setting is absent, and the Share manually screen gives the same corrective instruction instead of generating an unusable code. Inline queries are subscribed automatically in polling and webhook modes.

Each code resolves an immutable creative snapshot and is accepted only from an ID in `OWNER_USER_IDS`. Telegram caching is disabled for these results. Revocation takes effect on the next lookup; revoked records receive a 30-day MongoDB TTL. Campaign deletion removes all associated share snapshots. The downstream broadcast bot must deliberately preserve the incoming `reply_markup` when copying the post—ordinary forwarding or a bot that discards markup will lose the buttons, so iHarvester also exposes a row-preserving CTA manifest.

The Network **Top channels by subscribers** view ranks the 15 or 30 largest stored numeric `member_count` values. Counts are updated when a channel is registered or refreshed; opening the ranking does not call Telegram for every channel. Channels without a verified count are omitted and reported separately so unknown values are never presented as zero.
