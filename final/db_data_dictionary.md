# DB 데이터 딕셔너리 (team2)

- 생성 기준: 실 DB(`team2`) 라이브 조회 (`DESCRIBE` / `SHOW CREATE TABLE` / `SELECT COUNT(*)`) — **2026-09-10 (재조회 3회차)**
- 총 테이블 수: **32개**
- 코드 기준: `auth.py` / `models.py` / `schemas.py` / `chatbot.py` / `rag_ingest.py` / `rag_search.py`
- ⚠ 이 DB는 팀이 계속 실시간으로 수정 중인 라이브 인스턴스입니다. 직전 스냅샷(2026-09-08) 대비 변동:
  - **스키마 변경 없음** — 32개 테이블 구성 자체는 동일. 이번 재조회는 이번 세션의 챗봇 작업(혼잡도 RAG 전환, 지시어 재작성, 동명이인 disambiguation, `get_travel_time` 연동 등)이 전부 애플리케이션 로직이라 DDL에 영향이 없었음을 확인하기 위한 것으로, 실제로 컬럼/테이블 추가·삭제는 전혀 없었음.
  - `event.event_img_url` 신규 컬럼 반영(2026-09-09 도입, `GET /events/{event_no}` 응답에 2026-09-10 추가) — 1,928건 중 1,735건 값 있음.
  - `travel_time_cache`/`kakao_api_daily_usage` 2개 테이블을 이 문서에 처음 포함(AL-02 이동시간 캐시 도메인 — 직전 스냅샷에서 누락돼 있었음, 실제로는 그 전부터 존재).
  - 행 수만 실사용/테스트 누적으로 다음과 같이 증가: `favorite_group`(13→21), `chat_session`(0→2), `chat_message`(0→16), `event`(1,984→1,928, 감소 — 팀에서 정리성 삭제 있었던 것으로 보임), `congestion`(35,448→30,576, 감소 — 동일), `trip`(3→15), `trip_interest`(9→41), `rute_artist_select`(9→124), `accom`(3→18), `trip_route`(0→18), `trip_route_event`(0→84), `visit_feedback`(0→6), `external_review`(0→437), `review`(0→1), `refresh_token`(2→6).
  - `review_source` 테이블은 이번 조회 시점에도 여전히 없음(계속 들쭉날쭉했던 테이블 — 삭제가 최종 확정된 것으로 판단됨).

각 테이블은 `컬럼명 | 타입 | NULL | 키 | 설명` 순으로 정리했고, 그 아래에 제약조건(PK/FK/UNIQUE/CHECK)을 원문 그대로 덧붙였습니다.

---

## 목차

| 영역 | 테이블 |
|---|---|
| 인증/계정 | user, user_status, auth, refresh_token, nationality, nat_lang, lang |
| 아티스트 | artist_group, artist, favorite_group |
| 카테고리/이벤트 | ctg_type, ctg, op_status, event |
| AL-02 이동시간 캐시 | travel_time_cache, kakao_api_daily_usage |
| 여행(Trip) | trip_density, trip, trip_interest, rute_artist_select, accom |
| 동선(Trip Route) | usage_status, trip_route, trip_route_event, visit_feedback |
| 리뷰 | external_review, review_opt, review |
| 통계·운영(챗봇이 RAG/Function Calling으로 사용) | congestion, event_op_hour |
| 챗봇 대화 (WBS 5.4.1) | chat_session, chat_message |

