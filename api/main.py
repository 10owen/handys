import json
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import scoring as S
from db import conn, q, q1, x, xmany
from interpreters import ACTIVE_INTERPRETERS

app = FastAPI(title="스테이메이트 API", version="0.2", docs_url="/docs", openapi_url="/openapi.json")
LIKE_BONUS = 5   # 여행 멤버 한 명의 좋아요 = 그 여행에서 +5


def _event(c, user_id, trip_id, type_, payload):
    """원본에 한 줄 append → 활성 해석기 실행 → 해석 저장. 파생 재계산은 호출자가."""
    eid = x(c, "INSERT INTO events (user_id, trip_id, type, payload) VALUES (%s,%s,%s,%s)", (user_id, trip_id, type_, json.dumps(payload, ensure_ascii=False)))
    S.interpret_event(c, eid)
    return eid


# ───────── 사전·메타 ─────────

@app.get("/health")
def health():
    with conn() as c:
        q1(c, "SELECT 1")
    return {"ok": True}


@app.get("/meta")
def meta():
    """프론트가 한 번 받아 두는 사전."""
    with conn() as c:
        ch = q(c, "SELECT kind, code, parent_code, ord, text, image FROM choices ORDER BY kind, ord")
        by = lambda k: [r for r in ch if r["kind"] == k]
        quiz = [{"code": qq["code"], "ord": qq["ord"], "text": qq["text"], "options": [{"code": o["code"], "text": o["text"], "image": o["image"]} for o in by("quiz_opt") if o["parent_code"] == qq["code"]]} for qq in by("quiz_q")]
        reasons = [{"id": r["code"], "vote_kind": "keep", "text": r["text"]} for r in by("reason_keep")] + [{"id": r["code"], "vote_kind": "drop", "text": r["text"]} for r in by("reason_drop")]
        return {
            "interpreters": ACTIVE_INTERPRETERS,
            "tags": q(c, "SELECT id, name, parent, status, source FROM tags ORDER BY parent, name"),
            "stays": list(S.load_stays(c).values()),
            "quiz": quiz,
            "companions": [r["code"] for r in by("companion")],
            "purposes": [r["code"] for r in by("purpose")],
            "regions": [r["code"] for r in by("region")],
            "reasons": reasons,
        }


@app.get("/schema")
def schema():
    """리뷰용: 테이블·컬럼·코멘트를 information_schema 에서 읽는다."""
    with conn() as c:
        tables = q(c, "SELECT TABLE_NAME AS name, TABLE_COMMENT AS comment FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME")
        cols = q(c, "SELECT TABLE_NAME AS t, COLUMN_NAME AS name, COLUMN_TYPE AS type, IS_NULLABLE AS nullable, COLUMN_KEY AS `key`, COLUMN_COMMENT AS comment FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME, ORDINAL_POSITION")
        fks = q(c, "SELECT TABLE_NAME AS t, COLUMN_NAME AS col, REFERENCED_TABLE_NAME AS ref_t, REFERENCED_COLUMN_NAME AS ref_col FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY TABLE_NAME")
        for t in tables:
            t["columns"] = [col for col in cols if col["t"] == t["name"]]
            t["fks"] = [f for f in fks if f["t"] == t["name"]]
            t["count"] = q1(c, f"SELECT COUNT(*) AS n FROM `{t['name']}`")["n"]
        return tables


@app.get("/tags")
def tags():
    """어휘 트리. candidate 포함."""
    with conn() as c:
        return q(c, "SELECT id, name, parent, status, merged_into, source, definition, created_at FROM tags ORDER BY parent, name")


# ───────── 유저 ─────────

class UserIn(BaseModel):
    name: str


class QuizIn(BaseModel):
    answers: list[str]  # 선택지 코드 "문항:선택지", 문항 순서대로


USER_COLS = "id, name, nickname, joined_at, created_at"


@app.get("/users")
def users():
    with conn() as c:
        return q(c, f"SELECT {USER_COLS} FROM users ORDER BY created_at")


@app.post("/users")
def create_user(u: UserIn):
    """취향 테스트 링크로 들어온 사람. 이름만 받는다. 가입 전까지는 게스트(nickname NULL)."""
    with conn() as c:
        uid = x(c, "INSERT INTO users (name) VALUES (%s)", (u.name,))
        return q1(c, f"SELECT {USER_COLS} FROM users WHERE id=%s", (uid,))


