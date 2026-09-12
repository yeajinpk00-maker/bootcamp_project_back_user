-- 여행 초안(trip) 관련 마이그레이션
-- 2026-09-04 실 DB(team2)에 적용 완료. (주석이 한동안 "미실행"으로 잘못 남아있었음 — 실제로는 적용됨)
--
-- 확정된 범위 그대로입니다:
--   1) trip_density_no / trip_type_no를 nullable로 변경.
--      -> 이벤트 선택 + 여행 기간 시점엔 아직 정해지지 않고, 이후 단계(동선 스타일 선택)에서
--         채워지므로 이 시점의 trip INSERT에서는 NULL로 둔다.
--   2) event_date 컬럼 신규 추가.
--      -> 멀티데이 이벤트(예: 1/1~1/3 콘서트) 중 사용자가 실제로 고른 하루를 저장한다.
--
-- (참고: start_dt/end_dt는 건드리지 않습니다. trip 행 자체를 "이벤트 선택 + 여행 기간이
--  모두 정해진 뒤" 한 번에 생성하는 구조로 백엔드를 만들어서, 두 값 다 항상 채워진 상태로
--  INSERT되므로 NOT NULL을 유지해도 문제 없습니다.)
--
-- 참고: 이 DB(team2)는 여러 명이 같이 쓰는 라이브 스키마입니다. trip_type 테이블은 이미
-- 삭제된 상태였고 congestion이라는 새 테이블(혼잡도 관련, 이번 작업과 무관)이 생겨 있었는데,
-- 확인 결과 이번 마이그레이션에서 손댈 다른 테이블은 없습니다. trip.trip_type_no 컬럼 자체는
-- 남겨두고 nullable로만 바꿉니다(FK는 이미 없는 상태).

ALTER TABLE trip
  MODIFY COLUMN trip_density_no BIGINT NULL,
  MODIFY COLUMN trip_type_no BIGINT NULL,
  ADD COLUMN event_date DATE NOT NULL AFTER event_no;

-- trip 테이블은 현재 0 rows라 NOT NULL 신규 컬럼 추가에 DEFAULT가 필요 없습니다.
-- (행이 이미 있는 상태에서 실행한다면 DEFAULT를 지정하거나 nullable로 추가해야 합니다.)
