# FAN:GO (구 KTP) 프로젝트 종합 정리

- 정리 시점: 2026-09-08 (최초 작성) / **2026-09-09 바코클(백엔드 구현 세션)이 직접 검토·수정**
- 정리 범위: 지금까지 진행된 스키마 설계, 백엔드 구현, 챗봇(Phase 2) 개발, 문서 정합성 점검, ERD 업데이트 항목까지 프로젝트 전체 히스토리
- 참고: 이 문서는 대화 중 공유된 내용을 기준으로 정리한 것이라, 실제 코드/DB와 다른 부분이 있을 수 있습니다. 특히 "확인 필요"라고 표시된 항목은 팀 확인이 필요합니다.
- **2026-09-09 수정 내역**: 실제 구현을 담당한 바코클 세션이 원문을 직접 검토해 아래를 고쳤습니다 — ① 챗봇 아키텍처가 "LangChain 사용"으로 잘못 적혀있던 것을 실제(OpenAI SDK 직접 호출, LangChain 미사용)로 정정, ② `GET /trips` 아티스트 정보 추가(6-1절) 및 congestion 데이터 적재 스토리(2-2절)가 통째로 빠져있어 추가, ③ 8절/11절이 하루 전 시점에 멈춰있어 그 사이 실제로 처리 완료된 항목(2~5번)을 반영, ④ `event_op_hour` 채움 비율 등 stale한 수치 갱신, ⑤ "POST /trips(생성/수정)"이라는 부정확한 표현을 "생성 전용"으로 정정. 이 세션이 직접 확인 못 한 항목(ERD 대조, 프론트 공지 여부, 챗봇 명세서 v2.0 문서 등)은 손대지 않고 그대로 뒀습니다.

---

## 1. 프로젝트 개요

**FAN:GO**(내부 개발 코드명 KTP)는 해외(K-pop 팬덤이지만 국내 거주자가 아닌) K-pop 팬을 대상으로 한 여행/동선 추천 앱입니다. 관리자 페이지 이름은 "KTP ADMIN"입니다.

핵심 엔티티는 다음과 같이 정리되어 있습니다.

| 기획 용어 | 대응 테이블 | 비고 |
|---|---|---|
| 이벤트/장소 | `event` | 메인이벤트·성지·관광명소 혼재 |
| 유저동선(일자 단위) | `trip_route`, `trip_route_event` | |
| 일정(여행 헤더) | `trip` | |
| 관심사(다중선택) | `trip_interest` | 과거 `interest` 테이블 → `ctg` 기반으로 전환 완료 |
| 후기 | `review` | 별점 + 단일선택 후기옵션(`review_opt`) |
| 유저 | `user` | 국적 기반 추천 언어, 화면에서 직접 변경 가능 |
| 그룹/아티스트 | `artist_group`, `artist` | |
| 선호 그룹(다중선택, 가입 시 선택) | `favorite_group` | |

**백엔드 스택**: Python FastAPI + SQLAlchemy + MySQL(`team2` 스키마). 프로젝트 폴더명 "final" (`auth.py`, `database.py`, `deps.py`, `main.py`, `models.py`, `schemas.py`, `security.py`). 실제 구현은 별도 코딩 어시스턴트("바코클")가 담당하고, 이 세션은 주로 스키마 설계/이슈 트리아지/바코클에게 넘길 프롬프트 작성 역할을 맡고 있습니다. 개발 환경은 Windows 머신(`C:\workspaces\final`, 디바이스명 "asiae")이며, AWS RDS를 실 DB로 사용 중입니다(클라우드 샌드박스에서 직접 SQL 접속은 네트워크 제약으로 불가능해, `.sql` 마이그레이션 파일 형태로 전달).

**프론트엔드**: Vite 기반, dev 서버 `localhost:5173`, FastAPI CORS 설정 완료. 회원가입 화면은 이미 구현되어 있고(로그인ID 중복확인, 닉네임, 전화번호, 비밀번호+확인, 이메일, 국적 선택 → 추천 언어, 최애 그룹 다중선택), 그 외 화면은 순차 진행 중입니다.

