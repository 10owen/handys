"""
사전 로드 · 이벤트 해석 저장 · 파생 테이블 재계산 · 추천 점수.

흐름:  events (원본)  →  interpreters (rules-v1 | ai-*)  →  event_interpretations (해석)  →  taste_profiles / trip_keywords (파생)
파생은 해석 결과의 합이다. 해석기가 바뀌면 reinterpret_all → recompute_all 로 해석과 파생만 다시 만든다. 원본은 손대지 않는다.
"""
import json
from collections import defaultdict

from db import q, q1, x, xmany
from interpreters import ACTIVE_INTERPRETERS, Context, Interpretation, MIN_CONFIDENCE
from interpreters.rules_v1 import RulesV1
from interpreters.notes_v1 import NotesV1

INTERPRETERS = {RulesV1.name: RulesV1(), NotesV1.name: NotesV1()}   # 활성 해석기 인스턴스. AI 를 붙이면 여기에 추가


def jload(v):
    return json.loads(v) if isinstance(v, str) else v


# ───────── 사전 ─────────

def load_vocab(c):
    return {r["name"]: {"id": r["id"], "parent": r["parent"], "status": r["status"], "definition": r["definition"]}
            for r in q(c, "SELECT id, name, parent, status, definition FROM tags WHERE status <> 'merged'")}


def tag_id_map(c):
    """{id: name} — 저장된 tag_id 를 이름으로 풀 때"""
    return {r["id"]: r["name"] for r in q(c, "SELECT id, name FROM tags")}


def load_choices(c):
    """choices.tag_weights 는 [{"tag_id":12,"score":3}] / [{"parent_id":2,"score":2}] 형식(tags.id 참조).
    해석기는 태그 이름으로 일하므로 여기서 {name: score} / {"@parent:<name>": score} 로 풀어 준다."""
    id2name = {r["id"]: r["name"] for r in q(c, "SELECT id, name FROM tags")}
    def unpack(tw):
        tw = jload(tw) or []
        if isinstance(tw, dict):          # 구형식 호환
            return tw
        out = {}
        for e in tw:
            if "tag_id" in e and e["tag_id"] in id2name:
                out[id2name[e["tag_id"]]] = out.get(id2name[e["tag_id"]], 0) + e["score"]
            elif "parent_id" in e and e["parent_id"] in id2name:
                k = "@parent:" + id2name[e["parent_id"]]
                out[k] = out.get(k, 0) + e["score"]
        return out
    return {(r["kind"], r["code"]): {"text": r["text"], "tag_weights": unpack(r["tag_weights"]), "parent_code": r["parent_code"], "ord": r["ord"]}
            for r in q(c, "SELECT kind, code, parent_code, ord, text, tag_weights FROM choices")}


def load_stays(c):
    rows = q(c, "SELECT id, name, brand, is_handys, region, address, lat, lng, capacity, price_tier, min_price, rating, review_count, tags, amenities, room_types, review_keywords, description, source_url FROM stays ORDER BY id")
    out = {}
    for r in rows:
        for k in ("tags", "amenities", "room_types", "review_keywords"):
            r[k] = jload(r[k]) or []
        r["rating"] = float(r["rating"]) if r["rating"] is not None else None
        r["lat"] = float(r["lat"]) if r["lat"] is not None else None
        r["lng"] = float(r["lng"]) if r["lng"] is not None else None
        out[r["id"]] = r
    return out


def _add(acc, tag, w):
    if w:
        acc[tag] = acc.get(tag, 0) + w


def ctx_weights(choices, companions, purposes):
    """여행 컨텍스트가 미는 것. 저장하지 않고 읽을 때 계산한다 (사람의 취향이 아니라 이번 여행의 조건이므로)."""
    w = {}
    for k, v in (choices.get(("companion", companions), {}).get("tag_weights") or {}).items():
        _add(w, k, v)
    for p in purposes:
        for k, v in (choices.get(("purpose", p), {}).get("tag_weights") or {}).items():
            _add(w, k, v)
    return w


