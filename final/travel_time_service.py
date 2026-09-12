"""AL-02 이동시간 실 API 연동 — 카카오모빌리티 "다중 목적지 길찾기" + travel_time_cache
on-demand(lazy) 캐싱 + 일 900건 하드 락.

haversine_min() 근사(al02_pipeline.py)를 대체한다. 전체 조합을 미리 계산해서 채워두는
배치성 사전계산은 하지 않는다 — 트립 추천 요청 시점에 실제로 필요한 (origin, destination)
조합만 조회하고, travel_time_cache에 이미 있는 조합은 API를 다시 호출하지 않는다.

API 스펙(2026-09-09, developers.kakaomobility.com/guide/navi-api/destinations.html 확인):
  POST https://apis-navi.kakaomobility.com/v1/destinations/directions
  헤더: Authorization: KakaoAK {REST_API_KEY}, Content-Type: application/json
  바디: {"origin": {"x": 경도, "y": 위도}, "destinations": [{"x":.., "y":.., "key":..}, ...최대 30개],
        "radius": 미터(최대 10000)}
  응답: {"routes": [{"result_code":0, "key":.., "summary":{"distance":.., "duration":초}}, ...]}
  result_code != 0 인 항목은 "해당 목적지까지 경로를 못 찾음"(반경 초과/도로 없음 등) —
  요청 자체의 실패가 아니라 그 destination 한 건만 실패. 호출부가 개별적으로 폴백 처리한다.

⚠ radius=10000(카카오 API 허용 최댓값)을 고정으로 쓴다. al02_policy.
CANDIDATE_SEARCH_DEFAULT_RADIUS_KM(10km)과 맞춘 값이라 후보 검색 반경 안의 목적지는
직선거리 기준으로는 충분히 커버되지만, 실제 도로가 굽어 있어 반경을 넘는 것으로 판정되면
result_code=304(탐색 반경 초과)가 날 수 있다 — 이 경우도 개별 실패로 처리(아래 참고).

⚠ 과금이 "요청(호출) 건당"인지는 이 세션에서 재검증하지 않았다 — 카카오 콘솔에서 별도 확인 필요.
"""

import os
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

KAKAO_DESTINATIONS_URL = "https://apis-navi.kakaomobility.com/v1/destinations/directions"
MAX_DESTINATIONS_PER_CALL = 30  # 카카오 API 자체 제약(다중 목적지 최대 30개)
KAKAO_RADIUS_M = 10_000  # 카카오 API 허용 최댓값

# 2026-09-09 정정: kakao_api_daily_usage.usage_date를 DB의 CURRENT_DATE()에 맡기면
# DB 세션 타임존(SHOW COLUMNS/SELECT @@session.time_zone으로 재확인 결과 'UTC')을 따라가서
# "자정 리셋"이 실제로는 UTC 자정(=KST 오전 9시)에 일어난다 — 의도한 "하루"는 KST 기준이므로,
# DB에 맡기지 않고 이 모듈이 KST 날짜를 직접 계산해서 파라미터로 넘긴다.
KST = timezone(timedelta(hours=9))


def _today_kst():
    """지금 이 순간의 KST(UTC+9) 날짜. DB/서버 타임존과 무관하게 항상 KST 기준."""
    return datetime.now(timezone.utc).astimezone(KST).date()

# 일일 호출 하드 락. al02_policy.py의 "가중치/정책은 거기서만" 원칙과 달리 여기 둔 이유:
# 이건 추천 알고리즘 정책이 아니라 외부 API 사용량 한도(비용/쿼터)라 성격이 다르다.
DAILY_CALL_LIMIT = 900


class TravelTimeQuotaExceeded(Exception):
    """일일 900건 하드 락 도달 — API 호출 자체를 시도하지 않고 실패 처리한다.
    (개별 destination의 "경로 없음"과 달리, 여기선 haversine 등으로 폴백하지 않는다 — 요청 사양.)
    """


class TravelTimeAPIError(Exception):
    """카카오 API 키 누락/네트워크 오류 등 락과 무관한 호출 실패."""


def get_today_call_count(db: Session) -> int:
    """오늘(KST 자정 기준) 누적 호출 건수. 모니터링/응답 헤더 노출 등에 사용 가능."""
    row = db.execute(
        text("SELECT call_count FROM kakao_api_daily_usage WHERE usage_date = :today"),
        {"today": _today_kst()},
    ).scalar()
    return int(row or 0)


def _reserve_daily_call(db: Session) -> None:
    """오늘(KST) 날짜 호출 카운터를 원자적으로 1 증가시킨다. 900을 넘으면 증가분을 되돌리고
    TravelTimeQuotaExceeded를 던져 이번 API 호출 자체를 막는다(호출 전에 반드시 먼저 부른다).

    자정 리셋: 별도 배치/스케줄러가 없다 — KST 날짜가 바뀌면 _today_kst()가 새 값을 반환해
    INSERT ... ON DUPLICATE KEY UPDATE가 그 날짜의 새 행을 만들면서 자연스럽게 0부터 다시
    시작한다(어제 카운트에 영향 없음). DB의 CURRENT_DATE()는 쓰지 않는다(세션 타임존이
    UTC라 KST 자정과 어긋남 — 위 _today_kst() 참고).
    """
    today = _today_kst()
    db.execute(
        text(
            "INSERT INTO kakao_api_daily_usage (usage_date, call_count) "
            "VALUES (:today, 1) "
            "ON DUPLICATE KEY UPDATE call_count = call_count + 1"
        ),
        {"today": today},
    )
    count = db.execute(
        text("SELECT call_count FROM kakao_api_daily_usage WHERE usage_date = :today"),
        {"today": today},
    ).scalar()
    if count > DAILY_CALL_LIMIT:
        db.execute(
            text(
                "UPDATE kakao_api_daily_usage SET call_count = call_count - 1 "
                "WHERE usage_date = :today"
            ),
            {"today": today},
        )
        db.commit()
        raise TravelTimeQuotaExceeded(
            f"일일 카카오모빌리티 API 호출 한도({DAILY_CALL_LIMIT}건)에 도달해 "
            "이동시간을 조회할 수 없습니다."
        )
    db.commit()


