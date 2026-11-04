"""Create the short test project and run the local preflight (no provider calls).

Step 1 of SHORT_PROJECT_TEST (2026-11-04): project creation is local persistence;
preflight uses segment_pasted_script only (deterministic, no LLM).
Stops before narration confirmation (which would start AUTO).
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.identity_project.repository import (
    AccountRow,
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.domain.narration import segment_pasted_script
from tubeflow_test.identity_project.adapters import Argon2PasswordHasher
from tubeflow_test.config import Settings
from tubeflow_test.domain.common import AspectRatio, SupportedLocale
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.identity_project.service import IdentityProjectService

CHANNEL_ID = UUID("f28a0845-6ddf-4111-ae2d-f5de595a5fd6")
TITLE = "사장님 없는 무인 아이스크림 가게, 정말 돈이 될까?"
SCRIPT = """밤 11시, 골목 끝에 불이 켜진 작은 가게가 있습니다.
문을 열고 들어가도 반겨 주는 직원은 아무도 없습니다.
냉동고 몇 대만 조용히 윙윙 돌아가고 있죠.
손님은 아이스크림을 고르고, 키오스크에 카드를 대고, 그냥 나갑니다.
사장님 없이 돌아가는 이 가게, 정말 돈이 될까요?
---
오늘은 무인 아이스크림 가게 하나를 예로 들어, 돈이 들어오고 나가는 길을 따라가 보겠습니다.
아래 숫자는 이해를 돕기 위한 가정이고, 실제 가게마다 크게 다릅니다.
먼저 들어오는 돈부터 보겠습니다.
하루 손님이 백 명, 한 명이 평균 삼천 원어치를 산다고 가정해 보죠.
그러면 하루 매출은 삼십만 원, 한 달이면 약 구백만 원입니다.
숫자만 보면 꽤 괜찮아 보입니다.
---
하지만 아이스크림은 공짜로 들어오지 않습니다.
사장님은 도매로 물건을 사 와서 냉동고를 채워야 합니다.
할인 경쟁이 심한 상품이라 남는 몫은 생각보다 얇습니다.
여기에 월세가 붙고, 하루 종일 쉬지 않는 냉동고의 전기 요금이 붙습니다.
키오스크와 CCTV 사용료도 매달 빠져나갑니다.
그리고 무인 가게만의 비용이 하나 더 있습니다.
계산하지 않고 나가는 손님, 바로 도난입니다.
사장님은 밤마다 CCTV 화면을 돌려 보며 사라진 물건을 확인합니다.
---
그렇다면 정말 사장님 없이 돌아갈까요?
이른 아침, 사장님은 상자를 들고 가게 문을 엽니다.
빈 칸을 채우고, 유통기한을 확인하고, 바닥을 닦습니다.
냉동고 온도가 조금만 올라가도 아이스크림은 금세 녹아 버립니다.
그래서 휴대폰으로 온도 알림을 받으며 늘 신경을 씁니다.
무인 가게는 사람이 없는 가게가 아니라, 사장님이 보이지 않는 가게입니다.
---
계절도 큰 변수입니다.
한여름에는 저녁마다 손님이 줄을 섭니다.
하지만 찬 바람이 불면 가게 안이 텅 비는 날이 많아집니다.
그런데도 냉동고 전기 요금과 월세는 그대로 나갑니다.
게다가 장사가 잘되는 골목에는 비슷한 가게가 금세 생기기 쉽습니다.
손님은 둘로 나뉘고, 가격 경쟁은 더 치열해집니다.
---
그럼 어떤 가게가 살아남을까요?
학교와 아파트 단지 사이처럼 사람이 매일 지나다니는 자리가 먼저입니다.
아이스크림만이 아니라 과자와 음료를 함께 두어 겨울 매출을 버티기도 합니다.
그리고 도난과 재고를 꼼꼼히 관리하는 사장님이 결국 차이를 만듭니다.
정리하면, 무인 아이스크림 가게는 쉬운 부업처럼 보이지만 손이 꽤 많이 가는 장사입니다.
매출보다 중요한 것은 원가, 월세, 전기 요금, 그리고 사라지는 물건까지 뺀 진짜 남는 돈입니다.
여러분 동네의 무인 가게는 어떤가요?
댓글로 이야기해 주세요."""

settings = Settings()
database = Database(settings.database_url)
with database.unit_of_work() as session:
    channel = session.execute(
        text(
            "select owner_id, default_voice_version_id, default_character_image_ids, "
            "default_tts_provider, default_cloned_voice_id, default_visual_style, name "
            "from channels where id = :c"
        ),
        {"c": CHANNEL_ID},
    ).one()
    account_row = session.get(AccountRow, channel.owner_id)
    assert account_row is not None
    owner = Account(
        account_row.id,
        account_row.username,
        account_row.password_hash,
        AccountRole(account_row.role),
        account_row.active,
        account_row.session_generation,
    )
    service = IdentityProjectService(
        SqlAlchemyIdentityProjectRepository(session), Argon2PasswordHasher()
    )
    image_ids = tuple(UUID(str(i)) for i in (channel.default_character_image_ids or []))
    project = service.create_project(
        owner,
        channel_id=CHANNEL_ID,
        script=SCRIPT,
        image_ids=image_ids,
        locale=SupportedLocale.KO_KR,
        aspect_ratio=AspectRatio.LANDSCAPE,
        title=TITLE,
    )

# Local preflight only: deterministic segmentation, no provider call.

version = segment_pasted_script(SCRIPT, SupportedLocale.KO_KR)
all_pieces = version.sentences
separators = [s for s in all_pieces if s.text.strip().strip("-") == ""]
spoken = [s for s in all_pieces if s not in separators]
longest = sorted(spoken, key=lambda s: len(s.text), reverse=True)[:5]
result = {
    "project_id": str(project.id),
    "title": project.title,
    "locale": project.locale.value,
    "production_mode": project.production_mode,
    "channel_name": channel.name,
    "channel_tts_provider": channel.default_tts_provider,
    "channel_voice_version_id": str(channel.default_voice_version_id)
    if channel.default_voice_version_id
    else None,
    "channel_cloned_voice_id": str(channel.default_cloned_voice_id)
    if channel.default_cloned_voice_id
    else None,
    "channel_character_image_ids": [str(i) for i in image_ids],
    "channel_visual_style": channel.default_visual_style,
    "rule_version": version.rule_version,
    "total_pieces": len(all_pieces),
    "spoken_sentences": len(spoken),
    "separator_lines": len(separators),
    "longest_sentences": [
        {"sequence": s.sequence, "chars": len(s.text), "text": s.text[:60]} for s in longest
    ],
    "expected_spoken_sentences": 39,
}
database.engine.dispose()
print(json.dumps(result, ensure_ascii=False, indent=2))
