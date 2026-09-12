"""AL-02 동선 수정 — "현재 장소 주변 대체 장소" 추천 + 교체 검증 (2026-09-10 신규).

⚠ 이 세션은 이 작업이 참조하기로 한 al02_alternatives.py(기존 구현) 및
AL02_대체장소추천_구현명세.md를 이 프로젝트 어디서도 찾지 못했다(Glob 재확인 —
al02_*.py 목록에 이 파일이 없었고 *.md에도 없었음). "검토 완료"라고 전달받았지만
실제로 그 문서를 읽어본 적은 없다는 뜻이다. 아래 구현은 채팅으로 직접 전달된
스펙만 근거로 작성했다:
  - 반경 5km 고정(가변화 금지), 대상 이벤트와 같은 ctg_no 유지
  - ctg_type IN(2,3) 방어(§al02_candidates.CANDIDATE_SEARCH_SQL과 동일한 이중 방어 원칙)
  - op_status IN(1,2)
  - 거리순 정렬만 사용(relevance 결합 금지 — relevance는 응답에 표시용으로만 동봉)
  - 상위 5개, 콘서트/이미 그 trip 전체 동선(모든 day)에 포함된 장소는 후보에서 제외
    (2026-09-10 1차 수정 — 원래 그 날 하루치만 봐서 다른 날짜에 이미 쓴 장소가 후보로
    새는 버그가 있었다). 2차 수정(같은 날) — event_no(PK)만으로는 "좌표가 완전히
    같은 별개의 event_no"(DB에 같은 실제 장소가 중복 등록된 경우, 예: 278/287
    "도자기카페 줄")까지는 못 잡아서, 이미 쓴 이벤트들과 좌표(소수 5자리 반올림)가
    같은 후보도 팀 확정으로 함께 제외하도록 확장(get_alternatives의 used_coord_keys)
  - "추가"가 아니라 "교체"만 지원(자리 개수를 안 늘림)
  - 영업시간 필터는 S3(al02_pipeline.build_open_matrix)와 완전히 동일한 3단계 판정을
    그대로 재사용한다(행 없음=통과/둘 다 NULL=휴무/한쪽만 NULL=통과, close_tm="00:00"
    보정 포함) — 로직을 따로 만들지 않고 함수 자체를 import해서 쓴다. 기존
    "EXISTS...open_tm IS NOT NULL" 방식(행 없음=제외)은 성지류 카테고리가 event_op_hour
    커버리지 0%라 그대로 쓰면 대체 후보가 사실상 0건이 되는 문제가 있어 폐기한다.

verify_swap()의 실 이동시간 lazy 계산은 이 모듈(verify_swap 함수 내부)에서 처리하기로
결정했다 — 엔드포인트 레벨에서 하면 "이 스왑이 실제로 시간 안에 들어맞는지" 검증이라는
단일 책임이 두 곳(엔드포인트 + 이 함수)에 걸쳐 흩어진다. al02_candidates.py가 DB/API
호출을 캡슐화하는 기존 패턴(build_travel_matrix, build_extra_travel_minutes)과 같은
이유로, "무엇을 검증할지 아는 함수가 필요한 데이터도 직접 가져온다"가 일관적이다.

2026-09-11 다양성 제약 도입(AL02_diversity_constraint_handover.md): get_alternatives()가
al02_diversity.py의 can_insert_candidate()/diagnose_candidate()를 /recommend(al02_pipeline.py)
와 그대로 공유해, 교체 대상을 뺀 트립 전체·당일 상태 기준으로 후보를 한 번 더 거른다
(문서 6.2). 이 재검증이 없으면 "브랜드/카테고리 상한을 지키던 동선"에 alternatives로
새 후보를 끼워 넣는 순간 그 상한이 다시 깨질 수 있었다(2026-09-11 3자 검토에서 지적된
갭 — 실제로 코드에 없었음을 확인). verify_swap()에 use_cache_only 옵션을 추가해
alternatives 조회 단계에서는 카카오 라이브 API를 호출하지 않도록 분리했다(900건 쿼터가
"후보 목록을 열어보기만 해도" 깎이는 걸 막기 위함 — 문서에 없는 구현 결정).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

import numpy as np
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

import travel_time_service
from al02_candidates import build_artist_group_map, dedupe_by_coordinates, fetch_business_hours
from al02_diversity import (
    build_day_diversity_state,
    build_trip_diversity_state,
    can_insert_candidate,
    compute_n_non_concert_target,
    diagnose_candidate,
    diversity_fit as calc_diversity_fit,
    make_diversity_caps,
    replacement_score as calc_replacement_score,
    travel_fit as calc_travel_fit,
)
from al02_pipeline import (
    ROUTE_POLICY,
    STAY_MAP,
    _to_min_or_none,
    build_open_matrix,
    calc_relevance,
    hard_filter,
    haversine_min,
    pick_depot_accom,
    s4_solve,
)
from al02_policy import (
    ALLOWED_PREFERENCE_CTG_TYPES,
    CONCERT_VISIT_COUNT_PROFILES,
    TRIP_DENSITY_TO_WREL_KEY,
    VISIT_COUNT_PROFILES,
)
from models import Accom, Ctg, Event, Trip, TripInterest, TripRoute, TripRouteEvent, RuteArtistSelect

ALTERNATIVES_RADIUS_KM = 5.0  # 고정 — 가변화 금지(요청 사양)
ALTERNATIVES_LIMIT = 5

# defense-in-depth: al02_candidates.CANDIDATE_SEARCH_SQL과 동일한 원칙(§al02_candidates.py
# 2026-09-10 주석 참고) — ctg_no만 믿지 않고 ctg_type도 SQL에서 한 번 더 확인한다.
_ALLOWED_CTG_TYPES_SQL = "(" + ", ".join(str(t) for t in ALLOWED_PREFERENCE_CTG_TYPES) + ")"

_ALTERNATIVES_SEARCH_SQL = text(f"""
    SELECT
      e.event_no, e.event_nm, e.event_lat, e.event_lon, e.ctg_no, c.ctg_nm,
      e.artist_no, e.artist_group_no, e.op_status_no,
      (6371 * ACOS(
        COS(RADIANS(:origin_lat)) * COS(RADIANS(e.event_lat))
        * COS(RADIANS(e.event_lon) - RADIANS(:origin_lon))
        + SIN(RADIANS(:origin_lat)) * SIN(RADIANS(e.event_lat))
      )) AS distance_km
    FROM event e
    JOIN ctg c ON c.ctg_no = e.ctg_no
    WHERE e.op_status_no IN (1, 2)
      AND e.ctg_no = :ctg_no
      AND c.ctg_type_no IN {_ALLOWED_CTG_TYPES_SQL}
      AND e.event_no NOT IN :exclude_event_nos
    HAVING distance_km <= :radius_km
    ORDER BY distance_km
    LIMIT 50
