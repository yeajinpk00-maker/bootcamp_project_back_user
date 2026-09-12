-- accom_lat/accom_lon 정밀도 수정 — DECIMAL(10,8)이 경도 오버플로우를 조용히 삼키던 버그
-- 2026-09-09 실 DB(team2)에 적용 완료.
--
-- ⚠ 2026-09-09 정정: 아래 원문은 DECIMAL(9,6)으로 적용했다고 기록했으나, 실 DB에
-- SHOW COLUMNS로 재확인한 결과 실제로 적용된(그리고 현재 실사용 정상 동작 중인) 최종
-- 스펙은 DECIMAL(11,8)이다 — 정수부 3자리(11-8)는 그대로 유지해 오버플로우 버그 자체는
-- 동일하게 해결하면서, 소수부를 6자리가 아닌 8자리로 더 넓게 잡은 의도적 조치. 아래
-- 원문의 (9,6) 표기와 ALTER문은 실제 실행문이 아니므로, 이 파일을 참고할 땐 이 정정
-- 사항이 최종본이다. models.py의 Accom.accom_lat/accom_lon도 Numeric(11, 8)로 일치시켰음
-- (models.py 자체 수정은 불필요하다던 기존 결과 문구는 이 정정으로 갱신됨).
--
-- 배경(2026-09-09, POST /trips/{trip_no}/recommend 실 DB 연동 테스트 중 발견):
-- accom 테이블 13행 전부 accom_lon = 99.99999999로 저장돼 있던 원인을 진단한 결과,
-- 코드 버그가 아니라 컬럼 타입 자체의 설계 실수였다.
--
--   accom_lat DECIMAL(10,8)  -- 정수부 2자리 + 소수부 8자리
--   accom_lon DECIMAL(10,8)  -- 정수부 2자리 + 소수부 8자리   ← 문제
--
-- 위도(위 경도.md 기준 한국은 33~38)는 정수부 2자리로 충분해서 우연히 멀쩡해 보였지만,
-- 경도(한국은 126~129)는 정수부 3자리가 필요해서 DECIMAL(10,8)의 표현 범위를 넘는다.
-- team2 DB의 sql_mode가 'NO_ENGINE_SUBSTITUTION'뿐이라 STRICT_TRANS_TABLES가 빠져있어서,
-- 범위를 넘는 값을 넣어도 에러 없이 그 타입의 최댓값(99.99999999)으로 조용히 클리핑된다
-- (스크래치 테이블로 직접 재현 확인함: 126.925784를 DECIMAL(10,8)에 넣으면 경고 없이
-- 99.99999999로 저장되고, 같은 값을 DECIMAL(9,6)에 넣으면 정상 저장됨).
--
-- 코드 쪽(schemas.AccomIn, auth.py의 accom_lat=accom.accom_lat / accom_lon=accom.accom_lon
-- 매핑)은 확인 결과 문제 없음 — 필드명 일치, 기본값 없음, lat/lon 뒤바뀜 없음. DB 컬럼
-- DEFAULT도 NULL이라 "잘못된 DEFAULT가 채워지는" 케이스도 아니었음(SHOW CREATE TABLE로 확인).
-- 결국 요청이 프론트에서 정상적으로 왔더라도 이 컬럼을 거치는 순간 무조건 이렇게 됐다는 뜻.
--
-- 스키마의 다른 좌표 컬럼(event_lat/event_lon, trip.start_place_lat/lon 등)은 전부
-- NUMERIC(9,6)(정수부 3자리 + 소수부 6자리)를 쓰고 있어서, accom_lat/accom_lon만 여기서
-- 벗어나 있었다. (원안은 그 컬럼들과 동일한 9,6으로 맞추는 것이었으나, 위 2026-09-09
-- 정정 내역대로 최종 적용은 DECIMAL(11,8) — accom만 다른 좌표 컬럼보다 소수부가 더 넓다.)
--
-- ⚠ 이 마이그레이션은 "앞으로 들어오는 값"만 고친다. 이미 99.99999999로 저장된 기존 13행은
-- 원본 경도값 자체가 INSERT 시점에 유실된 것이라(클리핑이지 반올림이 아님) 이 ALTER로
-- 되살아나지 않는다 — 기존 행 정정은 이번 마이그레이션 범위 밖(팀 논의 후 별도 처리, 아래
-- 처리 권고 참고).

ALTER TABLE accom
  MODIFY COLUMN accom_lat DECIMAL(11,8) NULL,
  MODIFY COLUMN accom_lon DECIMAL(11,8) NULL;

-- 결과: accom_lat/accom_lon이 정수부 3자리까지(최대 999.99999999) 담을 수 있게 되어, 한국
-- 경도(126~129대)를 더 이상 클리핑하지 않는다. SHOW COLUMNS로 실 DB 재확인 완료
-- (accom_lat/accom_lon 모두 decimal(11,8)). models.py의 Accom.accom_lat/accom_lon도
-- Numeric(11, 8)로 일치시켰음(2026-09-09).
