-- 마이 페이지 / 정보수정 화면에 필요한 컬럼 추가
-- 2026-09-06 실 DB(team2)에 적용 완료.
--
-- user 테이블에 프로필 사진 경로 컬럼이 없어서 추가합니다.
-- (다크모드는 화면단에서만 관리하기로 해서 컬럼 추가 안 함.)

ALTER TABLE user
  ADD COLUMN profile_img VARCHAR(255) NULL;
