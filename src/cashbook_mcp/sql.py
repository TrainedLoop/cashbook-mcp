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

MAX_SQL_BYTES = 100 * 1024
MAX_CELL_CHARS = 8192
MAX_COLUMNS = 100
# Upper bound on the characters DuckDB holds for the capped result: the cell
# cap and row limit shrink with the number of variable-size columns so the
# whole LIMIT result stays under it (DuckDB builds it before the first fetch).
MAX_MATERIALIZED_CHARS = 64 * 1024 * 1024
MIN_CELL_CHARS = 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
TRUNCATED_SUFFIX = "…[truncated]"

# Grace period for the worker to roll back and close after an interrupt.
_INTERRUPT_GRACE_S = 5.0
# Query workers alive at once. A worker that ignores the interrupt keeps its
# slot until it ends, so stuck queries cannot pile up threads without bound.
MAX_WORKERS = 2
_workers = threading.BoundedSemaphore(MAX_WORKERS)
_MAX_MESSAGE_CHARS = 300
_FETCH_BATCH = 64
_SUBQUERY_ALIAS = "__cb_q"

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
# are not transactional in DuckDB), that write logs, that expose settings
# (paths), or that turn SQL text into plans/ASTs.
_DENIED_FUNCTIONS = frozenset(
    {
        "nextval",
        "setval",
        "write_log",
        "current_setting",
        "json_serialize_plan",
        "json_deserialize_sql",
    }
)
_DENIED_PREFIXES = ("__internal",)

# System views and tables that expose file paths or settings.
_DENIED_TABLES = frozenset(
    {"duckdb_databases", "pragma_database_list", "pg_settings", "pg_database"}
)

# Characters that make a table reference a file or URL (replacement scan).
_FILEISH = re.compile(r"[/\\.:]")

# Absolute POSIX paths and Windows drive paths inside error messages. A slash
# right after U+27E6 is the closing untrusted marker, not a path.
_PATH_RE = re.compile(r"(?:(?<![\w.⟦])/|\b[A-Za-z]:\\)[^\s\"'`,)]*")

# Result types whose values have a small, fixed size and need no cap.
_FIXED_TYPES = re.compile(
    r"(?:BOOLEAN|U?TINYINT|U?SMALLINT|U?INTEGER|U?BIGINT|U?HUGEINT|FLOAT|DOUBLE"
    r"|DECIMAL\(\d+,\d+\)|DATE|TIME|TIME WITH TIME ZONE|TIMESTAMP(?:_S|_MS|_NS)?"
    r"|TIMESTAMP WITH TIME ZONE|INTERVAL|UUID|\"NULL\")"
)
_TEXT_TYPES = frozenset({"VARCHAR", "JSON"})


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


_CONTEXT_RE = re.compile(r"\n\s*LINE \d+:.*", re.DOTALL)


def _clean_message(message: str, *, runtime: bool = False) -> str:
    if runtime:
        # The LINE context would quote the sandbox wrapper, not the agent's SQL.
        message = _CONTEXT_RE.sub("", message)
        message = message.replace(_SUBQUERY_ALIAS, "query")
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
            if isinstance(table, str):
                if _FILEISH.search(table):
                    found.add("file or URL reference")
                elif table.lower() in _DENIED_TABLES:
                    found.add(table.lower())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _parse(sql: str) -> dict:
    """Serialize sql to its AST on an empty, locked connection; nothing is executed."""
    error: str | None = None
    parser = duckdb.connect(
        ":memory:",
        config={"threads": 1, "memory_limit": "64MB", "enable_external_access": False},
    )
    try:
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
    return tree


def _check(sql: str) -> dict:
    """Accept exactly one allowed SELECT; return its statement AST."""
    if len(sql.encode("utf-8", "surrogatepass")) > MAX_SQL_BYTES:
        raise SqlRejected("too_long", f"sql is longer than {MAX_SQL_BYTES} bytes")
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
    tree = _parse(sql)
    if len(tree.get("statements") or []) != 1:
        raise SqlRejected("not_single_statement", "expected 1 statement")
    denied = _violations(tree["statements"])
    if denied:
        raise SqlRejected("not_allowed", ", ".join(sorted(denied)))
    return tree["statements"][0]


def _strip_locations(node: object) -> object:
    if isinstance(node, dict):
        return {
            k: _strip_locations(v)
            for k, v in node.items()
            if not k.startswith("query_location")
        }
    if isinstance(node, list):
        return [_strip_locations(v) for v in node]
    return node


def _wrap(inner: str, select_list: str, aliases: str, limit: int) -> str:
    # The newlines end any trailing line comment of the inner query.
    return (
        f"SELECT {select_list} FROM (\n{inner}\n) AS {_SUBQUERY_ALIAS}{aliases} "
        f"LIMIT {int(limit)}"
    )


