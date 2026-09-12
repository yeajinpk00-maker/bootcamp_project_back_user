-- AL-02 이동시간 캐시 테이블 신규 생성
-- 2026-09-09 최초 작성 시엔 "아직 DB에 적용하지 않았습니다"였으나, 이후 재확인 결과
-- 실제로 한 번도 적용된 적이 없었음(SHOW TABLES에 없음, `Table 'team2.travel_time_cache'
-- doesn't exist`로 직접 재현 확인). 이번 AL-02 실 API 연동 작업에서 실제로 필요해져서
-- 최종 스펙(origin_type 포함)으로 지금 처음 적용한다 — 아래 CREATE TABLE이 실행된 버전.
--
-- 배경: 지금까지 AL-02 파이프라인(al02_pipeline.py)은 haversine_min()으로 두 좌표 간
-- 이동시간을 직선거리/평균속도(25km/h)로 근사해왔다. 이 테이블은 그 근사값을 실제
-- 카카오모빌리티 "다중 목적지 길찾기" API 조회 결과로 대체하기 위한 on-demand(lazy) 캐시다
-- (travel_time_service.py). 전체 조합을 미리 계산해 채워두는 배치성 사전계산은 하지 않고,
-- 트립 추천 요청 시점에 실제로 필요한 조합만 계산해서 이 테이블에 쌓는다.
--
-- origin_type을 둔 이유: 캐시해야 하는 origin이 event_no 하나로 끝나지 않는다.
--   - AL02Pipeline.run()의 augment_matrix()가 매 요청마다 depot(숙소)/출발핀/도착핀에서
--     각 후보 이벤트까지의 이동시간을 필요로 함(al02_pipeline.py 참고).
--   - 숙소(accom, PK accom_no)는 트립마다 새로 생기는 유동 데이터라 event<->event처럼
--     "고정된 장소 쌍"으로 취급할 수 없다 — accom_no 자체를 origin으로 캐시한다.
-- origin_no 하나만으로는 이 넷(event/accom/trip 출발핀/trip 도착핀)을 구분할 수 없으므로
-- origin_type을 추가해 (origin_type, origin_no) 조합으로 origin을 특정한다:
--   origin_type='event'          -> origin_no = event.event_no
--   origin_type='accom'          -> origin_no = accom.accom_no
--   origin_type='trip_start_pin' -> origin_no = trip.trip_no (좌표는 trip.start_place_lat/lon)
--   origin_type='trip_end_pin'   -> origin_no = trip.trip_no (좌표는 trip.end_place_lat/lon)
-- trip_start_pin/trip_end_pin을 좌표가 아니라 trip_no로 캐시해도 되는 이유: POST /trips는
-- 생성 전용이고 trip 수정 API 자체가 없어(auth.py 확인 완료) 한 번 저장된 출발/도착 핀
-- 좌표는 그 trip_no에 대해 이후 절대 바뀌지 않는다 — event_no/accom_no와 동등하게
-- "안정적인 PK 기반 캐시 키"로 취급해도 안전하다.
--
-- 설계:
--   destination_event_no: event.event_no 참조(추천 후보가 전부 event라 destination은
--     이것만 지원하면 충분). origin/destination 모두 FK는 걸지 않음 — 이벤트/숙소가
--     나중에 삭제돼도 캐시는 그냥 stale해질 뿐이라 무해하다는 판단. 다른 캐시성 조회
--     데이터 테이블(congestion 등)과 달리 FK 없이 독립적으로 두는 편이 batch 갱신/삭제에 유리.
--   travel_mode: 'car' | 'transit' — 지금은 'car'만 실제로 채워짐(카카오 다중 목적지
--     API 자체가 자동차 기준), 'transit'은 나중에 대중교통 옵션이 생기면 대비.
--   PK를 (origin_type, origin_no, destination_event_no, travel_mode)로 잡아 같은 조합
--     중복 저장을 막는다. 방향성 있게 저장(A→B, B→A 각각 별도 행) — 다만 지금 코드
--     (travel_time_service.py)는 event<->event 쌍을 i<j 방향만 조회해서 양방향에
--     대칭 적용한다(실제 편도 소요시간 차이 반영은 이번 범위 밖 — al02_pipeline.
--     augment_matrix()도 기존부터 동일한 대칭 근사를 쓰고 있었다).
--   fetched_at: 캐시 신선도 판단용(오래된 캐시를 재조회해서 갱신하는 로직은 이번엔
--     추가하지 않음 — 물리적 거리는 안 변하니 만료 없이 계속 재사용).

CREATE TABLE travel_time_cache (
  origin_type ENUM('event', 'accom', 'trip_start_pin', 'trip_end_pin') NOT NULL,
  origin_no BIGINT NOT NULL,
  destination_event_no BIGINT NOT NULL,
  travel_mode ENUM('car', 'transit') NOT NULL,
  duration_min INT NOT NULL,
  fetched_at DATETIME NOT NULL,
  PRIMARY KEY (origin_type, origin_no, destination_event_no, travel_mode)
);

-- 결과: travel_time_cache 신규 생성 완료(SHOW TABLES/SHOW COLUMNS로 재확인 완료).
-- travel_time_service.py가 이 테이블을 읽고 쓰는 유일한 코드다.
