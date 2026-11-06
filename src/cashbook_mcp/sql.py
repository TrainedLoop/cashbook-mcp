"""Read-only SQL sandbox for agent-written queries.

An accepted query is exactly one statement that DuckDB's own parser types as
SELECT (WITH ... SELECT included). The store's lockdown blocks most file,
network and extension access but does not make cursors read-only, so this
module adds:

- statement-type check via duckdb.extract_statements (never a regex);
- an AST walk (json_serialize_sql on a separate, empty connection) allowing
  only listed table functions and rejecting side-effecting scalar functions
  such as setval(), and table references that look like files or URLs;
- execution on a fresh cursor inside BEGIN ... ROLLBACK, closed afterwards;
- a timeout that interrupts the query, and a row cap.
"""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import duckdb

from cashbook_mcp import schema_doc
from cashbook_mcp.content import has_marker
from cashbook_mcp.store.db import Database

MAX_ROWS = 1000
TIMEOUT_S = 10

# Grace period for the worker to roll back and close after an interrupt.
_INTERRUPT_GRACE_S = 5.0
_MAX_MESSAGE_CHARS = 300

# Table functions the agent may use. Everything else is rejected: file readers
# (DuckDB allows reading the store file and its WAL even with external access
# disabled), functions that run SQL from a string the AST walk cannot see
# (query, query_table, json_execute_serialized_sql), configuration and logging
# functions, Python object scans, and metadata that exposes file paths.
_ALLOWED_TABLE_FUNCTIONS = frozenset(
    {
        "range",
        "generate_series",
        "unnest",
        "repeat",
        "json_each",
        "json_tree",
        "duckdb_tables",
        "duckdb_views",
        "duckdb_columns",
        "duckdb_constraints",
        "duckdb_indexes",
        "duckdb_types",
        "duckdb_functions",
        "duckdb_keywords",
        "pragma_table_info",
        "pg_timezone_names",
        "icu_calendar_names",
    }
)

# Scalar functions with effects outside the rolled-back transaction (sequences
# are not transactional in DuckDB) or that write logs.
_DENIED_FUNCTIONS = frozenset({"nextval", "setval", "write_log"})
_DENIED_PREFIXES = ("__internal",)

# Characters that make a table reference a file or URL (replacement scan).
_FILEISH = re.compile(r"[/\\.:]")

# Absolute POSIX paths and Windows drive paths inside error messages.
_PATH_RE = re.compile(r"(?:(?<![\w.])/|\b[A-Za-z]:\\)[^\s\"'`,)]*")


