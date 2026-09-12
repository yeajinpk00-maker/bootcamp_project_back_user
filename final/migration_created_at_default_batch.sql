-- trip / user / favorite_group / trip_interest — created_at 기본값 추가
-- 2026-09-09 실 DB(team2)에 적용 완료.
-- accom과 원인이 완전히 동일해서 같은 패턴으로 묶어서 처리한다(각 테이블 다른 컬럼은 안 건드림).
--
-- 배경: migration_accom_created_at_default.sql에서 확인한 대로, 이 네 테이블도 전부
--   1) DB 컬럼 자체에 DEFAULT가 없고,
--   2) 저장 코드(auth.py의 signup/create_trip 등)도 created_at을 명시적으로 안 채워서
-- 100% NULL이었다(2026-09-09 확인: trip 0/8, user 0/10, favorite_group 0/15, trip_interest 0/24).
-- models.py엔 네 모델 전부 server_default=func.now()가 선언돼 있지만, 이미 ERD 기준으로
-- 만들어져 있던 기존 테이블이라 main.py의 Base.metadata.create_all()이 건드리지 않아
-- (없는 테이블만 생성) 실제 DB엔 한 번도 반영된 적이 없었다.
--
-- accom과 동일하게, 이 마이그레이션은 "앞으로 들어오는 값"만 고친다 — 기존 NULL 행들은
-- 소급 반영되지 않는다(그 행들을 지금 시점 값으로 채우는 건 사실과 다른 값을 만드는 것이라
-- 하지 않음. 필요하면 팀 논의 후 별도 처리).

ALTER TABLE trip
  MODIFY COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE user
  MODIFY COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE favorite_group
  MODIFY COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE trip_interest
  MODIFY COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP;

-- 결과: 네 테이블 모두 앞으로 생성되는 행은 created_at이 자동으로 채워진다. 기존 NULL 행은
-- 그대로 NULL로 남는다.
