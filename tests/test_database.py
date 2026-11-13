"""Tests for DatabaseSource — reading rows from a database like a CSV export."""

import csv
import datetime
import decimal
import io
import sqlite3
import sys

import pytest

from cavell_client import DatabaseSource
from cavell_client.database import _KEYS_PER_STATEMENT, cell_to_text
from tests.helpers import load_table

CSV_TEXT = """\
note_id,patient_id,note_date,note_text,department
note-1,MRN-1,2024-01-15,"Patient presents with cough, ""dry"".",Cardiology
note-2,MRN-1,2024-02-01,Follow-up visit; symptoms resolved.,
note-3,MRN-2,2024-03-10,"Multi-line
note text.",Oncology
"""


def _csv_rows() -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(CSV_TEXT)))


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    load_table(connection, _csv_rows())
    yield connection
    connection.close()


class TestRowsMatchCsv:
    def test_table_rows_equal_csv_dictreader(self, conn):
        """NULLs come back as "" — exactly what the CSV export would hold."""
        assert DatabaseSource(conn, table="notes").rows() == _csv_rows()

    def test_columns_reads_no_rows(self, conn):
        src = DatabaseSource(conn, table="notes")
        assert src.columns() == [
            "note_id",
            "patient_id",
            "note_date",
            "note_text",
            "department",
        ]

    def test_query_with_params(self, conn):
        src = DatabaseSource(
            conn,
            query="SELECT note_id, note_text FROM notes WHERE patient_id = ?;",
            params=["MRN-1"],
        )
        assert [r["note_id"] for r in src.rows()] == ["note-1", "note-2"]

    def test_iter_rows_projects_columns(self, conn):
        src = DatabaseSource(conn, table="notes")
        rows = list(src.iter_rows(["patient_id", "note_id"]))
        assert rows[0] == {"patient_id": "MRN-1", "note_id": "note-1"}

    def test_streams_in_fetch_size_batches(self, conn):
        src = DatabaseSource(conn, table="notes", fetch_size=1)
        assert len(list(src.iter_rows())) == 3


