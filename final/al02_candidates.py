"""AL-02 S0 — 후보 이벤트 검색 (노트북에 없던 부분, 이번에 신규 작성).

숙소 좌표 반경 안의 이벤트를 DB에서 뽑아 al02_pipeline.calc_relevance()로 채점하고,
AL02Pipeline.run()에 그대로 넘길 수 있는 (candidates, events, matrix)를 만든다.

흐름: fetch_candidate_rows(SQL) -> artist_group_map 조회 -> hard_filter/calc_relevance
     -> relevance 내림차순 정렬 -> 상위 top_n으로 재축소 -> build_travel_matrix(실 API+캐시)

⚠ 범위 밖(이번엔 건드리지 않음, 요청 7번):
- POST /api/v1/trips/generate 라우팅/인증/DB 저장 연결 — 여기 함수들을 "호출하는 쪽"이 남아있음.
  이 모듈은 순수 함수 모음이라 라우터 없이 바로 테스트 가능.

2026-09-09 이동시간 실 API 연동: build_travel_matrix()가 haversine_min 근사 대신
travel_time_service(카카오모빌리티 다중 목적지 길찾기 + travel_time_cache)를 쓰도록 교체.
이벤트 i를 origin, i보다 뒤(j>i)의 이벤트들을 destination으로 묶어(최대 30개씩) 호출하고,
그 값을 matrix[i,j]/matrix[j,i]에 대칭 적용한다(기존 haversine 버전과 동일한 대칭 근사
— 편도 방향 차이 반영은 범위 밖). 카카오 API가 특정 쌍의 경로를 못 찾으면(반경 초과 등)
그 쌍만 haversine로 개별 폴백한다 — 이건 "일 900건 하드 락"과는 다른 케이스라 폴백 허용.
일 900건 한도 자체에 도달하면 travel_time_service.TravelTimeQuotaExceeded가 그대로
올라온다(폴백 없이 호출부 실패 처리 — auth.py의 POST /trips/{trip_no}/recommend 참고).

place_quality 실측 반영(2026-09-09): external_review가 501행 채워졌고(event_no 기준 중복
없음 확인됨) total_score도 0~100 범위로 실측 확인돼서, 상관 서브쿼리로 event_no당
total_score 1개를 가져와 place_quality()에 그대로 넘긴다. 리뷰가 아예 없는 이벤트(전체
1984개 중 여전히 다수)는 서브쿼리가 NULL을 반환하고, place_quality(None)이 0.0으로
정규화하는 기존 로직이 그대로 처리한다 — "리뷰 없음"의 정상적인 표현이라 별도 분기 불필요.
상관 서브쿼리(LEFT JOIN 대신)를 쓴 이유: event_no당 1건이 지금은 보장되지만, 나중에 다시
중복이 생기더라도 서브쿼리는 항상 스칼라 1개만 반환해서 후보 행이 중복 생성될 위험이 없다
(LEFT JOIN이었다면 이벤트당 리뷰가 2건 이상일 때 후보 행 자체가 뻥튀기됨).

⚠ SQL만으로는 부족한 부분(문서엔 없지만, run()이 실제로 요구해서 이 모듈에서 같이 처리함):
- 콘서트(메인 이벤트, ctg_type_no=1)는 사용자의 "선호 카테고리"(ctg_type_no 2/3)로 필터링하는
  이 SQL 대상이 아니다. AL02Pipeline.run()은 concert.event_no가 events 리스트에 없으면
  ValueError를 던지므로, fetch_candidates()가 user_input["concert"]["event_no"]를 보고
  없으면 별도로 그 1건을 조회해 events에 합쳐준다.

2026-09-10 좌표 중복 제거(dedupe_by_coordinates): "경복궁"처럼 멤버별 개별 성지로 각각
등록됐지만 좌표가 사실상 동일한(같은 건물) 이벤트가 여럿 후보에 나란히 채택되는 문제가
실측(대체 장소 추천 §al02_alternatives.get_alternatives) 중 발견됨 — DA의 실제 데이터
정리를 기다리지 않고 이 공통 후보 조회 단계에서 알고리즘 레벨로 임시 대응하기로 확정.
S0(fetch_candidates)와 대체 장소 추천(al02_alternatives.get_alternatives)이 이 함수를
공유한다 — 콘서트는 이 함수가 받는 리스트에 애초에 안 들어있어서(항상 dedup 이후에
별도로 삽입) 영향 없음.
"""

