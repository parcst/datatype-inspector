"""Core inspection logic: tunnel to each DB and query column data types."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

import pymysql

from .models import DatabaseEntry, InspectionQuery, InspectionResult, InspectionStatus
from .teleport import find_tsh, list_mysql_databases, start_tunnel, stop_tunnel

logger = logging.getLogger(__name__)


def _parse_database_names(raw: str) -> list[str]:
    """Split comma-separated database names, strip whitespace, drop empties."""
    return [name.strip() for name in raw.split(",") if name.strip()]


async def inspect_databases(
    query: InspectionQuery,
) -> AsyncGenerator[tuple[InspectionResult, int, int], None]:
    """Async generator yielding (result, current_index, total) for each database.

    Tunnels are opened sequentially (one at a time) to avoid port conflicts.
    Each blocking tunnel+query operation runs in a thread via asyncio.to_thread().
    """
    tsh = find_tsh()
    raw_dbs = list_mysql_databases(tsh, query.cluster)
    entries = [
        DatabaseEntry(
            name=d["name"],
            uri=d["uri"],
            account_id=d["account_id"],
            region=d["region"],
            instance_id=d["instance_id"],
        )
        for d in raw_dbs
    ]

    db_names = _parse_database_names(query.database_name)
    total = len(entries)
    if total == 0:
        return

    for idx, entry in enumerate(entries):
        results = await asyncio.to_thread(
            _inspect_single_database, tsh, entry, query, db_names
        )
        for result in results:
            yield result, idx + 1, total


def _inspect_single_database(
    tsh: str,
    db_entry: DatabaseEntry,
    query: InspectionQuery,
    db_names: list[str],
) -> list[InspectionResult]:
    """Connect to a single RDS instance via tunnel and check the column data type
    across one or more database schemas."""
    tunnel = None
    try:
        tunnel = start_tunnel(
            tsh,
            db_entry.name,
            query.db_user,
            cluster=query.cluster,
        )

        conn = pymysql.connect(
            host=tunnel.host,
            port=tunnel.port,
            user=tunnel.db_user,
            database="information_schema",
            connect_timeout=10,
            read_timeout=10,
        )
        try:
            with conn.cursor() as cursor:
                placeholders = ", ".join(["%s"] * len(db_names))
                cursor.execute(
                    f"SELECT TABLE_SCHEMA, DATA_TYPE FROM information_schema.COLUMNS "
                    f"WHERE TABLE_SCHEMA IN ({placeholders}) "
                    f"AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                    (*db_names, query.table_name, query.column_name),
                )
                rows = cursor.fetchall()
        finally:
            conn.close()

        # Build a map of schema -> data_type from results
        found: dict[str, str] = {}
        for schema, data_type in rows:
            found[schema] = data_type.lower()

        results: list[InspectionResult] = []
        expected = query.expected_data_type.lower()

        for db_name in db_names:
            if db_name in found:
                actual = found[db_name]
                status = (
                    InspectionStatus.MATCH if actual == expected else InspectionStatus.MISMATCH
                )
                results.append(InspectionResult(
                    connection_name=db_entry.name,
                    uri=db_entry.uri,
                    account_id=db_entry.account_id,
                    region=db_entry.region,
                    actual_data_type=actual,
                    status=status,
                    database_name=db_name,
                ))
            else:
                results.append(InspectionResult(
                    connection_name=db_entry.name,
                    uri=db_entry.uri,
                    account_id=db_entry.account_id,
                    region=db_entry.region,
                    actual_data_type="",
                    status=InspectionStatus.NOT_FOUND,
                    database_name=db_name,
                ))

        return results

    except Exception as e:
        logger.exception("Error inspecting %s", db_entry.name)
        # Return one error result per database name
        return [
            InspectionResult(
                connection_name=db_entry.name,
                uri=db_entry.uri,
                account_id=db_entry.account_id,
                region=db_entry.region,
                actual_data_type="",
                status=InspectionStatus.ERROR,
                database_name=db_name,
                error_message=str(e),
            )
            for db_name in db_names
        ]

    finally:
        if tunnel is not None:
            try:
                stop_tunnel(tsh, tunnel)
            except Exception:
                logger.warning("Failed to stop tunnel for %s", db_entry.name)