def _is_wrapper_of(wrapped: str, statement: dict) -> bool:
    """True if wrapped is our outer SELECT over a subquery identical to statement."""
    try:
        tree = _parse(wrapped)
    except SqlRejected:
        return False
    statements = tree.get("statements") or []
    if len(statements) != 1:
        return False
    node = statements[0].get("node") or {}
    from_table = node.get("from_table") or {}
    return (
        node.get("type") == "SELECT_NODE"
        and from_table.get("type") == "SUBQUERY"
        and from_table.get("alias") == _SUBQUERY_ALIAS
        and any(m.get("type") == "LIMIT_MODIFIER" for m in node.get("modifiers", []))
        and _strip_locations((from_table.get("subquery") or {}).get("node"))
        == _strip_locations(statement.get("node"))
        and not _violations(statements)
    )


def _inner_candidates(sql: str, statement: dict) -> list[str]:
    stripped = sql.strip()
    while stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    candidates = [stripped]
    # Fallback for a terminator followed by a comment ("SELECT 1; -- note"):
    # DuckDB's own rendering of the AST. Only used if its AST is identical.
    parser = duckdb.connect(":memory:", config={"threads": 1, "memory_limit": "64MB"})
    try:
        rendered = parser.execute(
            "SELECT json_deserialize_sql(?)",
            [json.dumps({"error": False, "statements": [statement]})],
        ).fetchone()[0]
        candidates.append(rendered)
    except duckdb.Error:
        pass
    finally:
        parser.close()
    return candidates


def _bounded_sql(sql: str, statement: dict) -> str:
    """The inner query text that, wrapped, reproduces statement exactly."""
    for inner in _inner_candidates(sql, statement):
        if _is_wrapper_of(_wrap(inner, "*", "", 0), statement):
            return inner
    raise SqlRejected("syntax", "the query cannot be run in the sandbox")


