"""Read Prism input rows straight from a database table or query.

A :class:`DatabaseSource` stands in for a CSV export: it returns rows with the
same columns and the same string values a ``csv.DictReader`` over that export
would give, so every ``from_rows`` mapping and validation behaves identically.
It also supports the incremental reads the database pipelines need —
streaming a few key columns, then fetching full rows only for the keys that
still need processing.

Any DB-API 2.0 connection works (psycopg, pyodbc, oracledb, pymysql, sqlite3,
the Snowflake and Databricks connectors, …), as does a SQLAlchemy Engine,
Connection or URL string — SQLAlchemy is optional
(``pip install 'cavell-prism-client[database]'``).
"""

from __future__ import annotations

import contextlib
import datetime
import decimal
import importlib
import logging
import math
import re
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: ``[catalog.][schema.]name`` — plain identifiers only, so a table name can be
#: put into SQL unquoted (the database folds its case as it would in a query
#: you typed) and can never carry anything but a name.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")

#: Keys per lookup statement: 250 two-column keys bind 500 parameters, well
#: under SQL Server's 2100-parameter limit.
_KEYS_PER_STATEMENT = 250

#: Rows fetched per round trip when streaming.
_FETCH_SIZE = 10_000

#: Driver modules whose SQL dialect quotes identifiers with backticks.
_BACKTICK_DRIVERS = frozenset(
    {"pymysql", "MySQLdb", "mysql", "mariadb", "databricks", "pyhive"}
)

_PARAMSTYLES = frozenset({"qmark", "numeric", "named", "format", "pyformat"})

_NO_RESULT_SET = (
    "The statement returned no result set — table=/query= must name rows to "
    "read (a SELECT)"
)

_INSTALL_HINT = (
    "Connecting with a URL needs SQLAlchemy: "
    "pip install 'cavell-prism-client[database]' (plus the driver for your "
    "database, e.g. psycopg for PostgreSQL)"
)


def _import_sqlalchemy() -> Any:
    try:
        return importlib.import_module("sqlalchemy")
    except ImportError:
        raise ImportError(_INSTALL_HINT) from None


