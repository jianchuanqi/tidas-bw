"""Public error types."""


class TidasBwError(Exception):
    """Base error for expected migration failures."""


class PackageError(TidasBwError):
    """The TIDAS package cannot be read or written safely."""


class ValidationFailure(TidasBwError):
    """Input data failed structural, licence, or reference checks."""


class LinkingFailure(TidasBwError):
    """A technosphere exchange cannot be linked unambiguously."""


class BrightwayError(TidasBwError):
    """A Brightway project or database operation failed."""
