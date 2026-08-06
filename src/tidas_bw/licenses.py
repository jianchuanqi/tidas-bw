"""Conservative open-data policy used before importing into Brightway."""

from __future__ import annotations

import re

OPEN_LICENSE_MARKERS = (
    "free of charge for all users and uses",
    "creative commons",
    "creativecommons.org",
    "cc0",
    "cc by",
    "open data commons",
    "odc by",
    "public domain",
    "mit license",
    "apache license",
    "bsd license",
)

RESTRICTED_LICENSE_MARKERS = (
    "license fee",
    "free of charge for some user types or use types",
    "free of charge for members only",
    "commercial use prohibited",
    "not for commercial use",
    "non-commercial",
    "noncommercial",
    "commercial licence required",
    "commercial license required",
    "cc by nc",
    "cc by nd",
    "licenses by nc",
    "licenses by nd",
    "no derivatives",
    "noderivatives",
    "restricted",
    "exclusive access",
    "proprietary",
    "not for redistribution",
    "no redistribution",
    "all rights reserved",
    "not licensed under",
    "not an open licence",
    "not an open license",
    "internal research only",
    "internal use only",
    "research use only",
    "academic use only",
    "personal use only",
    "no modification",
)


def normalise_license(value: str | None) -> str:
    text = re.sub(r"[-_/]+", " ", (value or "").strip().lower())
    return re.sub(r"\s+", " ", text)


def is_open_license(value: str | None) -> bool:
    normalised = normalise_license(value)
    return not is_restricted_license(normalised) and (
        normalised in {"no restrictions", "unrestricted"}
        or any(marker in normalised for marker in OPEN_LICENSE_MARKERS)
    )


def is_restricted_license(value: str | None) -> bool:
    normalised = normalise_license(value)
    return any(marker in normalised for marker in RESTRICTED_LICENSE_MARKERS)