| 테이블 | 행 수 | 비고 |
|---|---|---|
| user | 14 | 실 유저 데이터 존재 — 테스트 시 주의 |
| user_status | 4 | 1활성 2정지 3삭제요청 4탈퇴 |
| auth | 4 | 1사용자 2총괄관리자 3중간관리자 4직원 |
| refresh_token | 6 | 현재 로그인 세션 |
| nationality | 249 | 전세계 국가 — nationality_no=1은 '가나' |
| nat_lang | 249 | 국적별 기본 언어 매핑, 전 국적 완료 |
| lang | 6 | |
| artist_group | 5 | |
| artist | 56 | |
| favorite_group | 21 | |
| ctg_type | 3 | 1행사 2성지 3관광 명소 |
| ctg | 12 | |
| op_status | 4 | 1준비중 2진행중 3종료됨 4취소됨 |
| event | 1,928 | ctg_type_no별 1(메인이벤트)/2(성지)/3(관광명소) 혼재. `event_img_url` 신규 컬럼 포함 |
| travel_time_cache | 4,907 | 카카오모빌리티 이동시간 lazy 캐시. 챗봇 `get_travel_time`이 읽기 전용으로 재사용 |
| kakao_api_daily_usage | 2 | 일일 호출 900건 하드 쿼터 카운터 |
| trip_density | 3 | 1여유 우선 2적당히 3많이 보기 |
| trip | 15 | |
| trip_interest | 41 | |
| rute_artist_select | 124 | |
| accom | 18 | |
| usage_status | 3 | 1대기중 2진행중 3진행완료 |
| trip_route | 18 | 일자 단위(2026-09 구조 변경) |
| trip_route_event | 84 | 이벤트 단위 |
| visit_feedback | 6 | |
| external_review | 437 | |
| review_opt | 4 | 리뷰 '한마디 더' 칩 |
| review | 1 | |
| congestion | 30,576 | 2026-09-10부터 챗봇은 RAG(ChromaDB)로만 조회 — `get_congestion_pattern` Function Calling은 완전히 제거됨 |
| event_op_hour | 9,100 | 챗봇 `get_poi_business_hours`가 사용(다중 event_no 지원) |
| chat_session | 2 | WBS 5.4.1 |
| chat_message | 16 | WBS 5.4.1 |

---

## 인증/계정

### user (14행)
로그인 계정.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| user_no | BIGINT | NO | PK | |
| login_id | VARCHAR(50) | NO | UNIQUE | 로그인 아이디(이메일) |
| login_pw | VARCHAR(255) | NO | | bcrypt 해시 |
| phone | VARCHAR(20) | YES | | |
| nationality_no | BIGINT | NO | FK→nationality | |
| nickname | VARCHAR(50) | NO | | 중복 허용(기획 확정) |
| status_no | BIGINT | NO | FK→user_status | |
| auth_no | BIGINT | NO | FK→auth | |
| lang_no | BIGINT | NO | FK→lang | |
| name | VARCHAR(50) | YES | | |
| gender | VARCHAR(10) | YES | | |
| birth | DATE | YES | | |
| created_at | DATETIME | YES | | |
| profile_img | VARCHAR(255) | YES | | 마이페이지 프로필 사진 경로 |

제약: `UNIQUE(login_id)` · `FK nationality_no→nationality` · `FK status_no→user_status` · `FK auth_no→auth` · `FK lang_no→lang`

### user_status (4행)
| status_no | status_nm |
|---|---|
| 1 | 활성 |
| 2 | 정지 |
| 3 | 삭제요청 |
| 4 | 탈퇴 |

`deps.py`가 로그인 이후 상태가 2/4로 바뀐 계정을 토큰 검증 시점에 즉시 차단하는 데 이 값을 하드코딩 참조함.

### auth (4행)
| auth_no | auth_nm |
|---|---|
| 1 | 사용자 |
| 2 | 총괄관리자 |
| 3 | 중간관리자 |
| 4 | 직원 |

### refresh_token (6행)
유저당 세션 1개 — 재로그인 시 기존 행을 덮어씀. 원문 토큰은 저장하지 않고 SHA-256 해시만 보관.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| refresh_token_no | BIGINT | NO | PK | |
| user_no | BIGINT | NO | UNIQUE, FK→user | 유저당 1개 |
| token_hash | VARCHAR(64) | NO | UNIQUE | SHA-256 해시 |
| issued_at | DATETIME | YES | | DEFAULT CURRENT_TIMESTAMP |
| expires_at | DATETIME | NO | | 발급 시각+14일 |
| revoked_at | DATETIME | YES | | 로그아웃/탈퇴 시 채워짐 |