def cell_to_text(value: Any, column: str = "?") -> str:
    """One database value as the string a CSV export would have held.

    ``NULL`` (and a non-finite float) becomes ``""`` so "blank" means the same
    thing it does in a CSV; dates and datetimes become ISO text; ``Decimal``
    is written without an exponent; LOBs (an Oracle ``CLOB``) are read;
    ``bytes`` are decoded as UTF-8.

    A naive datetime at exactly midnight becomes a plain date: that is how
    ``DATE`` columns come back from Oracle and SQL Server, and it keeps a
    ``birth_date`` a valid FHIR date. Any other datetime keeps its time and,
    when it has one, its UTC offset.

    Raises:
        ValueError: If ``bytes`` are not valid UTF-8.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        # Written as is: in a lab value "7.0" and "7" differ in precision.
        return str(value) if math.isfinite(value) else ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return format(value, "f") if value.is_finite() else ""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None and value.time() == datetime.time(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(
                f"Column '{column}' holds bytes that are not valid UTF-8 ({e}). "
                f"Convert it in query= (e.g. CAST/CONVERT to text)."
            ) from None
    read = getattr(value, "read", None)
    if callable(read):  # LOB handle (oracledb CLOB/BLOB)
        return cell_to_text(read(), column)
    return str(value)


class UnreadableRow(dict[str, str]):
    """A row with a cell that cannot be read as text (``bytes`` that are not
    UTF-8): its readable cells (the others are ``""``) and the error, so the
    pipelines reject this one row instead of stopping the run."""

    error: ValueError

    @classmethod
    def of(cls, columns: list[str], values: Sequence[Any]) -> UnreadableRow:
        row = cls()
        for col, value in zip(columns, values, strict=True):
            try:
                row[col] = cell_to_text(value, col)
            except ValueError as e:
                row[col] = ""
                if not hasattr(row, "error"):
                    row.error = e
        return row


class DatabaseSource:
    """A database table or query, read like a CSV export of it.

    Args:
        source: Where to read from. Any of:

            - a SQLAlchemy URL string, e.g.
              ``"postgresql+psycopg://reader@db.example.org/clinical"``
              (needs ``pip install 'cavell-prism-client[database]'``);
            - a SQLAlchemy ``Engine`` or ``Connection``;
            - any DB-API 2.0 connection (``psycopg.connect(...)``,
              ``pyodbc.connect(...)``, ``oracledb.connect(...)``,
              ``sqlite3.connect(...)``, …).

            The source never commits, and never closes a connection you passed
            in. Every read is a short statement; when the read is what opened
            a transaction, that transaction is ended (rolled back — it only
            read) as soon as its rows are in, so the session is not left
            "idle in transaction" while extraction runs. A transaction you
            had open yourself is left alone.
        table: Table (or view) to read, as ``name``, ``schema.name`` or
            ``catalog.schema.name``. Plain identifiers only; for anything
            else — quoted names, filters, joins, renamed columns — use
            ``query``.
        query: A ``SELECT`` returning the rows, with the same columns as the
            CSV export. Write it the way you would run it yourself; it is used
            as a sub-query, so do not end it with ``;``. (SQL Server does not
            allow ``ORDER BY`` in a sub-query — order is applied by the
            pipelines anyway.)
        params: Parameters for ``query``. With SQLAlchemy, write ``:name``
            placeholders and pass a dict. With a DB-API connection, use your
            driver's own placeholder style (``%(name)s`` for psycopg/pymysql,
            ``?`` for pyodbc/sqlite3, ``:name`` for oracledb).
        paramstyle: Override the DB-API placeholder style the SDK uses for its
            own key lookups (``qmark``, ``numeric``, ``named``, ``format`` or
            ``pyformat``). Detected from the driver; only needed for a driver
            that does not declare it.
        fetch_size: Rows fetched per round trip when streaming.

    Raises:
        ValueError: If neither or both of ``table``/``query`` are given, or
            ``table`` is not a plain identifier.
        TypeError: If ``source`` is none of the accepted kinds.
        ImportError: If ``source`` is a URL and SQLAlchemy is not installed.
    """

    def __init__(
        self,
        source: Any,
        *,
        table: str | None = None,
        query: str | None = None,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
        paramstyle: str | None = None,
        fetch_size: int = _FETCH_SIZE,
    ) -> None:
        if (table is None) == (query is None):
            raise ValueError("Pass exactly one of table= or query=")
        if table is not None:
            if not _TABLE_RE.match(table):
                raise ValueError(
                    f"table={table!r} is not a plain [catalog.][schema.]name. "
                    f"For quoted names, filters or joins pass query='SELECT ...' "
                    f"instead."
                )
            # Safe: `table` matched _TABLE_RE (plain identifiers only).
            self._base_sql = f"SELECT * FROM {table}"  # noqa: S608
            if params:
                raise ValueError("params= only applies to query=")
        elif query is not None:
            text = query.strip().rstrip(";").strip()
            if not text:
                raise ValueError("query= is empty")
            self._base_sql = text
        if paramstyle is not None and paramstyle not in _PARAMSTYLES:
            raise ValueError(
                f"paramstyle must be one of {sorted(_PARAMSTYLES)}, got {paramstyle!r}"
            )
        if fetch_size < 1:
            raise ValueError("fetch_size must be >= 1")

        self._params = params or None
        self._fetch_size = fetch_size
        self._owned_engine: Any = None
        self._sa: Any = None  # the sqlalchemy module, when used
        self._sa_engine: Any = None
        self._sa_connection: Any = None
        self._dbapi: Any = None
        #: Python type of the first non-NULL value seen per column while
        #: streaming — used to bind key lookups with the column's own type.
        self._observed_types: dict[str, type] = {}

        if isinstance(source, str):
            self._sa = _import_sqlalchemy()
            self._sa_engine = self._owned_engine = self._sa.create_engine(source)
        elif hasattr(source, "exec_driver_sql"):
            self._sa = _import_sqlalchemy()
            self._sa_connection = source
        elif hasattr(source, "connect") and hasattr(source, "dialect"):
            self._sa = _import_sqlalchemy()
            self._sa_engine = source
        elif hasattr(source, "cursor"):
            self._dbapi = source
        else:
            raise TypeError(
                f"Unsupported database source {type(source).__name__}: pass a "
                f"SQLAlchemy URL string, Engine or Connection, or a DB-API 2.0 "
                f"connection (an object with .cursor())"
            )

        if self._dbapi is not None:
            style = paramstyle or _detect_paramstyle(self._dbapi)
            if style == "pyformat" and params and not isinstance(params, Mapping):
                # pyformat drivers (psycopg, …) also take %s with a sequence;
                # the SDK's own placeholders then follow suit.
                style = "format"
            self._paramstyle = style
            module = type(self._dbapi).__module__.split(".", 1)[0]
            self._quote_char = "`" if module in _BACKTICK_DRIVERS else '"'
        else:
            self._paramstyle = "named"  # SQLAlchemy text() binds
            self._quote_char = '"'
        # Checked here rather than at the first key lookup, which only comes
        # after the full key scan.
        wants_mapping = self._paramstyle in ("named", "pyformat")
        if self._params is not None and wants_mapping != isinstance(
            self._params, Mapping
        ):
            raise ValueError(
                f"params= must be "
                f"{'a dict of named values' if wants_mapping else 'a sequence'} "
                f"for the {self._paramstyle!r} placeholder style"
                + ("" if self._dbapi is not None else " (SQLAlchemy text())")
            )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Dispose of the engine this source created from a URL. Engines and
        connections you passed in are left alone."""
        if self._owned_engine is not None:
            self._owned_engine.dispose()
            self._owned_engine = None

    def __enter__(self) -> DatabaseSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        kind = "dbapi" if self._dbapi is not None else "sqlalchemy"
        return f"DatabaseSource({kind}, {self._base_sql[:60]!r})"

    # -- public reads ------------------------------------------------------

    def columns(self) -> list[str]:
        """Column names of the table/query, as the driver reports them —
        the names a ``columns={...}`` mapping refers to. Reads no rows."""
        sql = f"SELECT * FROM ({self._base_sql}) src WHERE 1=0"  # noqa: S608
        with self._execute(sql, []) as (cols, _):
            return cols

    def rows(self) -> list[dict[str, str]]:
        """Every row, as ``csv.DictReader`` would return a CSV export of it.

        Loads the whole result into memory — right for a practitioner list or
        a moderate notes table. For millions of rows use
        :meth:`IngestionPipeline.extract_from_database
        <cavell_client.IngestionPipeline.extract_from_database>` /
        :meth:`LabIngestionPipeline.ingest_from_database
        <cavell_client.LabIngestionPipeline.ingest_from_database>`, which
        stream and only read what still needs processing.
        """
        return list(self.iter_rows())

    def iter_rows(
        self, columns: Sequence[str] | None = None
    ) -> Iterator[dict[str, str]]:
        """Stream rows as string dicts, ``fetch_size`` at a time.

        Args:
            columns: Only read these columns (names as :meth:`columns` reports
                them). ``None`` reads every column.

        Raises:
            ValueError: At a cell that cannot be read as text (``bytes`` that
                are not UTF-8).
        """
        for row in self._scan(columns):
            if isinstance(row, UnreadableRow):
                raise row.error
            yield row

    def _scan(self, columns: Sequence[str] | None) -> Iterator[dict[str, str]]:
        """:meth:`iter_rows` for the pipelines' key scans: a row with a cell
        that cannot be read comes back as an :class:`UnreadableRow` (to be
        rejected) instead of stopping the scan."""
        if columns is None:
            # Wrapped even here: anything but a SELECT then fails to parse
            # instead of running.
            sql = f"SELECT * FROM ({self._base_sql}) src"  # noqa: S608
        else:
            if not columns:
                raise ValueError("columns must not be empty")
            projection = ", ".join(f"src.{self._quote(c)}" for c in columns)
            # Identifiers are driver-quoted; the base is the caller's own SQL.
            sql = f"SELECT {projection} FROM ({self._base_sql}) src"  # noqa: S608
        yield from self._stream(sql, [])

    def fetch_matching(
        self, key_columns: Sequence[str], keys: Iterable[tuple[str, ...]]
    ) -> dict[tuple[str, ...], list[dict[str, str]]]:
        """Full rows for exactly ``keys``, grouped by key.

        Reads ``_KEYS_PER_STATEMENT`` keys per statement with
        ``WHERE (a = ? AND b = ?) OR ...`` — exact pairs, so only the requested
        rows are read (an index on the key columns makes it a lookup). Keys
        are bound with the type each column had in earlier reads (``int`` for
        an integer id column), so comparisons run on the column's own type.
        All rows are read before this returns, so no cursor stays open while
        the caller works with them.

        Args:
            key_columns: The columns identifying a row, e.g.
                ``("patient_id", "note_id")``.
            keys: The values to fetch, as text, one tuple per key.

        Returns:
            Key -> every row holding it (more than one when the key repeats
            in the table). Keys with no row are absent.
        """
        if not key_columns:
            raise ValueError("key_columns must not be empty")
        convert = [self._key_converter(c) for c in key_columns]
        quoted = [f"src.{self._quote(c)}" for c in key_columns]
        found: dict[tuple[str, ...], list[dict[str, str]]] = {}
        pending = list(dict.fromkeys(keys))
        for start in range(0, len(pending), _KEYS_PER_STATEMENT):
            chunk = pending[start : start + _KEYS_PER_STATEMENT]
            values = [f(v) for key in chunk for f, v in zip(convert, key, strict=True)]
            marks, params = self._bind(values)
            it = iter(marks)
            where = " OR ".join(
                "(" + " AND ".join(f"{col} = {next(it)}" for col in quoted) + ")"
                for _ in chunk
            )
            # Values are bound parameters; identifiers are quoted by _quote.
            sql = f"SELECT * FROM ({self._base_sql}) src WHERE {where}"  # noqa: S608
            wanted = set(chunk)
            for row in self._stream(sql, params):
                # Re-checked as text: a case- or space-insensitive collation
                # can match rows whose value differs from the key.
                key = tuple(row.get(c, "") for c in key_columns)
                if key in wanted:
                    found.setdefault(key, []).append(row)
        return found

    def fetch_unique(
        self,
        key_columns: Sequence[str],
        keys: Iterable[tuple[str, ...]],
        compare: Iterable[str],
    ) -> dict[tuple[str, ...], dict[str, str] | ValueError]:
        """One row per key, for the pipelines' text fetch.

        A key on several rows is fine when they agree on the ``compare``
        columns (an exact repeat); when they differ there is no telling which
        one is meant, and the key maps to a ``ValueError`` saying so. Keys
        whose row is gone (deleted since it was scanned) are absent, with one
        warning for all of them.
        """
        keys = list(dict.fromkeys(keys))
        cols = list(compare)
        found = self.fetch_matching(key_columns, keys)
        if missing := len(keys) - len(found):
            logger.warning(
                f"{missing} row(s) disappeared between the key scan and the "
                f"fetch — skipped; they are picked up on the next run if they "
                f"come back"
            )
        resolved: dict[tuple[str, ...], dict[str, str] | ValueError] = {}
        for key, rows in found.items():
            unreadable = next((r for r in rows if isinstance(r, UnreadableRow)), None)
            if unreadable is not None:
                resolved[key] = unreadable.error
                continue
            first = [rows[0].get(c) for c in cols]
            if all([r.get(c) for c in cols] == first for r in rows[1:]):
                resolved[key] = rows[0]
            else:
                where = ", ".join(
                    f"{c}={v!r}" for c, v in zip(key_columns, key, strict=True)
                )
                resolved[key] = ValueError(
                    f"{len(rows)} rows share {where} with different content — "
                    f"none is used until the key is made unique"
                )
        return resolved

    def _key_converter(self, column: str) -> Callable[[str], Any]:
        """How to turn a key's text back into the type the driver returned
        for ``column`` (seen while streaming): integer and decimal columns are
        bound as numbers, everything else as text."""
        kind = self._observed_types.get(column)
        if kind is not None and issubclass(kind, int) and not issubclass(kind, bool):
            number: Callable[[str], Any] = int
        elif kind is not None and issubclass(kind, decimal.Decimal):
            number = decimal.Decimal
        else:
            return str

        def convert(text: str) -> Any:
            # A column can mix types (SQLite stores 1001 and 'A-17' side by
            # side): what is not a number is bound as the text it is.
            try:
                return number(text)
            except (ValueError, decimal.InvalidOperation):
                return text

        return convert

    # -- SQL plumbing ------------------------------------------------------

    def _quote(self, name: str) -> str:
        """A column name, as SQL that refers to the column the driver reported.

        A DB-API driver reports a column's name exactly as stored (Oracle and
        Snowflake: ``NOTE_ID`` for an unquoted column; PostgreSQL:
        ``PATIENT_ID`` for one created quoted in upper case), so the name is
        always quoted — left bare, the database would fold its case, and a
        reserved word (``DATE``) would not parse. SQLAlchemy normalizes names
        per dialect (Oracle's ``NOTE_ID`` becomes ``note_id``), so there the
        dialect decides.
        """
        if self._sa is not None:
            dialect = (self._sa_engine or self._sa_connection).dialect
            return dialect.identifier_preparer.quote(name)
        q = self._quote_char
        return f"{q}{name.replace(q, q * 2)}{q}"

    def _bind(self, values: list[Any]) -> tuple[list[str], list[Any] | dict[str, Any]]:
        """Placeholders for ``values`` in this source's style, and the
        parameters to pass with them (after the user's own, if any)."""
        style = self._paramstyle
        if style in ("named", "pyformat"):
            names = [f"_cavell{i}" for i in range(len(values))]
            mark = ":{}" if style == "named" else "%({})s"
            return [mark.format(n) for n in names], dict(
                zip(names, values, strict=True)
            )
        if style == "numeric":
            user = self._params
            start = 1 + (len(user) if user and not isinstance(user, Mapping) else 0)
            return [f":{start + i}" for i in range(len(values))], list(values)
        mark = "?" if style == "qmark" else "%s"
        return [mark] * len(values), list(values)

    def _all_params(
        self, extra: Sequence[Any] | dict[str, Any]
    ) -> Sequence[Any] | Mapping[str, Any] | None:
        """The user's ``params`` combined with the SDK's own."""
        user = self._params
        if not extra:
            if user is None and self._paramstyle in ("format", "pyformat"):
                # Always pass a container for the %-styles so a literal % in
                # query= means the same thing (%%) whether or not the SDK adds
                # parameters of its own.
                return {} if self._paramstyle == "pyformat" else []
            return user
        if user is None:
            return extra
        # Same kind as `extra`: __init__ matched params= to the style.
        if isinstance(user, Mapping) and isinstance(extra, dict):
            return {**user, **extra}
        return [*user, *extra]

    def _stream(
        self, sql: str, extra: Sequence[Any] | dict[str, Any]
    ) -> Iterator[dict[str, str]]:
        observed = self._observed_types
        with self._execute(sql, extra) as (cols, fetch):
            unseen = [i for i, col in enumerate(cols) if col not in observed]
            while batch := fetch():
                if unseen:  # stops costing anything once every type is known
                    for raw in batch:
                        for i in unseen:
                            if raw[i] is not None:
                                observed.setdefault(cols[i], type(raw[i]))
                    unseen = [i for i in unseen if cols[i] not in observed]
                for raw in batch:
                    try:
                        row = {
                            col: cell_to_text(value, col)
                            for col, value in zip(cols, raw, strict=True)
                        }
                    except ValueError:
                        row = UnreadableRow.of(cols, raw)
                    yield row

    @contextlib.contextmanager
    def _execute(
        self, sql: str, extra: Sequence[Any] | dict[str, Any]
    ) -> Generator[tuple[list[str], Callable[[], list]]]:
        """Run ``sql`` and yield ``(column names, fetch-next-batch)``.

        A transaction the read opened is ended afterwards, so a long run does
        not leave the session "idle in transaction" between batches (where a
        server's idle timeout can cut it off). Only when the read opened it:
        never one the caller had open, whose uncommitted work a rollback
        would lose — and for a DB-API driver that cannot say, never.
        """
        params = self._all_params(extra)
        if self._dbapi is not None:
            conn = self._dbapi
            ours = _in_transaction(conn) is False
            cursor = conn.cursor()
            try:
                if params is None:
                    cursor.execute(sql)
                else:
                    cursor.execute(sql, params)
                if cursor.description is None:
                    raise ValueError(_NO_RESULT_SET)
                cols = _column_names([d[0] for d in cursor.description])
                yield cols, lambda: cursor.fetchmany(self._fetch_size)
            finally:
                cursor.close()
                if ours and _in_transaction(conn):
                    conn.rollback()
            return

        stmt = self._sa.text(sql)
        with contextlib.ExitStack() as stack:
            conn = self._sa_connection
            if conn is None:
                conn = stack.enter_context(self._sa_engine.connect())
            elif not conn.in_transaction():
                stack.callback(conn.rollback)  # ours to end, not the caller's
            # Server-side cursors where the driver supports them, so a large
            # scan streams instead of landing in client memory at once.
            # Set on this statement only: Connection.execution_options() would
            # change a caller's connection for good.
            result = conn.execute(
                stmt,
                dict(params) if isinstance(params, Mapping) else {},  # see __init__
                execution_options={"stream_results": True},
            )
            stack.callback(result.close)
            if not result.returns_rows:
                raise ValueError(_NO_RESULT_SET)
            cols = _column_names(list(result.keys()))
            yield cols, lambda: result.fetchmany(self._fetch_size)