class SignupIn(BaseModel):
    nickname: str


@app.post("/users/{uid}/signup")
def signup(uid: int, body: SignupIn):
    """가입: 닉네임 확정. 기본값은 테스트 때 입력한 이름이고 여기서 바꿀 수 있다. 이미 가입했으면 닉네임 변경."""
    nick = body.nickname.strip()
    if not nick:
        raise HTTPException(400, "nickname")
    with conn() as c:
        if not q1(c, "SELECT id FROM users WHERE id=%s", (uid,)):
            raise HTTPException(404, "user")
        x(c, "UPDATE users SET nickname=%s, joined_at=COALESCE(joined_at, NOW()) WHERE id=%s", (nick, uid))
        return q1(c, f"SELECT {USER_COLS} FROM users WHERE id=%s", (uid,))


def profile_of(c, uid):
    out = {}
    for r in q(c, "SELECT context, tag, weight FROM taste_profiles WHERE user_id=%s ORDER BY context, weight DESC", (uid,)):
        out.setdefault(r["context"], {})[r["tag"]] = r["weight"]
    return {"user_id": uid, "interpreters": ACTIVE_INTERPRETERS, "profiles": out}


@app.post("/users/{uid}/quiz")
def answer_quiz(uid: int, body: QuizIn):
    with conn() as c:
        if not q1(c, "SELECT id FROM users WHERE id=%s", (uid,)):
            raise HTTPException(404, "user")
        _event(c, uid, None, "quiz_answer", {"answers": body.answers})
        S.recompute_user(c, uid)
        return profile_of(c, uid)


@app.get("/users/{uid}/profile")
def user_profile(uid: int, context: Optional[str] = None):
    with conn() as c:
        p = profile_of(c, uid)
        if context:
            return {"user_id": uid, "context": context, "weights": p["profiles"].get(context, {})}
        return p


# ───────── 여행 ─────────

class TripIn(BaseModel):
    name: str
    companions: str
    people: int = 2
    region: str = "무관"
    location: Optional[str] = None      # 세부 장소 (선택)
    start_date: Optional[str] = None    # YYYY-MM-DD (선택)
    end_date: Optional[str] = None
    purposes: list[str] = []
    member_ids: list[int]
    created_by: int


TRIP_COLS = "id, name, companions, people, region, location, start_date, end_date, purposes, created_by, created_at, deleted_at, chosen_stay_id"


def _fmt_trip(t):
    t["purposes"] = S.jload(t["purposes"])
    for k in ("start_date", "end_date"):
        if t.get(k) is not None:
            t[k] = str(t[k])
    return t


def _load_trip(c, tid):
    t = q1(c, f"SELECT {TRIP_COLS} FROM trips WHERE id=%s AND deleted_at IS NULL", (tid,))
    if not t:
        raise HTTPException(404, "trip")
    _fmt_trip(t)
    t["members"] = q(c, "SELECT u.id, u.name, u.nickname FROM trip_members m JOIN users u ON u.id=m.user_id WHERE m.trip_id=%s ORDER BY m.joined_at", (tid,))
    return t


def _auto_candidates(c, trip, member_ids):
    """컨텍스트 + 멤버 프로필로 카탈로그를 점수화해 상위 5개를 미리 담는다."""
    choices, stays = S.load_choices(c), S.load_stays(c)
    members = q(c, f"SELECT id, COALESCE(nickname, name) AS name FROM users WHERE id IN ({','.join(['%s'] * len(member_ids))})", tuple(member_ids))
    profiles = S.profiles_for(c, member_ids)
    ctx = S.ctx_weights(choices, trip["companions"], trip["purposes"])
    ranked = sorted(((S.score_stay(s, members, profiles, {}, trip["companions"], ctx)["total"], s["id"]) for s in stays.values() if S.eligible(s, trip)), reverse=True)
    return [sid for _, sid in ranked[:5]]


@app.get("/trips")
def trips():
    with conn() as c:
        ts = q(c, f"SELECT {TRIP_COLS} FROM trips WHERE deleted_at IS NULL ORDER BY created_at DESC")
        for t in ts:
            _fmt_trip(t)
            t["members"] = q(c, "SELECT u.id, u.name, u.nickname FROM trip_members m JOIN users u ON u.id=m.user_id WHERE m.trip_id=%s", (t["id"],))
            t["candidate_count"] = q1(c, "SELECT COUNT(*) AS n FROM trip_candidates WHERE trip_id=%s", (t["id"],))["n"]
        return ts


