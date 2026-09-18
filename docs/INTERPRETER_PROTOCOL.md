# 해석기 프로토콜 (Interpreter Protocol)

취향 신호를 코드의 if/else 로 해석하면 예외 처리가 끝없이 늘어난다. 그래서 해석을 **교체 가능한 부품**으로 분리한다.
이 문서는 그 부품의 계약이다. 지금 붙어 있는 것은 규칙 해석기(`rules-v1`)뿐이고, AI 해석기(`ai-claude`)는 이 계약만 따르면 언제든 붙일 수 있다.

## 위치

```
events (원본, append-only)
   │  이벤트 하나
   ▼
Interpreter.interpret(event, ctx) ──► Interpretation {tag_deltas, new_tags, rationale}
   │                                          │
   │                                          ├─ new_tags → tags 에 candidate 로 삽입 (어휘가 자란다)
   ▼                                          ▼
event_interpretations (이벤트 × 해석기, 한 행)
   │  활성 해석기의 결과를 confidence 임계값으로 걸러 합산
   ▼
taste_profiles / trip_keywords (파생)
```

원본은 절대 바뀌지 않는다. 해석기를 바꾸면 `POST /api/admin/reinterpret` 가 해석과 파생만 다시 만든다.

## 입력

| 필드 | 내용 |
|---|---|
| `event` | `{id, user_id, trip_id, type, payload}`. type = quiz_answer · vote · vote_removed · candidate_added · trip_created · checkout_feedback |
| `ctx.vocabulary` | 현재 어휘 트리 `{name: {parent, status, definition}}`. merged 제외 |
| `ctx.choices` | 선택지와 고정 가중치 `{(kind, code): {text, tag_weights}}` |
| `ctx.stay` | vote 류 이벤트의 대상 숙소 `{id, name, tags}` |
| `ctx.trip` | `{companions, purposes, region, people}` |
| `ctx.user_profile` | 그 사람의 현재 base 프로필 `{tag: weight}` — "이미 아는 취향"을 참고하라고 준다 |

해석기는 DB 를 읽지도 쓰지도 않는다. 받은 것만 보고 결과만 돌려준다. 그래서 테스트가 쉽고, AI 호출을 붙여도 트랜잭션 밖에 둘 수 있다.

## 출력 `Interpretation`

```json
{
  "tag_deltas": [
    {"tag": "도심상권", "weight": -2, "confidence": 0.9, "evidence": "note: 서면은 너무 복잡해"},
    {"tag": "교외조용", "weight": 1,  "confidence": 0.7, "evidence": "탈락 숙소가 시내형, 남은 후보는 해변형"}
  ],
  "new_tags": [
    {"name": "바다뷰/일출", "parent": "바다뷰", "definition": "동향 객실에서 일출이 보임", "evidence": "checkout note: 아침에 해 뜨는 게 좋았다"}
  ],
  "rationale": "탈락 이유가 소음이므로 도심상권을 내리고 조용함을 올린다. 일출 언급은 기존 바다뷰보다 구체적이라 하위 태그를 제안한다."
}
```

## 규칙

1. **가중치는 −3~+3 정수.** 한 이벤트가 프로필을 뒤집지 못하게.
2. **`tag` 는 어휘에 있거나 같은 출력의 `new_tags` 에 선언돼 있어야 한다.** 아니면 그 항목은 버려진다 (저장 시 검증).
3. **새 태그는 기존 태그의 자식으로만.** 최상위 범주(입지·뷰·공간·등급·편의)는 사람이 만든다. 이름은 `부모/이름` 꼴을 권장.
4. **뜻이 같은 태그가 있으면 새로 만들지 않는다.** 판단이 어려우면 기존 태그를 쓰고 evidence 에 적는다. 나중에 사람이 `merged` 로 정리한다.
5. **`confidence` 가 `MIN_CONFIDENCE`(0.6) 미만이면 합산에서 제외.** 저장은 된다 (감사·재학습용).
6. **멱등.** 같은 (event_id, interpreter) 는 한 행. 다시 돌리면 덮어쓴다.
8. **저장 형식.** 해석기는 태그 *이름*으로 결과를 내고(`Interpretation.tag_deltas[].tag`), 저장할 때 `tags.id` 로 바꿔 `[{"tag_id", "weight", "confidence", "evidence"}]` 로 넣는다. 읽을 때 이름을 붙인다. 해석기가 DB id 를 알 필요가 없게 하려는 것.
7. **선택지는 한 번만.** 구조화된 입력(취향 테스트·이유 선택지)은 `rules-v1` 이 번역한다. AI 해석기는 자유 문장·맥락·피드백만 본다. 같은 신호를 두 번 세지 않는다.

## 어휘 생애주기

| status | 뜻 | 전이 |
|---|---|---|
| `candidate` | 해석기가 제안. 파생에는 반영되지만 화면 필터·마케팅 세그먼트에는 안 씀 | N명 이상에게 붙거나 운영자가 승인 → `active` |
| `active` | 정식 어휘 | 운영자가 정리 → `merged` |
| `merged` | 다른 태그로 흡수 (`merged_into`) | 재계산 시 흡수 대상으로 치환 |

숙소 쪽 태그(`stays.tags`)도 같은 어휘를 쓴다. 새 태그가 `active` 가 되면 운영팀이 숙소에 붙여야 매칭이 성립한다. 매칭할 때 부모로 올려 부분 점수를 주는 것(바다뷰/일출 ↔ 바다뷰)은 다음 단계.

## 지금 구현된 것과 아닌 것

| | rules-v1 | notes-v1 | ai-claude |
|---|---|---|---|
| 상태 | 동작 | 동작 | 인터페이스 + 프롬프트 초안만 (`api/interpreters/ai.py`) |
| 읽는 것 | 선택지 코드, 유지한 숙소의 태그(+1). 탈락은 이유 선택지만(−) | 표의 자유 문장(note). 키워드 사전 22패턴, 유지/탈락 방향, "없어서" 같은 아쉬움 표현 | 자유 문장·맥락·체크아웃 피드백·현재 프로필 |
| 새 태그 제안 | 안 함 | 안 함 | 함 |
| confidence | 1.0 | 0.7 | 모델이 냄 |

notes-v1 은 AI 해석기가 오기 전까지 자유 문장 자리를 맡는 규칙 해석기다. 같은 이벤트에 rules-v1 과 나란히 행을 남기고, 파생은 둘의 합이다.
AI 를 붙일 때는 `ACTIVE_INTERPRETERS` 에서 notes-v1 을 빼고 ai-claude 를 넣은 뒤 `reinterpret` 하면 된다.

AI 해석기를 붙이는 순서: `interpreters/ai.py` 의 `interpret` 구현 → `ACTIVE_INTERPRETERS` 에 추가 → `POST /api/admin/reinterpret`. 스키마·프론트는 바뀌지 않는다.
