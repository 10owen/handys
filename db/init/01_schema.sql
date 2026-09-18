-- 스테이메이트 스키마 (MySQL 8) — 11 테이블
--
-- 네 계층:
--   사전   tags · stays · choices            취향 어휘(자라는 트리) · 카탈로그 · 입력 선택지
--   원본   users · trips · trip_members · trip_candidates · events        사람이 한 일. events 는 append-only
--   해석   event_interpretations             해석기(규칙 또는 AI)가 이벤트를 태그 가중치로 번역한 결과
--   파생   taste_profiles · trip_keywords    해석 결과의 합. 언제든 지우고 다시 만들 수 있다
--
-- 원칙: 키워드를 저장하지 않고, 키워드를 만든 원본 행동을 저장한다. 해석기가 바뀌면 해석과 파생만 다시 만든다.

CREATE DATABASE IF NOT EXISTS staymate CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
SET NAMES utf8mb4;
USE staymate;

-- ───────── 사전 ─────────

-- 취향 어휘. 고정 목록이 아니라 자라는 트리다. 해석기가 새 태그를 candidate 로 넣고, 쓰이면 active 가 된다.
CREATE TABLE tags (
  id          INT          NOT NULL AUTO_INCREMENT COMMENT '선택지 가중치(choices.tag_weights)가 참조하는 숫자 id',
  name        VARCHAR(48)  NOT NULL,
  parent      VARCHAR(48)  NULL     COMMENT '상위 태그. NULL 이면 최상위 범주(입지·뷰·공간·등급·편의). 분화는 자식 추가로',
  status      ENUM('active','candidate','merged') NOT NULL DEFAULT 'active' COMMENT 'candidate = 해석기가 제안, 아직 검증 전 / merged = 다른 태그로 흡수',
  merged_into VARCHAR(48)  NULL     COMMENT 'status=merged 일 때 흡수한 태그',
  source      ENUM('seed','ai','ops') NOT NULL DEFAULT 'seed' COMMENT '누가 만들었나',
  definition  VARCHAR(255) NULL     COMMENT '해석기가 이 태그를 붙일 때의 기준 (사람이 읽는 정의)',
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (name),
  UNIQUE KEY uq_tags_id (id),
  KEY idx_tags_parent (parent),
  CONSTRAINT fk_tags_parent FOREIGN KEY (parent) REFERENCES tags(name)
) COMMENT='취향 어휘 트리 (자란다)';

-- 숙소 카탈로그. 야놀자 상세 페이지에서 읽은 핸디즈 지점(어반스테이·르컬렉티브). scripts/scrape_yanolja.py → db/init/03_stays.sql
CREATE TABLE stays (
  id              VARCHAR(16)  NOT NULL COMMENT 'y + 야놀자 숙소 ID',
  name            VARCHAR(64)  NOT NULL,
  brand           VARCHAR(16)  NULL     COMMENT '어반스테이 | 르컬렉티브 | 플라트 (핸디즈) · NULL 이면 일반 숙소',
  is_handys       TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '핸디즈 지점 여부. 일반 숙소는 시연용 카탈로그 확장',
  region          VARCHAR(16)  NOT NULL COMMENT 'choices(kind=region).code',
  address         VARCHAR(128) NULL,
  lat             DECIMAL(9,6) NULL     COMMENT '위도 (야놀자 페이지)',
  lng             DECIMAL(9,6) NULL     COMMENT '경도',
  capacity        INT          NOT NULL COMMENT '객실 최대 인원 중 최대값',
  price_tier      TINYINT      NOT NULL COMMENT '1 실속(~8만) · 2 (~15만) · 3 프리미엄. 페이지의 최저 1박가에서',
  min_price       INT          NULL     COMMENT '스크래핑 시점 최저 1박가 (원). 참고용',
  rating          DECIMAL(2,1) NULL     COMMENT '야놀자 평점',
  review_count    INT          NOT NULL DEFAULT 0 COMMENT '야놀자 리뷰 수',
  tags            JSON         NOT NULL COMMENT '["해변","바다뷰",...] — tags.name 의 배열. 편의시설·객실·이름·리뷰 키워드에서 규칙으로 도출',
  amenities       JSON         NULL     COMMENT '페이지의 편의시설 표기',
  room_types      JSON         NULL     COMMENT '객실 타입명',
  review_keywords JSON         NULL     COMMENT '[{"keyword":"청결","count":18,"tag":"청결","polarity":"+"}, ...] 상위 5개. 리뷰 원문은 저장하지 않음',
  description     VARCHAR(255) NULL     COMMENT '페이지 소개 문구 한 줄',
  source_url      VARCHAR(128) NULL,
  created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_stays_region (region),
  INDEX idx_stays_tags ((CAST(tags AS CHAR(48) ARRAY)))
) COMMENT='숙소 카탈로그 (핸디즈 지점 + 시연용 일반 숙소, 야놀자 공개 페이지에서)';

