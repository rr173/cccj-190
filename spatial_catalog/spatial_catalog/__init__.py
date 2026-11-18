"""Offline disk-resident spatial catalog for region boundaries and line
facilities: packed Hilbert R-tree over JSONL/GeoJSON input, plus a
line/zone boundary-length attribution cross-table builder."""

__version__ = "1.1.0"

from .builder import build          # noqa: F401
from .catalog import Catalog        # noqa: F401
from .errors import (BuildError, CatalogError, CorruptionError,   # noqa: F401
                     CrossTableError, IllegalReferenceError,
                     NotACatalogError, QueryError,
                     SpillBudgetExceeded, UnsupportedVersionError)
from .xtab import build_crosstab    # noqa: F401
from .xreader import XTable         # noqa: F401