""").bindparams(bindparam("exclude_event_nos", expanding=True))


class SwapInfeasibleError(Exception):
    """verify_swap()이 s4_solve로 재검증한 결과 그 날 시간 예산 안에 못 들어간 경우.
    조용히 넘어가지 않고 호출부(auth.py PUT /trip-routes)가 저장을 막고 이 사유로
    명확한 에러를 반환한다."""


def get_alternatives(
    db: Session, trip_no: int, event_no: int, visit_day: int | None = None,
) -> list[dict]:
    """trip_no 여행의 event_no 자리에 대신 넣을 수 있는 "주변 대체 장소" 후보를 찾는다.

    반환: [{event_no, event_nm, ctg_nm, distance_km, relevance, is_open}, ...] 거리순
    (relevance는 표시용 — 정렬에는 안 씀). visit_day를 안 주면 그 trip 안에서 event_no가
    걸린 첫 번째 날짜를 쓴다(같은 event_no가 여러 날 동선에 중복 등장하는 경우의 모호성 —
    이번 범위에선 이 정도로만 처리, 명세서 부재로 판단한 부분)."""
    trip = db.query(Trip).filter(Trip.trip_no == trip_no).first()
    if not trip:
        return []

    target_event = db.query(Event).filter(Event.event_no == event_no).first()
    if not target_event:
        return []

    # 이 event_no가 걸린 그 트립의 TripRoute(day) 찾기 — visit_day로 특정하거나 첫 매치.
    route_query = (
        db.query(TripRouteEvent, TripRoute)
        .join(TripRoute, TripRouteEvent.trip_route_no == TripRoute.trip_route_no)
        .filter(TripRoute.trip_no == trip_no, TripRouteEvent.event_no == event_no)
    )
    if visit_day is not None:
        route_query = route_query.filter(TripRoute.visit_day == visit_day)
    match = route_query.order_by(TripRoute.visit_day).first()
    if not match:
        return []
    _, day_row = match
    resolved_visit_day = day_row.visit_day

    # 이 trip의 전체 동선(모든 day)에 이미 포함된 event_no 전부 — 후보에서 제외한다.
    # 2026-09-10 수정: 원래 day_row.trip_route_no(그 날짜) 하나로만 필터링해서 다른
    # 날짜에 이미 쓰인 장소가 그대로 후보로 새어나오는 버그가 있었다(실측:
    # trip_no=42/43/44에서 25건 재현) — trip_no 전체로 JOIN 범위를 넓혔다. 교체 대상
    # event_no 자기 자신도 이 안에 항상 포함된다(그 자신이 속한 TripRouteEvent 행이
    # 바로 이 집합에 들어있으므로).
    trip_wide_event_nos = {
        r.event_no
        for r in db.query(TripRouteEvent.event_no)
        .join(TripRoute, TripRouteEvent.trip_route_no == TripRoute.trip_route_no)
        .filter(TripRoute.trip_no == trip_no)
        .all()
    }
    exclude_event_nos = trip_wide_event_nos | {trip.event_no}  # 콘서트도 제외
    logger.debug(
        "get_alternatives: trip_no=%s target_event_no=%s resolved_visit_day=%s "
        "exclude_event_nos(PK)=%s (SQL 필터에 실제 바인딩됨)",
        trip_no, event_no, resolved_visit_day, sorted(exclude_event_nos),
    )

    # 이미 쓴 이벤트와 좌표가 동일한 "쌍둥이" event_no도 함께 제외한다(2026-09-10 추가,
    # 팀 확정 — 실측 사례: trip_no=47에서 event_no=287("도자기카페 줄")이 동선에 있는데
    # 좌표가 완전히 같은 별개 PK인 event_no=278("도자기카페 줄(JOOL)")이 그대로 대체
    # 후보에 노출됨). event_no(PK) 비교만으로는 이 케이스를 원리적으로 못 잡는다 — DB에
    # 같은 실제 장소가 서로 다른 event_no로 중복 등록돼 있기 때문(근본 원인은 DA 데이터
    # 정합성 문제, 별도 전달 필요). al02_candidates.dedupe_by_coordinates와 동일하게
    # 소수 5자리(약 1m) 반올림 키로 판정 — 그 함수 자체를 바꾸는 게 아니라 "이미 쓴 좌표"
    # 를 이번 exclude 단계에서 한 번 더 걸러내는 것뿐이라 §범위 밖(dedupe 로직 변경 없음)
    # 과 충돌하지 않는다.
    used_coord_keys = {
        (round(float(r.event_lat), 5), round(float(r.event_lon), 5))
        for r in db.query(Event.event_lat, Event.event_lon)
        .filter(Event.event_no.in_(exclude_event_nos))
        .all()
        if r.event_lat is not None and r.event_lon is not None
    }
    logger.debug("get_alternatives: used_coord_keys(5자리 반올림)=%s", used_coord_keys)

    rows = db.execute(
        _ALTERNATIVES_SEARCH_SQL,
        {
            "origin_lat": float(target_event.event_lat), "origin_lon": float(target_event.event_lon),
            "ctg_no": target_event.ctg_no,
            "exclude_event_nos": list(exclude_event_nos) or [-1],
            "radius_km": ALTERNATIVES_RADIUS_KM,
        },
    ).mappings().all()
    logger.debug(
        "get_alternatives: SQL 원본 결과 event_no=%s (PK exclude 적용 후, 좌표 교차 제외/dedupe 적용 전)",
        [r["event_no"] for r in rows],
    )
    candidates = [dict(r) for r in rows if hard_filter(r)]
    before_coord_filter = {c["event_no"] for c in candidates}
    candidates = [
        c for c in candidates
        if c.get("event_lat") is None or c.get("event_lon") is None
        or (round(float(c["event_lat"]), 5), round(float(c["event_lon"]), 5)) not in used_coord_keys
    ]
    dropped_as_twin = before_coord_filter - {c["event_no"] for c in candidates}
    if dropped_as_twin:
        logger.debug(
            "get_alternatives: 이미 쓴 이벤트와 좌표 동일해서 제외된 event_no=%s", dropped_as_twin,
        )
    # 좌표 중복 제거(2026-09-10) — al02_candidates.py의 공통 함수를 S0과 그대로 공유한다.
    # 이 함수는 거리순 정렬을 그대로 보존하므로(al02_candidates.dedupe_by_coordinates
    # docstring 참고) 아래 distance_km 오름차순이 흐트러지지 않는다. (위의 좌표 교차
    # 제외와는 목적이 다르다 — 위는 "이미 쓴 것과 겹침" 제거, 이건 "후보 목록 안에서
    # 서로 겹치는 것" 제거.)
    candidates = dedupe_by_coordinates(candidates)

    if not candidates:
        return []

    # ── 영업시간 3단계 판정 — S3와 완전히 동일한 함수 재사용(al02_pipeline.build_open_matrix) ──
    if resolved_visit_day is not None and trip.start_dt is not None:
        target_date = (trip.start_dt + timedelta(days=resolved_visit_day - 1)).strftime("%Y-%m-%d")
        business_hours = fetch_business_hours(db, [c["event_no"] for c in candidates])
        user_start_min = _to_min_or_none(trip.start_tm.strftime("%H:%M") if trip.start_tm else None)
        user_end_min = _to_min_or_none(trip.end_tm.strftime("%H:%M") if trip.end_tm else None)
        day_start_min = user_start_min if user_start_min is not None else ROUTE_POLICY["DAY_START_MIN"]
        day_end_min = user_end_min if user_end_min is not None else ROUTE_POLICY["DAY_END_MIN"]
        open_matrix = build_open_matrix(
            candidates, [target_date], business_hours, day_start_min, day_end_min,
        )
        is_open_by_idx = {i: bool(open_matrix[i, 0]) for i in range(len(candidates))}
    else:
        is_open_by_idx = {i: True for i in range(len(candidates))}  # 날짜 특정 불가 — 판단 보류(통과)

    is_open_by_event_no = {
        cand["event_no"]: is_open_by_idx.get(i, True) for i, cand in enumerate(candidates)
    }

    # ── relevance(표시용) — 트립의 실제 선호 카테고리/아티스트로 계산, 정렬엔 안 씀 ──
    ctg_nos = [
        r.ctg_no for r in db.query(TripInterest)
        .filter(TripInterest.trip_no == trip_no).order_by(TripInterest.rank).all()
    ]
    artist_nos = [
        r.artist_no for r in db.query(RuteArtistSelect.artist_no)
        .filter(RuteArtistSelect.trip_no == trip_no).all()
    ]
    # rute_artist_select는 그룹 전체 선택도 그 시점 멤버 전원을 개별 row로 풀어서 저장하므로
    # (POST /trips 참고, auth.py의 동일 주석과 일치) group_nos는 비워도 되고,
    # build_selected_group_nos가 artist_nos + artist_group_map만으로 소속 그룹을 복원한다.
    user_input_for_relevance = {"artist_nos": artist_nos, "group_nos": [], "ctg_nos": ctg_nos}

    # 2026-09-11 버그 수정: artist_group_map={}로 하드코딩돼 있어서, "그룹 전체" 태그
    # 이벤트(artist_no NULL, artist_group_no만 있음)가 전부 0.7 대신 0.0으로 깎여 relevance가
    # 사실상 상수처럼 보였다(al02_candidates.py의 실 recommend 경로는 이미 이 맵을 정상적으로
    # 채워서 넘기고 있었음 — 여기만 빠져 있던 것). candidates(후보 이벤트)의 artist_no와
    # 트립이 선택한 artist_nos를 합쳐서 al02_candidates.build_artist_group_map()으로 실제
    # {artist_no: artist_group_no} 매핑을 조회한다(같은 함수를 그대로 재사용 — 로직 이중
    # 구현 없음).
    candidate_artist_nos = {c["artist_no"] for c in candidates if c.get("artist_no") is not None}
    artist_group_map = build_artist_group_map(db, candidate_artist_nos | set(artist_nos))

    # ── 2026-09-11 다양성 컨텍스트(문서 6.2) — 교체 대상 event_no를 뺀 상태로 트립
    # 전체·당일 다양성 상태를 만든다. eventno(문자열)가 아니라 event_no(PK)로만 판정
    # 한다(문서 2.2 필수조건) — events_by_no의 키/exclude_idx 전부 event_no 그대로 사용.
    trip_wide_rows = (
        db.query(Event.event_no, Event.ctg_no, Event.event_nm)
        .filter(Event.event_no.in_(trip_wide_event_nos))
        .all()
    )
    events_by_no = {
        r.event_no: {"event_no": r.event_no, "ctg_no": r.ctg_no, "event_nm": r.event_nm}
        for r in trip_wide_rows
    }
    same_day_event_nos = [
        r.event_no
        for r in db.query(TripRouteEvent.event_no)
        .join(TripRoute, TripRouteEvent.trip_route_no == TripRoute.trip_route_no)
        .filter(TripRoute.trip_no == trip_no, TripRoute.visit_day == resolved_visit_day)
        .order_by(TripRouteEvent.seq)
        .all()
    ]

    # N_non_concert_target(문서 4.5)을 내려면 이 트립의 밀도 프로파일이 필요하다.
    # trip_density_no가 아직 없으면(레거시/미설정) 가장 보수적인 값이 아니라 B(균형)로
    # 폴백한다 — POST /trips/{trip_no}/recommend가 이 값 없이는 아예 400을 내는 것과
    # 달리, alternatives는 그 이전 단계에서도 호출될 수 있어(예: 저장된 동선을 나중에
    # 수정) 완전히 막지 않는 쪽을 택했다.
    wrel_key = TRIP_DENSITY_TO_WREL_KEY.get(trip.trip_density_no, "B")
    visit_profile = VISIT_COUNT_PROFILES[wrel_key]
    concert_visit_profile = CONCERT_VISIT_COUNT_PROFILES[wrel_key]
    n_days = (trip.end_dt - trip.start_dt).days + 1
    concert_day_idx = (trip.event_date - trip.start_dt).days if trip.event_date else None
    n_non_concert_target = compute_n_non_concert_target(
        n_days, concert_day_idx, visit_profile["max"], concert_visit_profile["max"],
    )
    caps = make_diversity_caps(ctg_nos, n_non_concert_target)
    is_concert_day = concert_day_idx is not None and resolved_visit_day == concert_day_idx + 1

    exclude_target = {event_no}
    trip_state = build_trip_diversity_state([trip_wide_event_nos], events_by_no, exclude_idx=exclude_target)
    day_state = build_day_diversity_state(same_day_event_nos, events_by_no, exclude_idx=exclude_target)
    day_other_events = [
        events_by_no[no] for no in same_day_event_nos if no != event_no and no in events_by_no
    ]

    excluded_reason_counts = Counter()
    diversity_filtered = []
    for cand in candidates:
        # axes를 duplicate_event/brand_trip_cap 두 개로 좁힌다(al02_diversity._diversity_checks
        # docstring 참고) — 이 SQL의 후보는 전부 target_event.ctg_no와 같은 ctg_no만
        # 가져오므로(_ALTERNATIVES_SEARCH_SQL), category_day_cap/category_trip_cap/
        # shopping_trip_cap 세 축은 어떤 후보를 고르든 교체 전후로 그 카테고리 개수가
        # 똑같아(수학적으로 불변) 항상 모든 후보에 동일하게 통과/실패한다 — 이미 하루
        # 상한을 넘어 배정된 실제 트립(예: 성지 음식점 4곳짜리 맛집 투어)에 그대로 걸면
        # 대체 후보가 전부 0건이 되는 회귀가 실측(trip_no=43)으로 확인됐다.
        ok, reason = can_insert_candidate(
            cand, day_state, trip_state, caps,
            is_concert_day=is_concert_day, allow_day_category_relaxation=False,
            axes=("duplicate_event", "brand_trip_cap"),
        )
        if ok:
            diversity_filtered.append(cand)
        else:
            excluded_reason_counts[reason] += 1
        if len(diversity_filtered) >= ALTERNATIVES_LIMIT * 3:
            break  # 기존 "top5*3만 넉넉히 계산" 취지 유지 — 더 먼 후보까지 볼 필요 없음
    if excluded_reason_counts:
        logger.debug(
            "get_alternatives: 다양성 제약으로 제외된 후보 사유 분포=%s", dict(excluded_reason_counts),
        )

    # ── 6.2/6.3: 교체 후 시간 예산·공연 버퍼 재검증 + added_travel_min ──
    # verify_swap(use_cache_only=True) — 카카오 라이브 API를 호출하지 않는다(캐시+haversine
    # 근사만 사용, 900건 쿼터 보호). 실제 저장 시(PUT /trip-routes)의 verify_swap()은
    # 여전히 라이브 조회를 하므로 최종 확정 시점의 정확도는 그대로 유지된다.
    baseline_result = verify_swap(db, trip_no, resolved_visit_day, same_day_event_nos, use_cache_only=True)
    baseline_total = (
        sum(s["travel_from_prev_min"] for s in baseline_result["schedule"])
        if baseline_result["ok"] else None
    )

    results = []
    for cand in diversity_filtered:
        if len(results) >= ALTERNATIVES_LIMIT:
            break
        rel_detail = calc_relevance(cand, user_input_for_relevance, artist_group_map=artist_group_map)

        cand_day_event_nos = [cand["event_no"] if no == event_no else no for no in same_day_event_nos]
        swap_result = verify_swap(db, trip_no, resolved_visit_day, cand_day_event_nos, use_cache_only=True)
        if not swap_result["ok"]:
            excluded_reason_counts["time_budget_or_concert_buffer"] += 1
            continue
        candidate_total = sum(s["travel_from_prev_min"] for s in swap_result["schedule"])
        added_travel_min = candidate_total - baseline_total if baseline_total is not None else 0
        # 2026-09-12(작업지시 7번) — 캐시에 없어 haversine 근사가 하나라도 섞였으면
        # 프론트에 "예상 추가 이동시간"으로 표시하도록 명시한다(정확한 실측값이 아님).
        added_travel_min_is_approximate = bool(
            baseline_result.get("approximate") or swap_result.get("approximate")
        )

        travel_fit_value = calc_travel_fit(added_travel_min)
        diversity_fit_value = calc_diversity_fit(cand, day_other_events)
        rscore = calc_replacement_score(rel_detail["relevance"], travel_fit_value, diversity_fit_value)

        constraint_check = diagnose_candidate(
            cand, day_state, trip_state, caps,
            is_concert_day=is_concert_day, allow_day_category_relaxation=False,
        )
        # can_insert_candidate가 이미 True를 보장한 다섯 축(diagnose_candidate가 재확인)에,
        # 방금 재검증한 시간/공연 버퍼 결과를 더해 문서 6.4의 constraint_check 전체를 채운다.
        constraint_check["time_budget_ok"] = True
        constraint_check["concert_buffer_ok"] = True

        results.append({
            "event_no": cand["event_no"], "event_nm": cand["event_nm"],
            "ctg_no": cand["ctg_no"], "ctg_nm": cand["ctg_nm"],
            "distance_km": round(float(cand["distance_km"]), 3),
            # relevance는 기존 필드 이름 그대로 유지(문서 6.4 "기존 응답 호환성 유지") —
            # base_relevance와 값이 같다(취향 적합도, 거리와 무관).
            "relevance": rel_detail["relevance"],
            "base_relevance": rel_detail["relevance"],
            "replacement_score": rscore,
            "display_score": round(rscore * 100),
            "added_travel_min": added_travel_min,
            # true면 캐시에 없어 haversine 근사로 계산된 값 — 프론트는 "예상 추가
            # 이동시간"처럼 근사치임을 표시하는 문구를 쓸 것(작업지시 7번).
            "added_travel_min_is_approximate": added_travel_min_is_approximate,
            "is_open": is_open_by_event_no.get(cand["event_no"], True),
            "score_breakdown": {
                "artist_match": rel_detail["artist_match"],
                "category_fitness": rel_detail["category_fitness"],
                "place_quality": rel_detail["place_quality"],
                "travel_fit": travel_fit_value,
                "diversity_fit": diversity_fit_value,
            },
            "constraint_check": constraint_check,
        })

    # 정렬은 어디까지나 거리순만(이미 SQL이 거리순으로 가져왔고, 다양성 필터링/verify_swap
    # 재검증도 그 순서를 그대로 보존하며 훑는다) — replacement_score는 참고용 표시값이지
    # 정렬 기준이 아니다. 휴무 후보를 자동으로 빼지 않는 이유: "제외"가 아니라 "표시"만
    # 하라는 §4.4 판정의 성격(휴무=False라는 사실을 프론트가 보여주되, 그래도 후보
    # 목록엔 남겨서 사용자가 알고 고를 수 있게)에 맞춘 판단 — 명세서 부재로 인한 해석.
    return results


def _travel_minutes_cache_or_haversine(db, origin_type, origin_no, origin_lat, origin_lon, destinations):
    """travel_time_service.get_travel_minutes()의 "조회 전용" 버전(2026-09-11 신규) —
    카카오 라이브 API를 절대 호출하지 않고, travel_time_cache 조회 + al02_pipeline.
    haversine_min() 근사만 쓴다(al02_candidates.build_travel_matrix()가 캐시 미스 개별
    건에 쓰는 것과 동일한 근사 정책). verify_swap(use_cache_only=True)가 alternatives의
    후보 사전 필터링(문서 6.2)에 쓴다 — "후보 목록을 열어보기만 해도" 라이브 API가
    나가 900건 쿼터를 깎는 걸 막기 위한 설계 결정(문서에 없음, 완료 보고에 명시).

    destinations: [{"event_no":.., "lat":.., "lon":..}, ...]. origin_type="event"인
    경우 반대 방향 캐시 행(al02_candidates.build_travel_matrix()가 i<j 한쪽만 저장하는
    대칭 근사라, 원래 추천 시점에 반대 방향으로만 적재됐을 수 있음)도 찾아본다.

    반환: (result, haversine_used_nos) — 2026-09-12 추가(작업지시 7번 "approximation
    여부 명시"). haversine_used_nos는 result 중 캐시에 없어서 haversine 근사로 채운
    destination event_no 집합 — 호출부(verify_swap)가 이걸 모아서 "이 스케줄 비용
    계산에 근사치가 하나라도 섞였는지"를 알려준다."""
    dest_by_no = {d["event_no"]: d for d in destinations}
    if not dest_by_no:
        return {}, set()
    result: dict[int, int] = {}
    rows = db.execute(
        text(
            "SELECT destination_event_no, duration_min FROM travel_time_cache "
            "WHERE origin_type=:ot AND origin_no=:on_ AND travel_mode='car' "
            "AND destination_event_no IN :dest_nos"
        ).bindparams(bindparam("dest_nos", expanding=True)),
        {"ot": origin_type, "on_": origin_no, "dest_nos": list(dest_by_no.keys())},
    ).all()
    for dest_no, minutes in rows:
        result[dest_no] = minutes
    missing = [no for no in dest_by_no if no not in result]
    if missing and origin_type == "event":
        rows2 = db.execute(
            text(
                "SELECT origin_no, duration_min FROM travel_time_cache "
                "WHERE origin_type='event' AND destination_event_no=:on_ AND travel_mode='car' "
                "AND origin_no IN :dest_nos"
            ).bindparams(bindparam("dest_nos", expanding=True)),
            {"on_": origin_no, "dest_nos": missing},
        ).all()
        for origin_no2, minutes in rows2:
            result[origin_no2] = minutes
        missing = [no for no in dest_by_no if no not in result]
    haversine_used_nos: set = set()
    for no in missing:
        d = dest_by_no[no]
        if d.get("lat") is None or d.get("lon") is None:
            continue
        result[no] = haversine_min(origin_lat, origin_lon, float(d["lat"]), float(d["lon"]))
        haversine_used_nos.add(no)
    return result, haversine_used_nos


def verify_swap(db: Session, trip_no: int, visit_day: int, event_nos_in_order: list[int],
                 use_cache_only: bool = False) -> dict:
    """그 날짜(visit_day)의 최종 이벤트 목록(event_nos_in_order — 교체 후 상태, 콘서트
    포함 가능)이 시간 예산 안에 들어가는 유효한 동선인지 s4_solve 1회로 재검증한다
    (트리밍 폴백 없음 — 안 맞으면 바로 실패 처리, s4_solve_fallback처럼 자동으로 다른
    장소를 더 빼지 않는다). "old->new 단일 교체"로 좁히지 않고 그 날의 전체 목록을
    받는 형태로 일반화했다 — PUT /trip-routes는 한 번에 여러 곳을 바꿀 수도 있는
    "그 날짜 통째 교체" 구조라(§replace_trip_routes), "old 1개/new 1개"로 좁힌 시그니처는
    그 흐름에 그대로 못 꽂힌다. 대신 단일 스왑도 "바뀐 목록 전체"로 표현하면 똑같이 처리됨.

    이동시간은 travel_time_cache 우선 조회, 없는 조합만 그 자리에서 카카오 API로 lazy
    계산한다 — 기존에 캐시된 이벤트(원래 추천 시점에 이미 event<->event로 캐시된 것들)는
    재호출이 없고, 새로 들어온 이벤트가 관련된 쌍만 실제로 조회된다(보통 3~6쌍 수준,
    "최대 5쌍" 요청과 같은 규모). 반환: {"ok": bool, "schedule": [...] or None,
    "reason": str or None}.

    use_cache_only(2026-09-11 신규): True면 카카오 라이브 API 호출 없이 캐시+haversine
    근사만 쓴다(_travel_minutes_cache_or_haversine 참고) — get_alternatives()가 후보
    여러 개를 "조회"만 할 때 이 값으로 부른다. PUT /trip-routes가 실제 저장 시 부르는
    호출은 여전히 기본값(False, 라이브 조회)이라 최종 확정 검증의 정확도는 그대로다."""
    trip = db.query(Trip).filter(Trip.trip_no == trip_no).first()
    if not trip:
        return {"ok": False, "schedule": None, "reason": "존재하지 않는 여행입니다."}
    if not event_nos_in_order:
        return {"ok": True, "schedule": [], "reason": None}  # 빈 날짜는 검증할 것도 없이 통과

    events_by_no = {
        e.event_no: e
        for e in db.query(Event).filter(Event.event_no.in_(event_nos_in_order)).all()
    }
    missing = [no for no in event_nos_in_order if no not in events_by_no]
    if missing:
        return {"ok": False, "schedule": None, "reason": f"존재하지 않는 event_no: {missing}"}

    # ── 그날 depot(숙소) — al02_pipeline.pick_depot_accom() 그대로 재사용(S3와 동일 정책) ──
    accoms = [
        {
            "accom_no": a.accom_no, "lat": float(a.accom_lat), "lon": float(a.accom_lon),
            "check_in_dt": a.check_in_dt.isoformat() if a.check_in_dt else None,
            "check_out_dt": a.check_out_dt.isoformat() if a.check_out_dt else None,
        }
        for a in db.query(Accom).filter(Accom.trip_no == trip_no).all()
        if a.accom_lat is not None and a.accom_lon is not None
    ]
    if not accoms:
        return {"ok": False, "schedule": None, "reason": "좌표가 등록된 숙소가 없습니다."}
    date_str = (trip.start_dt + timedelta(days=visit_day - 1)).strftime("%Y-%m-%d")
    depot_accom = pick_depot_accom(date_str, accoms)
    depot_lat, depot_lon = depot_accom["lat"], depot_accom["lon"]
    depot_origin_type, depot_origin_no = "accom", depot_accom["accom_no"]

    # 첫날 출발핀/마지막날 도착핀 — 있으면 depot 대신 그걸 씀(§4.2 우선순위, S3와 동일).
    n_days = (trip.end_dt - trip.start_dt).days + 1
    if visit_day == 1 and trip.start_place_lat is not None and trip.start_place_lon is not None:
        depot_lat, depot_lon = float(trip.start_place_lat), float(trip.start_place_lon)
        depot_origin_type, depot_origin_no = "trip_start_pin", trip_no
    if visit_day == n_days and trip.end_place_lat is not None and trip.end_place_lon is not None:
        depot_lat, depot_lon = float(trip.end_place_lat), float(trip.end_place_lon)
        depot_origin_type, depot_origin_no = "trip_end_pin", trip_no

    # ── 이동시간(캐시 우선 + lazy 계산) — 순서: 0=depot, 1..N=event_nos_in_order ──
    ordered_nos = list(event_nos_in_order)
    n = len(ordered_nos)
    names = [events_by_no[no].event_nm for no in ordered_nos]
    stays = [
        ROUTE_POLICY["CONCERT_DURATION_MIN"] if no == trip.event_no else STAY_MAP.get(
            db.query(Ctg.ctg_nm).filter(Ctg.ctg_no == events_by_no[no].ctg_no).scalar(), 40
        )
        for no in ordered_nos
    ]
    matrix = np.zeros((n + 1, n + 1), dtype=np.int64)  # +1 = depot(인덱스 0), 나머지는 1..n

    # 2026-09-12 추가(작업지시 7번) — use_cache_only 경로에서 캐시가 없어 haversine
    # 근사로 채운 event_no가 하나라도 있으면 approx_used=True. 반환 스케줄에
    # "approximate"로 실려 alternatives 응답까지 전달된다.
    approx_used = False

    def _get_minutes(origin_type, origin_no, origin_lat, origin_lon, destinations):
        nonlocal approx_used
        if use_cache_only:
            minutes, haversine_used_nos = _travel_minutes_cache_or_haversine(
                db, origin_type, origin_no, origin_lat, origin_lon, destinations,
            )
            if haversine_used_nos:
                approx_used = True
            return minutes
        return travel_time_service.get_travel_minutes(
            db, origin_type=origin_type, origin_no=origin_no,
            origin_lat=origin_lat, origin_lon=origin_lon, destinations=destinations,
        )

    try:
        minutes_from_depot = _get_minutes(
            depot_origin_type, depot_origin_no, depot_lat, depot_lon,
            destinations=[
                {"event_no": no, "lat": float(events_by_no[no].event_lat), "lon": float(events_by_no[no].event_lon)}
                for no in ordered_nos
            ],
        )
        for i, no_i in enumerate(ordered_nos):
            depot_min = minutes_from_depot.get(no_i, 0)
            matrix[0, i + 1] = depot_min
            matrix[i + 1, 0] = depot_min

        # 이벤트<->이벤트: i<j 방향만 실제로 조회하고 대칭 적용(al02_candidates.
        # build_travel_matrix와 동일한 근사·정책) — 캐시에 이미 있으면(원래 추천 시점에
        # 적재됐을 가능성이 높음) API 재호출 없음.
        for i, no_i in enumerate(ordered_nos):
            rest = [
                {"event_no": no_j, "lat": float(events_by_no[no_j].event_lat), "lon": float(events_by_no[no_j].event_lon)}
                for no_j in ordered_nos[i + 1:]
            ]
            if not rest:
                continue
            minutes_i = _get_minutes(
                "event", no_i, float(events_by_no[no_i].event_lat), float(events_by_no[no_i].event_lon),
                destinations=rest,
            )
            for j in range(i + 1, n):
                t = minutes_i.get(ordered_nos[j], 0)
                matrix[i + 1, j + 1] = t
                matrix[j + 1, i + 1] = t
    except (travel_time_service.TravelTimeQuotaExceeded, travel_time_service.TravelTimeAPIError) as e:
        return {"ok": False, "schedule": None, "reason": f"이동시간 조회 실패: {e}"}

    concert_idx = None
    concert_start = None
    concert_duration = None
    if trip.event_no in ordered_nos:
        concert_idx = ordered_nos.index(trip.event_no) + 1
        concert_event = events_by_no[trip.event_no]
        concert_start = _to_min_or_none(
            concert_event.start_dt.strftime("%H:%M") if concert_event.start_dt else None
        ) or 19 * 60
        concert_duration = ROUTE_POLICY["CONCERT_DURATION_MIN"]

    user_start_min = _to_min_or_none(trip.start_tm.strftime("%H:%M") if trip.start_tm else None)
    user_end_min = _to_min_or_none(trip.end_tm.strftime("%H:%M") if trip.end_tm else None)
    day_start_min = user_start_min if user_start_min is not None else ROUTE_POLICY["DAY_START_MIN"]
    day_end_min = user_end_min if user_end_min is not None else ROUTE_POLICY["DAY_END_MIN"]

    result = s4_solve(
        list(range(1, n + 1)), 0, 0, matrix, ["숙소"] + names, [0] + stays,
        concert_idx=concert_idx, concert_start=concert_start, concert_duration=concert_duration,
        day_start=day_start_min, day_end=day_end_min, max_places=n,
    )
    if result["order"] is None or result["cost"] >= 99999:
        return {"ok": False, "schedule": None,
                "reason": "그 날 시간 예산 안에 들어가는 동선을 찾지 못했습니다."}

    schedule = [
        {
            "event_no": ordered_nos[s["idx"] - 1], "event_nm": s["name"],
            "arrive": s["arrive"], "depart": s["depart"], "stay_min": s["stay"],
            "travel_from_prev_min": s["travel"],
        }
        for s in result["schedule"]
    ]
    return {"ok": True, "schedule": schedule, "reason": None, "approximate": approx_used}