@app.post("/trips")
def create_trip(t: TripIn):
    with conn() as c:
        choices = S.load_choices(c)
        if ("companion", t.companions) not in choices or ("region", t.region) not in choices or any(("purpose", p) not in choices for p in t.purposes):
            raise HTTPException(400, "unknown companions/region/purpose")
        member_ids = t.member_ids or [t.created_by]
        loc = (t.location or "").strip() or None
        tid = x(c, "INSERT INTO trips (name, companions, people, region, location, start_date, end_date, purposes, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (t.name, t.companions, t.people, t.region, loc, t.start_date or None, t.end_date or None, json.dumps(t.purposes, ensure_ascii=False), t.created_by))
        xmany(c, "INSERT INTO trip_members (trip_id, user_id) VALUES (%s,%s)", [(tid, m) for m in member_ids])
        trip = {"companions": t.companions, "purposes": t.purposes, "region": t.region, "people": t.people,
                "location": loc, "start_date": t.start_date or None, "end_date": t.end_date or None}
        auto = _auto_candidates(c, trip, member_ids)
        xmany(c, "INSERT INTO trip_candidates (trip_id, stay_id, source, added_by) VALUES (%s,%s,'auto',NULL)", [(tid, s) for s in auto])
        _event(c, t.created_by, tid, "trip_created", {**trip, "member_ids": member_ids, "auto_candidates": auto})
        return {"id": tid, "auto_candidates": auto}


@app.get("/trips/{tid}")
def trip_board(tid: int):
    """보드 한 장에 필요한 것 전부: 컨텍스트 키워드, 학습 키워드, 후보(점수·근거·표), 더 담을 후보."""
    with conn() as c:
        t = _load_trip(c, tid)
        choices, stays = S.load_choices(c), S.load_stays(c)
        member_ids = [m["id"] for m in t["members"]]
        profiles = S.profiles_for(c, member_ids)
        member_kw = S.trip_member_keywords(c, tid)
        ctx = S.ctx_weights(choices, t["companions"], t["purposes"])
        votes = S.current_votes(c, tid)
        for m in t["members"]:
            m["name"] = m.get("nickname") or m["name"]
        names = {m["id"]: m["name"] for m in t["members"]}
        reason_text = {k[1]: v["text"] for k, v in choices.items() if k[0] in ("reason_keep", "reason_drop")}
        cand_ids = [r["stay_id"] for r in q(c, "SELECT stay_id FROM trip_candidates WHERE trip_id=%s", (tid,))]
        # 멤버가 과거 여행에서 결정(묵음)했던 숙소: {stay_id: [{user_name, trip_name, start_date}]}
        visited = {}
        ph = ",".join(["%s"] * len(member_ids)) if member_ids else "NULL"
        for r in q(c, f"SELECT DISTINCT tr.chosen_stay_id AS stay_id, tr.name AS trip_name, tr.start_date, tr.region, COALESCE(u.nickname, u.name) AS user_name "
                      f"FROM trips tr JOIN trip_members tm ON tm.trip_id=tr.id JOIN users u ON u.id=tm.user_id "
                      f"WHERE tr.id<>%s AND tr.deleted_at IS NULL AND tr.chosen_stay_id IS NOT NULL AND tm.user_id IN ({ph}) ORDER BY tr.start_date", (tid, *member_ids)):
            r["start_date"] = str(r["start_date"]) if r["start_date"] else None
            visited.setdefault(r["stay_id"], []).append(r)
        likes_by_stay = {}
        for r in q(c, "SELECT l.stay_id, l.user_id, COALESCE(u.nickname, u.name) AS user_name FROM stay_likes l JOIN users u ON u.id=l.user_id WHERE l.trip_id=%s ORDER BY l.created_at", (tid,)):
            likes_by_stay.setdefault(r["stay_id"], []).append({"user_id": r["user_id"], "user_name": r["user_name"]})
        # 한 줄(note) 해석 결과: 표마다 notes-v1 이 남긴 가중치 요약
        note_interp = {}
        eids = [v["event_id"] for v in votes.values()]
        if eids:
            for r in q(c, f"SELECT event_id, tag_deltas FROM event_interpretations WHERE interpreter='notes-v1' AND event_id IN ({','.join(['%s'] * len(eids))})", tuple(eids)):
                note_interp[r["event_id"]] = [(d["tag"], d["weight"]) for d in S.deltas_with_names(c, r["tag_deltas"]) if d["tag"]]

        def pack(sid):
            s = stays[sid]
            sc = S.score_stay(s, t["members"], profiles, member_kw, t["companions"], ctx)
            vs = [{"user_id": uid, "user_name": names.get(uid, "?"), "kind": v["kind"], "reason_id": v["reason"], "reason_text": reason_text.get(v["reason"], ""), "note": v["note"], "event_id": v["event_id"],
                   "note_tags": note_interp.get(v["event_id"], [])}
                  for (uid, ssid), v in votes.items() if ssid == sid]
            lk = likes_by_stay.get(sid, [])
            for l in lk:   # 멤버의 좋아요 = 이번 여행 점수 +5 (취향 학습과 별개로, 지금 이 여행에서의 의사 표시)
                sc["parts"].append({"src": f"{l['user_name']}·좋아요", "kind": "like", "tag": "좋아요", "v": LIKE_BONUS})
                sc["total"] += LIKE_BONUS
            return {"stay": s, "total": sc["total"], "parts": sc["parts"], "votes": vs, "dropped": any(v["kind"] == "drop" for v in vs), "likes": lk, "visited": visited.get(sid, [])}

        candidates = sorted((pack(sid) for sid in cand_ids), key=lambda z: -z["total"])
        more = sorted((pack(sid) for sid, s in stays.items() if sid not in cand_ids and S.eligible(s, t)), key=lambda z: -z["total"])
        # 이번 여행 조건(지역·인원) 밖 숙소. 지도를 축소하면 보이도록 같이 준다
        others = sorted((pack(sid) for sid, s in stays.items() if sid not in cand_ids and not S.eligible(s, t)), key=lambda z: -z["total"])
        for o in others:
            o["out_reason"] = ("지역 " + o["stay"]["region"] if t["region"] != "무관" and o["stay"]["region"] != t["region"] else "") + \
                              (" · " if (t["region"] != "무관" and o["stay"]["region"] != t["region"]) and o["stay"]["capacity"] < t["people"] else "") + \
                              (f"최대 {o['stay']['capacity']}인 < {t['people']}명" if o["stay"]["capacity"] < t["people"] else "")
        kw_trip = {r["tag"]: r["weight"] for r in q(c, "SELECT tag, weight FROM trip_keywords WHERE trip_id=%s AND scope='trip'", (tid,))}
        member_profiles = {uid: profiles[uid].get("base", {}) for uid in member_ids}
        return {"trip": t, "ctx_keywords": ctx, "keywords": {"trip": kw_trip, "members": member_kw}, "member_profiles": member_profiles,
                "candidates": candidates, "more": more, "others": others, "interpreters": ACTIVE_INTERPRETERS}


class MemberIn(BaseModel):
    user_id: int


@app.post("/trips/{tid}/members")
def add_member(tid: int, body: MemberIn):
    """초대 링크로 들어온 사람을 여행에 합류시킨다."""
    with conn() as c:
        _load_trip(c, tid)
        if not q1(c, "SELECT id FROM users WHERE id=%s", (body.user_id,)):
            raise HTTPException(404, "user")
        x(c, "INSERT IGNORE INTO trip_members (trip_id, user_id) VALUES (%s,%s)", (tid, body.user_id))
        return {"ok": True}


class DeleteIn(BaseModel):
    user_id: int


@app.delete("/trips/{tid}")
def delete_trip(tid: int, user_id: int):
    """소프트 삭제. 멤버만. 이벤트·해석·파생은 남긴다 (취향 학습은 유지)."""
    with conn() as c:
        t = _load_trip(c, tid)
        if user_id not in [m["id"] for m in t["members"]]:
            raise HTTPException(403, "not a member")
        x(c, "UPDATE trips SET deleted_at=NOW() WHERE id=%s", (tid,))
        _event(c, user_id, tid, "trip_deleted", {"name": t["name"]})
        return {"ok": True}


class CandidateIn(BaseModel):
    stay_id: str
    user_id: int


@app.post("/trips/{tid}/candidates")
def add_candidate(tid: int, body: CandidateIn):
    with conn() as c:
        _load_trip(c, tid)
        x(c, "INSERT IGNORE INTO trip_candidates (trip_id, stay_id, source, added_by) VALUES (%s,%s,'manual',%s)", (tid, body.stay_id, body.user_id))
        _event(c, body.user_id, tid, "candidate_added", {"stay_id": body.stay_id, "source": "manual"})
        return {"ok": True}


class VoteIn(BaseModel):
    user_id: int
    stay_id: str
    kind: str  # keep | drop
    reason: Optional[str] = None
    note: str = ""


@app.post("/trips/{tid}/votes")
def vote(tid: int, body: VoteIn):
    if body.kind not in ("keep", "drop"):
        raise HTTPException(400, "kind")
    with conn() as c:
        t = _load_trip(c, tid)
        if body.user_id not in [m["id"] for m in t["members"]]:
            raise HTTPException(403, "not a member")
        _event(c, body.user_id, tid, "vote", {"stay_id": body.stay_id, "kind": body.kind, "reason": body.reason, "note": body.note})
        S.recompute_trip(c, tid)
        S.recompute_user(c, body.user_id)
        return {"ok": True}


@app.delete("/trips/{tid}/votes")
def unvote(tid: int, user_id: int, stay_id: str):
    with conn() as c:
        _load_trip(c, tid)
        _event(c, user_id, tid, "vote_removed", {"stay_id": stay_id})
        S.recompute_trip(c, tid)
        S.recompute_user(c, user_id)
        return {"ok": True}


@app.get("/trips/{tid}/keywords")
def trip_keywords(tid: int):
    with conn() as c:
        _load_trip(c, tid)
        out = {"trip": {}, "members": {}}
        for r in q(c, "SELECT scope, user_id, tag, weight FROM trip_keywords WHERE trip_id=%s ORDER BY scope, user_id, weight DESC", (tid,)):
            (out["trip"] if r["scope"] == "trip" else out["members"].setdefault(r["user_id"], {}))[r["tag"]] = r["weight"]
        return out


class NextIn(BaseModel):
    created_by: int
    name: Optional[str] = None


@app.post("/trips/{tid}/next")
def next_trip(tid: int, body: NextIn):
    """같은 멤버·같은 동행 유형으로 새 여행. taste_profiles[동행유형] 이 초기 정렬에 들어간다."""
    with conn() as c:
        t = _load_trip(c, tid)
    return create_trip(TripIn(name=body.name or f"{t['name']} 다음", companions=t["companions"], people=t["people"], region="무관",
                              purposes=t["purposes"], member_ids=[m["id"] for m in t["members"]], created_by=body.created_by))


# ───────── 댓글 + 링크 미리보기 ─────────

import html as _html
import re as _re
import urllib.request as _ur

URL_RE = _re.compile(r"https?://[^\s<>\"']+")


def link_preview(url: str) -> dict:
    """OpenGraph 메타 읽기. naver.me 같은 단축 링크는 리다이렉트를 따라간다. 실패하면 url 만."""
    out = {"url": url}
    try:
        import urllib.parse as _up
        safe_url = _up.quote(url, safe=":/?&=#%+,;@!$'()*[]~-._")   # 한글 등 비ASCII 를 퍼센트 인코딩
        req = _ur.Request(safe_url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                                        "Accept-Language": "ko-KR,ko;q=0.9"})
        with _ur.urlopen(req, timeout=6) as r:
            out["final_url"] = r.geturl()
            page = r.read(400_000).decode("utf-8", "ignore")
        def meta(*names):
            for n in names:
                m = _re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % _re.escape(n), page, _re.I) \
                    or _re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % _re.escape(n), page, _re.I)
                if m and m.group(1).strip():
                    return _html.unescape(m.group(1).strip())
            return None
        out["title"] = meta("og:title", "twitter:title") or (lambda m: _html.unescape(m.group(1).strip()) if m else None)(_re.search(r"<title[^>]*>([^<]{1,200})", page, _re.I))
        out["description"] = meta("og:description", "description", "twitter:description")
        out["image"] = meta("og:image", "twitter:image")
        out["site"] = meta("og:site_name") or _re.sub(r"^www\.", "", _re.match(r"https?://([^/]+)", out.get("final_url") or url).group(1))
        for k in ("title", "description"):
            if out.get(k) and len(out[k]) > 200:
                out[k] = out[k][:200] + "…"
        # 네이버 지도는 장소 정보를 OpenGraph 로 주지 않는다 (클라이언트 렌더링). 링크 카드로만.
        if "naver.com" in (out.get("final_url") or url) and (out.get("title") in (None, "네이버지도", "네이버 지도")):
            m = _re.search(r"place/(\d+)", url) or _re.search(r"pinId=(\d+)", out.get("final_url") or "")
            out["title"] = f"네이버 지도 장소{' #' + m.group(1) if m else ''}"
            out["description"] = "네이버 지도는 장소 정보를 미리보기로 주지 않아요. 눌러서 열어 보세요. (실서비스: 네이버 지역검색·카카오 로컬 API 로 대체)"
    except Exception as e:
        out["error"] = type(e).__name__
    return out


