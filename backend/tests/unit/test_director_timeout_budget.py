"""导演调用超时预算的三处联动守护（见 runtime/operations/director_timeout_fix_plan_20261013.md）。

不变式：provider HTTP 超时（TUBEFLOW_DIRECTOR_PROVIDER_TIMEOUT_SECONDS，目标 900s）
        < 单元租约（PROJECT_DIRECTOR_UNIT_LEASE）
        ≤ 活动超时（DIRECTOR_CALL_ACTIVITY_TIMEOUT，含退避余量）。
任一处被单独改小都会触发本测试失败，防止"只改一处"的回归。
"""

from datetime import timedelta

from tubeflow_test.application.project_director import PROJECT_DIRECTOR_UNIT_LEASE
from tubeflow_test.workflows.storyboards import DIRECTOR_CALL_ACTIVITY_TIMEOUT

_PROVIDER_TIMEOUT_BUDGET = timedelta(seconds=900)


def test_unit_lease_covers_provider_timeout_budget() -> None:
    assert PROJECT_DIRECTOR_UNIT_LEASE > _PROVIDER_TIMEOUT_BUDGET


def test_activity_timeout_covers_provider_timeout_budget() -> None:
    assert DIRECTOR_CALL_ACTIVITY_TIMEOUT > _PROVIDER_TIMEOUT_BUDGET
