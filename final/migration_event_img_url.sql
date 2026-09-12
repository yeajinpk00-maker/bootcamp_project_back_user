-- event.event_img_url 컬럼 추가 — 이벤트 대표 이미지 URL 저장용
-- 2026-09-09 실 DB(team2)에 적용 완료.
--
-- 배경: AL-02 이동시간 실 API 연동 작업과 함께 요청됨. event 테이블(PK: event_no)에
-- 이미지 URL을 저장할 컬럼이 아직 없어서 신규 추가한다.
--
-- 범위: 컬럼 생성까지만. 기존 1,984행은 전부 NULL로 유지하고 별도 백필은 하지 않는다
-- (실제 이미지 URL을 채우는 작업은 이번 요청 범위 밖 — 프론트/기획 쪽에서 이미지 소스가
-- 정해지면 별도 백필 스크립트로 진행할 것).

ALTER TABLE event
  ADD COLUMN event_img_url VARCHAR(500) NULL AFTER event_dtl;

-- 결과: event.event_img_url VARCHAR(500), NULL 허용, 기본값 없음(=NULL). models.py의
-- Event.event_img_url = Column(String(500))로 반영 완료. 기존 행 전부 NULL 확인.