**인증 흐름**: `POST /signup`, `POST /signin`(JWT, httpOnly 쿠키 + 응답 바디), `GET /me`(`deps.get_current_user`). `login_id`(로그인용 문자열)와 `user_no`(PK)는 분리되어 있습니다.

---

## 2. 데이터베이스 스키마 — 현재 기준 (2026-09-08, 30개 테이블)

실 DB(`team2`) 라이브 조회(`DESCRIBE`/`SHOW CREATE TABLE`/`SELECT COUNT(*)`) 기준 최신 데이터 딕셔너리를 이미 PDF로 전달드렸습니다(`db_data_dictionary.pdf`). 표/제약조건 전체를 다시 보시려면 그 파일을 참고해 주세요. 여기서는 영역별 테이블 목록과 행 수만 요약합니다.

| 영역 | 테이블 |
|---|---|
| 인증/계정 | user, user_status, auth, refresh_token, nationality, nat_lang, lang |
| 아티스트 | artist_group, artist, favorite_group |
| 카테고리/이벤트 | ctg_type, ctg, op_status, event |
| 여행(Trip) | trip_density, trip, trip_interest, rute_artist_select, accom |
| 동선(Trip Route) | usage_status, trip_route, trip_route_event, visit_feedback |
| 리뷰 | external_review, review_opt, review |
| 통계·운영(챗봇 Function Calling 사용) | congestion, event_op_hour |
| 챗봇 대화(WBS 5.4.1) | chat_session, chat_message |

행 수가 큰 테이블: `event` 1,984건, `congestion` 35,448건, `event_op_hour` 9,100건, `nationality`/`nat_lang` 각 249건. 나머지는 대부분 코드성 테이블(수십 건 이하)이거나, 아직 실사용이 적은 테이블(`trip`/`trip_interest`/`rute_artist_select`/`accom` 등은 한 자릿수~9건 수준으로 계속 늘어나는 중)입니다.

### 2-1. 최근 스키마 변경/삭제 이력

- **`interest` 테이블 삭제** — 관심사 로직이 `ctg` 기반으로 전환되면서 `DROP TABLE`. `trip_interest.ctg_no`가 그 역할을 대체(`ctg_type_no` 2/3만 허용).
- **`reaction` 테이블 삭제** — `visit_feedback`도 `reaction_no` 컬럼을 제거하고, 행 존재 여부만으로 "좋아요"를 판단하는 구조로 단순화.
- **`trip_type` 테이블 삭제** — `trip.trip_type_no`는 죽은 컬럼으로 남아있으나 어떤 코드도 사용하지 않음.
- **`event.add_dtl` 컬럼 삭제**.
- **`review_source` 테이블 및 `external_review.source_no` 컬럼 삭제** — 출처(구글맵/카카오맵/캐치테이블) 라벨 제거. 이로 인해 `GET /events/{event_no}` 응답에서 `source_nm` 필드도 제거 대상(아래 9절 참고). `review_source` 테이블이 한때 빈 상태로 재생성됐다가 다시 사라진 것이 관찰되어, 안정성은 계속 지켜봐야 함(확인 필요). **바코클 가설**: 이 저장소(`auth.py`/`models.py`)엔 `review_source` 참조가 전혀 없어 우리 코드가 원인은 아님을 확인함 — `main.py`가 서버 기동 시마다 `Base.metadata.create_all()`(모델에 있는데 DB에 없는 테이블만 생성)을 호출하는 패턴이라, 팀 내 다른 누군가가 아직 `ReviewSource` 모델이 남아있는 구버전 코드를 로컬에서 돌리고 있고 그 서버가 재시작될 때마다 재생성되는 것으로 추정(빈 테이블로 반복 재생성되는 패턴과 일치) — 확정은 아니고 팀 확인 필요.
- **`chat_session`/`chat_message` 신규 추가** — WBS 5.4.1, 아래 6절에서 상세 설명.
- **`trip.end_place`/`end_place_lat`/`end_place_lon` 신규 추가** — 아래 8절에서 상세 설명.
- **`congestion.week_avg` NULL 백필 완료** — 이제 NULL 0건.
- **`event.event_nm` 길이 확장** — VARCHAR(100) → VARCHAR(255) (변경 이력 있음, ERD 갱신 필요 — 10절 참고).