### nationality (249행)
전세계 국가 코드. `nationality_no=1`은 '가나' — 과거 "1=대한민국" 가정은 더 이상 유효하지 않음(하드코딩된 곳 없어 기능 영향 없음).

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| nationality_no | BIGINT | NO | PK | |
| nationality_nm | VARCHAR(50) | NO | | |

### nat_lang (249행)
국적별 기본 언어 매핑, 전 국적 매핑 완료.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| nat_lang_no | BIGINT | NO | PK | |
| nationality_no | BIGINT | NO | UNIQUE, FK→nationality | 국적당 매핑 1개 |
| lang_no | BIGINT | NO | FK→lang | |

### lang (6행)
화면 언어.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| lang_no | BIGINT | NO | PK | |
| lang_nm | VARCHAR(50) | NO | | |

---

## 아티스트

### artist_group (5행)
| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| artist_group_no | BIGINT | NO | PK | |
| group_nm | VARCHAR(100) | NO | | |
| agency | VARCHAR(100) | YES | | |
| debut_dt | DATE | YES | | |
| fandom_nm | VARCHAR(100) | YES | | |
| created_at | DATETIME | YES | | |

### artist (56행)
| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| artist_no | BIGINT | NO | PK | |
| artist_nm | VARCHAR(100) | NO | | 활동명, "한글 (영문)" 형식(예: "정국 (Jung Kook)") |
| artist_group_no | BIGINT | NO | FK→artist_group | |
| birth | DATE | YES | | |
| real_nm | VARCHAR(100) | YES | | |
| gender | VARCHAR(10) | YES | | |
| origin | VARCHAR(100) | YES | | |
| agency | VARCHAR(100) | YES | | |
| created_at | DATETIME | YES | | |
| nationality_no | BIGINT | NO | FK→nationality | |

### favorite_group (21행)
유저의 즐겨찾기 그룹. 회원가입 시 1~2개 필수.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| favorite_group_no | BIGINT | NO | PK | |
| artist_group_no | BIGINT | NO | FK→artist_group | |
| user_no | BIGINT | NO | FK→user | |
| created_at | DATETIME | YES | | |

제약: `UNIQUE(artist_group_no, user_no)` — 같은 그룹 중복 즐겨찾기 방지. API 레벨(pydantic validator)에서도 요청 내 중복을 사전 차단.

---

## 카테고리/이벤트

### ctg_type (3행)
| ctg_type_no | ctg_type_nm |
|---|---|
| 1 | 행사 |
| 2 | 성지 |
| 3 | 관광 명소 |

### ctg (12행)
카테고리. `ctg_type_no=1`은 메인이벤트 분류(event.ctg_no가 참조), `2`/`3`은 선호 카테고리 선택지(구 `interest` 테이블 대체, `GET /interests`가 이 두 타입만 반환).

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| ctg_no | BIGINT | NO | PK | |
| ctg_nm | VARCHAR(50) | NO | | |
| ctg_type_no | BIGINT | NO | FK→ctg_type | |

> 참고: 예전에 있던 `interest` 테이블은 ERD 확정에 따라 완전히 삭제됨 — `trip_interest.ctg_no`가 그 역할을 대체.

### op_status (4행)
| op_status_no | op_status_nm |
|---|---|
| 1 | 준비중 |
| 2 | 진행중 |
| 3 | 종료됨 |
| 4 | 취소됨 |

event의 FK 대상이지만 코드에서 값으로 직접 조회하지는 않음.