# ───────── 해석 ─────────

def _load_event(c, event_id):
    e = q1(c, "SELECT id, user_id, trip_id, type, payload FROM events WHERE id=%s", (event_id,))
    e["payload"] = jload(e["payload"])
    return e


def _context_for(c, e, vocab, choices, stays):
    stay = stays.get((e["payload"] or {}).get("stay_id")) if e["type"] in ("vote", "checkout_feedback", "candidate_added", "like", "comment") else None
    trip = None
    if e["trip_id"]:
        t = q1(c, "SELECT companions, purposes, region, people FROM trips WHERE id=%s", (e["trip_id"],))
        if t:
            t["purposes"] = jload(t["purposes"])
            trip = t
    prof = {r["tag"]: r["weight"] for r in q(c, "SELECT tag, weight FROM taste_profiles WHERE user_id=%s AND context='base'", (e["user_id"],))}
    return Context(vocabulary=vocab, choices=choices, stay=stay, trip=trip, user_profile=prof)


def store_interpretation(c, event_id, interp: Interpretation, vocab):
    """해석 결과 저장. new_tags 는 어휘 트리에 candidate 로 넣는다. 어휘에 없는 tag 를 가리키는 delta 는 버린다."""
    for nt in interp.new_tags:
        if nt.name not in vocab and nt.parent in vocab:
            x(c, "INSERT IGNORE INTO tags (name, parent, status, source, definition) VALUES (%s,%s,'candidate','ai',%s)", (nt.name, nt.parent, nt.definition))
            vocab[nt.name] = {"parent": nt.parent, "status": "candidate", "definition": nt.definition}
    interp.tag_deltas = [d for d in interp.tag_deltas if d.tag in vocab]
    if any(v.get("id") is None for v in vocab.values()):   # 방금 candidate 로 넣은 태그의 id 보충
        for r in q(c, "SELECT id, name FROM tags"):
            if r["name"] in vocab:
                vocab[r["name"]]["id"] = r["id"]
    row = interp.as_row()
    # 저장 형식은 tags.id 참조: [{"tag_id":7,"weight":-2,"confidence":1.0,"evidence":"..."}]
    deltas_db = [{"tag_id": vocab[d["tag"]]["id"], "weight": d["weight"], "confidence": d["confidence"], "evidence": d["evidence"]} for d in row["tag_deltas"]]
    x(c, "INSERT INTO event_interpretations (event_id, interpreter, tag_deltas, new_tags, rationale) VALUES (%s,%s,%s,%s,%s) "
         "ON DUPLICATE KEY UPDATE tag_deltas=VALUES(tag_deltas), new_tags=VALUES(new_tags), rationale=VALUES(rationale)",
      (event_id, interp.interpreter, json.dumps(deltas_db, ensure_ascii=False), json.dumps(row["new_tags"], ensure_ascii=False), row["rationale"]))


def interpret_event(c, event_id, vocab=None, choices=None, stays=None):
    """활성 해석기 전부를 이벤트 하나에 돌려 저장한다."""
    vocab = vocab or load_vocab(c)
    choices = choices or load_choices(c)
    stays = stays or load_stays(c)
    e = _load_event(c, event_id)
    ctx = _context_for(c, e, vocab, choices, stays)
    for name in ACTIVE_INTERPRETERS:
        it = INTERPRETERS.get(name)
        if not it:
            continue
        interp = it.interpret(e, ctx)
        if interp:
            store_interpretation(c, event_id, interp, vocab)