class TestFetchMatching:
    def test_returns_exact_pairs_only(self, conn):
        src = DatabaseSource(conn, table="notes")
        # (MRN-2, note-1) shares a value with each other key but is not a row.
        keys = [("MRN-1", "note-2"), ("MRN-2", "note-3"), ("MRN-2", "note-1")]
        got = src.fetch_matching(("patient_id", "note_id"), keys)
        assert {k: [r["note_id"] for r in rows] for k, rows in got.items()} == {
            ("MRN-1", "note-2"): ["note-2"],
            ("MRN-2", "note-3"): ["note-3"],
        }

    def test_reads_only_the_requested_rows_when_ids_restart_per_patient(
        self, monkeypatch
    ):
        """Note ids 1..N per patient: asking for (P0, 1) and (P1, 2) must not
        return every note 1 and 2 of both patients (an IN x IN cross product)."""
        conn = sqlite3.connect(":memory:")
        load_table(
            conn,
            [
                {"patient_id": f"P{p}", "note_id": str(n), "note_text": "x"}
                for p in range(50)
                for n in range(1, 51)
            ],
        )
        src = DatabaseSource(conn, table="notes")
        returned = 0
        stream = src._stream

        def counting(sql, params):
            nonlocal returned
            for row in stream(sql, params):
                returned += 1
                yield row

        monkeypatch.setattr(src, "_stream", counting)
        keys = [(f"P{p}", str(p + 1)) for p in range(50)]
        got = src.fetch_matching(("patient_id", "note_id"), keys)
        assert set(got) == set(keys)
        assert returned == len(keys)

    def test_repeated_rows_come_back_together(self, conn):
        conn.execute(
            "INSERT INTO notes VALUES ('note-1', 'MRN-1', '2024-01-15', 'again', '')"
        )
        got = DatabaseSource(conn, table="notes").fetch_matching(
            ("patient_id", "note_id"), [("MRN-1", "note-1")]
        )
        assert len(got[("MRN-1", "note-1")]) == 2

    def test_chunks_large_key_sets(self):
        conn = sqlite3.connect(":memory:")
        n = _KEYS_PER_STATEMENT * 2 + 7
        rows = [
            {"note_id": f"n{i}", "patient_id": f"P{i % 3}", "note_text": "x"}
            for i in range(n)
        ]
        load_table(conn, rows)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        src = DatabaseSource(conn, table="notes")
        keys = [(r["patient_id"], r["note_id"]) for r in rows]
        got = src.fetch_matching(("patient_id", "note_id"), keys)
        assert sum(len(v) for v in got.values()) == n
        assert len([s for s in statements if " OR " in s]) == 3

    def test_single_key_column(self, conn):
        got = DatabaseSource(conn, table="notes").fetch_matching(
            ("note_id",), [("note-2",)]
        )
        assert list(got) == [("note-2",)]

    def test_no_keys_runs_nothing(self, conn):
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        assert (
            DatabaseSource(conn, table="notes").fetch_matching(("note_id",), []) == {}
        )
        assert statements == []

    def test_keys_with_quotes_and_sql_are_bound_not_spliced(self, conn):
        evil = "x' OR '1'='1"
        conn.execute(
            "INSERT INTO notes VALUES (?, 'MRN-9', '2024-01-01', 'quoted', '')", (evil,)
        )
        got = DatabaseSource(conn, table="notes").fetch_matching(
            ("patient_id", "note_id"), [("MRN-9", evil)]
        )
        assert [r["note_text"] for r in got[("MRN-9", evil)]] == ["quoted"]

    def test_integer_key_columns_bind_as_numbers(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (pid INTEGER, nid INTEGER, txt TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?, ?)", [(1, 10, "a"), (2, 20, "b")])
        src = DatabaseSource(conn, table="t")
        list(src.iter_rows(["pid", "nid"]))  # observe the column types
        assert src._key_converter("pid")("2") == 2
        got = src.fetch_matching(("pid", "nid"), [("2", "20")])
        assert got == {("2", "20"): [{"pid": "2", "nid": "20", "txt": "b"}]}

    def test_a_column_mixing_numbers_and_text(self):
        """SQLite stores 1001 and 'A-17' side by side in an untyped column."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (pid, txt TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [(1001, "a"), ("A-17", "b")])
        src = DatabaseSource(conn, table="t")
        list(src.iter_rows(["pid"]))  # observes int first
        got = src.fetch_matching(("pid",), [("1001",), ("A-17",)])
        assert {k: [r["txt"] for r in v] for k, v in got.items()} == {
            ("1001",): ["a"],
            ("A-17",): ["b"],
        }


class TestFetchUnique:
    @pytest.fixture
    def src(self, conn):
        return DatabaseSource(conn, table="notes")

    def test_one_row_per_key(self, src):
        got = src.fetch_unique(("note_id",), [("note-1",)], compare=["note_text"])
        assert got[("note-1",)]["patient_id"] == "MRN-1"

    def test_identical_repeats_are_one_row(self, conn, src):
        conn.execute("INSERT INTO notes SELECT * FROM notes WHERE note_id = 'note-1'")
        got = src.fetch_unique(("note_id",), [("note-1",)], compare=["note_text"])
        assert isinstance(got[("note-1",)], dict)

    def test_repeats_that_differ_are_refused(self, conn, src):
        conn.execute(
            "INSERT INTO notes (note_id, note_text) VALUES ('note-1', 'other')"
        )
        got = src.fetch_unique(("note_id",), [("note-1",)], compare=["note_text"])
        assert str(got[("note-1",)]) == (
            "2 rows share note_id='note-1' with different content — none is used "
            "until the key is made unique"
        )

    def test_rows_gone_since_the_scan_are_absent_and_logged(self, src, caplog):
        got = src.fetch_unique(
            ("note_id",), [("note-1",), ("gone",)], compare=["note_text"]
        )
        assert list(got) == [("note-1",)]
        assert "1 row(s) disappeared between the key scan and the fetch" in caplog.text


class TestCellToText:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""),
            ("as is ", "as is "),
            (42, "42"),
            (4.5, "4.5"),
            (float("nan"), ""),
            (float("inf"), ""),
            (decimal.Decimal("1E+1"), "10"),
            (decimal.Decimal("4.50"), "4.50"),
            (datetime.date(2024, 1, 15), "2024-01-15"),
            (datetime.datetime(2024, 1, 15), "2024-01-15"),
            (datetime.datetime(2024, 1, 15, 9, 30), "2024-01-15T09:30:00"),
            (
                datetime.datetime(
                    2024,
                    1,
                    15,
                    9,
                    30,
                    tzinfo=datetime.timezone(datetime.timedelta(hours=1)),
                ),
                "2024-01-15T09:30:00+01:00",
            ),
            (
                datetime.datetime(2024, 1, 15, tzinfo=datetime.UTC),
                "2024-01-15T00:00:00+00:00",
            ),
            (b"caf\xc3\xa9", "café"),
            (True, "True"),
        ],
    )
    def test_conversions(self, value, expected):
        assert cell_to_text(value) == expected

    def test_lob_is_read(self):
        class FakeLob:
            def read(self):
                return "A long CLOB note."

        assert cell_to_text(FakeLob()) == "A long CLOB note."

    def test_invalid_utf8_names_the_column(self):
        with pytest.raises(ValueError, match="Column 'note_text'.*UTF-8"):
            cell_to_text(b"\xff\xfe", "note_text")


class TestArguments:
    def test_needs_exactly_one_of_table_and_query(self, conn):
        with pytest.raises(ValueError, match="exactly one"):
            DatabaseSource(conn)
        with pytest.raises(ValueError, match="exactly one"):
            DatabaseSource(conn, table="notes", query="SELECT 1")

    @pytest.mark.parametrize(
        "table", ["notes; DROP TABLE notes", "a b", '"Notes"', "1notes", "a.b.c.d"]
    )
    def test_rejects_non_plain_table_names(self, conn, table):
        with pytest.raises(ValueError, match="query="):
            DatabaseSource(conn, table=table)

    def test_accepts_schema_qualified_names(self, conn):
        DatabaseSource(conn, table="clinical.notes")
        DatabaseSource(conn, table="warehouse.clinical.notes_v2")

    def test_non_select_statement_never_runs(self, conn):
        """query= is always read as a sub-query, so a statement that is not a
        SELECT fails to parse rather than executing."""
        src = DatabaseSource(conn, query="UPDATE notes SET department = 'x'")
        with pytest.raises(sqlite3.OperationalError):
            src.rows()
        departments = [r[0] for r in conn.execute("SELECT department FROM notes")]
        assert "x" not in departments

    def test_duplicate_column_names_are_refused(self, monkeypatch):
        """A dict row would silently keep only one of two same-named columns.
        (Some databases rename duplicates in a sub-query; others do not.)"""
        monkeypatch.setattr(_FakeCursor, "description", [("id",), ("id",)])
        src = DatabaseSource(_FakeConnection(), table="t", paramstyle="qmark")
        with pytest.raises(ValueError, match=r"duplicate column name\(s\) \['id'\]"):
            src.rows()

    def test_unsupported_source(self):
        with pytest.raises(TypeError, match="DB-API 2.0 connection"):
            DatabaseSource(object(), table="notes")

    def test_bad_paramstyle(self, conn):
        with pytest.raises(ValueError, match="paramstyle"):
            DatabaseSource(conn, table="notes", paramstyle="dollar")


class _FakeCursor:
    description = [("a",), ("b",)]

    def __init__(self, log, fail=False):
        self._log = log
        self._fail = fail
        self._rows = [(1, "x")]

    def execute(self, sql, params=None):
        self._log.append(("execute", sql, params))
        if self._fail:
            raise RuntimeError("boom")

    def fetchmany(self, n):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        self._log.append(("close",))


class _FakeConnection:
    """A DB-API connection that does not say whether it is in a transaction."""

    def __init__(self, fail=False):
        self.log: list = []
        self.fail = fail

    def cursor(self):
        return _FakeCursor(self.log, self.fail)

    def rollback(self):  # pragma: no cover - must never be called
        self.log.append(("rollback",))


class _TransactionalConnection(_FakeConnection):
    """Reports its transaction state like psycopg 3 (0 = idle)."""

    def __init__(self, open_before=False):
        super().__init__()
        self.info = type("Info", (), {"transaction_status": int(open_before)})()

    def cursor(self):
        self.info.transaction_status = 2  # a statement opens a transaction
        return super().cursor()

    def rollback(self):
        self.log.append(("rollback",))
        self.info.transaction_status = 0

    def commit(self):  # pragma: no cover - must never be called
        self.log.append(("commit",))

    def close(self):  # pragma: no cover - must never be called
        self.log.append(("conn-close",))


class TestDbApiHygiene:
    def test_cursor_closed_connection_untouched(self):
        conn = _FakeConnection()
        src = DatabaseSource(conn, table="t", paramstyle="qmark")
        assert src.rows() == [{"a": "1", "b": "x"}]
        assert ("close",) in conn.log
        assert ("commit",) not in conn.log and ("conn-close",) not in conn.log

    def test_cursor_closed_when_execute_fails(self):
        conn = _FakeConnection(fail=True)
        src = DatabaseSource(conn, table="t", paramstyle="qmark")
        with pytest.raises(RuntimeError, match="boom"):
            src.rows()
        assert conn.log[-1] == ("close",)

    def test_transaction_the_read_opened_is_ended(self):
        """Otherwise a psycopg session sits "idle in transaction" for the whole
        run, between batches."""
        conn = _TransactionalConnection()
        src = DatabaseSource(conn, table="t", paramstyle="qmark")
        src.columns()
        src.rows()
        assert [e[0] for e in conn.log] == [
            "execute",
            "close",
            "rollback",
            "execute",
            "close",
            "rollback",
        ]

    def test_transaction_the_caller_had_open_is_left_alone(self):
        conn = _TransactionalConnection(open_before=True)
        DatabaseSource(conn, table="t", paramstyle="qmark").rows()
        assert ("rollback",) not in conn.log

    def test_driver_that_cannot_tell_is_left_alone(self):
        conn = _FakeConnection()
        DatabaseSource(conn, table="t", paramstyle="qmark").rows()
        assert ("rollback",) not in conn.log

    def test_sqlite_uncommitted_work_survives_and_reads_leave_no_transaction(
        self, conn
    ):
        src = DatabaseSource(conn, table="notes")
        list(src.iter_rows(["note_id"]))
        assert not conn.in_transaction
        conn.execute("INSERT INTO notes (note_id) VALUES ('uncommitted')")
        assert conn.in_transaction
        src.fetch_matching(("note_id",), [("note-1",)])
        assert conn.in_transaction  # the caller's, untouched
        assert conn.execute(
            "SELECT count(*) FROM notes WHERE note_id = 'uncommitted'"
        ).fetchone() == (1,)


class TestQuoting:
    @pytest.mark.parametrize(
        ("name", "sql"),
        [
            # Always quoted: the driver reports the name as stored, and a bare
            # name would be case-folded (PostgreSQL: PATIENT_ID -> patient_id;
            # Oracle/Snowflake: note_id -> NOTE_ID) or, if reserved, not parse.
            ("note_id", '"note_id"'),
            ("NOTE_ID", '"NOTE_ID"'),
            ("DATE", '"DATE"'),
            ("NoteId", '"NoteId"'),
            ("note id", '"note id"'),
            ('we"ird', '"we""ird"'),
            ("1st", '"1st"'),
        ],
    )
    def test_dbapi(self, name, sql):
        assert (
            DatabaseSource(_FakeConnection(), table="t", paramstyle="qmark")._quote(
                name
            )
            == sql
        )

    def test_backtick_dialects(self, monkeypatch):
        conn = _FakeConnection()
        monkeypatch.setattr(type(conn), "__module__", "databricks.sql.client")
        src = DatabaseSource(conn, table="t", paramstyle="named")
        assert src._quote("Note Id") == "`Note Id`"
        assert src._quote("note_id") == "`note_id`"

    def test_sqlalchemy_quotes_only_when_needed(self, tmp_path):
        """Oracle and Snowflake report NOTE_ID as note_id through SQLAlchemy;
        quoting that lower-case name would no longer match the column."""
        src = DatabaseSource(f"sqlite:///{tmp_path / 'x.db'}", table="t")
        assert src._quote("note_id") == "note_id"
        assert src._quote("NoteId") == '"NoteId"'
        assert src._quote("select") == '"select"'  # reserved word
        src.close()


class TestParamstyleDetection:
    def _connection_from(self, monkeypatch, module, **packages):
        import types

        for name, style in packages.items():
            mod = types.ModuleType(name)
            if style:
                mod.__dict__["paramstyle"] = style
            monkeypatch.setitem(sys.modules, name, mod)
        conn = _FakeConnection()
        monkeypatch.setattr(type(conn), "__module__", module)
        return conn

    def test_found_on_a_parent_package(self, monkeypatch):
        conn = self._connection_from(
            monkeypatch,
            "fakesnow.connector.connection",
            **{"fakesnow.connector.connection": None, "fakesnow.connector": "pyformat"},
        )
        assert DatabaseSource(conn, table="t")._paramstyle == "pyformat"

    def test_unknown_falls_back_to_qmark_with_a_warning(self, monkeypatch, caplog):
        conn = self._connection_from(
            monkeypatch, "nodriver.conn", **{"nodriver.conn": None}
        )
        assert DatabaseSource(conn, table="t")._paramstyle == "qmark"
        assert "paramstyle=" in caplog.text

    def test_paramstyle_detected_from_driver(self, conn):
        assert DatabaseSource(conn, table="notes")._paramstyle == "qmark"

    @pytest.mark.parametrize(
        ("style", "placeholders", "params"),
        [
            ("qmark", ["?", "?"], ["x", "y"]),
            ("format", ["%s", "%s"], ["x", "y"]),
            ("numeric", [":1", ":2"], ["x", "y"]),
            ("named", [":_cavell0", ":_cavell1"], {"_cavell0": "x", "_cavell1": "y"}),
            (
                "pyformat",
                ["%(_cavell0)s", "%(_cavell1)s"],
                {"_cavell0": "x", "_cavell1": "y"},
            ),
        ],
    )
    def test_bind_placeholders(self, style, placeholders, params):
        src = DatabaseSource(_FakeConnection(), table="t", paramstyle=style)
        assert src._bind(["x", "y"]) == (placeholders, params)

    def test_numeric_style_numbers_after_user_params(self):
        src = DatabaseSource(
            _FakeConnection(),
            query="SELECT * FROM t WHERE x = :1",
            params=["v"],
            paramstyle="numeric",
        )
        assert src._bind(["p", "q"])[0] == [":2", ":3"]

    @pytest.mark.parametrize(
        "style", ["named", "pyformat", "qmark", "format", "numeric"]
    )
    def test_user_params_and_lookups_combine(self, style):
        conn = _FakeConnection()
        user = {"pid": "MRN-1"} if style in ("named", "pyformat") else ["MRN-1"]
        src = DatabaseSource(
            conn, query="SELECT * FROM t WHERE p = ?", params=user, paramstyle=style
        )
        src.fetch_matching(("a",), [("1",)])
        params = conn.log[0][2]
        if isinstance(user, dict):
            assert params == {"pid": "MRN-1", "_cavell0": "1"}
        else:
            assert params == ["MRN-1", "1"]

    @pytest.mark.parametrize(
        ("style", "params"),
        [
            ("named", ["MRN-1"]),
            ("qmark", {"pid": "MRN-1"}),
            ("format", {"pid": "MRN-1"}),
            ("numeric", {"pid": "MRN-1"}),
        ],
    )
    def test_params_of_the_wrong_kind_fail_at_construction(self, style, params):
        """Not at the first key lookup, which only comes after the full scan."""
        with pytest.raises(ValueError, match="params= must be"):
            DatabaseSource(
                _FakeConnection(), query="SELECT 1", params=params, paramstyle=style
            )

    def test_pyformat_driver_with_positional_params(self):
        """psycopg declares pyformat but also takes %s with a sequence; the
        SDK's own lookups then use %s too."""
        conn = _FakeConnection()
        src = DatabaseSource(
            conn,
            query="SELECT * FROM t WHERE ward = %s",
            params=("ICU",),
            paramstyle="pyformat",
        )
        src.fetch_matching(("a",), [("1",)])
        sql, params = conn.log[0][1], conn.log[0][2]
        assert sql.endswith('WHERE (src."a" = %s)') and params == ["ICU", "1"]

    def test_percent_styles_always_pass_params(self):
        """So a literal % in query= is always written %% — with or without the
        SDK's own parameters."""
        conn = _FakeConnection()
        DatabaseSource(conn, table="t", paramstyle="pyformat").rows()
        assert conn.log[0][2] == {}


class TestSqlAlchemy:
    @pytest.fixture
    def db_url(self, tmp_path):
        path = tmp_path / "notes.db"
        connection = sqlite3.connect(path)
        load_table(connection, _csv_rows())
        connection.close()
        return f"sqlite:///{path}"

    def test_url(self, db_url):
        with DatabaseSource(db_url, table="notes") as src:
            assert src.rows() == _csv_rows()

    def test_engine_and_named_params(self, db_url):
        import sqlalchemy as sa

        engine = sa.create_engine(db_url)
        src = DatabaseSource(
            engine,
            query="SELECT * FROM notes WHERE patient_id = :pid",
            params={"pid": "MRN-2"},
        )
        assert [r["note_id"] for r in src.rows()] == ["note-3"]
        got = src.fetch_matching(("patient_id", "note_id"), [("MRN-2", "note-3")])
        assert list(got) == [("MRN-2", "note-3")]
        src.close()  # does not dispose an engine it did not create
        assert engine.pool is not None
        engine.dispose()

    def test_connection(self, db_url):
        import sqlalchemy as sa

        engine = sa.create_engine(db_url)
        with engine.connect() as connection:
            src = DatabaseSource(connection, table="notes")
            assert len(src.rows()) == 3
            assert src.columns()[0] == "note_id"
        engine.dispose()

    def test_connection_transaction_ended_unless_the_caller_owns_it(self, db_url):
        import sqlalchemy as sa

        engine = sa.create_engine(db_url)
        with engine.connect() as connection:
            DatabaseSource(connection, table="notes").rows()
            assert not connection.in_transaction()  # ours: ended
            connection.execute(sa.text("SELECT 1"))  # the caller's own
            DatabaseSource(connection, table="notes").rows()
            assert connection.in_transaction()  # left alone
        engine.dispose()

    def test_callers_connection_options_are_left_alone(self, db_url):
        """Streaming is set per statement; Connection.execution_options()
        would switch the caller's connection to server-side cursors for
        good."""
        import sqlalchemy as sa

        engine = sa.create_engine(db_url)
        with engine.connect() as connection:
            before = dict(connection.get_execution_options())
            src = DatabaseSource(connection, table="notes")
            src.rows()
            src.fetch_matching(("patient_id", "note_id"), [("MRN-2", "note-3")])
            assert dict(connection.get_execution_options()) == before
        engine.dispose()

    def test_sequence_params_fail_at_construction(self, db_url):
        with pytest.raises(ValueError, match="a dict of named values"):
            DatabaseSource(db_url, query="SELECT 1", params=["x"])

    def test_url_without_sqlalchemy_names_the_extra(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        with pytest.raises(ImportError, match=r"cavell-prism-client\[database\]"):
            DatabaseSource("postgresql://reader@db/clinical", table="notes")
