"""Make the integration package importable without loading Home Assistant.

custom_components/epex_spot/__init__.py imports homeassistant, which is not
needed for the source modules under test. The package is registered as an
empty namespace that points at the real directory.
"""

import sys
import types
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "epex_spot"

_package = types.ModuleType("epex_spot")
_package.__path__ = [str(PACKAGE_DIR)]
sys.modules.setdefault("epex_spot", _package)

# The Spotty source raises homeassistant.exceptions.ConfigEntryError for an
# invalid contract id. Home Assistant itself is not installed in this venv, so
# provide the minimal stub needed to import and exercise the source module.
_homeassistant = types.ModuleType("homeassistant")
_exceptions = types.ModuleType("homeassistant.exceptions")


class ConfigEntryError(Exception):
    """Stub of HA's ConfigEntryError for source-only unit tests."""


_exceptions.ConfigEntryError = ConfigEntryError
_homeassistant.exceptions = _exceptions
sys.modules.setdefault("homeassistant", _homeassistant)
sys.modules.setdefault("homeassistant.exceptions", _exceptions)