-- 화면에 보여주는 모든 선택지. 선택지가 구조화된 입력이므로 태그 가중치를 선택지 자체에 붙인다.
CREATE TABLE choices (
  kind        ENUM('quiz_q','quiz_opt','companion','purpose','region','reason_keep','reason_drop') NOT NULL,
  code        VARCHAR(32)  NOT NULL COMMENT 'quiz_q: "1" / quiz_opt: "1:2" / companion: 커플 / reason_drop: d_city',
  parent_code VARCHAR(32)  NULL     COMMENT 'quiz_opt 의 문항 코드',
  ord         INT          NOT NULL DEFAULT 0,
  text        VARCHAR(128) NOT NULL,
  image       VARCHAR(64)  NULL     COMMENT '선택지 사진 경로 (app/img/*.jpg). 취향 테스트 선택지에만',
  tag_weights JSON         NULL     COMMENT '[{"tag_id":12,"score":3}, ...] 또는 [{"parent_id":2,"score":2}] = 그 숙소의 그 범주(부모) 하위 태그에. tags.id 참조. 규칙 해석기가 읽는다',
  PRIMARY KEY (kind, code),
  KEY idx_choices_parent (kind, parent_code)
) COMMENT='입력 선택지 (문항·동행·목적·지역·이유) + 고정 태그 가중치';

-- ───────── 원본 ─────────

CREATE TABLE users (
  id          INT          NOT NULL AUTO_INCREMENT,
  name        VARCHAR(32)  NOT NULL COMMENT '취향 테스트 링크로 들어올 때 입력한 이름',
  nickname    VARCHAR(32)  NULL     COMMENT '가입 시 정한 닉네임. 기본값은 name, 언제든 변경 가능. NULL 이면 아직 게스트',
  joined_at   TIMESTAMP    NULL     COMMENT '가입 시각. NULL 이면 테스트만 한 게스트',
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) COMMENT='멤버 (게스트 → 가입)';

CREATE TABLE trips (
  id          INT          NOT NULL AUTO_INCREMENT,
  name        VARCHAR(64)  NOT NULL,
  companions  VARCHAR(16)  NOT NULL COMMENT 'choices(kind=companion).code',
  people      INT          NOT NULL DEFAULT 2,
  region      VARCHAR(16)  NOT NULL DEFAULT '무관' COMMENT 'choices(kind=region).code',
  location    VARCHAR(64)  NULL     COMMENT '세부 장소 (선택, 자유 입력: 해운대, 속초 시내 …)',
  start_date  DATE         NULL     COMMENT '체크인 (선택)',
  end_date    DATE         NULL     COMMENT '체크아웃 (선택)',
  purposes    JSON         NOT NULL COMMENT '["휴양","맛집"] — choices(kind=purpose).code 의 배열',
  created_by  INT          NOT NULL,
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted_at  TIMESTAMP    NULL     COMMENT '소프트 삭제. 원본 이벤트는 남긴다',
  chosen_stay_id VARCHAR(16) NULL   COMMENT '최종 결정한 숙소. 있으면 "○○이 묵었던 곳" 표시와 다음 여행의 이력에 쓴다',
  PRIMARY KEY (id),
  KEY idx_trips_created (created_at),
  KEY idx_trips_chosen (chosen_stay_id),
  CONSTRAINT fk_trips_creator FOREIGN KEY (created_by) REFERENCES users(id)
) COMMENT='여행 방 (컨텍스트)';

CREATE TABLE trip_members (
  trip_id     INT          NOT NULL,
  user_id     INT          NOT NULL,
  joined_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (trip_id, user_id),
  CONSTRAINT fk_trip_members_trip FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE,
  CONSTRAINT fk_trip_members_user FOREIGN KEY (user_id) REFERENCES users(id)
) COMMENT='여행 멤버';

CREATE TABLE trip_candidates (
  trip_id     INT          NOT NULL,
  stay_id     VARCHAR(12)  NOT NULL,
  source      ENUM('auto','manual') NOT NULL COMMENT 'auto = 컨텍스트 사전 추천, manual = 멤버가 담음',
  added_by    INT          NULL,
  added_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (trip_id, stay_id),
  CONSTRAINT fk_trip_candidates_trip FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE,
  CONSTRAINT fk_trip_candidates_stay FOREIGN KEY (stay_id) REFERENCES stays(id)
) COMMENT='여행 후보 숙소 (공동 후보 보드)';

-- 좋아요 현재 상태 (원본은 events 의 like/unlike). 목록의 "○○이 좋아하는 숙소예요!" 와 취향 학습(+1)에 쓴다.
CREATE TABLE stay_likes (
  trip_id     INT          NOT NULL,
  stay_id     VARCHAR(16)  NOT NULL,
  user_id     INT          NOT NULL,
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (trip_id, stay_id, user_id),
  CONSTRAINT fk_likes_trip FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE,
  CONSTRAINT fk_likes_stay FOREIGN KEY (stay_id) REFERENCES stays(id),
  CONSTRAINT fk_likes_user FOREIGN KEY (user_id) REFERENCES users(id)
) COMMENT='여행×숙소 좋아요 (현재 상태)';