### 2-2. congestion 데이터 적재 (사용자가 직접 발견/수정 요청)

팀(다른 담당자)이 넘긴 혼잡도 원본 CSV를 `congestion` 테이블에 넣는 SQL 스크립트(`insert_congestion.sql`)를 사용자가 실행했더니 DB에 아무것도 안 들어가는 문제가 있었음 — 원인 분석 결과 스크립트에 버그 2개가 있었음:
1. `event` 테이블과 조인할 때 `place_name`이라는 존재하지 않는 컬럼명 사용(실제는 `event_nm`).
2. `weekday` 값을 "월"~"일" 한글 그대로 넣으려 해서 `congestion.weekday`가 요구하는 '1'~'7' 숫자 문자열(CHECK 제약 있음) 포맷과 안 맞음.

두 버그를 고쳐서 재실행 → **35,448행 적재 성공**. 이후 사용자 요청으로 `week_avg` NULL 백필(같은 `event_no`+`hour_of_day` 그룹의 `day_avg` 평균으로 채움, 2-1절 참고)과 `source` 컬럼 DROP까지 완료.

⚠ **남은 데이터 품질 이슈**: 적재 스크립트가 `event_nm`(이름) 기준으로 조인해서, DB에 동명 event가 여러 개 있는 경우(예: "에스엠엔터테인먼트" 29건, "경복궁" 10건 이상) 이름만 같고 실제로 다른 장소일 수 있는 event들이 같은 혼잡도 패턴을 그대로 공유하게 됨 — `event_no` 식별자 기준 재매핑 필요 여부는 팀 확인 대기 중.

---

## 3. 챗봇(Phase 2) — 아키텍처 및 구현 현황

공식 스펙 문서: "FAN:GO 챗봇(Phase 2) 기능명세서 v2.0"(docx, 2026-09-08 전달).

**아키텍처**: FastAPI + `gpt-4o-mini`(OpenAI SDK 직접 호출). ⚠ 스펙 원안엔 "FastAPI + LangChain"으로 돼 있었지만, 기존 코드베이스에 LangChain 의존성이 전혀 없어서 도입하지 않고 OpenAI SDK(`openai` 패키지)를 직접 쓰는 쪽으로 판단·구현함(바코클 판단, 사용자 확인 완료). Intent Router + 최종 답변 생성 모두 `gpt-4o-mini` 사용. Intent Router는 Path A(Function Calling)/Path B(RAG)/Path C(둘 다) 중 하나로 분기. 구조화 데이터는 MySQL(`team2`), 비정형 검색은 ChromaDB(`./rag_store/chroma`, cosine space, FAISS 대신 바코클이 선택). 임베딩 모델은 한국어 특화 `snunlp/KR-SBERT-V40K-klueNLI-augSTS`(768차원).

**RAG 소스**: `event.event_desc`/`event_dtl`(evidence_tier="A", 공식 정보) + `external_review.review`(품질 게이트: `total_score` >= 4.5는 A, 3.5~4.5는 B — 둘 다 적재, 3.5 미만/NULL은 C — 제외). 매일 03:00 KST 배치 적재(`rag_ingest.py`). 실제 첫 적재 결과: **event_chunks 515건 / review_chunks 0건**(`external_review`가 그 시점 0건이라 예상된 결과 — 리뷰 데이터 들어오면 다음 배치부터 자동 반영). 이후 event 데이터가 계속 늘면서 재적재 시 1,087건까지 증가 확인. 검색 전 `rag_search.py`가 아티스트/그룹/카테고리명을 `artist_no`/`ctg_no`/`event_no`로 먼저 해석(메타데이터 필터). 유사도 임계값은 스펙상 0.75였으나 실측 후 0.45로 재조정(팀 승인).

