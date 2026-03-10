"""Data models and MySQL type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Categorized MySQL data types for the dropdown <optgroup> elements
MYSQL_DATA_TYPES: dict[str, list[str]] = {
    "Numeric": [
        "tinyint",
        "smallint",
        "mediumint",
        "int",
        "bigint",
        "decimal",
        "float",
        "double",
        "bit",
    ],
    "String": [
        "char",
        "varchar",
        "tinytext",
        "text",
        "mediumtext",
        "longtext",
        "binary",
        "varbinary",
        "tinyblob",
        "blob",
        "mediumblob",
        "longblob",
        "enum",
        "set",
    ],
    "Date & Time": [
        "date",
        "datetime",
        "timestamp",
        "time",
        "year",
    ],
    "Spatial": [
        "geometry",
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
    ],
    "JSON": [
        "json",
    ],
}


class InspectionStatus(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class DatabaseEntry:
    name: str
    uri: str
    account_id: str
    region: str
    instance_id: str


@dataclass
class InspectionResult:
    connection_name: str
    uri: str
    account_id: str
    region: str
    actual_data_type: str
    status: InspectionStatus
    database_name: str = ""
    error_message: str = ""


@dataclass
class InspectionQuery:
    cluster: str
    database_name: str
    table_name: str
    column_name: str
    expected_data_type: str
    db_user: str
    started_at: datetime = field(default_factory=datetime.now)


@dataclass
class InspectionSession:
    query: InspectionQuery
    results: list[InspectionResult] = field(default_factory=list)
    total_databases: int = 0
    completed: bool = False

    @property
    def match_count(self) -> int:
        return sum(1 for r in self.results if r.status == InspectionStatus.MATCH)

    @property
    def mismatch_count(self) -> int:
        return sum(1 for r in self.results if r.status == InspectionStatus.MISMATCH)

    @property
    def not_found_count(self) -> int:
        return sum(1 for r in self.results if r.status == InspectionStatus.NOT_FOUND)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == InspectionStatus.ERROR)
