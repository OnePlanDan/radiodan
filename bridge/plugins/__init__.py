"""
RadioDan Plugin Discovery and Loading

Uses a @register_plugin decorator and pkgutil-based discovery
to find and load all plugins in this package.

Supports multi-instance plugins: each plugin class is a template,
users create named instances with independent configs via SQLite
or YAML fallback.
"""

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

from bridge.plugins.base import DJPlugin, PluginContext

if TYPE_CHECKING:
    from bridge.config_store import ConfigStore

logger = logging.getLogger(__name__)

# Global plugin registry: name -> class
_plugin_registry: dict[str, type[DJPlugin]] = {}


def register_plugin(cls: type[DJPlugin]) -> type[DJPlugin]:
    """Decorator to register a plugin class."""
    _plugin_registry[cls.name] = cls
    logger.debug(f"Registered plugin: {cls.name}")
    return cls


def get_registry() -> dict[str, type[DJPlugin]]:
    """Return the plugin registry (after discovery)."""
    return dict(_plugin_registry)


def discover_plugins() -> None:
    """Import all modules in the plugins package to trigger @register_plugin."""
    package_dir = Path(__file__).parent
    for importer, modname, ispkg in pkgutil.iter_modules([str(package_dir)]):
        if modname == "base":
            continue
        try:
            importlib.import_module(f"bridge.plugins.{modname}")
        except Exception:
            logger.exception(f"Failed to import plugin module: {modname}")


async def load_plugin_instances(
    config_store: "ConfigStore",
    plugin_configs: dict,
    ctx_kwargs: dict,
) -> list[DJPlugin]:
    """
    Discover and instantiate plugin instances.

    Resolution order:
    1. Load instances from SQLite (config_store)
    2. For any plugin type in YAML that has no SQLite instances,
       auto-create a default instance from the YAML config

    Args:
        config_store: SQLite config store for instance definitions
        plugin_configs: YAML plugin configs (e.g. {"presenter": {"enabled": true, ...}})
        ctx_kwargs: Shared services to pass into PluginContext

    Returns:
        List of instantiated plugin objects
    """
    discover_plugins()

    plugins: list[DJPlugin] = []

    # Track which plugin types have SQLite instances
    types_with_instances: set[str] = set()

    # 1. Load instances from SQLite
    db_instances = await config_store.list_instances()
    for inst in db_instances:
        plugin_type = inst["plugin_type"]
        instance_id = inst["id"]

        if plugin_type not in _plugin_registry:
            logger.warning(f"Unknown plugin type '{plugin_type}' for instance '{instance_id}', skipping")
            continue

        types_with_instances.add(plugin_type)

        if not inst["enabled"]:
            logger.info(f"Instance {instance_id} is disabled, skipping")
            continue

        try:
            plugin_cls = _plugin_registry[plugin_type]
            ctx = PluginContext(config=inst["config"], **ctx_kwargs)
            plugin = plugin_cls(ctx, instance_id=instance_id, display_name=inst["display_name"])
            plugins.append(plugin)
            logger.info(f"Loaded instance: {instance_id} ({plugin_type}) v{plugin.version}")
        except Exception:
            logger.exception(f"Failed to instantiate: {instance_id} ({plugin_type})")

    # 2. YAML fallback: auto-create default instances for types not in SQLite
    for name, plugin_cls in _plugin_registry.items():
        if name in types_with_instances:
            continue  # Already has SQLite instances

        plugin_cfg = plugin_configs.get(name, {})

        if not plugin_cfg.get("enabled", True):
            logger.info(f"Plugin {name} is disabled in YAML, skipping")
            continue

        instance_id = f"default-{name}"
        display_name = f"Default {name.replace('_', ' ').title()}"

        try:
            # Auto-create the instance in SQLite for future web GUI editing
            existing = await config_store.get_instance(instance_id)
            if not existing:
                await config_store.create_instance(
                    instance_id=instance_id,
                    plugin_type=name,
                    display_name=display_name,
                    config=plugin_cfg,
                    enabled=True,
                )
                logger.info(f"Migrated YAML config to SQLite instance: {instance_id}")

            ctx = PluginContext(config=plugin_cfg, **ctx_kwargs)
            plugin = plugin_cls(ctx, instance_id=instance_id, display_name=display_name)
            plugins.append(plugin)
            logger.info(f"Loaded plugin: {instance_id} ({name}) v{plugin.version} [from YAML]")
        except Exception:
            logger.exception(f"Failed to instantiate plugin: {name}")

    return plugins


def load_plugins(
    plugin_configs: dict,
    ctx_kwargs: dict,
) -> list[DJPlugin]:
    """
    Legacy synchronous loader (backwards compatibility).

    For new code, use load_plugin_instances() instead.
    """
    discover_plugins()

    plugins: list[DJPlugin] = []
    for name, plugin_cls in _plugin_registry.items():
        plugin_cfg = plugin_configs.get(name, {})

        if not plugin_cfg.get("enabled", True):
            logger.info(f"Plugin {name} is disabled, skipping")
            continue

        try:
            ctx = PluginContext(config=plugin_cfg, **ctx_kwargs)
            plugin = plugin_cls(ctx, instance_id=f"default-{name}")
            plugins.append(plugin)
            logger.info(f"Loaded plugin: {name} v{plugin.version}")
        except Exception:
            logger.exception(f"Failed to instantiate plugin: {name}")

    return plugins
