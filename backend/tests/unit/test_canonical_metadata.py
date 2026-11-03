from __future__ import annotations

from tubeflow_test.adapters.persistence.identity_project.models import Base
from tubeflow_test.adapters.persistence.schema import canonical_metadata
from tubeflow_test.video_generation.persistence import VideoGenerationBase


def test_all_models_use_one_base_and_cross_domain_foreign_keys_resolve() -> None:
    metadata = canonical_metadata()
    assert VideoGenerationBase is Base
    assert tuple(metadata.sorted_tables)
    assert "authoritative_narration_groups" in metadata.tables
    assert "storyboard_generations" in metadata.tables
    assert "video_generation_attempts" in metadata.tables
    assert "channel_description_templates" in metadata.tables
    assert "project_description_attempts" in metadata.tables
    assert "project_descriptions" in metadata.tables
