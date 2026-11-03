from __future__ import annotations

import hashlib
from contextlib import nullcontext
from uuid import uuid4

import pytest

from tubeflow_test.adapters.persistence.media_authority import SqlTransactionalCompositionInputs
from tubeflow_test.application.media_authority import (
    CompositionProjectSnapshot,
    LatestFeatureCompositionInputs,
    LatestH3Group,
    LatestNarrationSentence,
)
from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.errors import DomainError


def test_latest_authority_consumes_saved_boundaries_without_duration_reaccumulation() -> None:
    project, owner = uuid4(), uuid4()

    class Narration:
        items = ()

        def timeline(self, project_id, owner_id):
            if self.items:
                return self.items
            rows = (("one", 0, 1001), ("two", 1001, 2002))
            self.items = tuple(
                LatestNarrationSentence(
                    uuid4(),
                    index + 1,
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                    start * 48,
                    end * 48,
                    48000,
                    uuid4(),
                )
                for index, (text, start, end) in enumerate(rows)
            )
            return self.items

    class H3:
        def current_groups(self, project_id, owner_id):
            sentences = narration.timeline(project_id, owner_id)
            return (
                LatestH3Group(uuid4(), 1, uuid4(), (sentences[0].id,), 0, 1001, 1280, 720),
                LatestH3Group(uuid4(), 2, uuid4(), (sentences[1].id,), 1001, 2002, 1280, 720),
            )

    class Snapshots:
        def snapshot(self, project_id, owner_id):
            return CompositionProjectSnapshot("a" * 64, AspectRatio.LANDSCAPE, 1280, 720)

    narration = Narration()
    request = LatestFeatureCompositionInputs(narration, H3(), Snapshots()).load(project, owner)
    assert request is not None
    assert [(item.start_ms, item.end_ms) for item in request.sentences] == [
        (0, 1001),
        (1001, 2002),
    ]
    assert [item.target_duration_ms for item in request.groups] == [1001, 1001]


def test_relational_authorities_are_read_in_one_session_without_legacy_fallback() -> None:
    project, owner, sentence_id = uuid4(), uuid4(), uuid4()

    class FakeSession:
        def begin(self):
            return nullcontext()

    session = FakeSession()

    class Sessions:
        def __call__(self):
            class Context:
                def __enter__(self):
                    return session

                def __exit__(self, *args):
                    return None

            return Context()

    class Narration:
        def timeline_in_session(self, actual, project_id, owner_id):
            assert actual is session
            return (
                LatestNarrationSentence(
                    sentence_id,
                    1,
                    "text",
                    hashlib.sha256(b"text").hexdigest(),
                    0,
                    48000,
                    48000,
                    uuid4(),
                ),
            )

    class H3:
        def current_archived_groups_in_session(self, actual, project_id, owner_id):
            assert actual is session
            return (LatestH3Group(uuid4(), 1, uuid4(), (sentence_id,), 0, 1000, 1280, 720),)

    class Snapshot:
        def snapshot_in_session(self, actual, project_id, owner_id):
            assert actual is session
            return CompositionProjectSnapshot("a" * 64, AspectRatio.LANDSCAPE, 1280, 720)

    request = SqlTransactionalCompositionInputs(
        Sessions(),
        Narration(),
        H3(),
        Snapshot(),  # type: ignore[arg-type]
    ).load(project, owner)
    assert request is not None
    assert request.groups[0].narration_segments[0].sentence_id == str(sentence_id)


@pytest.mark.parametrize("invalid", ("omitted", "duplicate", "cross_group", "gap", "overlap"))
def test_latest_authority_requires_exact_timeline_membership(invalid: str) -> None:
    project, owner = uuid4(), uuid4()
    sentences = tuple(
        LatestNarrationSentence(
            uuid4(), index + 1, str(index), hashlib.sha256(str(index).encode()).hexdigest(),
            index * 48000, (index + 1) * 48000, 48000, uuid4()
        )
        for index in range(3)
    )
    memberships = ((sentences[0].id,), (sentences[1].id,), (sentences[2].id,))
    boundaries = ((0, 1000), (1000, 2000), (2000, 3000))
    if invalid == "omitted":
        memberships = memberships[:2]
        boundaries = boundaries[:2]
    elif invalid == "duplicate":
        memberships = ((sentences[0].id,), (sentences[0].id,), (sentences[2].id,))
    elif invalid == "cross_group":
        memberships = ((sentences[1].id,), (sentences[0].id,), (sentences[2].id,))
    elif invalid == "gap":
        boundaries = ((0, 1000), (1001, 2000), (2000, 3000))
    else:
        boundaries = ((0, 1001), (1000, 2000), (2000, 3000))

    class Narration:
        def timeline(self, _project_id, _owner_id):
            return sentences

    class H3:
        def current_groups(self, _project_id, _owner_id):
            return tuple(
                LatestH3Group(uuid4(), index + 1, uuid4(), members, start, end, 1280, 720)
                for index, (members, (start, end)) in enumerate(
                    zip(memberships, boundaries, strict=True)
                )
            )

    class Snapshots:
        def snapshot(self, _project_id, _owner_id):
            return CompositionProjectSnapshot("a" * 64, AspectRatio.LANDSCAPE, 1280, 720)

    with pytest.raises(DomainError):
        LatestFeatureCompositionInputs(Narration(), H3(), Snapshots()).load(project, owner)
