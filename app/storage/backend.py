"""Opt-in PostgreSQL support for the application's bounded SQLite-style SQL.

SQLite stays the default. This adapter handles the SQL forms used by this app;
it is not an arbitrary SQL transpiler. Values always remain DB-API parameters.
The three stores use separate PostgreSQL schemas and short pooled transactions.
"""
from __future__ import annotations

import atexit
import hashlib
import re
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

_pools = {}
_lock = threading.Lock()
_identifier = re.compile(r'^[a-z][a-z0-9_]{0,49}$')


class Row:
    def __init__(self, names, values):
        self.names, self.values = tuple(names), tuple(values)
    def keys(self): return self.names
    def __getitem__(self, key):
        return self.values[self.names.index(key)] if isinstance(key, str) else self.values[key]
    def __iter__(self): return iter(self.values)
    def __len__(self): return len(self.values)


class Result:
    def __init__(self, cursor=None, *, rows=None, names=(), rowcount=-1):
        self.cursor, self.rows, self.names = cursor, rows, tuple(names)
        self.rowcount = cursor.rowcount if cursor is not None else rowcount
    def fetchone(self):
        if self.rows is not None:
            return self.rows.pop(0) if self.rows else None
        values = self.cursor.fetchone()
        return Row([c.name for c in self.cursor.description], values) if values is not None else None
    def fetchall(self): return list(self)
    def __iter__(self):
        while (row := self.fetchone()) is not None: yield row


def close_pools():
    with _lock:
        for pool in _pools.values(): pool.close()
        _pools.clear()


atexit.register(close_pools)


def _pool(url, schema):
    if not _identifier.fullmatch(schema): raise ValueError('Invalid PostgreSQL schema name')
    key = (hashlib.sha256(url.encode()).hexdigest(), schema)
    with _lock:
        if key not in _pools:
            import psycopg
            from psycopg import sql
            from psycopg_pool import ConnectionPool
            try:
                with psycopg.connect(url, connect_timeout=10, autocommit=True) as connection:
                    connection.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
                pool = ConnectionPool(url, min_size=1, max_size=8, timeout=15,
                    kwargs={'connect_timeout': 10, 'options': f'-c search_path={schema},pg_catalog'},
                    name=f'phone-{schema}', open=True)
                pool.wait(timeout=15)
            except Exception:
                # Connection diagnostics may contain a DSN/password: never forward them.
                raise sqlite3.OperationalError('PostgreSQL connection unavailable; check private database configuration') from None
            _pools[key] = pool
        return _pools[key]


@lru_cache(maxsize=2048)
def _sql(statement):
    import sqlparse
    from sqlparse import tokens as T
    # These are the only JSON paths currently queried by application SQL.
    statement = re.sub(r"json_extract\(\s*([\w.]+)\s*,\s*'\$\.([a-z_]+)'\s*\)",
        lambda m: f"({m[1]}::jsonb ->> '{m[2]}')", statement, flags=re.I)
    parts, case_depth, previous = [], 0, ''
    for kind, text in sqlparse.lexer.tokenize(statement):
        if kind in T.Literal.String or kind in T.Comment:
            parts.append(text.replace('%', '%%'))
            if kind in T.Literal.String: previous = 'literal'
            continue
        word = text.upper()
        if word == 'CASE': case_depth += 1
        if word == 'END':
            expects_value = previous in ('THEN','ELSE','WHEN','SELECT','WHERE','AND','OR',
                'SET','ON','AS',',','(','.','=','>','<','>=','<=','!=','+','-','*','/')
            if case_depth and not expects_value: case_depth -= 1
            else: text = '"end"'
        if text == '?': text = '%s'
        elif word == 'BLOB': text = 'BYTEA'
        elif word == 'REAL': text = 'DOUBLE PRECISION'
        elif word == 'AUTOINCREMENT': text = ''
        elif word == 'INTEGER': text = 'BIGINT'
        else: text = text.replace('%', '%%')
        parts.append(text)
        if not text.isspace(): previous = word
    translated = ''.join(parts)
    # SQLite's integer PK is a generated row ID even without AUTOINCREMENT.
    translated = re.sub(r'\bBIGINT\s+PRIMARY\s+KEY\b', 'BIGSERIAL PRIMARY KEY', translated, flags=re.I)
    return translated


