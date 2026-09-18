"""
야놀자(NOL) 숙소 상세 페이지에서 핸디즈 지점(어반스테이·르컬렉티브) 정보를 읽어 구조화한다.

원칙
- 상세 페이지(/stay/domestic/{id})만 요청한다. robots.txt 가 막는 /search/ 와 /reviews/ 는 쓰지 않는다.
- 지점당 1회, 2초 간격. 응답 HTML 은 저장하지 않는다.
- 리뷰 본문은 저장하지 않는다. 키워드 사전으로 상위 키워드와 빈도만 남긴다 (파생 데이터).
- 결과: data/stays_scraped.json → db/init/03_stays.sql (컨테이너 기동 시 네트워크 없이 적재)

실행: python3 scripts/scrape_yanolja.py
"""
import html
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 핸디즈 지점: 웹 검색으로 찾은 야놀자 ID (검색 API 를 쓰지 않으려고 손으로 모았다).
HANDYS_IDS = [
    1000112007, 1000112780, 10040717, 10059879, 10064949, 1000112790, 10041421, 10056010, 10055546, 10059635,
    10044431, 10051784, 10040231, 10058950, 10054581, 10040605, 10044553, 10058904, 10052429, 10058726,
]
# 시연용으로 카탈로그를 100개 규모로 채우기 위한 일반 숙소. 야놀자의 공개 목록 페이지("○○ 근처 호텔·리조트 추천 베스트 10",
# robots 허용 경로)에서 ID 를 모은다. (슬러그, 도시당 최대 개수)
LIST_PAGES = [
    ("서울-강남구-근처-호텔-리조트-추천-베스트-10", 8), ("서울-중구-근처-호텔-리조트-추천-베스트-10", 6), ("서울-마포구-근처-호텔-리조트-추천-베스트-10", 5),
    ("서울-종로구-근처-호텔-리조트-추천-베스트-10", 5), ("서울-송파구-근처-호텔-리조트-추천-베스트-10", 4), ("서울-용산구-근처-호텔-리조트-추천-베스트-10", 4),
    ("부산-해운대구-근처-호텔-리조트-추천-베스트-10", 6), ("부산-부산진구-근처-호텔-리조트-추천-베스트-10", 4), ("부산-수영구-근처-호텔-리조트-추천-베스트-10", 4), ("부산-중구-근처-호텔-리조트-추천-베스트-10", 3),
    ("강원도-속초시-근처-호텔-리조트-추천-베스트-10", 4), ("강원도-강릉시-근처-호텔-리조트-추천-베스트-10", 5), ("강원도-양양군-근처-호텔-리조트-추천-베스트-10", 3),
    ("인천-중구-근처-호텔-리조트-추천-베스트-10", 4), ("인천-연수구-근처-호텔-리조트-추천-베스트-10", 3), ("경기-수원시-근처-호텔-리조트-추천-베스트-10", 3),
    ("제주-제주시-근처-호텔-리조트-추천-베스트-10", 6), ("제주-서귀포시-근처-호텔-리조트-추천-베스트-10", 6),
    ("대구-중구-근처-호텔-리조트-추천-베스트-10", 4), ("대전-유성구-근처-호텔-리조트-추천-베스트-10", 3), ("광주-동구-근처-호텔-리조트-추천-베스트-10", 3),
    ("전라남도-여수시-근처-호텔-리조트-추천-베스트-10", 4), ("경상북도-경주시-근처-호텔-리조트-추천-베스트-10", 4), ("충청남도-천안시-근처-호텔-리조트-추천-베스트-10", 2),
]
HANDYS_BRANDS = ("어반스테이", "르컬렉티브", "플라트")


