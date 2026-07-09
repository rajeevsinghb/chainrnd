"""
scenarios package — plugin system.

To add a NEW scenario in the future:
  1. Create scenarios/<your_scenario>.py
  2. Define SCENARIO_NAME (str) and a function
         run(df: pd.DataFrame, prices: pd.DataFrame, config) -> pd.DataFrame
  3. That's it — run.py auto-discovers it, no other file needs editing.

To remove a scenario: delete/rename its file.
"""
import importlib
import pkgutil

_REGISTRY = {}


def _discover():
    if _REGISTRY:
        return _REGISTRY
    pkg_dir = __path__
    for _, module_name, _ in pkgutil.iter_modules(pkg_dir):
        if module_name.startswith("_"):
            continue
        module = importlib.import_module(f"scenarios.{module_name}")
        name = getattr(module, "SCENARIO_NAME", module_name)
        _REGISTRY[name] = module
    return _REGISTRY


def list_scenarios():
    return sorted(_discover().keys())


def get_scenario(name: str):
    reg = _discover()
    if name not in reg:
        raise ValueError(f"Unknown scenario '{name}'. Available: {list(reg.keys())}")
    return reg[name]
