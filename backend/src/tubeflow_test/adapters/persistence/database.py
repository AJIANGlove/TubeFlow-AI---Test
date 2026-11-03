from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# S6：PostgreSQL 上的硬性上限，避免一次锁等待把 activity 拖满 start_to_close。
# 15s 远大于正常事务耗时（毫秒级），又远小于 activity 的 10 分钟超时。
_PG_LOCK_TIMEOUT = "15s"
_PG_STATEMENT_TIMEOUT = "60s"


class Database:
    """Creates one SQLAlchemy unit of work per request/activity."""

    def __init__(self, url: str) -> None:
        options: dict[str, object] = {"pool_pre_ping": True}
        if make_url(url).get_backend_name() == "postgresql":
            options.update(
                pool_size=20,
                max_overflow=20,
                connect_args={
                    "options": (
                        f"-c lock_timeout={_PG_LOCK_TIMEOUT} "
                        f"-c statement_timeout={_PG_STATEMENT_TIMEOUT}"
                    )
                },
            )
        self.engine: Engine = create_engine(url, **options)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def unit_of_work(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def session(self) -> Session:
        return self._sessions()