**Function Calling 도구 (구현 완료, 2개)**
- `get_congestion_pattern` — `congestion` 테이블 조회. `poi_ids`가 정확히 1개 `event_no`로 좁혀질 때만 실행, 그렇지 않으면 "근거 없음" 폴백. 특정 요일+시간대 조회, 무슬롯 요약(상위/하위 3개 패턴), 동명이인 다수(예: "N서울타워") 모호성 시 정직하게 거절하는 3가지 케이스 실측 테스트 통과.
- `get_poi_business_hours` — `event_op_hour` 조회. 실측 중 스펙과 다른 점 발견: `op_dt`는 DATE가 아니라 VARCHAR(50) 한글 요일 문자열("월"~"일")이라 함수 시그니처를 `op_dt: str | None`으로 맞춤. `event_op_hour` 데이터는 계속 늘고 있음 — 확인 시점마다 62%(1,225건) → 65%(1,300/1,984건)로 증가, 다른 팀이 계속 채우는 중.
- **요일 포맷 스키마 점검(2026-09-08 추가 요청, 완료)**: `get_congestion_pattern`의 `weekday`('1'~'7' 숫자)와 `get_poi_business_hours`의 `op_dt`('월'~'일' 한글)가 서로 다른 포맷인데, gpt-4o-mini에 넘기는 OpenAI Function Calling 스키마엔 둘 다 느슨한 `"type":"string"`으로만 정의돼 있어 모델이 포맷을 헷갈릴 위험이 있었음(예: congestion에 "월요일"을 보내거나 business_hours에 "1"을 보내는 경우) → 각각 `enum:["1"..."7"]` / `enum:["월"..."일"]`로 스키마 레벨에서 못박음. 두 도구로 재검증 완료, 정상 동작.

**보류/제외된 도구**
- `get_travel_time` — AL-02의 `travel_time_cache` 테이블(아직 미존재)에 대한 읽기 전용 조회로 범위를 좁혔으나, Phase 1에서는 완전히 제외하기로 결정.
- 개인화된 동선 기반 질의(`trip`/`trip_route`/`trip_route_event` 참조)는 이번 Phase 명시적으로 범위 외.

**동명이인(event_nm 중복) 처리**: DA팀이 데이터 정제(dedup)를 하지 않기로 결정. 대신 `rag_search.py`의 `_canonicalize_event_nos()`가 동일 `event_nm`을 `(artist_group_no, artist_no)` 기준으로 그룹화 — 두 키가 모두 non-null이고 일치하면 진짜 중복(대표 `event_no`로 병합, `MIN` 사용), 그렇지 않으면 서로 다른 장소로 간주해 모호 응답 유지. 실측 검증: "경복궁"은 11건이 실제로 아티스트별 별개 성지(병합 안 함)였고, "갤러리카페 휴가"(event_no 170/173, 둘 다 artist_group_no=2)는 실제 중복이라 정상적으로 하나로 병합됨.

**할루시네이션 가드**: 근거(Function Calling 결과 또는 RAG 검색 결과)가 전혀 없으면 LLM 호출 자체를 생략하고 고정 폴백 답변 반환.

**알려진 한계**: Path A/C는 `tool_choice="required"`로 고정(→ "auto"로 바꾸면 이미 알고 있는 장소는 도구 호출을 건너뛰어 혼잡도 조회를 놓치는 부작용 발견). 그 대가로 혼잡도와 무관한 질문에도 가끔 혼잡도 도구가 실행되는 부작용이 있으나(할루시네이션은 아니고 실제 DB 데이터), Function Calling 도구가 늘어나며 자연히 개선되는 추세.

**5턴 확장 테스트(추가 5케이스) 전부 통과**: Path B 단독 동작, Path C의 출처 중복 제거(같은 장소가 Function Calling+RAG 둘 다에서 잡혀도 1건으로), 0.45 임계값 및 "Function Calling 결과 없음 → RAG 재시도 → 그래도 임계값 미달 → 정직한 폴백" 체인, C등급(평점 3.5 미만) 리뷰가 절대 ChromaDB에 적재되지 않는 것(임시로 평점 2.0 리뷰를 넣어 확인 후 제거)까지 확인 완료. → 이로써 챗봇 Phase 1 백엔드/AI 측 작업은 사실상 종료 (5.4.2~5.4.4 완료, 5.4.5 API 계약 문서화, 5.4.6 테스트 커버리지 확장). 남은 것은 외부 입력 대기: `event_op_hour` 데이터(다른 팀), `event_nm` 정제 여부(DA팀 — 재확인 결과 정제 안 하기로 최종 확정).

