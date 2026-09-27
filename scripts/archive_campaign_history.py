#!/usr/bin/env python3
"""Archive disposable campaign delivery history to durable disk before pruning.

The tool is deliberately conservative: it never touches live campaign data.
Closed archived campaigns need no live-post pointers; ending campaigns can only
shed terminal send rows, keeping their cleanup jobs and live-state pointers.
Export and prune are separate commands so database data is never deleted until
a compressed archive and its SHA-256 manifest have been verified.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import secrets
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from bson import json_util
from pymongo import MongoClient


TERMINAL_SEND_STATUSES = {"SENT", "FAILED_PERMANENT", "UNKNOWN_SEND_STATE", "CANCELLED"}
TERMINAL_REPAIR_STATUSES = {"SUCCEEDED", "SKIPPED", "FAILED"}
FORMAT_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(UTC)


def mongo() -> tuple[MongoClient, Any]:
    uri = os.environ.get("MONGODB_URI")
    name = os.environ.get("MONGODB_DB_NAME", "telegram_campaign_orchestrator")
    if not uri:
        raise SystemExit("MONGODB_URI is required; keep it only on the archive host.")
    client = MongoClient(uri, serverSelectionTimeoutMS=20_000, connectTimeoutMS=20_000, socketTimeoutMS=60_000)
    client.admin.command("ping")
    return client, client[name]


def ending_terminal_query(campaign_id: str) -> dict[str, Any]:
    return {
        "campaign_id": campaign_id,
        "operation": {"$ne": "CLEANUP"},
        "status": {"$in": sorted(TERMINAL_SEND_STATUSES)},
    }


def ending_all_send_query(campaign_id: str) -> dict[str, Any]:
    """Emergency-only query used when Atlas is over quota and blocks writes."""
    return {"campaign_id": campaign_id, "operation": {"$ne": "CLEANUP"}}


def candidates(db: Any, include_ending_terminal: bool, include_ending_all_send: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for campaign in db.campaigns.find(
        {"status": {"$in": ["ARCHIVED", "ENDING"]}},
        {"_id": 0, "campaign_id": 1, "name": 1, "status": 1},
    ):
        campaign_id = campaign["campaign_id"]
        live_posts = db.campaign_channel_state.count_documents({"campaign_id": campaign_id})
        if campaign["status"] == "ARCHIVED" and not live_posts:
            # A previously compacted campaign has its counters on the campaign
            # document and no raw delivery data left to archive again.
            if db.deliveries.count_documents({"campaign_id": campaign_id}, limit=1) or db.join_events.count_documents({"campaign_id": campaign_id}, limit=1):
                result.append({"campaign_id": campaign_id, "name": campaign.get("name", "Untitled"), "scope": "ARCHIVED_CLOSED", "live_posts": 0})
        elif campaign["status"] == "ENDING" and (include_ending_terminal or include_ending_all_send):
            scope = "ENDING_ALL_SEND" if include_ending_all_send else "ENDING_TERMINAL_SEND"
            query = ending_all_send_query(campaign_id) if include_ending_all_send else ending_terminal_query(campaign_id)
            if db.deliveries.count_documents(query, limit=1):
                result.append({"campaign_id": campaign_id, "name": campaign.get("name", "Untitled"), "scope": scope, "live_posts": live_posts})
    return result


def archive_queries(candidate: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    campaign_id = candidate["campaign_id"]
    if candidate["scope"] == "ARCHIVED_CLOSED":
        query = {"campaign_id": campaign_id}
        return [("campaigns", query), ("campaign_cycles", query), ("deliveries", query), ("join_events", query)]
    if candidate["scope"] == "ENDING_ALL_SEND":
        return [("deliveries", ending_all_send_query(campaign_id))]
    return [("deliveries", ending_terminal_query(campaign_id))]


def empty_rollup() -> dict[str, Any]:
    return {
        "delivery_totals": {},
        "cleanup_totals": {},
        "cleanup_failure_totals": {},
        "metrics": {"attempts": 0, "replaced_messages": 0, "cleaned_messages": 0, "first_created_at": None, "last_updated_at": None},
        "join_count": 0,
        "failure_samples": [],
    }


def observe(rollup: dict[str, Any], collection: str, document: dict[str, Any]) -> None:
    if collection == "join_events":
        rollup["join_count"] += 1
        return
    if collection != "deliveries":
        return
    # A historical safety-refresh bug could materialize many never-dispatched
    # replacement cycles.  Those rows remain in the offline audit archive but
    # must not distort the campaign's customer-facing delivery statistics.
    if document.get("discard_from_history_rollup"):
        return
    status = str(document.get("status", "UNKNOWN"))
    operation = document.get("operation") or "SEND"
    key = "cleanup_totals" if operation == "CLEANUP" else "delivery_totals"
    rollup[key][status] = int(rollup[key].get(status, 0)) + 1
    metrics = rollup["metrics"]
    for source, destination in (("attempts", "attempts"), ("replaced_message_count", "replaced_messages"), ("cleaned_message_count", "cleaned_messages")):
        metrics[destination] += int(document.get(source) or 0)
    created_at, updated_at = document.get("created_at"), document.get("updated_at")
    if created_at and (metrics["first_created_at"] is None or created_at < metrics["first_created_at"]):
        metrics["first_created_at"] = created_at
    if updated_at and (metrics["last_updated_at"] is None or updated_at > metrics["last_updated_at"]):
        metrics["last_updated_at"] = updated_at
    if operation == "CLEANUP" and status == "CLEANUP_FAILED":
        category = str(document.get("error_category") or "UNKNOWN")
        rollup["cleanup_failure_totals"][category] = int(rollup["cleanup_failure_totals"].get(category, 0)) + 1
    if status in {"FAILED_PERMANENT", "UNKNOWN_SEND_STATE", "CLEANUP_FAILED"} and len(rollup["failure_samples"]) < 20:
        rollup["failure_samples"].append(
            {key: document.get(key) for key in ("channel_id", "cycle_number", "operation", "status", "error_category", "error_summary", "updated_at")}
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def count_plan(db: Any, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for candidate in rows:
        counts = {collection: db[collection].count_documents(query) for collection, query in archive_queries(candidate)}
        result.append({**candidate, "counts": counts, "total_documents": sum(counts.values())})
    return result


def plan(db: Any, include_ending_terminal: bool, include_ending_all_send: bool) -> int:
    rows = count_plan(db, candidates(db, include_ending_terminal, include_ending_all_send))
    for row in rows:
        print(json_util.dumps(row, json_options=json_util.CANONICAL_JSON_OPTIONS))
    print(f"Safe candidates: {len(rows)} | records: {sum(row['total_documents'] for row in rows)}")
    return 0


def export(db: Any, archive_dir: Path, include_ending_terminal: bool, include_ending_all_send: bool) -> Path:
    rows = candidates(db, include_ending_terminal, include_ending_all_send)
    if not rows:
        raise SystemExit("No safe candidates found; nothing was exported.")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_id = f"iharvester-history-{utcnow():%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    archive_path = archive_dir / f"{archive_id}.jsonl.gz"
    manifest_path = archive_dir / f"{archive_id}.manifest.json"
    manifest: dict[str, Any] = {"format_version": FORMAT_VERSION, "archive_id": archive_id, "created_at": utcnow(), "archive_path": str(archive_path), "campaigns": []}
    # Archive durability matters more than squeezing a few extra megabytes out
    # of the VPS disk.  A low compression level keeps Atlas reads short and
    # avoids a prolonged maintenance window on shared/free-tier clusters.
    with gzip.open(archive_path, "wb", compresslevel=1) as handle:
        for candidate in rows:
            entry = {**candidate, "counts": Counter(), "rollup": empty_rollup()}
            for collection, query in archive_queries(candidate):
                # Atlas free/shared tiers disallow no-timeout cursors.  This
                # cursor is consumed continuously in small batches, so the
                # standard server timeout is both sufficient and compatible.
                cursor = db[collection].find(query).batch_size(500)
                try:
                    for document in cursor:
                        handle.write(json_util.dumps({"collection": collection, "document": document}, json_options=json_util.CANONICAL_JSON_OPTIONS).encode("utf-8") + b"\n")
                        entry["counts"][collection] += 1
                        observe(entry["rollup"], collection, document)
                finally:
                    cursor.close()
            entry["counts"] = dict(entry["counts"])
            manifest["campaigns"].append(entry)
            print(f"Exported {candidate['name']}: {sum(entry['counts'].values())} records", flush=True)
    manifest["sha256"] = sha256(archive_path)
    manifest["archive_bytes"] = archive_path.stat().st_size
    manifest_path.write_text(json_util.dumps(manifest, json_options=json_util.CANONICAL_JSON_OPTIONS, indent=2), encoding="utf-8")
    print(f"Archive: {archive_path}\nManifest: {manifest_path}\nSHA256: {manifest['sha256']}")
    return manifest_path


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"Manifest does not exist: {path}")
    manifest = json_util.loads(path.read_text(encoding="utf-8"))
    archive_path = Path(manifest.get("archive_path", ""))
    if manifest.get("format_version") != FORMAT_VERSION or not archive_path.is_file():
        raise SystemExit("Manifest or archive is invalid.")
    if sha256(archive_path) != manifest.get("sha256"):
        raise SystemExit("Archive checksum mismatch; nothing was pruned.")
    return manifest


def merge_rollup(existing: dict[str, Any], incoming: dict[str, Any], archive_id: str) -> dict[str, Any]:
    result = dict(existing or {})
    applied = list(result.get("applied_archives", []))
    if archive_id in applied:
        return result
    for key in ("delivery_totals", "cleanup_totals", "cleanup_failure_totals"):
        values = dict(result.get(key, {}))
        for name, value in incoming.get(key, {}).items():
            values[name] = int(values.get(name, 0)) + int(value)
        result[key] = values
    metrics = dict(result.get("metrics", {}))
    source_metrics = incoming.get("metrics", {})
    for name in ("attempts", "replaced_messages", "cleaned_messages"):
        metrics[name] = int(metrics.get(name, 0)) + int(source_metrics.get(name, 0))
    for name, choose_minimum in (("first_created_at", True), ("last_updated_at", False)):
        value = source_metrics.get(name)
        if value and (not metrics.get(name) or (value < metrics[name] if choose_minimum else value > metrics[name])):
            metrics[name] = value
    result["metrics"] = metrics
    result["join_count"] = int(result.get("join_count", 0)) + int(incoming.get("join_count", 0))
    result["failure_samples"] = (list(result.get("failure_samples", [])) + list(incoming.get("failure_samples", [])))[:20]
    result["applied_archives"] = [*applied, archive_id]
    result["updated_at"] = utcnow()
    return result


def prune(db: Any, manifest_path: Path, *, emergency_delete_first: bool = False) -> int:
    manifest = load_manifest(manifest_path)
    archive_id = str(manifest["archive_id"])
    descriptor = {key: manifest[key] for key in ("archive_id", "archive_path", "sha256", "created_at", "archive_bytes")}
    total = 0
    for entry in manifest["campaigns"]:
        campaign_id, scope = entry["campaign_id"], entry["scope"]
        campaign = db.campaigns.find_one({"campaign_id": campaign_id})
        if not campaign:
            print(f"Skip {campaign_id}: campaign is gone.")
            continue
        if scope == "ARCHIVED_CLOSED":
            valid = campaign.get("status") == "ARCHIVED" and not db.campaign_channel_state.count_documents({"campaign_id": campaign_id})
            delete_query, collections = {"campaign_id": campaign_id}, ("deliveries", "join_events")
        elif scope in {"ENDING_TERMINAL_SEND", "ENDING_ALL_SEND"}:
            valid = campaign.get("status") == "ENDING"
            delete_query = ending_all_send_query(campaign_id) if scope == "ENDING_ALL_SEND" else ending_terminal_query(campaign_id)
            collections = ("deliveries",)
        else:
            valid, collections = False, ()
            delete_query = {}
        if not valid:
            print(f"Skip {campaign_id}: no longer eligible.")
            continue
        # A campaign can continue changing while a long archive is streaming.
        # Never let a broad delete catch a record that was not in this exact
        # checksum-verified archive; export a fresh manifest instead.
        expected_delivery_count = int(entry.get("counts", {}).get("deliveries", 0))
        if "deliveries" in collections:
            actual_delivery_count = db.deliveries.count_documents(delete_query)
            if actual_delivery_count != expected_delivery_count:
                print(
                    f"Skip {campaign_id}: delivery count changed since export "
                    f"({expected_delivery_count} archived, {actual_delivery_count} current)."
                )
                continue
        def record_archive() -> None:
            refreshed = db.campaigns.find_one({"campaign_id": campaign_id}) or campaign
            rollup = merge_rollup(refreshed.get("history_rollup", {}), entry["rollup"], archive_id)
            archives = list(refreshed.get("offline_archives", []))
            if not any(item.get("archive_id") == archive_id for item in archives):
                archives.append({**descriptor, "scope": scope, "records": entry["counts"]})
            db.campaigns.update_one({"campaign_id": campaign_id}, {"$set": {"history_rollup": rollup, "offline_archives": archives, "history_archived_at": utcnow()}})

        if not emergency_delete_first:
            record_archive()
        for collection in collections:
            removed = db[collection].delete_many(delete_query).deleted_count
            total += removed
            print(f"Pruned {campaign_id} {collection}: {removed}", flush=True)
        if emergency_delete_first:
            # Atlas blocks document updates while over quota but permits the
            # deletes that recover it.  The checksum-verified disk archive is
            # therefore the safety boundary for this one recovery path.
            record_archive()
    print(f"Prune complete: {total} records removed.")
    return 0


def maintenance(db: Any, update_retention_hours: int, completed_retention_days: int) -> int:
    now = utcnow()
    updates = db.processed_updates.delete_many({"received_at": {"$lt": now - timedelta(hours=max(1, update_retention_hours))}}).deleted_count
    cutoff = now - timedelta(days=max(1, completed_retention_days))
    repairs = db.live_text_repairs.delete_many({"status": {"$in": sorted(TERMINAL_REPAIR_STATUSES)}, "updated_at": {"$lt": cutoff}}).deleted_count
    jobs = db.network_refresh_jobs.delete_many({"status": {"$in": ["COMPLETED", "FAILED"]}, "updated_at": {"$lt": cutoff}}).deleted_count
    runs = db.network_refresh_runs.delete_many({"status": "COMPLETED", "completed_at": {"$lt": cutoff}}).deleted_count
    print(f"Maintenance removed processed_updates={updates}, live_text_repairs={repairs}, network_jobs={jobs}, network_runs={runs}.")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("plan", "export"):
        item = commands.add_parser(name)
        item.add_argument("--include-ending-terminal", action="store_true")
        item.add_argument("--include-ending-all-send", action="store_true", help="quota emergency: archive all non-cleanup send rows from ENDING campaigns")
        if name == "export":
            item.add_argument("--archive-dir", type=Path, required=True)
    item = commands.add_parser("prune")
    item.add_argument("--manifest", type=Path, required=True)
    item.add_argument("--emergency-delete-first", action="store_true", help="only after a verified export, use deletes to recover an Atlas quota lock")
    item = commands.add_parser("maintenance")
    item.add_argument("--update-retention-hours", type=int, default=24)
    item.add_argument("--completed-retention-days", type=int, default=7)
    return result


def main() -> int:
    args = parser().parse_args()
    client, db = mongo()
    try:
        if args.command == "plan":
            return plan(db, args.include_ending_terminal, args.include_ending_all_send)
        if args.command == "export":
            export(db, args.archive_dir, args.include_ending_terminal, args.include_ending_all_send)
            return 0
        if args.command == "prune":
            return prune(db, args.manifest, emergency_delete_first=args.emergency_delete_first)
        return maintenance(db, args.update_retention_hours, args.completed_retention_days)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