def _call_kakao_destinations(api_key: str, origin_lat: float, origin_lon: float,
                              dest_points: list[tuple[str, float, float]]) -> dict[str, int | None]:
    """dest_points: [(key, lat, lon), ...] 최대 30개. 반환: {key: 이동시간(분) 또는 None(경로 없음)}."""
    body = {
        "origin": {"x": str(origin_lon), "y": str(origin_lat)},
        "destinations": [
            {"x": str(lon), "y": str(lat), "key": key} for key, lat, lon in dest_points
        ],
        "radius": KAKAO_RADIUS_M,
    }
    try:
        resp = requests.post(
            KAKAO_DESTINATIONS_URL,
            headers={"Authorization": f"KakaoAK {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise TravelTimeAPIError(f"카카오모빌리티 API 호출 실패: {e}") from e

    data = resp.json()
    result: dict[str, int | None] = {}
    for route in data.get("routes", []):
        key = route.get("key")
        if route.get("result_code") == 0:
            duration_sec = route.get("summary", {}).get("duration")
            result[key] = max(1, round(duration_sec / 60)) if duration_sec is not None else None
        else:
            result[key] = None  # 그 목적지만 경로 탐색 실패(반경 초과/도로 없음 등)
    return result


def get_travel_minutes(
    db: Session,
    origin_type: str,
    origin_no: int,
    origin_lat: float,
    origin_lon: float,
    destinations: list[dict],
    travel_mode: str = "car",
) -> dict[int, int]:
    """origin(origin_type, origin_no) -> 각 destination event_no까지 이동시간(분).

    destinations: [{"event_no":.., "lat":.., "lon":..}, ...]
    반환: {event_no: 분} — 캐시 히트 + 이번에 새로 조회한 것 전부 포함. 카카오가 "경로 없음"
    (result_code != 0)으로 응답한 destination은 이 dict에서 빠진다(호출부가 개별 폴백 처리 —
    al02_candidates.build_travel_matrix()는 haversine로, 그 외엔 상황에 맞게).

    캐시에 없는 조합만 최대 30개씩 배치로 카카오 API를 호출한다(전체 사전계산 없음).
    일 900건 한도에 도달하면 남은 미스 조합에 대해 TravelTimeQuotaExceeded를 던진다
    (폴백 없이 실패 처리 — 요청 사양).
    """
    if travel_mode != "car":
        raise TravelTimeAPIError(f"지원하지 않는 travel_mode: {travel_mode}(현재 'car'만 지원)")

    dest_by_no = {d["event_no"]: d for d in destinations}
    result: dict[int, int] = {}
    if not dest_by_no:
        return result

    api_key = os.getenv("KAKAO_REST_API_KEY")
    if not api_key:
        raise TravelTimeAPIError("KAKAO_REST_API_KEY가 .env에 설정되어 있지 않습니다.")

    # 1) 캐시 조회
    rows = db.execute(
        text(
            "SELECT destination_event_no, duration_min FROM travel_time_cache "
            "WHERE origin_type = :ot AND origin_no = :on_ AND travel_mode = :mode "
            "AND destination_event_no IN :dest_nos"
        ).bindparams(bindparam("dest_nos", expanding=True)),
        {"ot": origin_type, "on_": origin_no, "mode": travel_mode,
         "dest_nos": list(dest_by_no.keys())},
    ).all()
    for dest_no, minutes in rows:
        result[dest_no] = minutes

    missing = [no for no in dest_by_no if no not in result]
    if not missing:
        return result

    # 2) 캐시 미스만 최대 30개씩 배치로 API 호출 + 캐시 적재
    for i in range(0, len(missing), MAX_DESTINATIONS_PER_CALL):
        batch = missing[i:i + MAX_DESTINATIONS_PER_CALL]
        _reserve_daily_call(db)  # 900건 락 체크 — 여기서 막히면 아래 API 호출 자체를 안 함
        dest_points = [(str(no), float(dest_by_no[no]["lat"]), float(dest_by_no[no]["lon"]))
                       for no in batch]
        durations = _call_kakao_destinations(api_key, origin_lat, origin_lon, dest_points)
        now = datetime.utcnow()
        for no in batch:
            minutes = durations.get(str(no))
            if minutes is None:
                continue  # 경로 탐색 실패 — 캐시에 남기지 않음(다음에 다시 시도됨)
            db.execute(
                text(
                    "INSERT INTO travel_time_cache "
                    "(origin_type, origin_no, destination_event_no, travel_mode, duration_min, fetched_at) "
                    "VALUES (:ot, :on_, :dn, :mode, :dur, :fetched_at) "
                    "ON DUPLICATE KEY UPDATE duration_min = :dur, fetched_at = :fetched_at"
                ),
                {"ot": origin_type, "on_": origin_no, "dn": no, "mode": travel_mode,
                 "dur": minutes, "fetched_at": now},
            )
            result[no] = minutes
        db.commit()

    return result