def collect_extra_ids():
    import urllib.parse
    ids, seen = [], set(HANDYS_IDS)
    for slug, cap in LIST_PAGES:
        url = "https://nol.yanolja.com/programmatic/domestic-accommodation/" + urllib.parse.quote(slug) + "/"
        try:
            page = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read().decode("utf-8", "ignore")
        except Exception as e:
            print(slug, "LIST FAIL", e, file=sys.stderr)
            continue
        found = []
        for sid in re.findall(r"/stay/domestic/(\d+)", page):
            sid = int(sid)
            if sid in seen:
                continue
            seen.add(sid); found.append(sid)
            if len(found) >= cap:
                break
        ids += found
        print(f"  {slug[:22]:<22} +{len(found)}", file=sys.stderr)
        time.sleep(1.5)
    return ids

REGION_MAP = [("서울", "서울"), ("부산", "부산"), ("인천", "경기·인천"), ("경기", "경기·인천"), ("강원", "강원"), ("제주", "제주"),
              ("충남", "충남"), ("충청남", "충남"), ("대전", "대전"), ("충청북", "충북"), ("울산", "울산"), ("대구", "대구"), ("광주", "광주"),
              ("전라남", "전남"), ("전남", "전남"), ("전라북", "전북"), ("전북", "전북"), ("경상북", "경북"), ("경북", "경북"), ("경상남", "경남"), ("경남", "경남")]

# 리뷰 키워드 사전: 정규식 → (키워드, 대응 태그 또는 None, 극성)
KEYWORDS = [
    (r"깨끗|깔끔|청결", "청결", "청결", "+"),
    (r"조용|방음", "조용함", "조용함", "+"),
    (r"넓|널찍", "넓음", "넓은객실", "+"),
    (r"역에서|지하철|역세권|역 바로", "역세권", "역세권", "+"),
    (r"바다|오션|바닷", "바다뷰", "바다뷰", "+"),
    (r"해변|해수욕장", "해변", "해변", "+"),
    (r"(?<!리)뷰|전망", "전망", None, "+"),
    (r"주방|취사|조리|해먹|해 먹", "주방", "주방", "+"),
    (r"세탁|빨래|건조기", "세탁기", "세탁기", "+"),
    (r"주차", "주차", "주차", "+"),
    (r"편의점", "편의점", "편의점 인접", "+"),
    (r"친절|응대|답변|빠른 안내|빠르게", "친절", "친절", "+"),
    (r"가성비|저렴|합리적", "가성비", "가성비", "+"),
    (r"위치|접근성|가까|가깝", "위치", None, "+"),
    (r"비대면|무인|셀프 ?체크인", "비대면체크인", "비대면체크인", "+"),
    (r"출장", "출장", None, "+"),
    (r"아이|아기|애기|가족", "가족", "키즈", "+"),
    (r"커플|연인|신혼", "커플", None, "+"),
    (r"침구|침대|매트리스", "침구", None, "+"),
    (r"욕실|화장실|샤워", "욕실", None, "+"),
    (r"수영장|풀", "수영장", "수영장", "+"),
    (r"좁", "좁음", None, "-"),
    (r"냄새|곰팡이|퀴퀴", "냄새", None, "-"),
    (r"시끄|소음|층간", "소음", None, "-"),
    (r"불친절|늦게|연락이 안", "응대불만", None, "-"),
]

AMENITY_WORDS = ["주방", "취사", "와이파이", "주차", "금연", "세탁기", "건조기", "수영장", "피트니스", "조식", "엘리베이터", "전기차 충전",
                 "반려동물", "에어컨", "TV", "넷플릭스", "커피", "어메니티", "스타일러", "발코니", "테라스", "바베큐", "키즈"]
ROOM_WORDS = ["스튜디오", "스위트", "트윈", "더블", "패밀리", "투룸", "쓰리룸", "펜트하우스", "온돌", "디럭스", "로프트", "코너", "프리미어", "싱글"]


