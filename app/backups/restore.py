from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime
from typing import Any

from app.backups.export import SCHEMA_VERSION, _json_default
from app.db.repositories import Repositories


def parse_backup(payload: bytes) -> dict[str, Any]:
    try:
        def object_hook(value: dict[str, Any]) -> Any:
            if set(value) == {"$date"}:
                return datetime.fromisoformat(value["$date"])
            if set(value) == {"$oid"}:
                return value["$oid"]
            return value

        envelope = json.loads(gzip.decompress(payload), object_hook=object_hook)
        backup = envelope["payload"]
        canonical = json.dumps(backup, default=_json_default, sort_keys=True, separators=(",", ":")).encode()
    except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("This is not a recognized iHarvester backup file.") from error
    if backup.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported backup schema version.")
    if envelope.get("sha256") != hashlib.sha256(canonical).hexdigest():
        raise ValueError("Backup checksum did not match; restoration was not started.")
    if not isinstance(backup.get("collections"), dict):
        raise ValueError("Backup has no valid collections payload.")
    return backup


async def restore_backup(repositories: Repositories, backup: dict[str, Any]) -> dict[str, tuple[int, int]]:
    results: dict[str, tuple[int, int]] = {}
    # Historical active campaigns intentionally return as archives rather than silently resuming old broadcasts.
    campaigns = backup["collections"].get("campaigns", [])
    for campaign in campaigns:
        if campaign.get("status") in {"ACTIVE", "SCHEDULED", "ENDING"}:
            campaign["status"] = "ARCHIVED"
            campaign["end_reason"] = "restored_interrupted"
    for name in ("channels", "campaigns", "settings"):
        documents = backup["collections"].get(name, [])
        if not isinstance(documents, list):
            raise ValueError(f"Backup collection {name} is invalid.")
        results[name] = await repositories.restore_collection(name, documents)
    return results