### event (1,928행)
메인 이벤트/장소.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| event_no | BIGINT | NO | PK | |
| event_nm | VARCHAR(255) | NO | | **동명이인(중복) 다수** — 70종/264건, 그중 58종은 좌표까지 완전히 동일한 실제 같은 장소가 멤버별로 중복 등록된 경우. 챗봇은 이런 클러스터를 좌표 기준으로 묶어 처리하며, event_no 식별이 원칙 |
| start_dt | TIMESTAMP | YES | | |
| end_dt | TIMESTAMP | YES | | |
| event_desc | TEXT | YES | | RAG 청킹 원천(챗봇) |
| event_dtl | TEXT | YES | | RAG 보조 청킹 원천 — event_desc보다 값이 훨씬 적음 |
| event_img_url | VARCHAR(500) | YES | | **신규 컬럼**(2026-09-09 `migration_event_img_url.sql`). `GET /events/{event_no}` 응답에 2026-09-10 추가 반영. 1,928건 중 1,735건 이미 값 있음 |
| ctg_no | BIGINT | NO | FK→ctg | ctg_type_no=1인 것만 "메인이벤트" |
| add | VARCHAR(255) | NO | | 주소 원문(지역 전용 컬럼 없음) |
| post | VARCHAR(10) | YES | | 우편번호 |
| event_lat | DECIMAL(9,6) | NO | | |
| event_lon | DECIMAL(9,6) | NO | | |
| op_status_no | BIGINT | NO | FK→op_status | |
| artist_no | BIGINT | YES | — FK 없음 | ERD상 원래 FK 없는 게 정상. 행마다 artist_no/artist_group_no 중 하나만 채워지는 패턴(개별 멤버 vs 그룹 전체). `GET /events/{event_no}`·`GET /events/main` 응답 스키마가 이 컬럼을 필수(int)로 잘못 선언해 individual-member 행 조회 시 500이 나던 버그를 nullable로 수정 완료 |
| artist_group_no | BIGINT | YES | — FK 없음 | 위와 동일 |
| created_at | DATETIME | YES | | |

> `add_dtl` 컬럼은 팀에서 완전히 삭제됨(models.py 반영 완료).

---

## AL-02 이동시간 캐시

카카오모빌리티 다중 목적지 길찾기 실API 연동 — on-demand(lazy) 캐시와 하루 900건 하드 쿼터 카운터. 2026-09-10부터 챗봇 `get_travel_time` 도구도 이 캐시를 읽기 전용으로 재사용한다(API 재호출 없음, 캐시 미스면 정직하게 "정보 없음"으로 응답 — AL-02의 900/일 쿼터를 침범하지 않기 위한 설계).

### travel_time_cache (4,907행)
origin(이벤트/숙소/여행 시작·종료 핀) → event 이동시간 캐시. 복합 PK라 같은 조합이면 항상 갱신(UPSERT).

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| origin_type | ENUM('event','accom','trip_start_pin','trip_end_pin') | NO | PK | |
| origin_no | BIGINT | NO | PK | origin_type이 accom/event면 그 PK, trip_*_pin이면 trip_no |
| destination_event_no | BIGINT | NO | PK, FK→event | |
| travel_mode | ENUM('car','transit') | NO | PK | 현재는 car만 실사용 |
| duration_min | INT | NO | | |
| fetched_at | DATETIME | NO | | |

> 챗봇 `get_travel_time`은 항상 `origin_type='event'`로만 조회(장소↔장소 이동시간 질의 전용).

### kakao_api_daily_usage (2행)
카카오모빌리티 API 일일 호출 수 카운터.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| usage_date | DATE | NO | PK | |
| call_count | INT | NO | | 기본값 0, 900 도달 시 TravelTimeQuotaExceeded로 하드 락 |

> PK가 날짜라 자정이 지나면 새 행이 생기는 방식으로 자동 리셋(별도 배치 불필요). 리셋 기준은 DB 세션 UTC가 아니라 애플리케이션이 계산한 KST 자정.

---

## 여행 (Trip)

