"""Migration rollback and re-upgrade test coverage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def _alembic_config() -> Config:
    project_root = Path(__file__).resolve().parents[1]
    return Config(str(project_root / "alembic.ini"))


def _current_revision(database_file: Path) -> str | None:
    connection = sqlite3.connect(database_file)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is None:
            return None
        return str(row[0])
    finally:
        connection.close()


def _table_exists(database_file: Path, table_name: str) -> bool:
    connection = sqlite3.connect(database_file)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def test_alembic_upgrade_downgrade_and_reupgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_file = tmp_path / "migration-test.sqlite"
    database_url = f"sqlite+aiosqlite:///{database_file}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    config = _alembic_config()
    script_directory = ScriptDirectory.from_config(config)
    expected_head = script_directory.get_current_head()
    assert expected_head is not None

    command.upgrade(config, "head")

    head_revision = _current_revision(database_file)
    assert head_revision == expected_head
    assert _table_exists(database_file, "activity_logs")

    command.downgrade(config, "-1")

    downgraded_revision = _current_revision(database_file)
    assert downgraded_revision is not None
    assert downgraded_revision != expected_head

    command.upgrade(config, "head")

    reupgraded_revision = _current_revision(database_file)
    assert reupgraded_revision == expected_head
