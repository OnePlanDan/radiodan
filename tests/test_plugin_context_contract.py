"""PluginContext is a fixed-field dataclass and main.py splats ctx_kwargs into it.

Adding a service to ctx_kwargs that PluginContext doesn't declare raises
TypeError at load time and every plugin silently fails to instantiate — the
station keeps streaming music and simply has no DJ, which is exactly the class
of failure this repo has been bitten by twice.
"""

import ast
import inspect
from pathlib import Path

from bridge.plugins.base import PluginContext


def _ctx_kwargs_keys_in_main() -> set[str]:
    """Statically read the ctx_kwargs literal out of main.py.

    Static parse rather than import-and-run: building the real dict would need
    a database, Liquidsoap and a live TTS host.
    """
    source = Path(inspect.getfile(PluginContext)).parent.parent / "main.py"
    tree = ast.parse(source.read_text())

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "ctx_kwargs" not in targets:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        return {
            k.value for k in node.value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    raise AssertionError("ctx_kwargs dict literal not found in bridge/main.py")


def test_every_ctx_kwarg_is_a_declared_plugin_context_field():
    declared = set(PluginContext.__dataclass_fields__)
    passed = _ctx_kwargs_keys_in_main()
    undeclared = passed - declared
    assert not undeclared, (
        f"main.py passes {sorted(undeclared)} into PluginContext, which does not "
        "declare them. Either add the field to PluginContext or hand the service "
        "to the consumer directly instead of through ctx_kwargs."
    )


def test_plugin_context_accepts_the_kwargs_main_passes():
    """The construction main.py actually performs, with placeholder values."""
    kwargs = {key: None for key in _ctx_kwargs_keys_in_main()}
    ctx = PluginContext(config={}, **kwargs)
    assert ctx.config == {}


def test_voice_watchdog_is_not_routed_through_ctx_kwargs():
    """It reaches the API via app['voice_watchdog']; plugins have no use for it."""
    assert "voice_watchdog" not in _ctx_kwargs_keys_in_main()