---

## 4. 챗봇 대화 이력 저장 (WBS 5.4.1, `chat_session`/`chat_message`) — 구현 완료

문서 정합성 점검(9절) 중 WBS 항목 "5.4.1 chat_sessions/chat_messages API 설계"(담당 박예진)가 실제로는 구현되지 않고 있었다는 것을 발견 → 사용자가 지금 구현하기로 결정 → 바코클에게 요청 → **구현 완료**.

- `chat_session(chat_session_no PK, user_no FK→user, created_at, last_message_at)`
- `chat_message(chat_message_no PK, chat_session_no FK→chat_session, role VARCHAR(10) CHECK(user/assistant), content TEXT, sources JSON nullable, is_fallback TINYINT(1) nullable, created_at)`
- `POST /chat`에 `chat_session_no` 요청/응답 필드 추가(null이면 신규 세션 생성, 값이 있으면 본인 소유 검증 후 이전 대화를 컨텍스트로 로드).
- 기존 챗봇 로직(Intent Router/Function Calling/RAG/할루시네이션 가드)은 건드리지 않음 — 이전 대화는 `chatbot.generate_final_answer()`에 별도 파라미터로만 주입.
- History window(컨텍스트로 재사용하는 이전 대화 길이)는 최근 3턴(메시지 6개)로 결정, `auth.py`의 `CHAT_HISTORY_TURNS` 상수로 조정 가능. Intent Router/Function Calling/RAG 판단 자체는 여전히 "이번 메시지만" 보고, 최종 답변 생성 단계에만 히스토리가 반영됨.
- 4케이스 테스트 통과: 새 세션 생성 / 존재하지 않는 세션 404 / 타인 세션 403 / 이어서 질문 시 맥락 반영.
- 이어서 `GET /chat/sessions`(세션 목록, 본인 것만, `last_message_at` 최신순, 프론트 채팅목록 UI를 고려해 스펙에 없던 `preview`(첫 user 메시지 40자 요약) 필드를 바코클이 자체 추가), `GET /chat/sessions/{no}/messages`(시간순 전체 조회, 동일한 소유권 검증)도 구현 완료. 6케이스 테스트(목록조회/특정세션조회/404/403/타유저 교차노출 없음/세션 cleanup) 전부 통과.
- **알려진 한계**: "거기 말고 다른 데는?"처럼 지시어로 이전 턴을 가리키는 질문은 여전히 폴백으로 빠짐 — Intent Router/RAG가 "이번 메시지만" 보는 기존 설계라서 지시어를 실제 장소로 치환하지 못함. 버그는 아니고("기존 로직 안 건드림" 요청과 일치하는 결과) 실사용 관점에서는 아직 "완전한 자연스러운 대화"는 아니라는 한계 — 추후 Intent Router 개선 후보.
- → **WBS 5.4.1 항목 전체 구현 완료로 종결.**

---

## 5. 트립 라우트 상세 화면 — 영업시간/고정 스케줄 표시

**영업시간(`business_hours`)**: `GET /trips/{trip_no}/routes`(`list_trip_routes`)의 `events[]` 항목마다 `business_hours: {has_data, is_closed, open_tm, close_tm}` 객체 추가. `trip.start_dt` + `visit_day`로 실제 캘린더 날짜를 계산 → 한글 요일 도출 → `event_op_hour(event_no, op_dt)` 조회하는 방식. `has_data:false`=해당 event_no에 대한 `event_op_hour` 행 자체가 없음(데이터 미제공), `has_data:true`+`is_closed:true`=요일은 매칭됐지만 `open_tm`/`close_tm`이 둘 다 NULL(진짜 휴무일), `has_data:true`+`is_closed:false`="HH:MM" 문자열 정상 반환, `business_hours:null`=아직 `visit_day` 미설정. 실측 3케이스(매일 동일시간/데이터없음/요일별 상이) + 실제 휴무 1건으로 검증 완료.

