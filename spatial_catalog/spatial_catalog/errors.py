"""Error types for the spatial catalog.

Every failure mode is a distinct, explicit exception so that callers and
tests can tell apart: not-a-catalog, corrupt on-disk data, illegal
references, bad input records, and bad query parameters.  Nothing fails
silently.
"""


class CatalogError(Exception):
    """Base class for all catalog errors."""


class NotACatalogError(CatalogError):
    """Directory is not a committed catalog (missing/invalid manifest).

    A half-built or failed build never has a manifest, so it raises this
    error on open and can never be queried.
    """


class UnsupportedVersionError(CatalogError):
    """Manifest or file version is not supported by this reader."""


class CorruptionError(CatalogError):
    """Checksum mismatch, bad magic, or truncated file detected on read."""


class IllegalReferenceError(CatalogError):
    """A node/payload reference points outside its file's valid range."""


class BuildError(CatalogError):
    """The build failed.  Carries structured reject evidence."""

    def __init__(self, message, rejects_path=None, reject_count=0):
        super().__init__(message)
        self.rejects_path = rejects_path
        self.reject_count = reject_count


class QueryError(CatalogError):
    """Invalid query parameters (non-finite coords, bad rect, bad k)."""


class CrossTableError(CatalogError):
    """Line/zone cross-table build, validation or open failure.

    Carries an optional structured evidence path (rejects/diagnostics
    written next to the failed build directory).
    """

    def __init__(self, message, evidence_path=None, reason=None, detail=None):
        super().__init__(message)
        self.evidence_path = evidence_path
        self.reason = reason
        self.detail = detail


class SpillBudgetExceeded(CrossTableError):
    """The explicit on-disk spill budget was exhausted; no partial output
    is published."""


class TopologyError(CatalogError):
    """Line-topology build, validation or open failure.

    Carries an optional structured evidence path (reject/diagnostic files
    written next to the failed build directory) so every bad record is
    locatable by source id and part position.
    """

    def __init__(self, message, evidence_path=None, reason=None, detail=None):
        super().__init__(message)
        self.evidence_path = evidence_path
        self.reason = reason
        self.detail = detail


class TopologySpillBudgetExceeded(TopologyError):
    """The topology build's explicit on-disk spill budget was exhausted;
    no partial network is published."""