import random

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

import travel_time_service
from al02_pipeline import calc_relevance, hard_filter, haversine_min
from al02_policy import (
    ALLOWED_PREFERENCE_CTG_TYPES,
    CANDIDATE_SEARCH_DEFAULT_RADIUS_KM,
    CANDIDATE_SEARCH_ROW_LIMIT,
    CANDIDATE_TOP_N_MAX,
    PREFERENCE_TIER_1,
    PREFERENCE_TIER_2,
)
from models import Artist

import numpy as np

# event_no당 total_score 1개(상관 서브쿼리 — 이유는 모듈 docstring 참고).
_TOTAL_SCORE_SUBQUERY = """(
        SELECT er.total_score FROM external_review er
        WHERE er.event_no = e.event_no
        ORDER BY er.created_at DESC, er.external_review_no DESC LIMIT 1
      ) AS total_score"""

# defense-in-depth 전용 상수 문자열(al02_policy.ALLOWED_PREFERENCE_CTG_TYPES=[2,3]에서
# 그대로 생성 — 하드코딩 금지 원칙 유지, SQL 리터럴로 박아넣을 수 있게 문자열로만 변환).
_ALLOWED_CTG_TYPES_SQL = "(" + ", ".join(str(t) for t in ALLOWED_PREFERENCE_CTG_TYPES) + ")"

# 반경 내 이벤트 검색. distance_km은 하버사인 공식(구면 근사) — event 테이블에 좌표 없는
# 행은 없음(event_lat/event_lon 둘 다 NOT NULL, 데이터 딕셔너리 기준)이라 NULL 걱정 없음.
#
# 2026-09-10 defense-in-depth 추가: `AND c.ctg_type_no IN (2, 3)`. 원래 이 SQL은
# e.ctg_no IN :selected_ctg_nos만 걸고 selected_ctg_nos가 이미 ctg_type 2/3만 담고
# 있다는 걸 호출부(auth.py POST /trips의 쓰기 시점 검증, 682~695행)에만 의존해서 보장하고
# 있었다 — 단일 지점 방어라 나중에 다른 호출부(관리자 API/배치 등)가 검증 안 된 ctg_nos로
# fetch_candidates()를 직접 부르면 ctg_type=1(행사) 이벤트가 새어 들어올 수 있는 구조였음
# (2026-09-09 조사 결과). auth.py 쪽 검증은 그대로 두고, 이 SQL 자체에도 같은 제약을
# 한 번 더 걸어 호출부가 뭐든 상관없이 안전하게 만든다. 정상 케이스(ctg_type 2/3만 들어오는
# 기존 흐름)는 이미 이 조건을 항상 만족하므로 결과에 영향 없음(회귀 확인 완료).
#
# 2026-09-10 ORDER BY 변경(d안 확정, radius_km 10km는 그대로 유지): 서울 8개 지역 전수
# 조사 결과 10km 반경 안 실제 이벤트가 전부 LIMIT(당시 200)을 초과(395~655건)해서,
# distance_km 단독 정렬이면 relevance 계산(artist_match 등) 전에 순전히 "가까운 순"으로만
# 잘려나가 — 반경 안이지만 조금 더 먼 "선택 멤버/그룹 매칭" 이벤트가 트림당하는 사례가
# 있었다. ORDER BY를 "아티스트/그룹 매칭 우선(0) -> distance_km" 2단 정렬로 바꿔서,
# 매칭되는 이벤트는 거리와 무관하게 항상 비매칭 이벤트보다 앞에 오도록 한다. LIMIT도
# 200->300으로 소폭 상향(al02_policy.CANDIDATE_SEARCH_ROW_LIMIT 참고, 안전장치 성격 —
# 근본 대응은 이 정렬 변경).
# selected_artist_nos/selected_group_nos가 비어 있을 때 `IN ()`이 SQL 오류가 나므로,
# 호출부(fetch_candidates)가 항상 최소 [-1](존재할 수 없는 PK)을 채워서 넘긴다.
CANDIDATE_SEARCH_SQL = text(f"""
    SELECT
      e.event_no, e.event_nm, e.event_lat, e.event_lon,
      e.artist_no, e.artist_group_no, e.ctg_no, c.ctg_nm, e.op_status_no,
      {_TOTAL_SCORE_SUBQUERY},
      (6371 * ACOS(
        COS(RADIANS(:lodging_lat)) * COS(RADIANS(e.event_lat))
        * COS(RADIANS(e.event_lon) - RADIANS(:lodging_lon))
        + SIN(RADIANS(:lodging_lat)) * SIN(RADIANS(e.event_lat))
      )) AS distance_km
    FROM event e
    JOIN ctg c ON c.ctg_no = e.ctg_no
    WHERE e.op_status_no IN (1, 2)
      AND e.ctg_no IN :selected_ctg_nos
      AND c.ctg_type_no IN {_ALLOWED_CTG_TYPES_SQL}
    HAVING distance_km <= :radius_km
    ORDER BY
      (CASE WHEN e.artist_no IN :selected_artist_nos
              OR e.artist_group_no IN :selected_group_nos
            THEN 0 ELSE 1 END),
      distance_km
    LIMIT {CANDIDATE_SEARCH_ROW_LIMIT}
""").bindparams(
    bindparam("selected_ctg_nos", expanding=True),
    bindparam("selected_artist_nos", expanding=True),
    bindparam("selected_group_nos", expanding=True),
)

