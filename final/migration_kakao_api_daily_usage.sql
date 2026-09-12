-- 카카오모빌리티 "다중 목적지 길찾기" API 일일 호출 카운터 — 900건 하드 락용
-- 2026-09-09 실 DB(team2)에 적용 완료.
--
-- 배경: AL-02 이동시간을 haversine 근사 대신 실제 카카오모빌리티 API로 조회하도록
-- 전환하면서, 하루 900건을 넘으면 이후 호출을 아예 시도하지 않고(하드 락) 이동시간이
-- 필요한 요청을 실패 처리해야 한다는 요구사항이 생겼다.
--
-- 설계: 날짜(usage_date)를 PK로 두고 그날의 누적 호출 건수(call_count)만 저장하는
-- 가장 단순한 카운터. "자정 기준 리셋"은 별도 배치/스케줄러 없이, 날짜가 바뀌면
-- INSERT ... ON DUPLICATE KEY UPDATE가 새 날짜의 새 행을 만들면서 자연스럽게 0부터
-- 다시 시작하는 방식으로 구현한다(travel_time_service.py의 _reserve_daily_call() 참고).
-- 900 초과 판정과 증가분 롤백까지 그 함수가 담당하고, 이 테이블 자체엔 제약을 걸지 않는다
-- (900이라는 한도 값 자체는 코드(travel_time_service.DAILY_CALL_LIMIT)에서만 관리 —
-- al02_policy.py의 "정책/가중치는 여기서만" 원칙과 같은 이유로, DB 제약이 아니라 코드
-- 상수 하나로 한도를 바꿀 수 있게 함).

CREATE TABLE kakao_api_daily_usage (
  usage_date DATE NOT NULL,
  call_count INT NOT NULL DEFAULT 0,
  PRIMARY KEY (usage_date)
);

-- 결과: kakao_api_daily_usage 신규 생성. 앱 기동 중 이 테이블을 건드리는 코드는
-- travel_time_service.py뿐이다.
