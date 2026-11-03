from __future__ import annotations


def raise_lifecycle_errors(
    message: str,
    primary: BaseException | None,
    cleanup_errors: list[BaseException],
) -> None:
    if primary is not None and cleanup_errors:
        raise BaseExceptionGroup(message, [primary, *cleanup_errors])
    if primary is not None:
        raise primary
    if cleanup_errors:
        raise BaseExceptionGroup(message, cleanup_errors)
