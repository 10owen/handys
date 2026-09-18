"""
해석기 프로토콜.

모든 취향 신호는 events 에 원본 그대로 쌓인다. 해석기는 이벤트 하나를 받아
"이 사람의 취향 어휘에 어떤 가중치를 더할 것인가"로 번역한다. 그 결과가 event_interpretations 에 남고,
파생 테이블(taste_profiles · trip_keywords)은 해석 결과의 합이다.

해석기는 두 종류다.
  - rules-v1 : 구조화된 입력(선택지)을 choices.tag_weights 로 번역한다. 지금 동작하는 것.
  - ai-*     : 자유 문장·탈락 맥락·체크아웃 피드백을 읽고, 필요하면 어휘 트리에 새 태그를 제안한다. 이번 프로젝트에서는 인터페이스만.

같은 이벤트에 여러 해석기가 각자 한 행을 남길 수 있다. 어느 해석기를 합산에 쓸지는 ACTIVE_INTERPRETERS 가 정한다.
자세한 규약은 docs/INTERPRETER_PROTOCOL.md
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, Protocol


@dataclass
class TagDelta:
    tag: str            # tags.name. 새 태그면 같은 Interpretation 의 new_tags 에 먼저 선언돼 있어야 한다
    weight: int         # -3 ~ +3
    confidence: float = 1.0   # 0~1. 규칙은 1.0, AI 는 자기 확신. 합산 시 임계값(MIN_CONFIDENCE) 미만은 버린다
    evidence: str = ""  # 무엇을 보고 그렇게 판단했나 (quiz 2:1 / stay tag / note "…")


@dataclass
class NewTag:
    name: str           # "바다뷰/일출" 처럼 부모를 접두로. 사람이 읽는 이름
    parent: str         # 반드시 기존 태그. 분화는 자식 추가로만 한다
    definition: str     # 이 태그를 붙이는 기준 한 줄
    evidence: str = ""


@dataclass
class Interpretation:
    interpreter: str
    tag_deltas: list[TagDelta] = field(default_factory=list)
    new_tags: list[NewTag] = field(default_factory=list)
    rationale: str = ""

    def as_row(self):
        return {"tag_deltas": [asdict(d) for d in self.tag_deltas], "new_tags": [asdict(n) for n in self.new_tags], "rationale": self.rationale}


@dataclass
class Context:
    """해석기에 주는 읽기 전용 맥락. 해석기는 DB 를 직접 만지지 않는다."""
    vocabulary: dict            # tags.name -> {"parent":..., "status":..., "definition":...}
    choices: dict               # (kind, code) -> {"text":..., "tag_weights": {...}}
    stay: Optional[dict] = None       # vote 류 이벤트의 대상 숙소 {id, name, tags:[...]}
    trip: Optional[dict] = None       # {companions, purposes, region, people}
    user_profile: Optional[dict] = None   # 현재 base 프로필 {tag: weight} (AI 가 "이미 아는 취향"을 참고하도록)


class Interpreter(Protocol):
    name: str

    def interpret(self, event: dict, ctx: Context) -> Optional[Interpretation]:
        """event = {id, user_id, trip_id, type, payload}. 해석할 것이 없으면 None."""
        ...


MIN_CONFIDENCE = 0.6
ACTIVE_INTERPRETERS = ["rules-v1", "notes-v1"]   # 합산에 쓰는 해석기. AI 를 붙이면 "ai-claude" 추가 (notes-v1 을 대체)
