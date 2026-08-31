from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime
from typing import Any

from bson import ObjectId

from app.db.repositories import Repositories

SCHEMA_VERSION = 1


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return {"$date": value.isoformat()}
    if isinstance(value, ObjectId):
        return {"$oid": str(value)}
    raise TypeError(f"Cannot serialize {type(value)!r}")


async def make_backup(repositories: Repositories, full: bool = False) -> bytes:
    collections = await repositories.export_collections(full)
    body = {"schema_version": SCHEMA_VERSION, "kind": "FULL" if full else "CORE", "collections": collections}
    canonical = json.dumps(body, default=_json_default, sort_keys=True, separators=(",", ":")).encode()
    envelope = {"payload": body, "sha256": hashlib.sha256(canonical).hexdigest()}
    return gzip.compress(json.dumps(envelope, default=_json_default).encode())