**고정 스케줄(`fixed_schedule`)**: 콘서트 등 행사(`ctg_type_no=1`)는 시작 시각만 기록되는 경우가 많아, `business_hours`와 별개로 `fixed_schedule: {start_tm, end_tm}`(`end_tm` nullable)을 `event.start_dt`/`end_dt`에서 시간 부분만 추출해 추가 요청 — 사용자가 나중에 미룰지 지금 할지 고민하다가 지금 진행하기로 결정, `ctg_type_no=1`에만 적용(`business_hours`/`event_op_hour`는 행사엔 해당 없음). `fixed_schedule`이 채워지면 해당 이벤트의 `business_hours`는 생략/null 처리.
- **엣지케이스 처리**: 3일짜리 콘서트처럼 `start_dt="날짜 00:00:00"`~`end_dt="날짜 23:59:59"`로 전체 기간이 저장된 경우, 시간 부분을 그대로 뽑으면 "시작 00:00"이라는 잘못된 표시가 나올 수 있어 — 이 정확한 00:00:00~23:59:59 패턴은 "구체적 시간 모름"으로 간주해 `fixed_schedule: null` 반환(`business_hours`의 `has_data:false`와 동일한 취급).

---

## 6. 트립 생성 화면 — 완료지점(마지막날) 신규 필드

"추천 일정의 기간을 알려주세요" 화면이 기존 출발지점(첫날, `start_place`/`start_place_lat`/`start_place_lon`)에 더해 완료지점(마지막날)도 수집하도록 변경. `trip` 테이블에 짝꿍 컬럼이 없어 신규 추가 요청:
- `trip.end_place` VARCHAR(255)
- `trip.end_place_lat` / `end_place_lon` DECIMAL(9,6)

`POST /trips`(**생성 전용** — trip 수정/PUT·PATCH API는 애초에 없음)가 출발·완료 지점+좌표를 모두 받아 저장하고, 응답에도 반환하도록 요청. 좌표는 서버 지오코딩이 아니라 프론트 지도 SDK 계산값을 그대로 받는 전제(바코클 확인 완료 — 기존 `accom_lat/lon`, `event_lat/lon`과 동일 패턴이라 서버가 지오코딩할 필요 없음). 2026-09-08 기준 이 3개 컬럼과 `POST /trips` 연동까지 완료된 것으로 최신 데이터 딕셔너리에 반영됨. 다만 `GET /trips`(목록)는 요약카드용이라 이 값들을 내려주지 않고, 단건 재조회 API(`GET /trips/{trip_no}`) 자체가 없어서 화면 재진입 시 서버에서 다시 불러오는 것은 아직 불가능 — 필요 시 추가 논의 대상.

### 6-1. GET /trips 응답 — 아티스트 정보 추가 (완료)

프론트가 `event_nm`이 "[방탄소년단(BTS)] 이벤트 상세명"처럼 대괄호 안에 그룹명을 담는 형식에 의존해 문자열 파싱으로 아티스트명을 뽑고 있었는데, 이 포맷이 사람이 입력하는 값이라 깨질 위험이 있다는 문제 제기. → `GET /trips` 목록 응답에 `artist_group_no`/`group_nm` 필드를 식별자 조인 기반(`trip.event_no → event.artist_group_no → artist_group.group_nm`, 문자열 파싱 아님)으로 추가 완료·실데이터 검증 완료(예: `artist_group_no=1, group_nm="엔시티 (NCT)"`). 개별 멤버 지정 이벤트는 `artist_group_no`가 없어 둘 다 `null`일 수 있음.

---

## 7. 스키마 드리프트 정리 (진행 과정에서 발견/해결된 것들)