### trip_density (3행)
동선 스타일.
| trip_density_no | trip_density_nm |
|---|---|
| 1 | 여유 우선 |
| 2 | 적당히 |
| 3 | 많이 보기 |

### trip (15행)
여행 초안 — 이벤트 선택 + 기간이 확정된 시점에 생성.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| trip_no | BIGINT | NO | PK | |
| start_dt | DATE | NO | | |
| end_dt | DATE | NO | | |
| start_place | VARCHAR(255) | YES | | 출발지점(첫날) |
| end_place | VARCHAR(255) | YES | | 완료지점(마지막날) |
| event_no | BIGINT | NO | FK→event | |
| event_date | DATE | NO | | 멀티데이 이벤트 중 고른 하루 |
| user_no | BIGINT | NO | FK→user | |
| trip_density_no | BIGINT | YES | FK→trip_density | 생성 시점엔 NULL 가능(이후 단계에서 채워짐) |
| start_tm | DATETIME | YES | | |
| end_tm | DATETIME | YES | | |
| start_place_lat | DECIMAL(9,6) | YES | | 좌표는 프론트가 지도 SDK로 계산해서 전달(서버는 지오코딩 안 함) |
| start_place_lon | DECIMAL(9,6) | YES | | |
| end_place_lat | DECIMAL(9,6) | YES | | |
| end_place_lon | DECIMAL(9,6) | YES | | |
| trip_type_no | BIGINT | YES | FK 없음(죽은 컬럼) | 대응 trip_type 테이블 삭제됨, 코드에서 사용 안 함 |
| created_at | DATETIME | YES | | |

> `start_place`/`end_place` 계열은 `POST /trips` 요청·응답에서만 다룬다. `GET /trips`(목록)는 요약 카드용이라 안 내려주고, 단건 재조회(`GET /trips/{trip_no}`) API 자체가 없어서 재진입 시 서버에서 다시 불러오는 건 아직 불가능함.

### trip_interest (41행)
여행이 선택한 선호 카테고리(ctg 기반).

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| trip_interest_no | BIGINT | NO | PK | |
| trip_no | BIGINT | NO | FK→trip | |
| created_at | DATETIME | YES | | |
| rank | INT | YES | | 프론트가 보낸 배열 순서(1부터) |
| ctg_no | BIGINT | NO | FK→ctg | ctg_type_no 2/3만 허용(API 레벨 검증) |

제약: `UNIQUE(trip_no, ctg_no)` — 같은 여행에 같은 카테고리 중복 선택 방지.

### rute_artist_select (124행)
여행에서 실제로 보러 갈 멤버 선택. PK만 INT(다른 테이블은 전부 BIGINT).

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| select_no | INT | NO | PK | |
| artist_no | BIGINT | YES | FK→artist | |
| trip_no | BIGINT | YES | FK→trip | |

### accom (18행)
숙소. 여러 개 등록 가능(멀티 depot) — 날짜별로 그 날 체크인~체크아웃 범위에 해당하는 숙소가 그 날의 depot이 된다. 서로 다른 두 숙소의 유효 기간이 겹치는 입력은 `POST /trips` 생성 시점에 400으로 차단(정책 확정).

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| accom_no | BIGINT | NO | PK | |
| accom_nm | VARCHAR(100) | YES | | |
| accom_lon | DECIMAL(11,8) | YES | | **최종 스펙은 DECIMAL(11,8)** — 경도 오버플로우 대응으로 (10,8)에서 확장됨 |
| accom_lat | DECIMAL(11,8) | YES | | 위와 동일 |
| trip_no | BIGINT | NO | FK→trip | |
| check_in_dt | DATE | YES | | |
| check_out_dt | DATE | YES | | |
| add | VARCHAR(255) | YES | | |
| created_by | VARCHAR(50) | YES | | |
| created_at | DATETIME | YES | | |

---

## 동선 (Trip Route)

### usage_status (3행)
| usage_status_no | usage_status_nm |
|---|---|
| 1 | 대기중 |
| 2 | 진행중 |
| 3 | 진행완료 |

