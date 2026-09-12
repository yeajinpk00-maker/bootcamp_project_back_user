-- access + refresh 토큰 구조로 전환하기 위한 신규 테이블
-- 2026-09-06 실 DB(team2)에 적용 완료.
--
-- 유저당 세션 1개(user_no UNIQUE) — 로그인할 때마다 기존 행을 덮어씁니다.
-- 원문 refresh token은 저장하지 않고 SHA-256 해시만 저장합니다(비밀번호처럼 유출 시
-- 그대로 재사용되는 걸 막기 위함 — 다만 무작위 고엔트로피 토큰이라 salt는 불필요).

CREATE TABLE refresh_token (
  refresh_token_no BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_no BIGINT NOT NULL,
  token_hash VARCHAR(64) NOT NULL,
  issued_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at DATETIME NOT NULL,
  revoked_at DATETIME NULL,
  UNIQUE KEY uq_refresh_token_user_no (user_no),
  UNIQUE KEY uq_refresh_token_hash (token_hash),
  CONSTRAINT fk_refresh_token_user FOREIGN KEY (user_no) REFERENCES user (user_no)
);