# 2026-09-12 신규 — Tier2 후보 검색(al02_policy.PREFERENCE_TIER_2). CANDIDATE_SEARCH_SQL과
# 완전히 동일한 조건(운영상태/반경/ctg_type 방어)이되, ctg_no는 "선택한 것과 다른"
# ctg_type 2/3 카테고리만 가져온다 — trip_interest 실측 결과 대부분의 트립이 카테고리를
# 2~3개만 고르는데(4개 이상 선택 0건), 그 소수 카테고리 후보만으로는 하루 카테고리
# 상한(1)과 부딪혀 방문 목표를 못 채우는 게 예외가 아니라 일반적인 상황이었다
# (trip_no=43 실측). ORDER BY는 매칭 우선순위를 그대로 유지 — Tier2도 선택 멤버/그룹
# 태그가 있으면 그쪽을 우선한다(단, category_fitness 자체는 al02_pipeline.category_fitness()
# 의 기존 MISS=0.3 그대로 낮게 나온다 — 여기서 손대는 값 아님).
TIER2_CANDIDATE_SEARCH_SQL = text(f"""
    SELECT
      e.event_no, e.event_nm, e.event_lat, e.event_lon,
      e.artist_no, e.artist_group_no, e.ctg_no, c.ctg_nm, e.op_status_no,
      {_TOTAL_SCORE_SUBQUERY},
      (6371 * ACOS(
        COS(RADIANS(:lodging_lat)) * COS(RADIANS(e.event_lat))
        * COS(RADIANS(e.event_lon) - RADIANS(:lodging_lon))
        + SIN(RADIANS(:lodging_lat)) * SIN(RADIANS(e.event_lat))
      )) AS distance_km
    FROM event e
    JOIN ctg c ON c.ctg_no = e.ctg_no
    WHERE e.op_status_no IN (1, 2)
      AND e.ctg_no NOT IN :selected_ctg_nos
      AND c.ctg_type_no IN {_ALLOWED_CTG_TYPES_SQL}
    HAVING distance_km <= :radius_km
    ORDER BY
      (CASE WHEN e.artist_no IN :selected_artist_nos
              OR e.artist_group_no IN :selected_group_nos
            THEN 0 ELSE 1 END),
      distance_km
    LIMIT {CANDIDATE_SEARCH_ROW_LIMIT}
""").bindparams(
    bindparam("selected_ctg_nos", expanding=True),
    bindparam("selected_artist_nos", expanding=True),
    bindparam("selected_group_nos", expanding=True),
)

