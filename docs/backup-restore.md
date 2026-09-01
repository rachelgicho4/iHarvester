# Backup and restore

`/backup` creates a compressed CORE export containing channels, campaign definitions/archive metadata, and settings. It excludes all environment secrets. A future FULL export can include delivery and join-event operational history.

When automatic backups are enabled, one coalesced CORE backup is sent to the configured owners after the selected number of newly discovered channels or when the configured time interval elapses. The owner can enable/disable this and change both triggers from **Settings**. The first non-empty registry receives an initial safety copy; successful automatic delivery records its baseline so restarts do not flood the owner.

For restoration, send `/restore`, attach a backup created by iHarvester, inspect the reported collection counts, then press **Confirm Restore**. The file is checksum-validated before any write. Restore is an upsert by stable key, so existing channels/campaigns are not blindly duplicated. Historical active, scheduled, paused, or ending campaigns become `ARCHIVED` with `restored_interrupted`; the bot never resumes an old broadcast without an explicit new campaign.
