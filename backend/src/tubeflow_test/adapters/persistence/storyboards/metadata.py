from __future__ import annotations

from sqlalchemy import MetaData

from tubeflow_test.adapters.persistence.schema import canonical_metadata


def storyboard_metadata() -> MetaData:
    """Explicit registration hook used by migration/final application assembly."""
    return canonical_metadata()