# 콘서트(메인 이벤트) 단건 조회 — 선호 카테고리 필터 대상이 아니라서 위 SQL로는 안 잡힘.
CONCERT_EVENT_SQL = text(f"""
    SELECT e.event_no, e.event_nm, e.event_lat, e.event_lon,
           e.artist_no, e.artist_group_no, e.ctg_no, c.ctg_nm, e.op_status_no,
           {_TOTAL_SCORE_SUBQUERY}
    FROM event e
    JOIN ctg c ON c.ctg_no = e.ctg_no
    WHERE e.event_no = :event_no
""")


def _coerce_coords(ev: dict) -> dict:
    """event_lat/event_lon을 float로 강제한다.

    pymysql은 DECIMAL 컬럼(event.event_lat/event_lon)을 decimal.Decimal로 반환하는데,
    al02_pipeline.haversine_min()은 순수 float(숙소/출발핀/도착핀 좌표)와 그대로 뺄셈해서
    Decimal-float TypeError가 난다(실 DB로 S0을 붙여보고 나서 발견 — 노트북 합성 테스트는
    처음부터 float만 써서 이 경로를 안 탔음). 좌표를 DB 경계에서 float로 정규화해 해결한다."""
    if ev.get("event_lat") is not None:
        ev["event_lat"] = float(ev["event_lat"])
    if ev.get("event_lon") is not None:
        ev["event_lon"] = float(ev["event_lon"])
    return ev


def dedupe_by_coordinates(events: list[dict]) -> list[dict]:
    """좌표 중복 후보 제거(2026-09-10 신규, 팀 확정 — 모듈 docstring 참고).

    판정 기준: event_lat/event_lon을 소수점 5자리(약 1m)로 반올림해서 일치하면 동일
    좌표로 간주한다. 동일 좌표 그룹은 그중 1건만 "순수 랜덤"으로 남기고 나머지는
    버린다 — relevance 등 점수로 대표를 고르는 로직은 넣지 말라는 팀 확정 사항이라
    random.choice() 그대로 쓴다(점수 비교 없음).

    좌표가 없는 행(이 SQL들에서는 이론상 안 나오지만 방어적으로)은 판정 대상이 아니라
    항상 그대로 통과시킨다. 원래 리스트 순서는 유지한다 — 대체 장소 추천(distance_km
    오름차순으로 이미 정렬된 al02_alternatives.get_alternatives)이 이 함수를 거친 뒤에도
    정렬이 흐트러지면 안 되기 때문(S0 쪽은 이후 relevance로 다시 정렬하니 순서가
    상관없지만, 공유 함수라 두 호출부 모두에 안전한 "순서 보존" 쪽으로 만들었다)."""
    groups: dict[tuple[float, float], list[int]] = {}
    for i, ev in enumerate(events):
        lat, lon = ev.get("event_lat"), ev.get("event_lon")
        if lat is None or lon is None:
            continue  # 좌표 없음 — 중복 판정 대상 아님, 항상 유지
        key = (round(float(lat), 5), round(float(lon), 5))
        groups.setdefault(key, []).append(i)

    drop_indices: set[int] = set()
    for indices in groups.values():
        if len(indices) <= 1:
            continue
        winner = random.choice(indices)
        drop_indices.update(i for i in indices if i != winner)

    if not drop_indices:
        return events
    return [ev for i, ev in enumerate(events) if i not in drop_indices]


