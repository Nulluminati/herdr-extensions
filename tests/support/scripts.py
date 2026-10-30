"""Load a plugin's bin script as a module."""
import importlib.util

from support.paths import PLUGINS


def load_script(plugin, name):
    """Load plugins/<plugin>/bin/<name>.py and return it as the module <name>."""
    path = PLUGINS / plugin / "bin" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