- `trip_interest.ctg_no`: 처음엔 실수로 추가된 NOT NULL 컬럼처럼 보여 되돌리려 했으나, ERD 스크린샷으로 이미 결정된 사항임을 재확인 → `interest_no` 완전 제거 + `ctg_no`만 사용하는 것으로 최종 확정. 실 DB는 `ctg_no` 추가까지만 반영되고 `interest_no` 제거/`interest` 테이블 삭제/관련 코드(`GET /interests`, `POST /trips`의 interest_nos 검증·삽입·응답, `auth.py` 133·654-664·760·793-794행)는 당시 미완이었음 — 이후 완료됨(2절 참고).
- `migration_visit_feedback.sql`(`visit_feedback.reaction_no` 제거) 실행 완료.
- `trip_density` 테이블이 한때 비어 있어 `POST /trips`의 `trip_density_no` 검증을 막고 있었음 — 사용자가 직접 3개 코드값(여유우선/AI추천/많이보기)을 채워 해결.
- `review_source` 삭제로 인한 실제 회귀: `GET /events/{event_no}`(장소 상세, 챗봇과 무관한 일반 API)가 여전히 `ReviewSource`를 조인하고 `ExternalReviewOut` 스키마가 `source_nm`을 선언하고 있어 500 에러(`Unknown column 'external_review.source_no'`) 발생. 바코클이 공개 API 응답 모양이 바뀌는 사안이라 임의로 고치지 않고 사용자 판단을 요청한 상태였음 → 이후 수정 완료로 확인(9절 참고), 다만 프론트(김보민)에게 `source_nm` 제거를 공지했는지는 별도 확인 필요.

---

## 8. 문서 정합성 크로스체크 결과 (2026-09-08)

3개 문서("백엔드 최종 정리" 아티팩트, "계정 인증 API" 아티팩트, "FAN:GO 챗봇 기능명세서 v2.0" docx)와 WBS 스크린샷(4.3 백엔드 API 28종, 5.4 챗봇 설계)을 상호 대조해서 나온 이슈:

1. **(가장 중요, 해결됨)** WBS 5.4.1 "chat_sessions/chat_messages API 설계"가 실제로는 미구현 상태였음 → 4절 내용대로 구현 완료.
2. ✅ **해결됨** — "백엔드 최종 정리" 아티팩트 자체 모순(`get_poi_business_hours`를 "데이터 보류"라고 적어놓고 다른 섹션은 구현 완료라고 설명하던 것) — "데이터 보류" 문구 삭제, "구현·검증 완료"로 통일 완료.
3. ✅ **해결됨** — `congestion`/`event_op_hour` DB_TABLES 표기를 "다른 팀 기능, 사용 안 함" → "✅ 사용중"으로 수정, `event_op_hour` 행 수도 0 → 9,100(현재는 더 늘어서 확인 시점 기준 상이, 3절 참고)로 갱신 완료.
4. ✅ **해결됨** — "`congestion.week_avg` NULL 1,728건 — 확인 대기" 이슈, 이슈 목록에서 완전히 제거 완료.
5. ✅ **해결됨** — `POST /chat` API_GROUPS 표 응답 예시에 `is_fallback`(및 `chat_session_no`까지) 반영 확인 완료.
6. **미해결 — 문서 위치 불명** — "FAN:GO 챗봇 기능명세서 v2.0" 10.2절 문구 업데이트 요청했으나, 바코클(이 세션)은 이 문서를 로컬/아티팩트 어디서도 찾지 못함(Downloads엔 review_source 이슈 이전 버전인 `FANGO_챗봇_기능명세서_2.docx`만 존재, 아티팩트 목록에도 없음) — **이 "v2.0" 문서가 실제로 어디 있는지(Google Docs/Notion 등) 확인해서 알려줘야 처리 가능.**

→ 2~5번은 2026-09-08~09 사이에 바코클이 전부 처리·확인 완료(각 문서에 실제로 반영됨, 재확인 완료). 6번만 문서 소재 확인 후 남은 작업.

---

## 9. 오늘(2026-09-08) 작업: ERD(V18) vs 실 DB 비교 결과

업로드해주신 `260908_ERD_V18.erwin` 파일(erwin 바이너리 포맷, 텍스트 추출 방식으로 분석 — 그림/관계선 방향까지는 확인 불가, 표 형태 텍스트만 신뢰도 높음)과 최신 데이터 딕셔너리(30개 테이블)를 대조한 결과입니다. ERD는 테이블 28개로, 정확히 아래 항목들이 반영 안 된 상태였습니다.