def build_artist_group_map(db: Session, artist_nos: set[int]) -> dict[int, int]:
    """{artist_no: artist_group_no} — S0 단계에서 artist 테이블을 한 번에 조회해 캐싱.
    calc_relevance()의 artist_group_map 인자로 그대로 전달한다."""
    if not artist_nos:
        return {}
    rows = (
        db.query(Artist.artist_no, Artist.artist_group_no)
        .filter(Artist.artist_no.in_(artist_nos))
        .all()
    )
    return {int(a): int(g) for a, g in rows}


def build_travel_matrix(
    db: Session, events: list[dict], travel_mode: str = "car", use_haversine: bool = False,
) -> np.ndarray:
    """이벤트 간 이동시간(분) NxN 행렬. 2026-09-09부터 haversine_min 근사 대신
    travel_time_service(카카오모빌리티 다중 목적지 길찾기 + travel_time_cache)를 사용한다.

    이벤트 i를 origin으로 놓고 j>i인 이벤트들을 destination으로 묶어(최대 30개씩 배치)
    한 번에 조회 — i<j 방향만 실제로 호출하고 matrix[i,j]/matrix[j,i]에 동일하게 채운다
    (기존 haversine 버전과 동일한 대칭 근사, al02_pipeline.augment_matrix()도 마찬가지).

    카카오 API가 특정 쌍의 경로를 못 찾으면(반경 초과/도로 없음 등, result_code!=0) 그
    쌍만 haversine_min으로 개별 폴백한다 — 좌표가 아예 없는 경우와 마찬가지로 "이동시간
    제약 없음(0)"보다 근사치를 쓰는 게 낫다는 판단. 일 900건 하드 락 도달 시엔 폴백 없이
    travel_time_service.TravelTimeQuotaExceeded를 그대로 전파한다(요청 사양).

    use_haversine=True: 카카오 API를 아예 호출하지 않고(캐시 조회도 안 함) 전부 haversine_min
    근사로 계산한다 — 실 API 연동 이전의 원래 로직 그대로. 기본값은 False(실 API 사용)라
    운영 동작은 그대로다. 2026-09-09 CANDIDATE_TOP_N_MAX 조사(al02_candidates 1차 스코어링
    순위 vs 최종 채택 분포 실측)처럼 "이동시간 최적화 결과가 필요하지만 API 비용은
    쓰면 안 되는" 조사용으로 추가."""
    n = len(events)
    matrix = np.zeros((n, n), dtype=np.int64)
    for i in range(n):
        lat1, lon1 = events[i].get("event_lat"), events[i].get("event_lon")
        if lat1 is None or lon1 is None:
            continue
        if use_haversine:
            for j in range(i + 1, n):
                lat2, lon2 = events[j].get("event_lat"), events[j].get("event_lon")
                t = (0 if None in (lat2, lon2)
                     else haversine_min(float(lat1), float(lon1), float(lat2), float(lon2)))
                matrix[i, j] = t
                matrix[j, i] = t
            continue
        rest = [
            {"event_no": events[j]["event_no"], "lat": events[j].get("event_lat"),
             "lon": events[j].get("event_lon")}
            for j in range(i + 1, n)
            if events[j].get("event_lat") is not None and events[j].get("event_lon") is not None
        ]
        if not rest:
            continue
        minutes_by_no = travel_time_service.get_travel_minutes(
            db, origin_type="event", origin_no=events[i]["event_no"],
            origin_lat=float(lat1), origin_lon=float(lon1),
            destinations=rest, travel_mode=travel_mode,
        )
        for j in range(i + 1, n):
            ev_j = events[j]
            lat2, lon2 = ev_j.get("event_lat"), ev_j.get("event_lon")
            no = ev_j.get("event_no")
            if no in minutes_by_no:
                t = minutes_by_no[no]
            elif None in (lat2, lon2):
                t = 0  # 좌표 없음 — 기존과 동일하게 제약 없음 취급
            else:
                t = haversine_min(float(lat1), float(lon1), float(lat2), float(lon2))
            matrix[i, j] = t
            matrix[j, i] = t
    return matrix


