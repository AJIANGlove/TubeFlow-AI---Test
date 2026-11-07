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


def postgres_connection_options(url: str) -> str:
    """libpq `options` for a PostgreSQL URL: the URL's own options, then the S6 timeouts.

    `connect_args` takes precedence over the URL query in SQLAlchemy, so passing only the
    timeouts silently dropped an `options=-csearch_path=...` given in the URL (2026-11-06:
    integration tests' writes through `Database` landed in `public` instead of their
    temporary schema). With no options in the URL the result is exactly the timeouts.
    """
    raw = make_url(url).query.get("options", ())
    existing = [raw] if isinstance(raw, str) else list(raw)
    timeouts = f"-c lock_timeout={_PG_LOCK_TIMEOUT} -c statement_timeout={_PG_STATEMENT_TIMEOUT}"
    return " ".join([*(item.strip() for item in existing if item.strip()), timeouts])


class Database:
    """Creates one SQLAlchemy unit of work per request/activity."""

    def __init__(self, url: str) -> None:
        options: dict[str, object] = {"pool_pre_ping": True}
        if make_url(url).get_backend_name() == "postgresql":
            options.update(
                pool_size=20,
                max_overflow=20,
                connect_args={"options": postgres_connection_options(url)},
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