class SqlRejected(Exception):
    """The query was not accepted or failed; reason is a short machine-readable code."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}" if message else reason)


@dataclass
class QueryResult:
    columns: list[str]
    types: list[str]
    rows: list[list]
    truncated: bool
    suspicious_cells: list[tuple[int, str]] = field(default_factory=list)


def _clean_message(message: str) -> str:
    message = _PATH_RE.sub("<path>", message)
    message = " ".join(message.split())
    if len(message) > _MAX_MESSAGE_CHARS:
        message = message[: _MAX_MESSAGE_CHARS - 1] + "…"
    return message


def _violations(node: object) -> set[str]:
    """Walk a json_serialize_sql tree; return the disallowed references found."""
    found: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("type") == "TABLE_FUNCTION":
                function = item.get("function") or {}
                name = str(function.get("function_name", "")).lower()
                if name not in _ALLOWED_TABLE_FUNCTIONS:
                    found.add(name or "<table function>")
            name = item.get("function_name")
            if isinstance(name, str) and (
                name.lower() in _DENIED_FUNCTIONS
                or name.lower().startswith(_DENIED_PREFIXES)
            ):
                found.add(name.lower())
            table = item.get("table_name")
            if isinstance(table, str) and _FILEISH.search(table):
                found.add("file or URL reference")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _check(sql: str) -> None:
    error: str | None = None
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        error = _clean_message(str(e))
    if error is not None:
        raise SqlRejected("syntax", error)
    if len(statements) != 1:
        raise SqlRejected(
            "not_single_statement", f"expected 1 statement, got {len(statements)}"
        )
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SqlRejected("not_select", "only SELECT (or WITH ... SELECT) is allowed")

    # Parse to an AST on an empty, locked connection; nothing is executed.
    parser = duckdb.connect(":memory:")
    try:
        parser.execute("SET enable_external_access=false")
        raw = parser.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    except duckdb.Error as e:
        error = _clean_message(str(e))
    finally:
        parser.close()
    if error is not None:
        raise SqlRejected("syntax", error)
    tree = json.loads(raw)
    if tree.get("error"):
        raise SqlRejected(
            "not_select", _clean_message(str(tree.get("error_message", "")))
        )
    denied = _violations(tree.get("statements"))
    if denied:
        raise SqlRejected("not_allowed", ", ".join(sorted(denied)))


def _convert(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta | UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): _convert(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(v) for v in value]
    return value


def _flagged(value: object) -> bool:
    if isinstance(value, dict):
        return any(_flagged(k) or _flagged(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_flagged(v) for v in value)
    return has_marker(value)


def run_query(
    db: Database, sql: str, *, max_rows: int = MAX_ROWS, timeout_s: float = TIMEOUT_S
) -> QueryResult:
    """Run one read-only SELECT. Raises SqlRejected on rejection, error or timeout."""
    if not isinstance(sql, str):
        raise SqlRejected("syntax", "sql must be a string")
    _check(sql)

    cur = db.read()
    done = threading.Event()
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            cur.execute("BEGIN")
            try:
                cur.execute(sql)
                outcome["description"] = cur.description or []
                outcome["rows"] = cur.fetchmany(max_rows + 1)
            finally:
                try:
                    cur.execute("ROLLBACK")
                except duckdb.Error:
                    pass
        except duckdb.Error as e:
            outcome["error"] = str(e)
        except BaseException as e:  # noqa: BLE001 - reported to the caller below
            outcome["error"] = f"{type(e).__name__}"
        finally:
            try:
                cur.close()
            finally:
                done.set()

    worker = threading.Thread(target=work, name="cashbook-sql", daemon=True)
    worker.start()
    if not done.wait(timeout_s):
        try:
            db.interrupt(cur)
        except duckdb.Error:
            pass
        done.wait(_INTERRUPT_GRACE_S)
        raise SqlRejected("timeout", f"query exceeded {timeout_s:g} s")

    if "error" in outcome:
        raise SqlRejected("error", _clean_message(str(outcome["error"])))

    description = outcome["description"]
    raw_rows = outcome["rows"]
    columns = [d[0] for d in description]
    types = [str(d[1]) for d in description]
    truncated = len(raw_rows) > max_rows
    rows = [[_convert(v) for v in row] for row in raw_rows[:max_rows]]
    suspicious = [
        (i, columns[j])
        for i, row in enumerate(rows)
        for j, v in enumerate(row)
        if _flagged(v)
    ]
    return QueryResult(columns, types, rows, truncated, suspicious)


def describe_schema(db: Database) -> dict:
    """Views (first) and tables of the store, with column types and descriptions."""
    cur = db.read()
    try:
        relations = cur.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema = 'main' "
            "ORDER BY table_type <> 'VIEW', table_name"
        ).fetchall()
        columns = cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_schema = 'main' "
            "ORDER BY table_name, ordinal_position"
        ).fetchall()
    finally:
        cur.close()

    by_table: dict[str, list[tuple[str, str]]] = {}
    for table, column, data_type in columns:
        by_table.setdefault(table, []).append((column, data_type))

    out = []
    for name, table_type in relations:
        kind = "view" if table_type == "VIEW" else "table"
        docs = schema_doc.COLUMN_DOCS.get(name, {})
        cols = by_table.get(name, [])
        entry: dict = {
            "name": name,
            "kind": kind,
            "columns": [
                {"name": c, "type": t, "description": docs.get(c, "")} for c, t in cols
            ],
        }
        if kind == "table" and any(c == "removed_at" for c, _ in cols):
            entry["note"] = f"includes removed rows; prefer v_{name}"
        out.append(entry)
    return {"relations": out, "notes": list(schema_doc.NOTES)}
