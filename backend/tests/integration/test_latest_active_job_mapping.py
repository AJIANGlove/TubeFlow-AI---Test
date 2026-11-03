from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import String, Uuid, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from tubeflow_test.adapters.persistence.media_repository import SqlMappedActiveJobGate


class Base(DeclarativeBase):
    pass


class LatestH3JobRow(Base):
    __tablename__ = "latest_h3_jobs_fixture"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    state: Mapped[str] = mapped_column(String)


def test_latest_feature_table_mapping_reads_active_jobs_from_fresh_session(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'latest.db'}")
    Base.metadata.create_all(engine)
    project = uuid4()
    with Session(engine) as session, session.begin():
        session.add(LatestH3JobRow(id=str(uuid4()), project_id=project, state="RUNNING"))
    gate = SqlMappedActiveJobGate(
        "h3",
        LatestH3JobRow,
        LatestH3JobRow.project_id,
        LatestH3JobRow.state,
        ("RUNNING", "SUBMITTED", "UNKNOWN"),
    )
    with Session(engine) as restarted_session:
        assert gate.has_active(restarted_session, project)