`batch.py`가 매일 자정(KST) `trip_route.usage_status_no`를 이 값 기준으로 자동 갱신.

### trip_route (18행)
**일자 단위** 동선 로우 — 하루당 1개. 그 날 방문하는 이벤트들은 `trip_route_event`에 자식으로 딸린다.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| trip_route_no | BIGINT | NO | PK | |
| trip_no | BIGINT | NO | FK→trip | |
| visit_day | SMALLINT | YES | | 몇 일차인지(1부터) |
| created_at | DATETIME | YES | | |
| usage_status_no | BIGINT | NO | FK→usage_status | 생성 시 항상 1(대기중) |

### trip_route_event (84행)
하루 동선(trip_route) 안의 개별 이벤트.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| trip_route_event_no | BIGINT | NO | PK | |
| trip_route_no | BIGINT | NO | FK→trip_route | |
| event_no | BIGINT | NO | FK→event | |
| seq | INT | YES | | 그 날 안에서의 방문 순서(1부터) |
| created_at | DATETIME | YES | | |

### visit_feedback (6행)
장소(그 날의 특정 이벤트)별 '좋아요'. 행이 있으면 좋아요, 없으면 아니다.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| visit_feedback_no | BIGINT | NO | PK | |
| trip_route_event_no | BIGINT | NO | FK→trip_route_event | |
| created_at | DATETIME | YES | | |

> ⚠ `trip_route_event_no`에 UNIQUE 제약은 걸려있지 않음 — 애플리케이션 로직(있으면 skip)으로만 중복 방지.

---

## 리뷰

> `review_source` 테이블은 팀이 삭제했습니다(이번 조회 시점 기준 없음). `external_review.source_no` 컬럼도 함께 삭제되어 출처(구글맵/카카오맵/캐치테이블) 라벨은 더 이상 없습니다.

### external_review (437행)
장소(이벤트)별 외부 리뷰. 챗봇 RAG의 리뷰 청크 원천이자 AL-02 등급 게이트(`total_score` 4.5↑=A, 3.5~4.5=B 적재, 3.5미만/NULL=C 제외) 대상.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| external_review_no | BIGINT | NO | PK | |
| review | TEXT | YES | | |
| event_no | BIGINT | NO | FK→event | |
| rating | DECIMAL(2,1) | YES | | |
| fake_score | DECIMAL(5,2) | YES | | |
| total_score | DECIMAL(5,2) | YES | | FAN:GO 추천점수, RAG 게이트 기준 |
| author_id | VARCHAR(100) | YES | | |
| created_at | DATETIME | YES | | |

제약: `CHECK (rating IS NULL OR (rating BETWEEN 0.5 AND 5 AND rating*10 % 5 = 0))`

### review_opt (4행)
리뷰 작성 화면의 '한마디 더' 칩(단일선택, 선택사항).

| opt_no | opt_nm | sentiment |
|---|---|---|
| 1 | 취향을 저격했어요! | 긍정 |
| 2 | 여유로웠어요! | 긍정 |
| 3 | 시간이 빠듯했어요 | 부정 |
| 4 | 추천이 부적절했어요 | 부정 |

제약: `CHECK (sentiment IN ('긍정','부정'))`

### review (1행)
여행 리뷰 — trip당 최대 1개, 생성 전용(수정 API 없음). 여행 전체를 통틀어 맨 마지막에 한 번만 평가(일자별 아님).

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| review_no | BIGINT | NO | PK | |
| review_content | TEXT | YES | | 선택 |
| opt_no | BIGINT | YES | FK→review_opt | 선택(단일선택) |
| written_at | TIMESTAMP | NO | | 서버가 요청 시각으로 채움 |
| rating | DECIMAL(2,1) | YES | | 선택, 0.5~5.0·0.5단위 |
| trip_no | BIGINT | NO | UNIQUE, FK→trip | |
| created_at | DATETIME | YES | | |

