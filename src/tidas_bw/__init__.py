"""Bidirectional migration between TIDAS packages and Brightway."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

from .api import export_tidas, import_tidas, preflight_import, validate_tidas

__all__ = ["export_tidas", "import_tidas", "preflight_import", "validate_tidas"]

try:
    __version__ = _package_version("tidas-bw")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+source"