class DecideIn(BaseModel):
    user_id: int
    stay_id: Optional[str] = None   # None 이면 결정 취소


@app.post("/trips/{tid}/decide")
def decide(tid: int, body: DecideIn):
    """이 여행의 최종 숙소. 이후 같은 멤버의 여행에서 "○○이 묵었던 곳"으로 표시된다."""
    with conn() as c:
        t = _load_trip(c, tid)
        if body.user_id not in [m["id"] for m in t["members"]]:
            raise HTTPException(403, "not a member")
        x(c, "UPDATE trips SET chosen_stay_id=%s WHERE id=%s", (body.stay_id, tid))
        _event(c, body.user_id, tid, "trip_decided", {"stay_id": body.stay_id})
        return {"ok": True, "chosen_stay_id": body.stay_id}


class LikeIn(BaseModel):
    user_id: int
    stay_id: str


@app.post("/trips/{tid}/likes")
def like(tid: int, body: LikeIn):
    with conn() as c:
        t = _load_trip(c, tid)
        if body.user_id not in [m["id"] for m in t["members"]]:
            raise HTTPException(403, "not a member")
        if q1(c, "SELECT 1 FROM stay_likes WHERE trip_id=%s AND stay_id=%s AND user_id=%s", (tid, body.stay_id, body.user_id)):
            return {"ok": True, "liked": True}
        x(c, "INSERT INTO stay_likes (trip_id, stay_id, user_id) VALUES (%s,%s,%s)", (tid, body.stay_id, body.user_id))
        _event(c, body.user_id, tid, "like", {"stay_id": body.stay_id})
        S.recompute_trip(c, tid); S.recompute_user(c, body.user_id)
        return {"ok": True, "liked": True}