def deltas_of(c, event_ids):
    """{event_id: {tag: weight}} — 활성 해석기의 해석을 confidence 임계값으로 걸러 합친다."""
    out = defaultdict(dict)
    if not event_ids:
        return out
    ph = ",".join(["%s"] * len(event_ids))
    ph2 = ",".join(["%s"] * len(ACTIVE_INTERPRETERS))
    names = tag_id_map(c)
    for r in q(c, f"SELECT event_id, tag_deltas FROM event_interpretations WHERE event_id IN ({ph}) AND interpreter IN ({ph2})", tuple(event_ids) + tuple(ACTIVE_INTERPRETERS)):
        for d in jload(r["tag_deltas"]):
            name = names.get(d.get("tag_id")) if "tag_id" in d else d.get("tag")   # 구형식(tag 이름) 호환
            if name and d.get("confidence", 1.0) >= MIN_CONFIDENCE:
                _add(out[r["event_id"]], name, d["weight"])
    return out


def deltas_with_names(c, tag_deltas):
    """저장된 tag_deltas 에 이름을 붙여 돌려준다 (조회·표시용)"""
    names = tag_id_map(c)
    out = []
    for d in jload(tag_deltas) or []:
        d = dict(d)
        d["tag"] = names.get(d["tag_id"]) if "tag_id" in d else d.get("tag")
        out.append(d)
    return out


# ───────── 원본에서 현재 상태 복원 ─────────

def current_votes(c, trip_id):
    """vote / vote_removed 를 순서대로 접어 현재 표. {(user_id, stay_id): {event_id, kind, reason, note}}"""
    votes = {}
    for r in q(c, "SELECT id, user_id, type, payload FROM events WHERE trip_id=%s AND type IN ('vote','vote_removed') ORDER BY id", (trip_id,)):
        p = jload(r["payload"])
        key = (r["user_id"], p["stay_id"])
        if r["type"] == "vote":
            votes[key] = {"event_id": r["id"], "kind": p["kind"], "reason": p.get("reason"), "note": p.get("note", "")}
        else:
            votes.pop(key, None)
    return votes


def current_likes(c, trip_id):
    """like / unlike 를 접어 현재 좋아요. {(user_id, stay_id): event_id}"""
    likes = {}
    for r in q(c, "SELECT id, user_id, type, payload FROM events WHERE trip_id=%s AND type IN ('like','unlike') ORDER BY id", (trip_id,)):
        key = (r["user_id"], jload(r["payload"])["stay_id"])
        if r["type"] == "like":
            likes[key] = r["id"]
        else:
            likes.pop(key, None)
    return likes


def latest_quiz_event(c, user_id):
    r = q1(c, "SELECT id FROM events WHERE user_id=%s AND type='quiz_answer' ORDER BY id DESC LIMIT 1", (user_id,))
    return r["id"] if r else None


# ───────── 파생 재계산 ─────────

INTERP_LABEL = "+".join(ACTIVE_INTERPRETERS)


def _replace_profile(c, user_id, context, weights):
    x(c, "DELETE FROM taste_profiles WHERE user_id=%s AND context=%s", (user_id, context))
    xmany(c, "INSERT INTO taste_profiles (user_id, context, tag, weight, interpreter) VALUES (%s,%s,%s,%s,%s)",
          [(user_id, context, t, w, INTERP_LABEL) for t, w in weights.items() if w])


def recompute_user(c, user_id):
    """base = 최신 취향 테스트의 해석. 동행유형별 = 그 유형 여행에서 낸 현재 표들의 해석 합."""
    qe = latest_quiz_event(c, user_id)
    _replace_profile(c, user_id, "base", deltas_of(c, [qe]).get(qe, {}) if qe else {})

    by_ctx = defaultdict(dict)
    for t in q(c, "SELECT t.id, t.companions FROM trips t JOIN trip_members m ON m.trip_id=t.id WHERE m.user_id=%s", (user_id,)):
        mine = [v["event_id"] for (uid, _), v in current_votes(c, t["id"]).items() if uid == user_id] + \
               [eid for (uid, _), eid in current_likes(c, t["id"]).items() if uid == user_id]
        for eid, ws in deltas_of(c, mine).items():
            for tag, w in ws.items():
                _add(by_ctx[t["companions"]], tag, w)
    for ctx in [r["code"] for r in q(c, "SELECT code FROM choices WHERE kind='companion'")]:
        _replace_profile(c, user_id, ctx, by_ctx.get(ctx, {}))