class PostgresConnection:
    dialect = 'postgresql'
    def __init__(self, url, schema):
        self.schema = schema
        self.pool = _pool(url, schema)
        self.raw = self.pool.getconn(timeout=15)
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, kind, value, tb):
        self.rollback() if kind else self.commit()
        return False
    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self):
        if not self.closed:
            self.pool.putconn(self.raw)
            self.closed = True
    def _run(self, statement, params=()):
        import psycopg
        try: return Result(self.raw.execute(statement, params))
        except psycopg.IntegrityError as error:
            raise sqlite3.IntegrityError(f'PostgreSQL integrity constraint ({error.sqlstate})') from None
        except psycopg.Error as error:
            raise sqlite3.DatabaseError(f'PostgreSQL operation failed ({error.sqlstate})') from None
    def _columns(self, table):
        rows = self.raw.execute('SELECT column_name FROM information_schema.columns '
            'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', (self.schema, table)).fetchall()
        return [row[0] for row in rows]
    def _primary(self, table):
        return [r[0] for r in self.raw.execute('SELECT k.column_name FROM information_schema.table_constraints t '
            'JOIN information_schema.key_column_usage k USING(constraint_catalog,constraint_schema,constraint_name) '
            "WHERE t.table_schema=%s AND t.table_name=%s AND t.constraint_type='PRIMARY KEY' ORDER BY k.ordinal_position",
            (self.schema, table)).fetchall()]
    def _table_info(self, table):
        primary = self._primary(table)
        rows = self.raw.execute('SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns '
            'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', (self.schema, table)).fetchall()
        names = ('cid', 'name', 'type', 'notnull', 'dflt_value', 'pk')
        types = {'bigint':'INTEGER','text':'TEXT','double precision':'REAL','bytea':'BLOB'}
        return Result(rows=[Row(names, (i, r[0], types.get(r[1], r[1]), int(r[2]=='NO'), r[3],
            primary.index(r[0])+1 if r[0] in primary else 0)) for i, r in enumerate(rows)])
    def execute(self, statement, params=()):
        statement = statement.strip().rstrip(';')
        pragma = re.fullmatch(r'PRAGMA\s+table_info\((\w+)\)', statement, re.I)
        if pragma: return self._table_info(pragma[1])
        if re.fullmatch(r'PRAGMA\s+(foreign_keys|journal_mode)\s*=\s*(ON|OFF|WAL)', statement, re.I):
            return Result(rows=[])
        if statement.upper() == 'BEGIN IMMEDIATE':
            # Match SQLite's single-writer critical sections across connections.
            key = int.from_bytes(hashlib.sha256(('phone:'+self.schema).encode()).digest()[:8], 'big', signed=True)
            return self._run('SELECT pg_advisory_xact_lock(%s)', (key,))
        if statement.upper() == 'BEGIN': return Result(rows=[])
        ignore = re.match(r'INSERT\s+OR\s+IGNORE\s+', statement, re.I)
        replace = re.match(r'INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*(\([^)]*\))?', statement, re.I)
        if replace:
            table = replace[1]
            columns = [s.strip().strip('"') for s in replace[2][1:-1].split(',')] if replace[2] else self._columns(table)
            primary = self._primary(table)
            if not primary: raise ValueError('Replacement requires a primary key')
            updates = [c for c in columns if c not in primary]
            statement = re.sub(r'^INSERT\s+OR\s+REPLACE', 'INSERT', statement, count=1, flags=re.I)
            quote = lambda name: '"'+name+'"'
            statement += ' ON CONFLICT ('+','.join(map(quote, primary))+') DO '+(
                'UPDATE SET '+','.join(quote(c)+'=excluded.'+quote(c) for c in updates) if updates else 'NOTHING')
        elif ignore:
            statement = re.sub(r'^INSERT\s+OR\s+IGNORE', 'INSERT', statement, count=1, flags=re.I)+' ON CONFLICT DO NOTHING'
        return self._run(_sql(statement), tuple(params))
    def executescript(self, script):
        import sqlparse
        for statement in sqlparse.split(script):
            if statement.strip(): self.execute(statement)
        return self
    def executemany(self, statement, rows):
        count = 0
        for values in rows: count += max(0, self.execute(statement, values).rowcount)
        return Result(rows=[], rowcount=count)


def connect(path: Path, *, database_url='', schema='phone_calls', timeout=10):
    url = database_url.get_secret_value() if hasattr(database_url, 'get_secret_value') else database_url
    if url: return PostgresConnection(url, schema)
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection
