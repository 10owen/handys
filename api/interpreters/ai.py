"""
ai-claude: 자유 문장과 맥락을 읽는 해석기. **이번 프로젝트에서는 구현하지 않는다.** 인터페이스와 프롬프트 규약만 둔다.

무엇을 하는가
  - vote 의 note("서면은 너무 복잡해"), checkout_feedback 의 note("작년 산보다 이번 바다가 좋았다. 아침에 조용해서")를 읽는다
  - 탈락한 숙소의 특징과 남은 숙소의 특징 차이를 보고 "이 사람이 실제로 피한 것/원한 것"을 태그 가중치로 낸다
  - 기존 어휘로 표현이 안 되면 새 태그를 제안한다. 반드시 기존 태그의 자식으로 (예: 바다뷰 → "바다뷰/일출")
  - 각 가중치에 confidence 를 붙인다. 합산은 MIN_CONFIDENCE 이상만 쓴다

무엇을 하지 않는가
  - DB 를 만지지 않는다. Interpretation 만 돌려주고, 저장·태그 생성은 scoring.store_interpretation 이 한다
  - 선택지 가중치를 다시 계산하지 않는다. 그건 rules-v1 이 이미 했다. 같은 이벤트에 두 해석이 나란히 남는다

규약 전문: docs/INTERPRETER_PROTOCOL.md
"""
from typing import Optional

from . import Context, Interpretation

NAME = "ai-claude"
ENABLED = False

PROMPT_TEMPLATE = """당신은 숙박 취향 해석기입니다. 아래 이벤트 하나를 읽고 JSON 만 출력하세요.

[취향 어휘 트리]  (name: parent — definition)
{vocabulary}

[이벤트]
type: {type}
payload: {payload}
대상 숙소: {stay}
여행 맥락: {trip}
이 사람의 현재 취향(base): {user_profile}

[출력 형식]
{{"tag_deltas": [{{"tag": "<기존 태그 또는 new_tags 의 name>", "weight": -3..3, "confidence": 0..1, "evidence": "<근거 한 줄>"}}],
  "new_tags": [{{"name": "<부모/이름>", "parent": "<기존 태그>", "definition": "<붙이는 기준>", "evidence": "<근거>"}}],
  "rationale": "<사람이 읽는 두 문장>"}}

[규칙]
1. 자유 문장에 근거가 없는 가중치는 내지 마세요. 선택지는 이미 다른 해석기가 처리했습니다.
2. 새 태그는 기존 태그의 자식으로만. 최상위 범주를 만들지 마세요. 이미 있는 태그와 뜻이 같으면 새로 만들지 말고 그 태그를 쓰세요.
3. weight 는 -3~3 정수. confidence 가 0.6 미만이면 그 항목은 합산에서 버려집니다.
4. tag_deltas 의 tag 는 어휘 트리에 있거나 new_tags 에 선언돼 있어야 합니다.
"""


class AIClaude:
    name = NAME

    def interpret(self, event: dict, ctx: Context) -> Optional[Interpretation]:
        raise NotImplementedError("ai-claude 해석기는 이번 범위 밖. docs/INTERPRETER_PROTOCOL.md 참고")