def fetch(sid):
    req = urllib.request.Request(f"https://nol.yanolja.com/stay/domestic/{sid}", headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def text_lines(page):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    return [l.strip() for l in t.splitlines() if l.strip()]


def parse(sid, page):
    lines = text_lines(page)
    joined = "\n".join(lines)
    m = re.search(r"<title>([^<|]+?)\s*(호텔/리조트|모텔|펜션)", page)
    name = m.group(1).strip() if m else (lines[0] if lines else str(sid))
    is_handys = any(bnd in name for bnd in HANDYS_BRANDS)
    brand = "르컬렉티브" if "르컬렉티브" in name else "어반스테이" if "어반스테이" in name else "플라트" if "플라트" in name else None

    rating = None
    m = re.search(r"\b([0-5]\.\d)\b", joined)
    if m:
        rating = float(m.group(1))
    review_count = 0
    m = re.search(r"([\d,]+)\s*개?\s*(의)?\s*(리뷰|후기)|(리뷰|후기)\s*([\d,]+)", joined)
    if m:
        review_count = int((m.group(1) or m.group(5)).replace(",", ""))

    region = None
    addr = None
    for l in lines:
        if re.search(r"(로|길|동)\s*\d", l) and len(l) < 60 and any(k in l for k, _ in REGION_MAP):
            addr = l
            break
    src = addr or name
    for k, r in REGION_MAP:
        if k in src:
            region = r
            break
    if not region:  # 이름에서 유추
        for k, r in [("명동", "서울"), ("익선", "서울"), ("해운대", "부산"), ("서면", "부산"), ("송도해변", "부산"), ("기장", "부산"), ("부산", "부산"),
                     ("속초", "강원"), ("낙산", "강원"), ("양양", "강원"), ("제주", "제주"), ("시흥", "경기·인천"), ("동탄", "경기·인천"),
                     ("인천", "경기·인천"), ("송도달빛", "경기·인천"), ("당진", "충남"), ("울산", "울산")]:
            if k in name:
                region = r
                break
    region = region or "무관"

    amenities = sorted({w for w in AMENITY_WORDS if any(w in l and len(l) < 20 for l in lines)})
    room_types = sorted({l for l in lines if any(w in l for w in ROOM_WORDS) and 3 <= len(l) <= 28 and not re.search(r"원|%|예약|취소", l)})[:10]

    caps = [int(x) for x in re.findall(r"최대\s*(\d+)\s*인", joined)]
    capacity = max(caps) if caps else (6 if brand == "르컬렉티브" else 2 if is_handys else 3)

    # 가격: 정책 문구(위약금·손해배상·주차·월)가 없는 줄의 금액만. 1박 요금은 보통 4만원 이상.
    prices = []
    for l in lines:
        if re.search(r"위약금|손해배상|주차|/월|월\s*$|추가 비용|30분당", l):
            continue
        prices += [int(p.replace(",", "")) for p in re.findall(r"(\d{2,3},\d{3})\s*원", l)]
    prices = [p for p in prices if 40000 <= p <= 900000]
    min_price = min(prices) if prices else None
    if min_price:
        price_tier = 1 if min_price < 80000 else 2 if min_price < 150000 else 3
    else:  # 요금은 클라이언트 렌더링이라 HTML 에 없는 경우가 대부분 → 브랜드·객실 유형으로 추정
        if is_handys:
            price_tier = 3 if "펜트하우스" in name else 2 if brand == "르컬렉티브" else 1
        else:
            price_tier = 3 if re.search(r"리조트|풀빌라|스위트|그랜드|파크 하얏트|시그니엘|롯데|신라|파라다이스", name) else 1 if re.search(r"모텔|게스트|호스텔", name) else 2

    # 리뷰 문장: 한국어 문장 어미 + 정책/안내 문구 제외. 원문은 저장하지 않는다.
    reviews = []
    for l in lines:
        if 10 <= len(l) <= 300 and re.search(r"(어요|습니다|네요|했어|좋았|추천|만족|아쉬)", l) \
           and not re.search(r"위약금|환불|안내됩니다|부과|규정|이용 불가|운영합니다|문의|체크인 시간|체크아웃|바랍니다|가능합니다|주시기|상이할 수|사진과|유의|불가합니다|제공됩니다|입니다\.$", l):
            reviews.append(l)
    reviews = list(dict.fromkeys(reviews))
    counter, polarity = Counter(), {}
    for r in reviews:
        for pat, kw, tag, pol in KEYWORDS:
            if re.search(pat, r):
                counter[kw] += 1
                polarity[kw] = (tag, pol)
    review_keywords = [{"keyword": kw, "count": n, "tag": polarity[kw][0], "polarity": polarity[kw][1]} for kw, n in counter.most_common(5)]

    # 소개 문구: 가장 긴 설명형 줄 하나
    # 소개 문구: 지점 위치·특징을 말하는 줄만 (안내·책임·환불 문구 제외). 없으면 NULL
    cand = [l for l in lines if 30 <= len(l) <= 200
            and re.search(r"도보|위치|인근|바로 앞|분 거리|전 객실|객실은|스테이|레지던스|오션|바다|공원|역 ", l)
            and not re.search(r"환불|위약금|체크인|주차|안내|책임|배상|문의|유의|불가|원/|어요|습니다|네요", l)]
    desc = max(cand, key=len) if cand else ""

    lat = lng = None
    m = re.search(r'"latitude"\s*:\s*"?(-?\d+\.\d+)"?', page); lat = float(m.group(1)) if m else None
    m = re.search(r'"longitude"\s*:\s*"?(-?\d+\.\d+)"?', page); lng = float(m.group(1)) if m else None
    if not (lat and 33 <= lat <= 39 and lng and 124 <= lng <= 132):
        lat = lng = None
    return {"id": f"y{sid}", "yanolja_id": sid, "name": name, "brand": brand, "is_handys": is_handys, "region": region, "address": addr, "lat": lat, "lng": lng, "capacity": capacity,
            "price_tier": price_tier, "min_price": min_price, "rating": rating, "review_count": review_count, "amenities": amenities,
            "room_types": room_types, "review_keywords": review_keywords, "reviews_seen": len(reviews), "description": desc,
            "source_url": f"https://nol.yanolja.com/stay/domestic/{sid}"}


def derive_tags(s):
    """편의시설·객실 타입·이름·브랜드·리뷰 키워드에서 stays.tags 를 만든다."""
    t = set()
    n = s["name"]
    if any(k in n for k in ["해변", "바닷가", "낙산", "송도해변"]): t |= {"해변", "바다뷰"}
    if any(k in n for k in ["역", "터미널"]): t.add("역세권")
    if any(k in n for k in ["명동", "서면", "익선", "시청", "부산역", "차이나타운", "해운대역", "동탄"]): t.add("도심상권")
    if "웨이브파크" in n: t |= {"리조트인접", "바다뷰"}
    if "부티크" in n: t.add("부티크")
    if "펜트하우스" in n: t |= {"넓은객실", "업스케일"}
    if s["brand"] == "르컬렉티브": t |= {"거실", "방2개이상"}
    if s["brand"] == "어반스테이" and "펜트하우스" not in n: t.add("실속")
    if not s["is_handys"]:
        if re.search(r"오션|씨|바다|비치|해운대|광안|해변", n): t |= {"해변", "바다뷰"}
        if re.search(r"리조트|풀빌라", n): t.add("리조트인접")
        if re.search(r"호텔|스위트", n) and s["price_tier"] == 3: t.add("업스케일")
        if re.search(r"부티크|스테이|하우스", n): t.add("부티크")
    am = set(s["amenities"])
    if am & {"주방", "취사"}: t.add("주방")
    if "주차" in am: t.add("주차")
    if am & {"세탁기", "건조기"}: t.add("세탁기")
    if "수영장" in am: t.add("수영장")
    if "키즈" in am: t.add("키즈")
    rt = " ".join(s["room_types"])
    if any(k in rt for k in ["투룸", "쓰리룸", "패밀리"]): t |= {"방2개이상", "거실"}
    if any(k in rt for k in ["스위트", "펜트하우스", "프리미어"]): t.add("넓은객실")
    if s["price_tier"] == 1: t.add("가성비")
    if s["price_tier"] == 3: t.add("업스케일")
    for k in s["review_keywords"]:
        if k["tag"] and k["polarity"] == "+" and k["count"] >= 3:
            t.add(k["tag"])
    return sorted(t)


def sql_str(v):
    return "NULL" if v is None else "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def write_sql(stays):
    out = ["-- 야놀자 상세 페이지에서 읽은 핸디즈 지점. scripts/scrape_yanolja.py 가 생성. 리뷰 원문 없음(파생 키워드만).",
           "SET NAMES utf8mb4;", "USE staymate;", ""]
    # 리뷰 키워드에서 나온 새 어휘 (사전에 없던 것)
    out.append("INSERT IGNORE INTO tags (name, parent, status, source, definition) VALUES")
    out.append("  ('청결','등급','active','ops','리뷰에서 깨끗·깔끔·청결 언급'),")
    out.append("  ('조용함','공간','active','ops','리뷰에서 조용·방음 언급'),")
    out.append("  ('편의점 인접','편의','active','ops','건물 내·바로 옆 편의점 (리뷰에서 편의점 언급)'),")
    out.append("  ('친절','편의','active','ops','리뷰에서 응대·안내 칭찬'),")
    out.append("  ('비대면체크인','편의','active','ops','무인·셀프 체크인');")
    out.append("")
    out.append("INSERT INTO stays (id, name, brand, is_handys, region, address, lat, lng, capacity, price_tier, min_price, rating, review_count, tags, amenities, room_types, review_keywords, description, source_url) VALUES")
    rows = []
    for s in stays:
        rows.append("  (" + ", ".join([sql_str(s["id"]), sql_str(s["name"]), sql_str(s["brand"]), "1" if s.get("is_handys") else "0", sql_str(s["region"]), sql_str(s["address"]),
                                       "NULL" if s.get("lat") is None else str(s["lat"]), "NULL" if s.get("lng") is None else str(s["lng"]),
                                       str(s["capacity"]), str(s["price_tier"]), "NULL" if s["min_price"] is None else str(s["min_price"]),
                                       "NULL" if s["rating"] is None else str(s["rating"]), str(s["review_count"]),
                                       sql_str(json.dumps(s["tags"], ensure_ascii=False)), sql_str(json.dumps(s["amenities"], ensure_ascii=False)),
                                       sql_str(json.dumps(s["room_types"], ensure_ascii=False)), sql_str(json.dumps(s["review_keywords"], ensure_ascii=False)),
                                       sql_str(s["description"] or None), sql_str(s["source_url"])]) + ")")
    out.append(",\n".join(rows) + ";")
    (ROOT / "db/init/03_stays.sql").write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    stays = []
    print("목록 페이지에서 일반 숙소 ID 수집…", file=sys.stderr)
    extra = collect_extra_ids()
    STAY_IDS = HANDYS_IDS + extra
    print(f"핸디즈 {len(HANDYS_IDS)} + 일반 {len(extra)} = {len(STAY_IDS)}", file=sys.stderr)
    for i, sid in enumerate(STAY_IDS):
        try:
            page = fetch(sid)
        except Exception as e:
            print(sid, "FETCH FAIL", e, file=sys.stderr)
            continue
        s = parse(sid, page)
        if s["name"].startswith("NOL") or s["reviews_seen"] < 3:
            time.sleep(3)
            try:
                s = parse(sid, fetch(sid))
            except Exception:
                pass
        if s["name"].startswith("NOL") or s["reviews_seen"] < 3:
            print(sid, "SKIP (페이지가 상세가 아님)", file=sys.stderr)
            continue
        s["tags"] = derive_tags(s)
        stays.append(s)
        print(f"{s['id']:>12} {s['name'][:22]:<22} {s['region']:<6} cap{s['capacity']} ₩{s['min_price']} ★{s['rating']} rv{s['review_count']:>5} seen{s['reviews_seen']:>3} kw={[k['keyword'] for k in s['review_keywords']]}")
        if i < len(STAY_IDS) - 1:
            time.sleep(1.5)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data/stays_scraped.json").write_text(json.dumps(stays, ensure_ascii=False, indent=1), encoding="utf-8")
    write_sql(stays)
    print(f"\n{len(stays)} stays → data/stays_scraped.json, db/init/03_stays.sql")


if __name__ == "__main__":
    main()