def build_extra_travel_minutes(
    db: Session, origins: dict[str, dict], events: list[dict], travel_mode: str = "car",
) -> dict[str, dict[int, int]]:
    """숙소(depot)/출발핀/도착핀 등 "고정 지점"에서 각 후보 이벤트까지의 이동시간(분).

    origins: {role: {"origin_type":.., "origin_no":.., "lat":.., "lon":..}, ...}
      role은 AL02Pipeline.run()이 augment_matrix에 넘기는 extra_points의 "role"과
      동일한 문자열이어야 한다("depot"/"start_pin"/"end_pin" — al02_pipeline.py 참고).
      origin_type/origin_no는 travel_time_cache 캐시 키(예: accom_no, trip_no).

    반환: {role: {event_index: 분}} — AL02Pipeline.run(extra_travel_minutes=...)에 그대로
    전달한다. events 리스트 인덱스 기준(al02_pipeline이 event_no가 아니라 인덱스로 다룸).

    카카오 API가 특정 목적지 경로를 못 찾으면 그 이벤트는 이 dict에서 빠지고,
    augment_matrix()가 haversine로 개별 폴백한다(build_travel_matrix()와 동일한 정책)."""
    dest = [
        {"event_no": ev["event_no"], "lat": ev.get("event_lat"), "lon": ev.get("event_lon")}
        for ev in events
        if ev.get("event_lat") is not None and ev.get("event_lon") is not None
    ]
    event_no_to_index = {ev["event_no"]: idx for idx, ev in enumerate(events)}

    out: dict[str, dict[int, int]] = {}
    for role, ref in origins.items():
        if ref.get("lat") is None or ref.get("lon") is None or not dest:
            out[role] = {}
            continue
        minutes_by_no = travel_time_service.get_travel_minutes(
            db, origin_type=ref["origin_type"], origin_no=ref["origin_no"],
            origin_lat=float(ref["lat"]), origin_lon=float(ref["lon"]),
            destinations=dest, travel_mode=travel_mode,
        )
        out[role] = {
            event_no_to_index[no]: minutes
            for no, minutes in minutes_by_no.items() if no in event_no_to_index
        }
    return out


def fetch_business_hours(db: Session, event_nos: list[int]) -> dict[int, dict[str, tuple]]:
    """S3 영업시간 필터(2026-09-10 신규) — event_op_hour을 event_no 목록으로 한 번에 조회.

    반환: {event_no: {op_dt("월"~"일"): (open_tm, close_tm)}}. 이벤트당 요일별로 여러 행이
    있는 게 정상 구조라(하나의 event_no가 최대 7개 요일 행을 가짐), 요일 문자열을 그대로
    키로 써서 다 담는다 — al02_pipeline.build_open_matrix()가 트립 날짜의 실제 요일로
    이 dict에서 정확히 하나씩 찾아 쓴다. open_tm/close_tm은 컬럼 실제 타입인 "HH:MM" 문자열
    그대로 반환(NULL이면 None — 두 값이 다 None이면 그 요일 휴무).

    이 함수가 반환한 dict에 event_no 자체가 아예 없으면(그 이벤트는 event_op_hour에 행이
    하나도 없음) build_open_matrix()가 "매칭되는 행이 없음"으로 보고 모든 날짜를 휴무
    취급한다 — event_op_hour가 아직 전체 이벤트의 일부만 채워진 상태라 실제 영향이 있다
    (완료 보고 참고)."""
    if not event_nos:
        return {}
    rows = db.execute(
        text(
            "SELECT event_no, op_dt, open_tm, close_tm FROM event_op_hour "
            "WHERE event_no IN :event_nos"
        ).bindparams(bindparam("event_nos", expanding=True)),
        {"event_nos": list(event_nos)},
    ).mappings().all()
    result: dict[int, dict[str, tuple]] = {}
    for r in rows:
        result.setdefault(r["event_no"], {})[r["op_dt"]] = (r["open_tm"], r["close_tm"])
    return result


