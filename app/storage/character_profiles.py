"""Widen the legacy personality constraint without changing profile IDs or references."""
import re
import sqlite3

LEGACY_MODE_CHECK = re.compile(
    r"CHECK\s*\(\s*presentation_mode\s+IN\s*\(\s*'assistant'\s*,\s*'owner_first_person'\s*\)\s*\)", re.I)


def allow_character_profiles(connection):
    schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='telephone_personalities'").fetchone()[0]
    if not LEGACY_MODE_CHECK.search(schema):
        return
    # SQLite cannot change a column CHECK in place. Rebuild the parent table in
    # one transaction, preserving its entire schema, rows and explicit objects.
    connection.commit()
    connection.execute('PRAGMA foreign_keys=OFF')
    try:
        connection.execute('BEGIN IMMEDIATE')
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='telephone_personalities'").fetchone()[0]
        if LEGACY_MODE_CHECK.search(schema):
            objects = [r[0] for r in connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='telephone_personalities' AND type IN ('index','trigger') AND sql IS NOT NULL")]
            widened = LEGACY_MODE_CHECK.sub("CHECK(presentation_mode IN ('assistant','owner_first_person','character'))", schema)
            connection.execute(widened.replace('telephone_personalities', 'telephone_personalities_character_migration', 1))
            connection.execute('INSERT INTO telephone_personalities_character_migration SELECT * FROM telephone_personalities')
            connection.execute('DROP TABLE telephone_personalities')
            connection.execute('ALTER TABLE telephone_personalities_character_migration RENAME TO telephone_personalities')
            for statement in objects:
                connection.execute(statement)
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Personality migration has invalid references')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute('PRAGMA foreign_keys=ON')