### 9-1. 통째로 빠진 테이블 (신규 2개, WBS 5.4.1)
- `chat_session` — user_no(FK→user), created_at, last_message_at
- `chat_message` — chat_session_no(FK→chat_session), role, content, sources, is_fallback, created_at
- 관계선: user 1—N chat_session, chat_session 1—N chat_message 추가 필요.

### 9-2. 기존 테이블에서 빠진 컬럼
- `trip` — `end_place`, `end_place_lat`, `end_place_lon` 3개 컬럼 없음(6절의 신규 컬럼). `start_place`는 이미 ERD에 있어 문제 없음.

### 9-3. 있지만 타입/길이가 실제 DB와 다른 컬럼 (문자열 추출 기반, erwin 프로그램에서 재확인 권장)
- `event.event_nm` — ERD: VARCHAR(100) / 실 DB: VARCHAR(255)
- `trip.start_tm` / `trip.end_tm` — ERD: NUMBER(19) / 실 DB: DATETIME
- `event_op_hour.open_tm` / `close_tm` — ERD: TIME / 실 DB: VARCHAR(5) ("HH:MM" 문자열)

### 9-4. 손 안 봐도 되는 것 (참고용)
- `interest`, `reaction`, `trip_type`, `event.add_dtl`, `review_source`, `external_review.source_no` — 이미 삭제된 것들인데 ERD에도 애초에 없어서 일치함.
- `trip.trip_type_no`는 실 DB엔 죽은 컬럼으로 남아있지만 ERD엔 없음 — ERD가 오히려 맞는 상태.
- 나머지 26개 테이블의 컬럼/FK 개수는 데이터 딕셔너리와 전부 일치 확인. row 수만 늘어난 `trip`/`accom`/`trip_interest`/`rute_artist_select` 등은 스키마 변경이 아니므로 ERD 수정 대상 아님.

---

## 10. 참고: 이 프로젝트 컨테이너에 함께 붙어있는 다른 문서

이 claude.ai 프로젝트에는 FAN:GO와 무관해 보이는 `rememo/schema.sql`, `rememo/db-schema-design.md`(RE:MEMO — AI 개인 기억 데이터베이스, PostgreSQL/pgvector 기반 완전히 다른 서비스 설계 문서)도 함께 들어 있습니다. FAN:GO 관련 내용과 혼동되지 않도록 이번 정리에는 포함하지 않았습니다. 혹시 이 프로젝트 컨테이너를 잘못 공유받으신 거라면 알려주세요.

---

## 11. 남은 이슈 / Open items 한눈에 보기

| 항목 | 상태 |
|---|---|
| ERD(V18) → chat_session/chat_message 추가, trip.end_place류 3컬럼 추가, event_nm/start_tm·end_tm/open_tm·close_tm 타입 수정 | 미반영 (이번 정리로 확인됨) |
| review_source 테이블 불안정(생겼다 사라짐) 원인 | 확인 필요 (원인 불명, 우리 코드는 원인 아님을 확인함) |
| GET /events/{event_no} 500 회귀 수정 후 프론트(김보민)에게 source_nm 제거 공지했는지 | 확인 필요 |
| "백엔드 최종 정리" 문서 내부 모순 5건(8절 2~6번) 반영 여부 | ✅ 2~5번 해결 완료 · 6번(챗봇 명세서 v2.0)만 문서 소재 확인 후 처리 가능 |
| get_travel_time (AL-02 travel_time_cache 기준) | Phase 1 범위 제외로 보류 중 |
| 지시어("거기 말고 다른 데는?") 기반 멀티턴 이해 | Intent Router 개선 후보로 보류 |
| GET /trips/{trip_no} 단건 재조회 API 부재 (재진입 시 출발/완료지점 등 재로딩 불가) | 필요 시 추가 논의 대상 |
| congestion 적재의 event_nm(이름) 기준 조인 — 동명이인 event 데이터 품질 이슈 | 팀 확인 대기 (2-2절 참고) |
