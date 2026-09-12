-- accom.created_at 기본값 추가
-- 2026-09-09 실 DB(team2)에 적용 완료.
--
-- 배경(2026-09-09): accom 13행 전부 created_at이 NULL. 원인은 두 가지가 겹친 것:
--   1) DB 컬럼 자체에 DEFAULT가 없음(`SHOW CREATE TABLE accom` 확인 —
--      `created_at datetime DEFAULT NULL`).
--   2) 저장 코드(auth.py의 create_trip, Accom(...) 생성 부분)도 created_at을 명시적으로
--      채우지 않음 — models.py의 Accom.created_at에 server_default=func.now()가 선언돼
--      있긴 하지만, 이 값은 SQLAlchemy가 CREATE TABLE을 직접 실행할 때만 반영되는
--      메타데이터다. accom 테이블은 이미 ERD 기준으로 만들어져 있던 기존 테이블이라
--      main.py의 Base.metadata.create_all()이 건드리지 않고(신규 테이블만 생성), 그래서
--      실제 DB 컬럼엔 이 DEFAULT가 한 번도 반영된 적이 없다.
--
-- ⚠ 참고로 확인해보니 이 패턴은 accom만의 문제가 아니다 — trip(8행 중 0행 채워짐),
-- user(10행 중 0행), favorite_group(15행 중 0행), trip_interest(24행 중 0행)도 전부 같은
-- 이유로 created_at이 100% NULL이다. 반대로 artist/artist_group/event는 대량 적재(크롤링/
-- 시드) 스크립트가 INSERT 시점에 값을 직접 채워 넣어서 채워져 있는 것이지, DB DEFAULT
-- 덕분이 아니다(이 테이블들도 DEFAULT는 동일하게 없음). refresh_token/chat_session/
-- congestion만 진짜 DB DEFAULT CURRENT_TIMESTAMP가 걸려 있는데, 이 셋은 각자
-- migration_refresh_token.sql 등에서 DEFAULT를 명시해서 CREATE TABLE했기 때문이다.
--
-- 이번 마이그레이션은 이번에 조사를 요청받은 accom 테이블만 고친다. trip/user/
-- favorite_group/trip_interest는 이번 요청 범위 밖이라 손대지 않았음 — 같은 방식으로
-- 고칠지는 팀 논의 후 별도 마이그레이션으로 진행할 것.

ALTER TABLE accom
  MODIFY COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP;

-- 결과: 앞으로 accom 행이 생성될 때(코드에서 created_at을 명시적으로 안 채워도) DB가 자동으로
-- 현재 시각을 채운다. 기존 13행의 NULL은 이 ALTER로 소급 반영되지 않는다(원한다면 별도
-- UPDATE가 필요 — 이번 마이그레이션 범위 밖).
