-- trip_interest를 ctg 기반으로 완전 전환 + interest 테이블 삭제
--
-- 배경: ERD(260901 이후 개정본)에 trip_interest는 interest_no 없이
-- (trip_interest_no, ctg_no FK, trip_no FK, rank, created_at)만 갖도록 이미 바뀌어
-- 있었고, 관심사를 카테고리로 나누기로 하면서 interest 테이블 자체를 삭제하기로
-- 되어 있었다. 그런데 실 DB에는 ctg_no(NOT NULL, FK->ctg)만 먼저 추가되고
-- interest_no 제거·interest 테이블 삭제·코드 반영은 안 된 반쪽 상태였다.
--
-- 실행 전 반드시 확인:
-- 1) `DESCRIBE trip_interest;` — interest_no 컬럼이 실제로 남아있는지, FK 제약이
--    걸려있는지 확인. FK가 있다면 `SHOW CREATE TABLE trip_interest;`로 제약 이름을
--    찾아서 DROP COLUMN 전에 `DROP FOREIGN KEY <제약명>`을 먼저 실행해야 한다
--    (아래는 제약 이름을 모른다는 전제하에 별도 문장으로 분리해뒀다).
-- 2) `SELECT COUNT(*) FROM trip_interest;` — 기존 행이 있으면 interest_no -> ctg_no
--    매핑 규칙이 없는 한 데이터가 유실된다. 0건이면 바로 컬럼 삭제 가능.
-- 3) `SELECT COUNT(*) FROM interest;` — 삭제 전 참고용 확인. 다른 테이블에서
--    interest_no를 참조하는 FK가 더 있는지도 같이 확인(있다면 그것도 먼저 정리).
--
-- 실행 전 확인 요청: 아직 DB에 적용하지 않았습니다.

-- 1) trip_interest의 interest_no FK 제약 제거 (SHOW CREATE TABLE로 확인한 실제 제약명으로 바꿔서 실행)
-- ALTER TABLE trip_interest DROP FOREIGN KEY <실제_제약명>;

-- 2) trip_interest에서 interest_no 컬럼 제거
ALTER TABLE trip_interest
  DROP COLUMN interest_no;

-- 3) interest 테이블 삭제 (다른 곳에서 참조하는 FK가 없어야 실행 가능)
DROP TABLE interest;

-- 결과: trip_interest는 (trip_interest_no, ctg_no, trip_no, rank, created_at)만 남고,
-- interest 테이블은 사라진다. ctg_no는 기존에 이미 NOT NULL, FK->ctg 상태이므로
-- 이 마이그레이션에서는 손대지 않는다.