-- 여행 안에서 숙소에 단 댓글. 링크가 있으면 등록 시점에 OpenGraph 미리보기를 읽어 함께 저장한다.
CREATE TABLE stay_comments (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  trip_id     INT          NOT NULL,
  stay_id     VARCHAR(16)  NOT NULL,
  user_id     INT          NOT NULL,
  text        VARCHAR(1000) NOT NULL,
  links       JSON         NULL     COMMENT '[{"url","title","description","image","site"}] — 본문의 URL 미리보기',
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_comments_trip_stay (trip_id, stay_id, created_at),
  CONSTRAINT fk_comments_trip FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE,
  CONSTRAINT fk_comments_stay FOREIGN KEY (stay_id) REFERENCES stays(id),
  CONSTRAINT fk_comments_user FOREIGN KEY (user_id) REFERENCES users(id)
) COMMENT='여행×숙소 댓글 (링크 미리보기 포함)';

-- 모든 취향 신호의 원본. append-only.
CREATE TABLE events (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  user_id       INT          NOT NULL,
  trip_id       INT          NULL,
  type          ENUM('quiz_answer','trip_created','trip_deleted','trip_decided','candidate_added','vote','vote_removed','like','unlike','comment','checkout_feedback') NOT NULL,
  payload       JSON         NOT NULL COMMENT 'quiz_answer: {answers:["1:2",...]} / vote: {stay_id, kind, reason, note} / checkout_feedback: {stay_id, rating, note}',
  created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_events_user (user_id, created_at),
  KEY idx_events_trip (trip_id, created_at),
  KEY idx_events_type (type),
  CONSTRAINT fk_events_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT fk_events_trip FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE SET NULL
) COMMENT='취향 신호 원본 (append-only)';

-- ───────── 해석 ─────────

-- 해석기가 이벤트 하나를 태그 가중치로 번역한 결과. 이벤트당 해석기별로 한 행. AI 해석기를 붙이면 같은 이벤트에 행이 하나 더 생긴다.
CREATE TABLE event_interpretations (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  event_id      BIGINT       NOT NULL,
  interpreter   VARCHAR(32)  NOT NULL COMMENT 'rules-v1 | ai-<model>',
  tag_deltas    JSON         NOT NULL COMMENT '[{"tag_id":11,"weight":3,"confidence":1.0,"evidence":"quiz 2:1"}, ...] — tags.id 참조',
  new_tags      JSON         NULL     COMMENT '[{"name":"바다뷰/일출","parent":"바다뷰","definition":"...","evidence":"..."}] — 해석기가 제안한 새 어휘',
  rationale     TEXT         NULL     COMMENT '사람이 읽는 해석 근거',
  created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_interp (event_id, interpreter),
  CONSTRAINT fk_interp_event FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
) COMMENT='해석: 이벤트 → 태그 가중치 (규칙 또는 AI)';

-- ───────── 파생 ─────────

-- 유저 × 컨텍스트 취향. context='base' 는 취향 테스트, 그 외는 동행 유형별 학습. 해석 결과의 합.
CREATE TABLE taste_profiles (
  user_id       INT          NOT NULL,
  context       VARCHAR(16)  NOT NULL COMMENT 'base | 커플 | 친구 | 가족',
  tag           VARCHAR(48)  NOT NULL,
  weight        INT          NOT NULL,
  interpreter   VARCHAR(32)  NOT NULL COMMENT '어느 해석기의 결과를 합한 것인가',
  updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, context, tag),
  CONSTRAINT fk_taste_profiles_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_taste_profiles_tag  FOREIGN KEY (tag)     REFERENCES tags(name)
) COMMENT='파생: 유저×컨텍스트 취향 (해석 결과의 합)';

-- 여행 안에서 학습된 키워드. scope='trip' 전체 합, 'member' 멤버별.
CREATE TABLE trip_keywords (
  trip_id       INT          NOT NULL,
  scope         ENUM('trip','member') NOT NULL,
  user_id       INT          NOT NULL DEFAULT 0 COMMENT 'scope=member 일 때 users.id, trip 이면 0 (PK 구성용)',
  tag           VARCHAR(48)  NOT NULL,
  weight        INT          NOT NULL,
  interpreter   VARCHAR(32)  NOT NULL,
  updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (trip_id, scope, user_id, tag),
  CONSTRAINT fk_trip_keywords_trip FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE,
  CONSTRAINT fk_trip_keywords_tag  FOREIGN KEY (tag)     REFERENCES tags(name)
) COMMENT='파생: 이 여행이 추구하는 키워드 (vote 해석의 합)';