@app.delete("/trips/{tid}/likes")
def unlike(tid: int, user_id: int, stay_id: str):
    with conn() as c:
        _load_trip(c, tid)
        x(c, "DELETE FROM stay_likes WHERE trip_id=%s AND stay_id=%s AND user_id=%s", (tid, stay_id, user_id))
        _event(c, user_id, tid, "unlike", {"stay_id": stay_id})
        S.recompute_trip(c, tid); S.recompute_user(c, user_id)
        return {"ok": True, "liked": False}


class CommentIn(BaseModel):
    user_id: int
    stay_id: str
    text: str


@app.get("/trips/{tid}/comments")
def list_comments(tid: int, stay_id: Optional[str] = None):
    with conn() as c:
        _load_trip(c, tid)
        rows = q(c, "SELECT cm.id, cm.stay_id, cm.user_id, COALESCE(u.nickname, u.name) AS user_name, cm.text, cm.links, cm.created_at "
                    "FROM stay_comments cm JOIN users u ON u.id=cm.user_id WHERE cm.trip_id=%s" + (" AND cm.stay_id=%s" if stay_id else "") + " ORDER BY cm.id",
                 (tid, stay_id) if stay_id else (tid,))
        for r in rows:
            r["links"] = S.jload(r["links"]) or []
        return rows


