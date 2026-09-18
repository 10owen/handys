"""
notes-v1: 표에 남긴 자유 문장("한 줄")을 읽는 규칙 해석기.

  vote.note 를 키워드 사전(LEXICON)으로 훑어 태그 가중치로 바꾼다.
  - 유지(keep) 표의 문장: 좋아한 특징 → +
  - 탈락(drop) 표의 문장: 싫었던 특징 → 그 반대 방향, 아쉬웠던 것("~이 없어서") → 원하는 것 +
  - confidence 0.7 (선택지보다 약한 신호). 새 태그는 제안하지 않는다.

같은 이벤트에 rules-v1 (선택지·숙소 태그) 해석과 나란히 저장되고, 파생 테이블은 둘의 합이다.
AI 해석기가 붙으면 이 자리(자유 문장)를 넘겨받는다. 문장이 없으면 None.
"""
import re
from typing import Optional

from . import Context, Interpretation, TagDelta

NAME = "notes-v1"
CONF = 0.7

# (패턴, 유지 표일 때 가중치, 탈락 표일 때 가중치). "@parent:뷰" = 그 숙소의 뷰 하위 태그.
LEXICON = [
    (r"복잡|시끄|소음|번잡|정신없|사람 많", {}, {"도심상권": -2, "조용함": 1}),
    (r"조용|한적|힐링|여유", {"조용함": 2, "교외조용": 1}, {"조용함": -1}),
    (r"바다|오션|바닷|파도", {"바다뷰": 2, "해변": 1}, {}),
    (r"야경|시티뷰|도시", {"도시야경": 2, "도심상권": 1}, {}),
    (r"(?<!리)뷰|전망|경치", {"@parent:뷰": 2}, {"@parent:뷰": -1}),
    (r"넓|널찍|쾌적", {"넓은객실": 2}, {}),
    (r"좁|답답|작아", {}, {"넓은객실": 2}),
    (r"비싸|비쌈|가격이|부담|돈", {}, {"가성비": 2, "업스케일": -1}),
    (r"저렴|싸|가성비|합리", {"가성비": 2}, {}),
    (r"주방|취사|해먹|요리|밥", {"주방": 2}, {"주방": 1}),
    (r"멀|외지|외진|불편|교통", {}, {"역세권": 2, "도심상권": 1}),
    (r"역|지하철|가까|접근|편리", {"역세권": 2}, {}),
    (r"아이|아기|애기|가족|부모님", {"키즈": 2, "방2개이상": 1}, {"키즈": 1}),
    (r"주차", {"주차": 2}, {"주차": 2}),
    (r"깨끗|깔끔|청결|새것|새거", {"청결": 2}, {}),
    (r"더럽|지저분|냄새|곰팡|낡", {}, {"청결": 2}),
    (r"수영장|풀\b|물놀이", {"수영장": 2}, {}),
    (r"세탁|빨래|건조기", {"세탁기": 2}, {"세탁기": 2}),
    (r"거실|소파|같이 앉", {"거실": 2}, {"거실": 1}),
    (r"방 ?[2두]개|투룸|쓰리룸|각자 방", {"방2개이상": 2}, {"방2개이상": 2}),
    (r"분위기|감성|예쁘|예뻐|인테리어", {"부티크": 2}, {"부티크": -1}),
    (r"고급|럭셔리|호캉스", {"업스케일": 2}, {}),
]
WANT = re.compile(r"없어|없다|없네|없으|부족|아쉬")   # "~이 없어서 아쉬움" → 원하는 것


def _expand(weights, stay, vocab):
    out = {}
    for k, w in weights.items():
        if k.startswith("@parent:"):
            parent = k.split(":", 1)[1]
            for t in (stay or {}).get("tags", []):
                if vocab.get(t, {}).get("parent") == parent:
                    out[t] = out.get(t, 0) + w
        elif k in vocab:
            out[k] = out.get(k, 0) + w
    return out


class NotesV1:
    name = NAME

    def interpret(self, event: dict, ctx: Context) -> Optional[Interpretation]:
        if event["type"] not in ("vote", "checkout_feedback"):
            return None
        note = (event["payload"].get("note") or "").strip()
        if not note:
            return None
        kind = event["payload"].get("kind", "keep")
        want = bool(WANT.search(note))
        acc, hits = {}, []
        for pat, on_keep, on_drop in LEXICON:
            if not re.search(pat, note):
                continue
            weights = on_keep if kind == "keep" else on_drop
            if want and kind == "drop":            # "주방이 없어서" → 원하는 것이므로 + 방향(유지 쪽 사전)으로
                weights = {k: abs(v) for k, v in on_keep.items()} or weights
            for t, w in _expand(weights, ctx.stay, ctx.vocabulary).items():
                acc[t] = acc.get(t, 0) + w
                hits.append(pat.split("|")[0].replace("(?<!리)", ""))
        if not acc:
            return Interpretation(self.name, [], [], f"한 줄 “{note}” 에서 사전에 맞는 표현을 찾지 못함")
        deltas = [TagDelta(tag=t, weight=max(-3, min(3, w)), confidence=CONF, evidence=f"한 줄: “{note}”") for t, w in acc.items() if w]
        return Interpretation(self.name, deltas, [], f"한 줄 “{note}” → {', '.join(f'{d.tag} {d.weight:+d}' for d in deltas)} (표: {'유지' if kind == 'keep' else '탈락'}{', 아쉬움 표현' if want else ''})")