제약: `UNIQUE(trip_no)` · `CHECK (rating IS NULL OR (rating BETWEEN 0.5 AND 5 AND rating*10 % 5 = 0))`
API 레벨에서 `opt_no`/`rating`/`review_content` 셋 다 비어있으면(공백 문자열 포함) 422로 거부.

---

## 통계·운영 (챗봇이 RAG/Function Calling으로 사용)

### congestion (30,576행)
혼잡도(다른 팀 AL-02 관리). event_no당 weekday(1~7)×hour_of_day(0~23) 조합만큼 행이 있는 게 정상 구조.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| cong_no | BIGINT | NO | PK | |
| event_no | BIGINT | NO | FK→event | |
| weekday | VARCHAR(2) | NO | | **'1'~'7' 숫자 문자열**(ISO-8601, 1=월). event_op_hour.op_dt와 인코딩이 다름(그쪽은 한글) |
| hour_of_day | TINYINT | NO | | 0~23 |
| day_avg | DECIMAL(4,1) | NO | | 요일 평균 혼잡도 0~100 |
| week_avg | DECIMAL(4,1) | YES | | 주간 평균 혼잡도 0~100 |
| day_ratio | DECIMAL(4,2) | YES | GENERATED | day_avg/week_avg (STORED). 앱에서 값 안 씀 |
| cong_level | TINYINT | YES | GENERATED | 0한산 1보통 2혼잡 3매우혼잡 (STORED) |
| created_at | DATETIME | YES | | DEFAULT CURRENT_TIMESTAMP |

제약: `UNIQUE(event_no, weekday, hour_of_day)` · `CHECK` 다수(day_avg/week_avg/hour_of_day/weekday 범위)

> ⚠ **2026-09-10, 챗봇 아키텍처 변경**: 이전에는 `get_congestion_pattern` Function Calling이 이 테이블을 직접 SQL 조회했으나, RAG 정확도 개선을 위해 완전히 RAG(ChromaDB)로 대체됨. `(event_no, weekday)` 단위로 자연어 문서를 생성해 임베딩하고, 그 위에서만 답변한다 — 직접 SQL 조회 경로는 코드에서 제거됨(`get_congestion_pattern` 함수 자체가 삭제됨).
>
> ⚠ 동명이인(같은 event_nm) event 클러스터가 있을 때, 클러스터 내 실제로 혼잡도 데이터가 있는 event_no만 정확히 골라내는 disambiguation 로직이 `rag_search._canonicalize_event_nos()`에 구현되어 있음 — 다수 event_no로 흩어져 있어도 대표 1개로 잘못 좁히지 않고 전체 클러스터를 유지한 뒤, 실제 데이터가 있는 쪽으로 필터링한다.

### event_op_hour (9,100행)
운영시간(다른 팀 관리). 챗봇 `get_poi_business_hours`와 `GET /trips/{trip_no}/routes`의 `business_hours`가 조회.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| op_hour_no | BIGINT | NO | PK | |
| open_tm | VARCHAR(5) | YES | | "HH:MM" |
| close_tm | VARCHAR(5) | YES | | "HH:MM" |
| event_no | BIGINT | NO | FK→event | |
| op_dt | VARCHAR(50) | YES | | ⚠ **컬럼명과 달리 캘린더 날짜가 아니라 '월'~'일' 한글 요일 문자열**(실측 확인). congestion.weekday와 인코딩이 다름 |
| created_at | DATETIME | YES | | |

제약: `UNIQUE(event_no, op_dt)`. 한 event_no에 op_hour가 있으면 항상 요일 7개가 전부 채워짐(부분만 채워진 경우 없음, 실측 확인).

