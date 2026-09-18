"""
rules-v1: 구조화된 입력만 해석한다.

  quiz_answer  → 선택지(choices quiz_opt).tag_weights
  vote(keep)   → 숙소 태그 전부에 +1 (남긴 숙소의 특징은 드러난 선호), 그리고 이유 선택지(reason_keep).tag_weights
  vote(drop)   → 이유 선택지(reason_drop).tag_weights 만. 숙소 특징 전체에 −1 을 주지 않는다 (탈락엔 이유가 있고, 그 이유만 신호다)
  그 외        → None (trip_created 의 컨텍스트 가중치는 읽을 때 계산하고 저장하지 않는다)

자유 문장(note)·체크아웃 피드백은 읽지 않는다. 그건 AI 해석기의 몫이다. 새 태그를 제안하지도 않는다.
"@parent:뷰" 표기 = 그 숙소의 태그 중 부모가 '뷰'인 것에 적용.
"""
from typing import Optional

from . import Context, Interpretation, TagDelta

NAME = "rules-v1"


def _expand(weights: dict, stay: Optional[dict], vocab: dict, evidence: str) -> list[TagDelta]:
    out = []
    for key, w in (weights or {}).items():
        if not w:
            continue
        if key.startswith("@parent:"):
            parent = key.split(":", 1)[1]
            for t in (stay or {}).get("tags", []):
                if vocab.get(t, {}).get("parent") == parent:
                    out.append(TagDelta(tag=t, weight=w, evidence=f"{evidence} → {parent} 하위 태그"))
        elif key in vocab:
            out.append(TagDelta(tag=key, weight=w, evidence=evidence))
    return out


class RulesV1:
    name = NAME

    def interpret(self, event: dict, ctx: Context) -> Optional[Interpretation]:
        p = event["payload"]
        t = event["type"]

        if t == "quiz_answer":
            deltas = []
            for code in p.get("answers", []):
                ch = ctx.choices.get(("quiz_opt", code))
                if ch:
                    deltas += _expand(ch.get("tag_weights"), None, ctx.vocabulary, f"quiz {code}")
            return Interpretation(self.name, deltas, [], f"취향 테스트 {len(p.get('answers', []))}문항을 선택지 가중치로 번역")

        if t == "vote" and ctx.stay:
            sign = 1 if p["kind"] == "keep" else -1
            # 유지: 남긴 숙소의 특징 전부 +1. 탈락: 특징 전체를 −로 잡지 않는다 — 이유에서 나온 키워드만.
            deltas = [TagDelta(tag=tag, weight=1, evidence="유지한 숙소의 특징") for tag in ctx.stay["tags"] if tag in ctx.vocabulary] if sign > 0 else []
            kind = "reason_keep" if sign > 0 else "reason_drop"
            ch = ctx.choices.get((kind, p.get("reason") or ""))
            if ch:
                deltas += _expand(ch.get("tag_weights"), ctx.stay, ctx.vocabulary, f"이유 '{ch['text']}'")
            note = (p.get("note") or "").strip()
            rat = (f"숙소 특징 {len(ctx.stay['tags'])}개에 +1, 이유 선택지 가중치 반영." if sign > 0
                   else ("탈락: 이유 선택지의 키워드만 반영 (숙소 특징 전체를 −로 잡지 않음)." if ch else "탈락: 이유가 없어 학습 신호 없음."))
            if note:
                rat += f" 자유 문장 “{note}” 은 notes-v1 이 따로 해석."
            return Interpretation(self.name, deltas, [], rat)

        if t == "like" and ctx.stay:
            deltas = [TagDelta(tag=tag, weight=1, evidence="좋아요 누른 숙소의 특징") for tag in ctx.stay["tags"] if tag in ctx.vocabulary]
            return Interpretation(self.name, deltas, [], f"좋아요: 숙소 특징 {len(deltas)}개에 +1 (유지 표보다 약한 신호)")

        return None
