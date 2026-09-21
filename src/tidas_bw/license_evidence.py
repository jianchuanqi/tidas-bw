"""Record-scoped licence declarations kept outside the official dataset schemas."""

from collections.abc import Iterable

from .models import DatasetRecord
from .utils import semantic_hash

STRUCTURAL_CATEGORIES = frozenset({"contacts", "flowproperties", "unitgroups"})


def license_evidence(
    records: Iterable[DatasetRecord], *, license: str, owner: str, source: str
) -> dict:
    """Build a declaration only for records the caller has authority to license.

    This records the caller's evidence; it neither infers rights nor verifies
    legal ownership. Never call it on third-party records without their evidence.
    """
    return {
        record.identity: {
            "license": license,
            "owner": owner,
            "source": source,
            "document_sha256": semantic_hash(record.document),
        }
        for record in records
        if record.category in STRUCTURAL_CATEGORIES
    }
