"""
RadioDan Config Store

SQLite-backed configuration storage. Works alongside YAML config:
- YAML provides defaults (version-controlled, deployment-friendly)
- SQLite stores user overrides (written by web GUI, persisted across restarts)

Tables:
- config: General key-value overrides (section/key → JSON value)
- plugin_instances: Named plugin instances with independent configs
"""

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config (
    section TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (section, key)
);

CREATE TABLE IF NOT EXISTS plugin_instances (
    id           TEXT PRIMARY KEY,
    plugin_type  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled      INTEGER DEFAULT 1,
    config       TEXT DEFAULT '{}',
    sort_order   INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);
"""


class ConfigStore:
    """
    Async SQLite config store for RadioDan.

    Usage:
        store = ConfigStore()
        await store.open(Path("config/radiodan.db"))

        # General config
        await store.set("audio", "music_vol", 0.8)
        vol = await store.get("audio", "music_vol", default=0.7)

        # Plugin instances
        await store.create_instance("chill-dj", "presenter", "Chill DJ", {...})
        instances = await store.list_instances("presenter")

        await store.close()
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None

    async def open(self, db_path: Path) -> None:
        """Open the SQLite database and ensure schema exists."""
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info(f"Config store opened: {db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Config store closed")

    # =========================================================================
    # GENERAL CONFIG
    # =========================================================================

    async def get(self, section: str, key: str, default: Any = None) -> Any:
        """Get a config value. Returns default if not found."""
        async with self._db.execute(
            "SELECT value FROM config WHERE section = ? AND key = ?",
            (section, key),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return default
            return json.loads(row["value"])

    async def set(self, section: str, key: str, value: Any) -> None:
        """Set a config value (JSON-encoded)."""
        await self._db.execute(
            "INSERT OR REPLACE INTO config (section, key, value) VALUES (?, ?, ?)",
            (section, key, json.dumps(value)),
        )
        await self._db.commit()

    async def get_section(self, section: str) -> dict:
        """Get all key-value pairs in a section."""
        result = {}
        async with self._db.execute(
            "SELECT key, value FROM config WHERE section = ?",
            (section,),
        ) as cursor:
            async for row in cursor:
                result[row["key"]] = json.loads(row["value"])
        return result

    async def delete(self, section: str, key: str) -> None:
        """Delete a config value."""
        await self._db.execute(
            "DELETE FROM config WHERE section = ? AND key = ?",
            (section, key),
        )
        await self._db.commit()

    # =========================================================================
    # PLUGIN INSTANCES
    # =========================================================================

    async def list_instances(self, plugin_type: str | None = None) -> list[dict]:
        """List plugin instances, optionally filtered by type."""
        if plugin_type:
            sql = "SELECT * FROM plugin_instances WHERE plugin_type = ? ORDER BY sort_order, id"
            params = (plugin_type,)
        else:
            sql = "SELECT * FROM plugin_instances ORDER BY sort_order, plugin_type, id"
            params = ()

        results = []
        async with self._db.execute(sql, params) as cursor:
            async for row in cursor:
                results.append(self._row_to_instance(row))
        return results

    async def get_instance(self, instance_id: str) -> dict | None:
        """Get a single plugin instance by ID."""
        async with self._db.execute(
            "SELECT * FROM plugin_instances WHERE id = ?",
            (instance_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_instance(row)

    async def create_instance(
        self,
        instance_id: str,
        plugin_type: str,
        display_name: str,
        config: dict | None = None,
        enabled: bool = True,
        sort_order: int = 0,
    ) -> dict:
        """Create a new plugin instance."""
        config = config or {}
        await self._db.execute(
            """INSERT INTO plugin_instances (id, plugin_type, display_name, enabled, config, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (instance_id, plugin_type, display_name, int(enabled), json.dumps(config), sort_order),
        )
        await self._db.commit()
        logger.info(f"Created plugin instance: {instance_id} ({plugin_type})")
        return await self.get_instance(instance_id)

    async def update_instance(self, instance_id: str, **kwargs: Any) -> dict | None:
        """
        Update a plugin instance. Pass only the fields to change.

        Supported kwargs: display_name, enabled, config, sort_order
        """
        sets = []
        params = []

        for field in ("display_name", "enabled", "sort_order"):
            if field in kwargs:
                sets.append(f"{field} = ?")
                val = kwargs[field]
                if field == "enabled":
                    val = int(val)
                params.append(val)

        if "config" in kwargs:
            sets.append("config = ?")
            params.append(json.dumps(kwargs["config"]))

        if not sets:
            return await self.get_instance(instance_id)

        sets.append("updated_at = datetime('now')")
        params.append(instance_id)

        await self._db.execute(
            f"UPDATE plugin_instances SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await self._db.commit()
        logger.info(f"Updated plugin instance: {instance_id}")
        return await self.get_instance(instance_id)

    async def delete_instance(self, instance_id: str) -> None:
        """Delete a plugin instance."""
        await self._db.execute(
            "DELETE FROM plugin_instances WHERE id = ?",
            (instance_id,),
        )
        await self._db.commit()
        logger.info(f"Deleted plugin instance: {instance_id}")

    async def toggle_instance(self, instance_id: str) -> bool:
        """Toggle an instance's enabled state. Returns the new state."""
        instance = await self.get_instance(instance_id)
        if instance is None:
            raise ValueError(f"Instance not found: {instance_id}")

        new_state = not instance["enabled"]
        await self._db.execute(
            "UPDATE plugin_instances SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
            (int(new_state), instance_id),
        )
        await self._db.commit()
        logger.info(f"Toggled instance {instance_id}: {'enabled' if new_state else 'disabled'}")
        return new_state

    def _row_to_instance(self, row: aiosqlite.Row) -> dict:
        """Convert a database row to an instance dict."""
        return {
            "id": row["id"],
            "plugin_type": row["plugin_type"],
            "display_name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "config": json.loads(row["config"]),
            "sort_order": row["sort_order"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
