# Backup and restore

`/backup` creates a compressed CORE export containing channels, campaign definitions/archive metadata, and settings. It excludes all environment secrets. A future FULL export can include delivery and join-event operational history.

For restoration, send `/restore`, attach a backup created by iHarvester, inspect the reported collection counts, then press **Confirm Restore**. The file is checksum-validated before any write. Restore is an upsert by stable key, so existing channels/campaigns are not blindly duplicated. Historical active/scheduled campaigns become `ARCHIVED` with `restored_interrupted`; the bot never resumes an old broadcast without an explicit new campaign.
