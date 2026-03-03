"""Core inspection logic: tunnel to each DB and query column data types."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

import pymysql

from .models import DatabaseEntry, InspectionQuery, InspectionResult, InspectionStatus
from .teleport import find_tsh, list_mysql_databases, start_tunnel, stop_tunnel

logger = logging.getLogger(__name__)


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

    total = len(entries)
    if total == 0:
        return

    for idx, entry in enumerate(entries):
        result = await asyncio.to_thread(
            _inspect_single_database, tsh, entry, query
        )
        yield result, idx + 1, total


def _inspect_single_database(
    tsh: str,
    db_entry: DatabaseEntry,
    query: InspectionQuery,
) -> InspectionResult:
    """Connect to a single database via tunnel and check the column data type."""
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
                cursor.execute(
                    "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                    (query.database_name, query.table_name, query.column_name),
                )
                row = cursor.fetchone()
        finally:
            conn.close()

        if row is None:
            return InspectionResult(
                connection_name=db_entry.name,
                uri=db_entry.uri,
                account_id=db_entry.account_id,
                region=db_entry.region,
                actual_data_type="",
                status=InspectionStatus.NOT_FOUND,
            )

        actual_type = row[0].lower()
        expected = query.expected_data_type.lower()
        status = (
            InspectionStatus.MATCH if actual_type == expected else InspectionStatus.MISMATCH
        )

        return InspectionResult(
            connection_name=db_entry.name,
            uri=db_entry.uri,
            account_id=db_entry.account_id,
            region=db_entry.region,
            actual_data_type=actual_type,
            status=status,
        )

    except Exception as e:
        logger.exception("Error inspecting %s", db_entry.name)
        return InspectionResult(
            connection_name=db_entry.name,
            uri=db_entry.uri,
            account_id=db_entry.account_id,
            region=db_entry.region,
            actual_data_type="",
            status=InspectionStatus.ERROR,
            error_message=str(e),
        )

    finally:
        if tunnel is not None:
            try:
                stop_tunnel(tsh, tunnel)
            except Exception:
                logger.warning("Failed to stop tunnel for %s", db_entry.name)
