"""Offline disk-resident spatial catalog for region boundaries and line
facilities: packed Hilbert R-tree over JSONL/GeoJSON input."""

__version__ = "1.0.0"

from .builder import build          # noqa: F401
from .catalog import Catalog        # noqa: F401
from .errors import (BuildError, CatalogError, CorruptionError,   # noqa: F401
                     IllegalReferenceError, NotACatalogError, QueryError,
                     UnsupportedVersionError)