@app.post("/trips/{tid}/comments")
def add_comment(tid: int, body: CommentIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text")
    with conn() as c:
        t = _load_trip(c, tid)
        if body.user_id not in [m["id"] for m in t["members"]]:
            raise HTTPException(403, "not a member")
        if not q1(c, "SELECT id FROM stays WHERE id=%s", (body.stay_id,)):
            raise HTTPException(404, "stay")
        links = [link_preview(u) for u in dict.fromkeys(URL_RE.findall(text))][:3]
        cid = x(c, "INSERT INTO stay_comments (trip_id, stay_id, user_id, text, links) VALUES (%s,%s,%s,%s,%s)",
                (tid, body.stay_id, body.user_id, text, json.dumps(links, ensure_ascii=False)))
        _event(c, body.user_id, tid, "comment", {"stay_id": body.stay_id, "comment_id": cid, "text": text})
        return q1(c, "SELECT id, stay_id, user_id, text, links, created_at FROM stay_comments WHERE id=%s", (cid,)) | {"links": links}


@app.get("/link-preview")
def preview(url: str):
    return link_preview(url)


# ───────── 이벤트·해석 조회 (리뷰용) ─────────

@app.get("/events")
def events(limit: int = 50):
    """원본 이벤트와 그 해석을 나란히."""
    with conn() as c:
        es = q(c, "SELECT id, user_id, trip_id, type, payload, created_at FROM events ORDER BY id DESC LIMIT %s", (limit,))
        ids = [e["id"] for e in es]
        its = q(c, f"SELECT event_id, interpreter, tag_deltas, new_tags, rationale FROM event_interpretations WHERE event_id IN ({','.join(['%s'] * len(ids))})", tuple(ids)) if ids else []
        for e in es:
            e["payload"] = S.jload(e["payload"])
            e["interpretations"] = [{**i, "tag_deltas": S.deltas_with_names(c, i["tag_deltas"]), "new_tags": S.jload(i["new_tags"])} for i in its if i["event_id"] == e["id"]]
        return es


@app.post("/admin/reinterpret")
def reinterpret():
    """해석기를 바꾼 뒤: 모든 이벤트를 다시 해석하고 파생을 다시 만든다. 원본은 그대로."""
    with conn() as c:
        S.reinterpret_all(c)
        S.recompute_all(c)
        return {"ok": True, "interpreters": ACTIVE_INTERPRETERS}


@app.post("/admin/recompute")
def recompute():
    with conn() as c:
        S.recompute_all(c)
        return {"ok": True}


# ───────── 데모 데이터 ─────────

def seed_demo():
    with conn() as c:
        has_users = q1(c, "SELECT COUNT(*) AS n FROM users")["n"]
        has_trips = q1(c, "SELECT COUNT(*) AS n FROM trips")["n"]
    if not has_users:
        owen = create_user(UserIn(name="루니"))["id"]
        jimin = create_user(UserIn(name="지민"))["id"]
        signup(owen, SignupIn(nickname="루니"))
        signup(jimin, SignupIn(nickname="지민"))
        answer_quiz(owen, QuizIn(answers=["1:1", "2:2", "3:1", "4:1", "5:1"]))   # 도심·야경·외식·위치·대중교통
        answer_quiz(jimin, QuizIn(answers=["1:3", "2:1", "3:2", "4:2", "5:2"]))  # 숙소가 목적·바다·해먹기·넓이·자차
    if not has_trips:
        with conn() as c:
            owen = q1(c, "SELECT id FROM users WHERE name='루니'")["id"]
            jimin = q1(c, "SELECT id FROM users WHERE name='지민'")["id"]
        tid = create_trip(TripIn(name="10월 부산", companions="커플", people=2, region="부산", purposes=["휴양", "맛집"], member_ids=[owen, jimin], created_by=owen))["id"]
        vote(tid, VoteIn(user_id=jimin, stay_id="y1000112007", kind="drop", reason="d_city", note="서면은 너무 복잡해"))   # 어반스테이 서면
        vote(tid, VoteIn(user_id=owen, stay_id="y10055546", kind="keep", reason="k_view"))                         # 어반스테이 부산송도해변


def seed_history():
    """시연용 이력: 멤버 10명, 과거 여행(결정 숙소 있음) 4개, 표·좋아요 몇 개. 이미 있으면 건너뛴다."""
    with conn() as c:
        names = {r["name"]: r["id"] for r in q(c, "SELECT id, name FROM users")}
        stays = S.load_stays(c)
    def user(name, answers):
        if name in names:
            return names[name]
        uid = create_user(UserIn(name=name))["id"]
        answer_quiz(uid, QuizIn(answers=answers)); signup(uid, SignupIn(nickname=name)); names[name] = uid
        return uid
    owen = user("루니", ["1:1", "2:2", "3:1", "4:1", "5:1"]); jimin = user("지민", ["1:3", "2:1", "3:2", "4:2", "5:2"])
    haeun = user("하은", ["1:3", "2:3", "3:2", "4:4", "5:2"]); minjun = user("민준", ["1:1", "2:4", "3:1", "4:3", "5:1"])
    subin = user("수빈", ["1:2", "2:1", "3:2", "4:2", "5:1"]); jiwoo = user("지우", ["1:2", "2:1", "3:3", "4:2", "5:2"])
    seoyeon = user("서연", ["1:3", "2:2", "3:1", "4:4", "5:1"]); doyun = user("도윤", ["1:2", "2:3", "3:2", "4:2", "5:2"])
    yerin = user("예린", ["1:1", "2:1", "3:3", "4:3", "5:1"]); taeo = user("태오", ["1:3", "2:1", "3:1", "4:4", "5:2"])
    with conn() as c:
        if q1(c, "SELECT 1 FROM trips WHERE name='작년 10월 부산 커플'"):
            return
    def past(name, companions, people, region, location, start, end, purposes, members, chosen, votes=(), likes=()):
        if chosen not in stays:
            return None
        tid = create_trip(TripIn(name=name, companions=companions, people=people, region=region, location=location, start_date=start, end_date=end, purposes=purposes, member_ids=members, created_by=members[0]))["id"]
        with conn() as c:
            if chosen not in [r["stay_id"] for r in q(c, "SELECT stay_id FROM trip_candidates WHERE trip_id=%s", (tid,))]:
                x(c, "INSERT IGNORE INTO trip_candidates (trip_id, stay_id, source, added_by) VALUES (%s,%s,'manual',%s)", (tid, chosen, members[0]))
        for (uid, sid, kind, reason, note) in votes:
            if sid in stays:
                vote(tid, VoteIn(user_id=uid, stay_id=sid, kind=kind, reason=reason, note=note))
        for (uid, sid) in likes:
            if sid in stays:
                like(tid, LikeIn(user_id=uid, stay_id=sid))
        decide(tid, DecideIn(user_id=members[0], stay_id=chosen))
        return tid
    past("작년 10월 부산 커플", "커플", 2, "부산", "송도", "2025-10-10", "2025-10-12", ["휴양", "맛집"], [owen, jimin], "y10055546",
         votes=[(jimin, "y10055546", "keep", "k_view", "바다 보이는 게 진짜 좋았어"), (owen, "y1000112007", "drop", "d_city", "")], likes=[(owen, "y10055546")])
    past("작년 여름 속초 친구", "친구", 4, "강원", "속초", "2025-08-01", "2025-08-03", ["휴양", "서핑"], [owen, haeun, minjun, subin], "y10041421",
         votes=[(haeun, "y10041421", "keep", "k_space", "넷이서도 안 좁았음"), (minjun, "y10056010", "drop", "d_small", "")], likes=[(subin, "y10041421")])
    past("2월 제주 커플", "커플", 2, "제주", "제주시", "2026-02-14", "2026-02-16", ["기념일"], [owen, jimin], "y10040231",
         votes=[(jimin, "y10040231", "keep", "k_price", "공항 가까워서 편했어")])
    past("봄 강릉 가족", "가족", 5, "강원", "강릉", "2026-04-05", "2026-04-07", ["아이동반", "휴양"], [doyun, yerin, taeo, jiwoo, seoyeon], "y3001494",
         votes=[(doyun, "y3001494", "keep", "k_loc", "애들 데리고 다니기 편했음")], likes=[(yerin, "y3001494")])
    # 지금 진행 중인 여행 하나 더 (5명, 후보 선별 중)
    with conn() as c:
        if not q1(c, "SELECT 1 FROM trips WHERE name='12월 강릉 친구'"):
            tid = create_trip(TripIn(name="12월 강릉 친구", companions="친구", people=5, region="강원", location="강릉", start_date="2026-12-05", end_date="2026-12-07",
                                     purposes=["휴양", "맛집"], member_ids=[owen, haeun, minjun, subin, jiwoo], created_by=owen))["id"]
            like(tid, LikeIn(user_id=haeun, stay_id="y3001494")) if "y3001494" in stays else None


@app.post("/admin/seed-history")
def seed_history_ep():
    seed_history()
    return {"ok": True}


@app.on_event("startup")
def on_startup():
    for i in range(60):
        try:
            with conn() as c:
                if q1(c, "SELECT COUNT(*) AS n FROM choices")["n"] > 0 and q1(c, "SELECT COUNT(*) AS n FROM stays")["n"] > 0:
                    break
        except Exception:
            pass
        time.sleep(2)
    if os.getenv("SEED_DEMO") == "1":
        seed_demo()
        try:
            seed_history()
        except Exception as e:
            print("seed_history skipped:", e)
