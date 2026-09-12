-- "좋았던 장소" 단순화 — visit_feedback.reaction_no 컬럼 제거
-- 실행 전 확인 요청: 아직 DB에 적용하지 않았습니다.
--
-- reaction 테이블은 이미 삭제되어 있고(확인함), visit_feedback.reaction_no는
-- 그 FK만 사라진 채 컬럼 자체는 NOT NULL로 남아있는 상태였습니다.
-- visit_feedback 테이블은 현재 0 rows라 데이터 손실 없이 바로 지울 수 있습니다.

ALTER TABLE visit_feedback
  DROP COLUMN reaction_no;

-- 결과: visit_feedback(visit_feedback_no, trip_route_no, created_at) 3컬럼만 남음.
