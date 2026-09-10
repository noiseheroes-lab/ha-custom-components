"""Import the component's pure modules without dragging in Home Assistant.

`custom_components/vimar_intercom/__init__.py` imports aiohttp and
Home Assistant. The modules under test are deliberately free of both, so
importing one through the real package would make the suite depend on
the whole of Home Assistant — and would hide an accidental HA import
creeping into a module that is supposed to stay pure.

Registering a stub package whose `__path__` points at the component
directory lets `from custom_components.vimar_intercom import qr` resolve
`qr.py` normally, including its relative imports, while the real
`__init__.py` never runs.
"""

import importlib.machinery
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT = REPO_ROOT / "custom_components" / "vimar_intercom"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _stub_package(name: str, path: Path) -> types.ModuleType:
    """Register a package whose body is never executed."""
    module = types.ModuleType(name)
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    module.__path__ = [str(path)]
    module.__spec__ = spec
    sys.modules[name] = module
    return module


_parent = _stub_package("custom_components", COMPONENT.parent)
_parent.vimar_intercom = _stub_package(
    "custom_components.vimar_intercom", COMPONENT)