def recompute_trip(c, trip_id):
    votes = current_votes(c, trip_id)
    likes = current_likes(c, trip_id)
    d = deltas_of(c, [v["event_id"] for v in votes.values()] + list(likes.values()))
    per_member, total = defaultdict(dict), {}
    for (uid, _), v in votes.items():
        for tag, w in d.get(v["event_id"], {}).items():
            _add(per_member[uid], tag, w)
            _add(total, tag, w)
    for (uid, _), eid in likes.items():
        for tag, w in d.get(eid, {}).items():
            _add(per_member[uid], tag, w)
            _add(total, tag, w)
    x(c, "DELETE FROM trip_keywords WHERE trip_id=%s", (trip_id,))
    rows = [(trip_id, "trip", 0, t, w, INTERP_LABEL) for t, w in total.items() if w]
    for uid, ws in per_member.items():
        rows += [(trip_id, "member", uid, t, w, INTERP_LABEL) for t, w in ws.items() if w]
    xmany(c, "INSERT INTO trip_keywords (trip_id, scope, user_id, tag, weight, interpreter) VALUES (%s,%s,%s,%s,%s,%s)", rows)


def reinterpret_all(c):
    vocab, choices, stays = load_vocab(c), load_choices(c), load_stays(c)
    for e in q(c, "SELECT id FROM events ORDER BY id"):
        interpret_event(c, e["id"], vocab, choices, stays)


def recompute_all(c):
    for u in q(c, "SELECT id FROM users"):
        recompute_user(c, u["id"])
    for t in q(c, "SELECT id FROM trips"):
        recompute_trip(c, t["id"])


# ───────── 추천 점수 ─────────

def profiles_for(c, user_ids):
    out = defaultdict(lambda: defaultdict(dict))
    if not user_ids:
        return out
    ph = ",".join(["%s"] * len(user_ids))
    for r in q(c, f"SELECT user_id, context, tag, weight FROM taste_profiles WHERE user_id IN ({ph})", tuple(user_ids)):
        out[r["user_id"]][r["context"]][r["tag"]] = r["weight"]
    return out


def trip_member_keywords(c, trip_id):
    out = defaultdict(dict)
    for r in q(c, "SELECT user_id, tag, weight FROM trip_keywords WHERE trip_id=%s AND scope='member'", (trip_id,)):
        out[r["user_id"]][r["tag"]] = r["weight"]
    return out


def score_stay(stay, members, profiles, member_kw, companions, ctx_w):
    """점수 = Σ멤버(base + 이전여행 학습 + 이번 선별 학습) + 여행 컨텍스트. parts 가 근거."""
    parts = []
    for m in members:
        uid, name = m["id"], m["name"]
        base = profiles[uid].get("base", {})
        learned_all = profiles[uid].get(companions, {})
        learned_now = member_kw.get(uid, {})
        for t in stay["tags"]:
            if base.get(t):
                parts.append({"src": name, "kind": "base", "tag": t, "v": base[t]})
            past = learned_all.get(t, 0) - learned_now.get(t, 0)
            if past:
                parts.append({"src": f"{name}·이전여행", "kind": "past", "tag": t, "v": past})
            if learned_now.get(t):
                parts.append({"src": f"{name}·이번선별", "kind": "learn", "tag": t, "v": learned_now[t]})
    for t in stay["tags"]:
        if ctx_w.get(t):
            parts.append({"src": "여행목적", "kind": "ctx", "tag": t, "v": ctx_w[t]})
    return {"total": sum(p["v"] for p in parts), "parts": parts}


def eligible(stay, trip):
    return (trip["region"] == "무관" or stay["region"] == trip["region"]) and stay["capacity"] >= (trip["people"] or 1)