def _limits(types: list[str], max_rows: int) -> tuple[int, int]:
    """(cell cap in characters, row limit) keeping the result materializable."""
    variable = sum(1 for t in types if not _FIXED_TYPES.fullmatch(t))
    if variable == 0:
        return MAX_CELL_CHARS, max_rows
    per_cell = MAX_MATERIALIZED_CHARS // ((max_rows + 1) * variable)
    cap = min(MAX_CELL_CHARS, max(MIN_CELL_CHARS, per_cell))
    rows = min(max_rows, max(1, MAX_MATERIALIZED_CHARS // (variable * cap) - 1))
    return cap, rows


def _describe_sql(inner: str) -> str:
    return f"DESCRIBE\n{inner}\n"


def _is_describe_of(described: str, statement: dict) -> bool:
    """True if described is DESCRIBE over a query identical to statement."""
    try:
        tree = _parse(described)
    except SqlRejected:
        return False
    statements = tree.get("statements") or []
    if len(statements) != 1:
        return False
    node = statements[0].get("node") or {}
    ref = node.get("from_table") or {}
    query = ref.get("query")  # the query node itself, not a statement
    return (
        node.get("type") == "SELECT_NODE"
        and ref.get("type") == "SHOW_REF"
        and ref.get("show_type") == "DESCRIBE"
        and _strip_locations(query) == _strip_locations(statement.get("node"))
        and not _violations(statements)
    )


def _capped_select(types: list[str], cap: int) -> tuple[str, list[str]]:
    """Outer select list capping variable-size values; returns (sql, plan)."""
    exprs: list[str] = []
    plan: list[str] = []
    for i, type_name in enumerate(types):
        col = f"c{i}"
        if _FIXED_TYPES.fullmatch(type_name):
            exprs.append(col)
            plan.append("plain")
        elif type_name in _TEXT_TYPES:
            exprs.append(f"left(CAST({col} AS VARCHAR), {cap + 1})")
            plan.append("text")
        elif type_name == "BLOB":
            over = f"octet_length({col}) > {cap // 2}"
            exprs.append(f"CASE WHEN {over} THEN NULL ELSE {col} END")
            exprs.append(f"CASE WHEN {over} THEN left(hex({col}), {cap}) END")
            plan.append("capped")
        else:
            text = f"CAST({col} AS VARCHAR)"
            over = f"strlen({text}) > {cap}"
            exprs.append(f"CASE WHEN {over} THEN NULL ELSE {col} END")
            exprs.append(f"CASE WHEN {over} THEN left({text}, {cap}) END")
            plan.append("capped")
    return ", ".join(exprs), plan


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


def _row_cells(raw: tuple, plan: list[str], cap: int) -> tuple[list, bool]:
    """Undo the capping columns of one fetched row; return (cells, any_cut)."""
    cells: list = []
    cut = False
    j = 0
    for kind in plan:
        if kind == "plain":
            cells.append(_convert(raw[j]))
            j += 1
        elif kind == "text":
            value = raw[j]
            j += 1
            if isinstance(value, str) and len(value) > cap:
                value = value[:cap] + TRUNCATED_SUFFIX
                cut = True
            cells.append(value)
        else:
            value, text = raw[j], raw[j + 1]
            j += 2
            if text is not None:
                cells.append(text + TRUNCATED_SUFFIX)
                cut = True
            else:
                cells.append(_convert(value))
    return cells, cut


def _size(row: list) -> int:
    return len(json.dumps(row, ensure_ascii=False, default=str).encode())


def run_query(
    db: Database, sql: str, *, max_rows: int = MAX_ROWS, timeout_s: float = TIMEOUT_S
) -> QueryResult:
    """Run one read-only SELECT. Raises SqlRejected on rejection, error or timeout.

    Results are bounded: at most MAX_COLUMNS columns (else "too_wide"), at most
    max_rows rows, text cells of at most MAX_CELL_CHARS characters, and at most
    MAX_RESULT_BYTES of JSON in total. With many variable-size columns the cell
    cap and row limit shrink so DuckDB's materialized result stays under
    MAX_MATERIALIZED_CHARS. Any cut sets truncated.
    """
    if not isinstance(sql, str):
        raise SqlRejected("syntax", "sql must be a string")
    statement = _check(sql)
    inner = _bounded_sql(sql, statement)

    if not _workers.acquire(blocking=False):
        raise SqlRejected("busy", "other queries are still running; retry shortly")
    try:
        cur = db.read()
    except BaseException:
        _workers.release()
        raise
    done = threading.Event()
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            cur.execute("BEGIN")
            try:
                # Names and types without running the query; DESCRIBE keeps
                # the agent's column names (a subquery would rename duplicates).
                described = _describe_sql(inner)
                if not _is_describe_of(described, statement):
                    raise SqlRejected(
                        "syntax", "the query cannot be run in the sandbox"
                    )
                columns_info = cur.execute(described).fetchall()
                names = [str(r[0]) for r in columns_info]
                types = [str(r[1]) for r in columns_info]
                if len(names) > MAX_COLUMNS:
                    raise SqlRejected(
                        "too_wide",
                        f"{len(names)} columns; at most {MAX_COLUMNS} are allowed",
                    )
                cap, row_limit = _limits(types, max_rows)
                select_list, plan = _capped_select(types, cap)
                aliases = "(" + ", ".join(f"c{i}" for i in range(len(types))) + ")"
                bounded = _wrap(inner, select_list or "*", aliases, row_limit + 1)
                if not _is_wrapper_of(bounded, statement):
                    raise SqlRejected(
                        "syntax", "the query cannot be run in the sandbox"
                    )
                cur.execute(bounded)
                rows: list[list] = []
                truncated = False
                full = False
                total = 2  # the enclosing []
                while not full:
                    batch = cur.fetchmany(_FETCH_BATCH)
                    if not batch:
                        break
                    for raw in batch:
                        if len(rows) == row_limit:
                            full = True
                            break
                        cells, cut = _row_cells(raw, plan, cap)
                        size = _size(cells) + 1
                        if total + size > MAX_RESULT_BYTES:
                            full = True
                            break
                        truncated = truncated or cut
                        total += size
                        rows.append(cells)
                truncated = truncated or full
                outcome["columns"] = names
                outcome["types"] = types
                outcome["rows"] = rows
                outcome["truncated"] = truncated
            finally:
                try:
                    cur.execute("ROLLBACK")
                except duckdb.Error:
                    pass
        except SqlRejected as e:
            outcome["rejected"] = e
        except duckdb.Error as e:
            outcome["error"] = str(e)
        except BaseException as e:  # noqa: BLE001 - reported to the caller below
            outcome["error"] = f"{type(e).__name__}"
        finally:
            try:
                cur.close()
            finally:
                _workers.release()
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

    if "rejected" in outcome:
        raise outcome["rejected"] from None
    if "error" in outcome:
        raise SqlRejected("error", _clean_message(str(outcome["error"]), runtime=True))

    columns = outcome["columns"]
    rows = outcome["rows"]
    suspicious = [
        (i, columns[j])
        for i, row in enumerate(rows)
        for j, v in enumerate(row)
        if _flagged(v)
    ]
    return QueryResult(
        columns, outcome["types"], rows, outcome["truncated"], suspicious
    )


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
