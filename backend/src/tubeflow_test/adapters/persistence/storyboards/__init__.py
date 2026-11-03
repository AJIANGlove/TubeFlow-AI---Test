"""Feature-local PostgreSQL persistence for director plans and grid storyboards."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .repository import SqlAlchemyStoryboardRepository

__all__ = ["SqlAlchemyStoryboardRepository"]


def __getattr__(name: str) -> Any:
    if name == "SqlAlchemyStoryboardRepository":
        from .repository import SqlAlchemyStoryboardRepository

        return SqlAlchemyStoryboardRepository
    raise AttributeError(name)