def fetch_candidates(
    db: Session,
    user_input: dict,
    lodging_lat: float,
    lodging_lon: float,
    radius_km: float = CANDIDATE_SEARCH_DEFAULT_RADIUS_KM,
    top_n: int = CANDIDATE_TOP_N_MAX,
    use_haversine: bool = False,
    include_tier2: bool = False,
    tier2_top_n: int = CANDIDATE_TOP_N_MAX,
):
    """S0: 반경 내 후보 이벤트를 뽑아 채점하고 run()에 바로 넘길 수 있는 형태로 반환한다.

    반환: (candidates, events, matrix)
      candidates: [(matrix_index, relevance), ...] — relevance 내림차순
      events:     candidates의 인덱스와 1:1 대응하는 이벤트 dict 리스트. 각 dict에
                  preference_tier(al02_policy.PREFERENCE_TIER_1/_2)가 채워진다.
      matrix:     이벤트 간 이동시간(분) NxN 행렬 (기본은 실 API+캐시, use_haversine=True면 근사)

    top_n: 재축소 상한(기본 CANDIDATE_TOP_N_MAX=80). 하한 가이드는
    CANDIDATE_TOP_N_MIN=40(강제 아님 — 반경 안에 그보다 적으면 있는 만큼만 쓴다).

    use_haversine: True면 build_travel_matrix()가 카카오 API/캐시를 전혀 건드리지 않고
    haversine 근사만 쓴다 — build_travel_matrix() 자체의 docstring 참고(조사/시뮬레이션용).

    include_tier2(2026-09-12 신규): True면 TIER2_CANDIDATE_SEARCH_SQL로 "선택 안 한
    ctg_type 2/3 카테고리" 후보도 함께 가져와 Tier1 후보 뒤에 이어붙인다(al02_pipeline.
    AL02Pipeline.run()의 2차 배정이 씀). False(기본값)면 기존과 완전히 동일하게 Tier1만
    반환한다 — 하위호환(al02_selftest.py 등 기존 호출은 이 인자를 안 주므로 영향 없음)."""
    selected_ctg_nos = user_input.get("ctg_nos") or []
    if not selected_ctg_nos:
        return [], [], np.zeros((0, 0), dtype=np.int64)

    # ORDER BY의 매칭 우선순위 판정용 — 비어 있으면 `IN ()` SQL 오류가 나므로 존재할 수
    # 없는 PK([-1])로 채운다(2026-09-10, CANDIDATE_SEARCH_SQL 주석 참고).
    selected_artist_nos_for_sort = list(user_input.get("artist_nos") or []) or [-1]
    selected_group_nos_for_sort = list(user_input.get("group_nos") or []) or [-1]

    rows = db.execute(
        CANDIDATE_SEARCH_SQL,
        {
            "lodging_lat": lodging_lat,
            "lodging_lon": lodging_lon,
            "selected_ctg_nos": list(selected_ctg_nos),
            "selected_artist_nos": selected_artist_nos_for_sort,
            "selected_group_nos": selected_group_nos_for_sort,
            "radius_km": radius_km,
        },
    ).mappings().all()
    events = [_coerce_coords(dict(r)) for r in rows]
    # 좌표 중복 제거(2026-09-10) — 콘서트 삽입보다 먼저 적용해서 콘서트는 대상에서
    # 자연히 빠진다(dedupe_by_coordinates 자체도 콘서트를 몰라도 됨 — 모듈 docstring 참고).
    events = dedupe_by_coordinates(events)
    for ev in events:
        ev["preference_tier"] = PREFERENCE_TIER_1

    # 콘서트(메인 이벤트)는 선호 카테고리 필터 대상이 아니라 위 SQL에 안 잡힌다.
    # run()이 events에서 못 찾으면 ValueError를 던지므로, 빠져 있으면 여기서 채워 넣는다.
    concert = user_input.get("concert") or {}
    concert_event_no = concert.get("event_no")
    if concert_event_no is not None and not any(e["event_no"] == concert_event_no for e in events):
        concert_row = db.execute(CONCERT_EVENT_SQL, {"event_no": concert_event_no}).mappings().first()
        if concert_row is not None:
            concert_ev = _coerce_coords(dict(concert_row))
            concert_ev["preference_tier"] = PREFERENCE_TIER_1
            events.insert(0, concert_ev)
        # 콘서트 event_no가 DB에 아예 없으면 여기선 조용히 넘어간다 — run()이
        # "발견되지 않습니다" ValueError로 명확하게 알려주므로 중복 검증하지 않는다.

    # 2026-09-12 신규 — Tier2(선택 안 한 ctg_type 2/3 카테고리) 후보. Tier1과 독립적으로
    # 좌표 중복 제거한다 — 두 티어를 한 번에 섞어서 dedupe하면 좌표가 겹치는 경우
    # (극히 드묾) Tier1(선호) 후보가 무작위로 밀려날 수 있어, Tier1 쪽 기존 동작을
    # 그대로 보존하는 쪽을 택했다.
    tier2_events: list[dict] = []
    if include_tier2:
        tier2_rows = db.execute(
            TIER2_CANDIDATE_SEARCH_SQL,
            {
                "lodging_lat": lodging_lat,
                "lodging_lon": lodging_lon,
                "selected_ctg_nos": list(selected_ctg_nos),
                "selected_artist_nos": selected_artist_nos_for_sort,
                "selected_group_nos": selected_group_nos_for_sort,
                "radius_km": radius_km,
            },
        ).mappings().all()
        tier2_events = [_coerce_coords(dict(r)) for r in tier2_rows]
        tier2_events = dedupe_by_coordinates(tier2_events)
        for ev in tier2_events:
            ev["preference_tier"] = PREFERENCE_TIER_2

    candidate_artist_nos = {
        ev["artist_no"] for ev in events + tier2_events if ev.get("artist_no") is not None
    }
    selected_artist_nos = set(user_input.get("artist_nos") or [])
    artist_group_map = build_artist_group_map(db, candidate_artist_nos | selected_artist_nos)

    scored = []
    for ev in events:
        if not hard_filter(ev):
            continue
        r = calc_relevance(ev, user_input, artist_group_map=artist_group_map)
        scored.append((ev, r["relevance"]))
    scored.sort(key=lambda pair: pair[1], reverse=True)

    # 콘서트는 relevance 순위와 무관하게 항상 포함(run()이 하드 요구) — 재축소로 잘려나가면 안 됨.
    if concert_event_no is not None:
        concert_pair = next((p for p in scored if p[0]["event_no"] == concert_event_no), None)
        rest = [p for p in scored if p[0]["event_no"] != concert_event_no]
        trimmed = ([concert_pair] if concert_pair else []) + rest[:top_n]
    else:
        trimmed = scored[:top_n]

    # Tier2도 같은 방식으로 채점 후 재축소(tier2_top_n) — category_fitness는
    # al02_pipeline.category_fitness()가 이미 계산하는 기존 MISS=0.3이 그대로 적용된다
    # (여기서 새 공식을 만들지 않음, 기존 calc_relevance() 그대로 재사용).
    tier2_scored = []
    for ev in tier2_events:
        if not hard_filter(ev):
            continue
        r = calc_relevance(ev, user_input, artist_group_map=artist_group_map)
        tier2_scored.append((ev, r["relevance"]))
    tier2_scored.sort(key=lambda pair: pair[1], reverse=True)
    tier2_trimmed = tier2_scored[:tier2_top_n]

    trimmed_events = [ev for ev, _ in trimmed] + [ev for ev, _ in tier2_trimmed]
    candidates = [(i, score) for i, (_, score) in enumerate(trimmed + tier2_trimmed)]
    matrix = build_travel_matrix(db, trimmed_events, use_haversine=use_haversine)
    return candidates, trimmed_events, matrix