> ⚠ **2026-09-10, 동명이인 대응 확장**: `get_poi_business_hours(db, event_nos, op_dt=None)`가 단일 event_no뿐 아니라 event_no 리스트도 받도록 확장됨 — 동명이인 클러스터에서 실제로 운영시간 데이터가 있는 event_no를 순서대로 시도해 찾는다("데이터 있는 것부터" 원칙, RAG 쪽과 동일한 사고방식). 클러스터당 정확히 1개 event_no에만 데이터가 있는 게 실측 확인된 패턴(여러 event_no에 서로 다른 값이 공존하는 사례는 없음).

---

## 챗봇 대화 (WBS 5.4.1)

### chat_session (2행)
챗봇 멀티턴 대화 세션. `POST /chat`이 `chat_session_no` 없이 호출되면 새로 생성.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| chat_session_no | BIGINT | NO | PK | |
| user_no | BIGINT | NO | FK→user | |
| created_at | DATETIME | YES | | |
| last_message_at | DATETIME | YES | | `GET /chat/sessions` 목록 정렬 기준 |

### chat_message (16행)
세션 내 개별 메시지.

| 컬럼 | 타입 | NULL | 키 | 설명 |
|---|---|---|---|---|
| chat_message_no | BIGINT | NO | PK | |
| chat_session_no | BIGINT | NO | FK→chat_session | |
| role | VARCHAR(10) | NO | | `CHECK (role IN ('user','assistant'))` |
| content | TEXT | NO | | role='user' 행은 사용자가 실제로 입력한 원문 그대로 저장됨 — "거기", "거기 말고" 같은 지시어를 해석해 재작성한 질의(`routed_query`)로 덮어쓰지 않는다(지시어 재작성은 라우팅/검색 단계에서만 쓰이는 휘발성 값) |
| sources | JSON | YES | | role='assistant'에만 채워짐(RAG/Function Calling 근거 event_no 배열, 같은 event_no 중복은 순서를 보존해 제거) |
| is_fallback | TINYINT(1) | YES | | role='assistant'에만 채워짐. role='user' 행은 sources/is_fallback 둘 다 NULL |
| created_at | DATETIME | YES | | |

제약: `FK chat_session_no→chat_session` · `CHECK (role IN ('user','assistant'))`

> 답변 생성 시 컨텍스트로 재사용하는 대화 이력은 최근 3턴(메시지 6개)로 제한됨(`auth.py`의 `CHAT_HISTORY_TURNS` 상수). Intent Router/Function Calling/RAG 판단은 지시어가 재작성된 질의(`routed_query`)를 보고, 최종 답변 생성과 `chat_message` 저장은 원문 그대로의 `query`를 사용한다.
>
> ⚠ 최종 답변 생성(LLM 호출)이 실패하면 더 이상 200 OK + 가짜 사과 메시지(`is_fallback=false`)로 응답하지 않는다. `FinalGenerationError`를 발생시켜 `/chat`이 **502**를 반환하며, 실패한 턴은 `chat_message`에 저장하지 않는다(사용자 질문 행도 함께 저장 안 됨 — 트랜잭션 단위로 묶여있어 실패 시 롤백).

---

## 삭제/변동된 테이블·컬럼 (참고)

- **interest**: 관심사 ctg 기반 전환에 따라 `DROP TABLE`로 완전히 삭제됨. `trip_interest.ctg_no`가 그 역할을 대체.
- **reaction**: 삭제됨. `visit_feedback`에서 `reaction_no` 컬럼도 함께 제거됨(현재는 행 존재 여부만으로 좋아요 판단).
- **trip_type**: 삭제됨. `trip.trip_type_no`는 죽은 컬럼으로 남아있음(어떤 코드도 사용하지 않음).
- **event.add_dtl**: 팀이 완전히 삭제함.
- **review_source / external_review.source_no**: 팀이 삭제함. 출처 라벨이 없어져 `ExternalReviewOut.source_nm` 필드도 API 응답에서 제거됨.
- **챗봇 `get_congestion_pattern` Function Calling**: 2026-09-10 완전히 제거되고 RAG(ChromaDB)로 대체됨(위 congestion 섹션 참고).