def _column_names(names: list[str]) -> list[str]:
    """Reject duplicate column names — a dict row would silently drop one."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for name in names:
        if name in seen:
            dupes.add(name)
        seen.add(name)
    if dupes:
        raise ValueError(
            f"The result has duplicate column name(s) {sorted(dupes)}; alias them in "
            f"query= (e.g. SELECT n.id AS note_id, p.id AS patient_id ...)"
        )
    return names


def _in_transaction(connection: Any) -> bool | None:
    """Whether a DB-API connection has a transaction open — for the drivers
    that say (sqlite3, psycopg 3, psycopg2, oracledb); ``None`` otherwise."""
    flag = getattr(connection, "in_transaction", None)  # sqlite3
    if isinstance(flag, bool):
        return flag
    status = getattr(getattr(connection, "info", None), "transaction_status", None)
    if status is not None:  # psycopg 3: 0 is IDLE
        return int(status) != 0
    get_status = getattr(connection, "get_transaction_status", None)
    if callable(get_status):  # psycopg2: 0 is TRANSACTION_STATUS_IDLE
        return get_status() != 0
    flag = getattr(connection, "transaction_in_progress", None)  # oracledb
    return flag if isinstance(flag, bool) else None


def _detect_paramstyle(connection: Any) -> str:
    """The DB-API ``paramstyle`` declared by the connection's driver: the
    connection class's module or the nearest package above it
    (``snowflake.connector.connection`` -> ``snowflake.connector``)."""
    module_name = type(connection).__module__
    parts = module_name.split(".")
    for candidate in (".".join(parts[:i]) for i in range(len(parts), 0, -1)):
        try:
            module = importlib.import_module(candidate)
        except ImportError:
            continue
        style = getattr(module, "paramstyle", None)
        if style in _PARAMSTYLES:
            return style
    logger.warning(
        f"Could not detect the DB-API paramstyle of {module_name}; assuming "
        f"'qmark'. Pass DatabaseSource(..., paramstyle=...) if lookups fail."
    )
    return "qmark"
