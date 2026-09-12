"""AL-02 동선 최적화 엔진 — 통합 파이프라인.

AL02_pipeline_v4_final.ipynb에서 그대로 이식(실행 검증 완료). 가중치/정책 상수는
전부 al02_policy 모듈에서만 가져온다(하드코딩 금지) — 값 자체를 바꾸려면 al02_policy.py를 고칠 것.

relevance = W_artist_match * artist_match + W_category_fitness * category_fitness
          + W_place_quality * place_quality   (가중치는 SCORING_POLICY 참고)

⚠ event_no vs matrix 인덱스
- user_input의 concert.event_no는 DB event_no.
- matrix/names/stays/candidates의 인덱스는 events 리스트 인덱스(0~N-1).
- AL02Pipeline.run() 안에서 event_no -> 인덱스 변환을 자동으로 처리한다.

⚠ artist_match 우선순위(v4, 2026-09-09 정정 — 반드시 이 순서)
1순위(1.0): 이벤트의 artist_no가 선택 멤버 본인과 정확히 일치.
2순위(0.7, 폴백): 선택 멤버 본인 이벤트가 아니고, 이벤트가 "그룹 전체" 태그
  (event.artist_group_no)로 걸려 있고, 그 그룹이 선택 멤버 소속 그룹(또는 명시적으로
  고른 그룹)과 일치.
그 외(0.0): 선택 안 한 다른 멤버의 개별 이벤트 포함, 전부 무관 처리 — "그룹 전체"
  콘텐츠가 아니라 "그룹 내 다른 개인" 콘텐츠라 폴백 대상이 아니다.
1.0/0.7 우선순위 자체는 S3가 relevance 내림차순으로 후보를 채우는 구조로 이미 구현된다 —
별도 우선순위 큐는 필요 없고, artist_match()의 점수 체계만 정확히 지키면 된다.
"""

import itertools
import logging
import math
from datetime import datetime, timedelta

import numpy as np

from al02_diversity import (
    ALL_DIVERSITY_AXES,
    CORE_AXES,
    build_day_diversity_state,
    build_trip_diversity_state,
    calculate_selection_score,
    can_insert_candidate,
    compute_n_non_concert_target,
    make_diversity_caps,
)
from al02_policy import (
    ALLOWED_OP_STATUS,
    CONCERT_DAY_GENERAL_POI_TARGET,
    CONCERT_VISIT_COUNT_PROFILES,
    DENSITY_LABEL_BY_WREL_KEY,
    PREFERENCE_TIER_1,
    PREFERENCE_TIER_2,
    ROUTE_POLICY,
    SCORING_POLICY,
    STAY_MAP,
    VISIT_COUNT_PROFILES,
    WREL_PROFILES,
)

logger = logging.getLogger(__name__)


class DepotOverlapError(Exception):
    """같은 날짜에 유효한 숙소(체크인<=날짜<체크아웃, 2026-09-11부터 반열린 구간)가
    2곳 이상 — 팀 확정 정책 위반(2026-09-10). "체크인~체크아웃 기준으로 같은 날짜엔
    숙소가 항상 1곳"이어야 하고, 겹치는 기간 자체가 존재해서는 안 된다 — 정상 케이스가
    아니므로 조용히 하나를 골라 넘어가지 않고 여기서 예외를 던진다. auth.py의
    POST /trips가 쓰기 시점에 이 상태를 막지만(2026-09-10 추가), 그 이전에 생성된
    레거시 데이터(trip_no=32~36 등)는 여전히 이 예외를 발생시킬 수 있다 — 그 데이터
    자체를 고치는 건 이번 범위 밖."""

# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------


def _roc(rank, n):
    return sum(1.0 / k for k in range(rank, n + 1)) / n


def _to_min(t):
    h, m = map(int, str(t).split(":"))
    return h * 60 + m


def _to_min_or_none(t):
    """_to_min의 관대한 버전(2026-09-10 신규) — None이면 None 그대로, datetime/time
    객체(hour/minute 속성 있음)면 거기서 직접 분을 뽑고, 그 외(주로 "HH:MM" 문자열)는
    기존 _to_min으로 처리한다. build_trip_frame()이 trip.start_tm/end_tm을 문자열로
    미리 변환해서 넘기는 게 지금 실제 경로(auth.py)지만, 이 헬퍼는 원본 datetime을
    직접 받는 경로도 안전하게 처리할 수 있게 방어적으로 만든 것."""
    if t is None:
        return None
    if hasattr(t, "hour") and hasattr(t, "minute"):
        return t.hour * 60 + t.minute
    return _to_min(t)


def _to_str(m):
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


# ---------------------------------------------------------------------------
# S2 — 추천 점수 (AHP 3-기준 가중합)
# ---------------------------------------------------------------------------


def build_selected_group_nos(selected_group_nos_explicit, selected_artist_nos, artist_group_map):
    """선택 멤버의 소속 그룹을 알아내는 용도로만 사용한다(폴백 대상 판정용).
    명시적으로 고른 그룹(그룹 전체 선택 모드) + 개별 선택 멤버들의 소속 그룹을 합집합으로 만든다.
    이렇게 해야 "멤버 N명만 개별 선택"한 경우에도, 그 멤버가 속한 그룹의 "그룹 전체" 태그
    콘텐츠(예: 그룹 팬미팅)가 폴백 후보로 정상 매칭된다.

    artist_group_map: {artist_no: artist_group_no} — S0 단계에서 DB(artist 테이블)로
    선택 멤버들의 소속 그룹을 한 번에 조회해 만들어 전달한다.
    """
    groups = set(int(g) for g in (selected_group_nos_explicit or []))
    for a in (selected_artist_nos or []):
        g = (artist_group_map or {}).get(int(a))
        if g is not None:
            groups.add(int(g))
    return groups


def artist_match(event_artist_no, event_artist_group_no,
                  selected_artist_nos, selected_group_nos):
    """1.0=선택 멤버 본인 이벤트(최우선). 0.7=그룹 전체 태그 이벤트(정 없으면 폴백).
    0.0=그 외 전부(선택 안 한 다른 멤버의 개별 이벤트 포함 — 그룹 전체 콘텐츠가 아니므로 폴백 대상 아님).
    다중 멤버 선택 시 max 사용.

    v4 정정: v3에서 "선택 안 한 같은 그룹 멤버 이벤트"에도 0.7을 주도록 확장했었으나 되돌림.
    "선택 멤버 우선, 부족하면 그룹 전체로 폴백"이지 "부족하면 같은 그룹 아무나로 폴백"이 아니기 때문.
    실 DB는 artist_no/artist_group_no 중 하나만 채워지므로(둘 다 채워지는 행 없음, 2026-09-09
    team2에서 재확인 — SELECT COUNT(*) FROM event WHERE artist_no IS NOT NULL AND
    artist_group_no IS NOT NULL = 0), 아래 두 조건은 사실상 겹치지 않는다.
    """
    if selected_artist_nos and event_artist_no is not None:
        if int(event_artist_no) in [int(a) for a in selected_artist_nos]:
            return 1.0
        return 0.0  # 선택 안 한 다른 멤버의 개별 이벤트 — 그룹 전체 콘텐츠가 아니므로 폴백 대상 아님
    if selected_group_nos and event_artist_group_no is not None:
        if int(event_artist_group_no) in [int(g) for g in selected_group_nos]:
            return 0.7
    return 0.0


def category_fitness(event_ctg_no, selected_ctg_nos):
    # 0.3~1.0. ROC + 하한 보정.
    MISS = 0.3
    if not selected_ctg_nos or event_ctg_no not in selected_ctg_nos:
        return MISS
    n, rank = len(selected_ctg_nos), selected_ctg_nos.index(event_ctg_no) + 1
    return round(MISS + (1 - MISS) * (_roc(rank, n) / _roc(1, n)), 4)


def place_quality(total_score, score_min=0.0, score_max=100.0):
    # total_score(external_review.total_score) 정규화. DA팀 확인 완료(2026-09-09): 0~100점 만점.
    # 2026-09-10 실측 재확인: external_review 437행(event_no당 1건, 중복 없음), total_score
    # 실측 범위 11.10~91.00 — 정규화(÷100)와 None(리뷰 없음)→0.0 폴백 둘 다 실 데이터로 검증 완료.
    # ⚠ total_score를 여기(연속값 정규화) 외에 다른 곳(hard_filter, CANDIDATE_SEARCH_SQL의
    # WHERE/HAVING)에서 임계값 배제 용도로 중복 사용하면 안 된다(§2.4) — 2026-09-10 재확인
    # 결과 hard_filter()는 op_status_no만 보고, SQL도 total_score로 필터링하지 않아
    # 저평점(예: total_score=26.5) 이벤트도 후보에서 배제되지 않고 정상적으로 낮은
    # place_quality 값만 받는 것으로 실측 확인됨 — 중복 사용 없음.
    if total_score is None:
        return 0.0
    ts = float(total_score)
    if score_max == score_min:
        return 0.0
    return round(max(0.0, min(1.0, (ts - score_min) / (score_max - score_min))), 4)


def hard_filter(event):
    op = event.get("op_status_no")
    if op is None:
        return False
    return int(op) in ALLOWED_OP_STATUS


def calc_relevance(event, user_input, score_range=(0.0, 100.0), artist_group_map=None):
    w = SCORING_POLICY["weights"]
    selected_artist_nos = user_input.get("artist_nos", [])
    selected_group_nos = build_selected_group_nos(
        user_input.get("group_nos", []), selected_artist_nos, artist_group_map)
    am = artist_match(event.get("artist_no"), event.get("artist_group_no"),
                       selected_artist_nos, selected_group_nos)
    cf = category_fitness(event.get("ctg_no"), user_input.get("ctg_nos", []))
    pq = place_quality(event.get("total_score"), score_range[0], score_range[1])
    score = w["artist_match"] * am + w["category_fitness"] * cf + w["place_quality"] * pq
    return {"relevance": round(score, 4),
            "artist_match": am, "category_fitness": cf, "place_quality": pq}


# ---------------------------------------------------------------------------
# S1 — 여행 프레임 설정
# ---------------------------------------------------------------------------


def build_trip_frame(user_input):
    # S1: 여행 프레임. concert.event_no는 DB event_no.
    start = datetime.strptime(user_input["trip_start"], "%Y-%m-%d")
    end = datetime.strptime(user_input["trip_end"], "%Y-%m-%d")
    n_days = (end - start).days + 1
    date_list = [(start + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(n_days)]

    concert = user_input.get("concert")
    concert_day_idx = None
    concert_event_no = None
    concert_start_min = None
    concert_duration = ROUTE_POLICY["CONCERT_DURATION_MIN"]

    if concert:
        if concert.get("event_date") in date_list:
            concert_day_idx = date_list.index(concert["event_date"])
        concert_event_no = concert.get("event_no")
        concert_start_min = _to_min(concert.get("start_time", "19:00"))
        concert_duration = concert.get("duration_min", ROUTE_POLICY["CONCERT_DURATION_MIN"])

    lodging = user_input.get("lodging", {})

    # ── 출발핀/도착핀 (핀으로 찍는 지점) ────────────────────────────
    # start_pin: Day 1 출발 지점 (예: 공항). 없으면 숙소에서 출발.
    # end_pin:   마지막 날 완료 지점 (예: 공항). 없으면 숙소로 복귀.
    start_pin = user_input.get("start_pin")  # {"latitude":.., "longitude":..} 또는 None
    end_pin = user_input.get("end_pin")

    # ── 다중 숙소(depot) — 2026-09-10 신규 ──────────────────────────
    # user_input["accoms"] = [{"accom_no":.., "lat":.., "lon":.., "check_in_dt":.., "check_out_dt":..}, ...]
    # (check_in_dt/check_out_dt는 date_list와 비교하는 "YYYY-MM-DD" 문자열).
    # 안 주어지면(al02_selftest.py 등 하위호환) lodging 하나만 accom_no=None으로 감싸서
    # 단일 depot이던 기존 동작과 완전히 동일하게 만든다 — day_depots()는 항상 accoms를 본다.
    accoms = user_input.get("accoms") or [{
        "accom_no": None, "lat": lodging.get("latitude"), "lon": lodging.get("longitude"),
        "check_in_dt": None, "check_out_dt": None,
    }]

    # 2026-09-10 신규 — day_budget: 그날의 (시작, 종료) 활동 시간대(분)를 날짜 인덱스별로
    # 담는다. user_input["day_start_time"/"day_end_time"]은 auth.py가 trip.start_tm/
    # end_tm(사용자가 입력한, "트립 전체에 매일 반복 적용되는" 하루 활동 시간대 — §4.4
    # 영업시간 필터가 참조하는 것과 정확히 같은 값)에서 뽑아 넘긴다. d==0/d==n_days-1
    # 같은 날짜별 분기는 일부러 안 둔다 — 모든 날짜가 동일한 (s, e)를 쓴다(신규 설계안
    # 검토 중 나온 "첫날만 시작/마지막날만 종료" 해석은 팀 확인 결과 틀렸고, §4.4가 이미
    # 쓰고 있던 "매일 반복 적용" 해석이 맞는 것으로 확정됨). 지금은 모든 날짜 값이 같아서
    # 굳이 dict일 필요가 없지만, 나중에 날짜별로 다른 시간대를 받게 될 확장을 대비해
    # 구조는 미리 dict로 유지한다.
    user_start_min = _to_min_or_none(user_input.get("day_start_time"))
    user_end_min = _to_min_or_none(user_input.get("day_end_time"))
    day_start_min = user_start_min if user_start_min is not None else ROUTE_POLICY["DAY_START_MIN"]
    day_end_min = user_end_min if user_end_min is not None else ROUTE_POLICY["DAY_END_MIN"]
    day_budget = {d: (day_start_min, day_end_min) for d in range(n_days)}

    return {
        "n_days": n_days, "date_list": date_list,
        "concert_day_idx": concert_day_idx,
        "concert_event_no": concert_event_no,
        "concert_start_min": concert_start_min,
        "concert_duration": concert_duration,
        # day_start_min/day_end_min: 하위호환용 단일값(day_budget의 모든 항목과 동일한
        # 값에서 파생 — build_open_matrix() 등 스칼라 하나만 받는 기존 호출부가 그대로 씀).
        "day_start_min": day_start_min, "day_end_min": day_end_min,
        "day_budget": day_budget,
        # depot_lat/lon: 하위호환용 단일값(accoms[0] 기준) — augment_matrix의 haversine
        # 폴백이나 예전 호출부가 참고할 수 있게 남겨둠. 날짜별 depot 선택엔 안 쓰인다.
        "depot_lat": accoms[0].get("lat"), "depot_lon": accoms[0].get("lon"),
        "accoms": accoms,
        "start_pin": start_pin, "end_pin": end_pin,
    }


def accoms_overlap(a_check_in, a_check_out, b_check_in, b_check_out) -> bool:
    """숙소 A, B의 체크인~체크아웃 기간이 겹치는지 판정한다 (반열린 구간, [check_in, check_out)).

    date 객체든 "YYYY-MM-DD" 문자열이든 상관없이 동작한다(둘 다 비교 연산자만 있으면 됨 —
    문자열은 ISO 형식이라 사전식 비교 = 날짜 비교와 동일). auth.py의 POST /trips 겹침
    검증(쓰기 시점)과 pick_depot_accom()의 날짜별 숙소 소속 판정(계산 시점)이 이 함수
    하나를 공유해서 반드시 같은 규칙으로 움직이게 한다 — 둘이 따로 반열린/폐구간으로
    갈리면, 겹침 검증은 통과시킨 "체크아웃일=체크인일" 데이터를 recommend 계산이 뒤늦게
    겹침으로 오판해 DepotOverlapError를 던지는 불일치가 생긴다(2026-09-11 버그 리포트
    재현 중 실제로 발견).

    2026-09-11 수정: 기존엔 양끝 포함 폐구간(<=/<=)이라 "A 체크아웃일 = B 체크인일"인
    정상적인 숙소 전환 케이스까지 겹침으로 오탐지했음. 체크아웃일 당일은 이미 그 숙소를
    떠난 것으로 보는 반열린 구간(</<)으로 수정 — 경계가 정확히 맞닿는 경우는 겹침이
    아니고, 하루라도 실제로 공유하면 여전히 겹침이다."""
    return a_check_in < b_check_out and b_check_in < a_check_out


def pick_depot_accom(date_str, accoms):
    """그 날짜(date_str, "YYYY-MM-DD")에 depot으로 쓸 숙소를 accoms 중에서 고른다.

    유효 기준(2026-09-11, 반열린 구간으로 정정): check_in_dt <= date_str < check_out_dt
    (문자열 그대로 비교 — ISO 날짜 형식이라 사전식 비교 = 날짜 비교와 동일). 체크아웃
    당일은 그 숙소 소속이 아니라 다음 숙소(그날 체크인하는 쪽) 소속으로 본다 — auth.py
    accoms_overlap()의 반열린 판정과 반드시 같은 규칙이어야 한다. 그쪽만 반열린으로
    바꾸고 여기를 폐구간(<=/<=)으로 남겨두면, "A 체크아웃일=B 체크인일"처럼 겹침 검증은
    통과한 정상 데이터인데도 이 함수가 그 날짜에 A/B 둘 다 유효하다고 보고 아래
    DepotOverlapError를 던져 recommend가 깨진다(실제로 2026-09-11 버그 리포트 재현 중
    발견). ⚠ 이 규칙은 그날의 depot을 "체크인하는 쪽 숙소" 하나로 정하는 근사치다 —
    day_depots()는 하루에 depot을 1개만 쓰므로(시작/종료 공용), 전환일 아침엔 실제로는
    아직 전 숙소에 있었더라도 그날 전체를 새 숙소 기준으로 계산한다. 하루를 출발
    depot/도착 depot으로 분리하는 건 별도 설계 변경이 필요해 이번 수정 범위 밖.

    ⚠ 2026-09-10 정정 유지: 겹침(동시에 유효한 숙소가 2곳 이상)에 "체크인 늦은 숙소 우선"
    으로 조용히 하나를 골라 넘어가던 잠정 tie-break는 없다. 팀 확정 정책은 "같은 날짜에
    유효한 숙소는 항상 1곳이어야 한다 — 겹치는 기간 자체가 존재해서는 안 된다"이고,
    이 겹침은 auth.py의 POST /trips 쓰기 시점 검증이 막는 정상 케이스가 아닌 상태다.
    그래서 겹침이 실제로 감지되면(레거시 데이터 등) 하나를 임의로 고르지 않고
    DepotOverlapError를 던진다 — 호출부(auth.py)가 명확한 에러로 처리한다.

    공백(그 날짜에 유효한 숙소가 하나도 없음) 폴백: 체크아웃이 이미 끝난 숙소 중 가장
    최근 것(체크아웃 당일 포함, 반열린 구간으로 위 valid에서 빠졌으므로 여기서 받는다)
    -> 없으면 체크인이 아직 안 된 숙소 중 가장 이른 것 -> 그래도 없으면 accoms[0] 순으로
    폴백한다."""
    valid = [a for a in accoms
             if a.get("check_in_dt") and a.get("check_out_dt")
             and a["check_in_dt"] <= date_str < a["check_out_dt"]]
    if len(valid) > 1:
        accom_nos = [a.get("accom_no") for a in valid]
        logger.error(
            "depot 겹침 감지: date=%s에 유효한 숙소가 %d곳(accom_no=%s) — "
            "체크인~체크아웃 기간이 겹치는 레거시/잘못된 데이터로 추정, 데이터 확인 필요",
            date_str, len(valid), accom_nos,
        )
        raise DepotOverlapError(
            f"{date_str}에 유효한 숙소가 {len(valid)}곳(accom_no={accom_nos})입니다 — "
            "숙소 예약 기간이 겹쳐 있어 동선을 계산할 수 없습니다."
        )
    if valid:
        return valid[0]

    already_out = [a for a in accoms if a.get("check_out_dt") and a["check_out_dt"] <= date_str]
    if already_out:
        return max(already_out, key=lambda a: a["check_out_dt"])
    not_yet_in = [a for a in accoms if a.get("check_in_dt") and a["check_in_dt"] > date_str]
    if not_yet_in:
        return min(not_yet_in, key=lambda a: a["check_in_dt"])
    return accoms[0] if accoms else None


# ---------------------------------------------------------------------------
# S4 — 하루 방문 순서 최적화 (완전탐색)
# ⚠ concert_idx는 matrix 인덱스(0~N-1). event_no가 아님.
# ---------------------------------------------------------------------------


def build_cache(events):
    names = [e["event_nm"] for e in events]
    stays = np.array([STAY_MAP.get(e.get("ctg_nm", "기타"), 40)
                       for e in events], dtype=np.int32)
    return names, stays


def haversine_min(lat1, lon1, lat2, lon2, speed_kmh=25.0):
    """두 좌표 간 이동시간(분) 근사. 실제로는 카카오맵 API/캐시(travel_time_cache)로 대체 예정.
    speed_kmh: 도심 평균 이동속도 가정(차량 25km/h)."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    dist_km = 2 * R * math.asin(math.sqrt(a))
    return int(round(dist_km / speed_kmh * 60))


def augment_matrix(matrix, events, extra_points, extra_travel_minutes=None):
    """POI 이동시간 행렬 끝에 임시 노드(숙소/출발핀/도착핀)를 추가한다.
    extra_points: [{"lat":.., "lon":.., "role":..}, ...] 순서대로 인덱스 N, N+1, ... 부여.
    반환: (확장행렬, [추가된 노드의 인덱스들])
    좌표가 없는(None) 지점은 이동시간 0(=제약 없음)으로 채운다.

    extra_travel_minutes: {role: {j: 분}} — 2026-09-09 이동시간 실 API 연동(al02_candidates.
    build_extra_travel_minutes())으로 미리 조회해둔 실제 이동시간. role별로 주어지면
    haversine 대신 이 값을 쓴다(j가 빠져 있으면, 즉 카카오 API가 그 쌍의 경로를 못 찾았으면
    해당 쌍만 haversine로 개별 폴백). None(기본값)이면 기존과 동일하게 전부 haversine.
    """
    N = matrix.shape[0]
    k = len(extra_points)
    if k == 0:
        return matrix, []
    new_size = N + k
    aug = np.zeros((new_size, new_size), dtype=matrix.dtype)
    aug[:N, :N] = matrix

    # 각 POI의 좌표
    poi_ll = [(e.get("event_lat"), e.get("event_lon")) for e in events]

    etm = extra_travel_minutes or {}
    added_idx = list(range(N, N + k))
    for offset, pt in enumerate(extra_points):
        gi = N + offset
        plat, plon = pt.get("lat"), pt.get("lon")
        provided = etm.get(pt.get("role"))
        for j in range(N):
            jlat, jlon = poi_ll[j]
            if provided is not None and j in provided:
                t = provided[j]
            elif plat is None or plon is None or jlat is None or jlon is None:
                t = 0  # 좌표 없으면 이동시간 0 (depot 미지정과 동일 취급)
            else:
                t = haversine_min(plat, plon, jlat, jlon)
            aug[gi, j] = t
            aug[j, gi] = t
    # 추가 노드끼리의 거리도 채움(같은 날 start=end일 때 등)
    for a in range(k):
        for b in range(k):
            if a == b:
                continue
            ga, gb = N + a, N + b
            la, lo = extra_points[a].get("lat"), extra_points[a].get("lon")
            lb, ob = extra_points[b].get("lat"), extra_points[b].get("lon")
            if None in (la, lo, lb, ob):
                aug[ga, gb] = 0
            else:
                aug[ga, gb] = haversine_min(la, lo, lb, ob)
    return aug, added_idx


def s4_solve(day_indices, start_idx, end_idx, matrix, names, stays,
             concert_idx=None, concert_start=None,
             concert_duration=None, concert_buffer=None,
             day_start=None, day_end=None, max_places=None):
    """S4: 완전탐색. 공연은 마지막 고정.
    start_idx: 그날의 출발 depot(matrix 인덱스). None이면 출발 이동 0.
    end_idx:   그날의 도착 depot(matrix 인덱스). None이면 복귀 이동 0.
    concert_idx는 matrix 인덱스 (0~N-1)."""
    buf = concert_buffer if concert_buffer is not None else ROUTE_POLICY["CONCERT_BUFFER_MIN"]
    ds = day_start if day_start is not None else ROUTE_POLICY["DAY_START_MIN"]
    de = day_end if day_end is not None else ROUTE_POLICY["DAY_END_MIN"]
    # 하위호환 기본값: max_places를 안 받으면 B 프로파일(균형, 기존 고정 MAX_PER_DAY_NORMAL=5와
    # 같은 값) 상한을 쓴다 — al02_selftest.py 등 프로파일을 아직 안 넘기는 호출 대비.
    mp = max_places if max_places is not None else VISIT_COUNT_PROFILES["B"]["max"]
    cd = concert_duration if concert_duration is not None else ROUTE_POLICY["CONCERT_DURATION_MIN"]

    non_concert = [int(i) for i in day_indices if int(i) != concert_idx]
    if len(non_concert) > mp:
        non_concert = non_concert[:mp]

    deadline = None
    if concert_idx is not None:
        deadline = (concert_start - buf) if concert_start is not None else (de - buf)

    stay_arr = stays.copy()
    if concert_idx is not None:
        stay_arr[concert_idx] = cd

    best_order, best_cost, best_sched = None, float("inf"), []
    for perm in itertools.permutations(non_concert):
        order = list(perm)
        if concert_idx is not None:
            order.append(concert_idx)

        # 출발/도착 depot을 앞뒤에 붙여 이동시간 계산
        # start_idx/end_idx가 None이면 해당 구간 이동 0
        seq = order[:]
        head = [start_idx] if start_idx is not None else []
        tail = [end_idx] if end_idx is not None else []
        route = head + seq + tail
        if len(route) >= 2:
            route_arr = np.array(route, dtype=np.int32)
            leg_times = matrix[route_arr[:-1], route_arr[1:]]
        else:
            leg_times = np.zeros(0, dtype=np.int64)

        # 방문지별 도착 이동시간: head가 있으면 leg_times[0]이 start→첫방문
        # 없으면 첫 방문의 이동시간 0
        ct = ds
        tt = 0
        sched = []
        ok = True
        # 각 방문지 k에 대한 진입 이동시간 인덱스 계산
        for k, idx in enumerate(order):
            if head:
                tr = int(leg_times[k])  # start→order[0], order[0]→order[1] ...
            else:
                tr = int(leg_times[k - 1]) if k > 0 else 0
            ar = ct + tr
            st = int(stay_arr[idx])
            dp = ar + st
            if concert_idx is not None and idx == concert_idx:
                if ar > deadline:
                    ok = False
                    break
            if dp > de:
                ok = False
                break
            sched.append({"idx": idx, "name": names[idx], "travel": tr,
                          "arrive": _to_str(ar), "depart": _to_str(dp), "stay": st})
            tt += tr
            ct = dp
        if not ok:
            continue
        # 마지막 방문 → 도착 depot 이동시간 추가
        if tail and len(order) > 0:
            tt += int(leg_times[-1])
        if tt < best_cost:
            best_cost = tt
            best_order = order
            best_sched = sched

    return {"order": best_order, "cost": int(best_cost) if best_order else 99999,
            "schedule": best_sched}


def s4_solve_fallback(day_indices, start_idx, end_idx, matrix, names, stays,
                       score_map, concert_idx=None, concert_start=None,
                       concert_duration=None, day_start=None, day_end=None,
                       min_places=None):
    """S4 완전탐색이 시간 초과(99999)로 실패하면,
    relevance 최저 장소부터 1개씩 제거하며 유효한 동선이 나올 때까지 재시도.
    공연(concert_idx)은 절대 제거하지 않는다(하드 제약).

    min_places: 그 날의 방문 개수 하한 — "총 개수" 기준(공연이 있는 날이면 al02_policy.
    CONCERT_VISIT_COUNT_PROFILES의 공연 포함 총합, 없는 날이면 al02_policy.
    VISIT_COUNT_PROFILES의 방문 개수, 둘 다 호출부가 미리 뽑아 넘긴다). "정상 범위"의
    기준선일 뿐 강제 하한은 아니다. 시간 예산이 min_places에서도 안 맞으면 시간 제약이
    개수 범위보다 우선이라 계속 더 뺀다(요청 사양 — "min 아래로 추가 제거 허용"). 그래서
    이 파라미터가 있어도 제거 루프 자체는 안 멈춘다 — 대신 반환값의 under_min으로
    "정상 범위 하한 아래까지 빠졌는지"를 호출부(al02_pipeline.AL02Pipeline.run())가 알
    수 있게 표시만 해준다.
    반환: (result, dropped_list, under_min) — dropped_list는 제외된 장소 인덱스,
    under_min은 최종 개수(공연 포함, 있다면)가 min_places 미만인 극단적 케이스였는지."""
    places = [int(p) for p in day_indices]
    dropped = []
    while places:
        result = s4_solve(places, start_idx, end_idx, matrix, names, stays,
                           concert_idx=concert_idx, concert_start=concert_start,
                           concert_duration=concert_duration,
                           day_start=day_start, day_end=day_end,
                           max_places=len(places))
        if result["order"] is not None and result["cost"] < 99999:
            under_min = min_places is not None and len(places) < min_places
            return result, dropped, under_min
        # 실패 → 공연 제외한 것 중 relevance 최저 제거
        removable = [p for p in places if p != concert_idx]
        if not removable:
            break  # 공연만 남았는데도 실패 → 포기
        worst = min(removable, key=lambda p: score_map.get(p, 0.0))
        places.remove(worst)
        dropped.append(worst)
    return {"order": None, "cost": 99999, "schedule": []}, dropped, (min_places is not None)


# ---------------------------------------------------------------------------
# S3 — 날짜별 장소 배정 (그리디 + 로컬서치)
# ⚠ 안전 체크는 반드시 내부 루프 안에!
# ---------------------------------------------------------------------------


def day_depots(d, n_days, frame):
    """그날의 (start_idx, end_idx)를 반환.
    depot 인덱스는 augment_matrix로 matrix 끝에 추가된 노드 인덱스를 사용:
      frame["depot_idx_by_accom_no"] = {accom_no: 그 숙소의 matrix 인덱스} (다중 숙소, 2026-09-10)
      frame["start_pin_idx"]  = Day1 출발핀 (없으면 None)
      frame["end_pin_idx"]    = 마지막날 도착핀 (없으면 None)
    규칙(§4.2 우선순위 그대로):
      1순위(불변, 이 함수 밖 s4_solve에서 처리): 공연 있는 날은 공연이 항상 마지막 방문.
      2순위: Day 0(첫날) start = 출발핀(있으면) / 마지막 날 end = 도착핀(있으면).
      기본값: 그 외 모든 지점(중간 날 전체 + 핀이 없는 첫/마지막 날)은 그날 날짜에
        체크인~체크아웃이 유효한 숙소(pick_depot_accom, frame["accoms"] 기준)를 depot으로
        사용 — 숙소가 1곳뿐이면 항상 그 1곳이라 기존 동작과 동일.
    """
    date_str = frame["date_list"][d]
    depot_accom = pick_depot_accom(date_str, frame.get("accoms") or [])
    depot = frame.get("depot_idx_by_accom_no", {}).get(depot_accom.get("accom_no") if depot_accom else None)
    spin = frame.get("start_pin_idx")
    epin = frame.get("end_pin_idx")

    if d == 0:
        start = spin if spin is not None else depot
    else:
        start = depot
    if d == n_days - 1:
        end = epin if epin is not None else depot
    else:
        end = depot

    # ── 날짜별 출발지 override (기본 숙소, 예외적으로 유저가 변경) ──────
    # frame["day_start_override"] = {날짜인덱스: matrix_인덱스}
    # UI: SC-02c 동선 수정 화면에서 "이 날 출발지 변경" 시 설정됨.
    # 첫날 출발핀/마지막날 도착핀과 별개로, 중간날 출발지를 바꾸는 용도.
    overrides = frame.get("day_start_override", {})
    if d in overrides and overrides[d] is not None:
        start = overrides[d]
    return start, end


def _day_cost(day_places, start_idx, end_idx, concert_idx, matrix, names, stays,
              concert_start, concert_duration, day_start=None, day_end=None):
    if not day_places:
        return 0
    # max_places=len(day_places)(2026-09-10 수정) — 안 넘기면 s4_solve가 기본값
    # VISIT_COUNT_PROFILES["B"]["max"](5)로 non_concert를 자른다. C 프로파일(최대 7)처럼
    # 5보다 큰 day_places가 들어오면 실제로는 이미 S3가 정상 배정한 항목인데도 비용
    # 계산 단계에서 조용히 2개가 잘려나가 S3 greedy/local search의 gain 계산이 틀어지는
    # 버그였다(최종 s4_solve_fallback 호출은 별도로 max_places=len(places)를 항상
    # 명시해서 최종 스케줄 자체는 안 잘렸지만, 그 전 단계 비용 추정이 부정확했음) —
    # _day_cost는 순수 비용 비교용이라 여기서 항목을 자를 이유가 없어 실제 길이 그대로 넘긴다.
    # day_start/day_end(2026-09-10 신규, day_budget[d]) — 안 넘기면 s4_solve가 기본값
    # ROUTE_POLICY["DAY_START_MIN"/"DAY_END_MIN"](09:00~21:00)을 쓰는데, 이전엔 여기서
    # 아예 안 넘겨서 사용자가 trip.start_tm/end_tm으로 다른 활동시간대를 넣어도 S3의
    # 중간 비용 추정(gain 계산)에는 전혀 반영이 안 되는 별건 버그였다(최종
    # s4_solve_fallback 호출엔 이미 반영돼 있어서 최종 스케줄 자체는 맞았지만, S3가
    # "어느 날에 넣을지" 판단하는 신호는 부정확했음) — day_budget[d]를 그대로 전달해 고친다.
    r = s4_solve(day_places, start_idx, end_idx, matrix, names, stays,
                 concert_idx=concert_idx, concert_start=concert_start,
                 day_start=day_start, day_end=day_end,
                 concert_duration=concert_duration, max_places=len(day_places))
    return r["cost"]


# 2026-09-10 신규 — S3 영업시간 필터. event_op_hour.op_dt는 "월"~"일" 한글 요일 문자열
# (실 DB 확인 완료), Python datetime.weekday()는 0=월요일이라 이 순서로 맞춘다.
_KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _weekday_kr(date_str):
    return _KOREAN_WEEKDAYS[datetime.strptime(date_str, "%Y-%m-%d").weekday()]


def build_open_matrix(events, date_list, business_hours_by_event, day_start_min, day_end_min):
    """이벤트 x 날짜 "그 날짜에 배정 가능한가" 불리언 행렬(S3 전용, 2026-09-10 신규).

    business_hours_by_event: {event_no: {"월":(open_tm,close_tm), ...}} — al02_candidates.
    fetch_business_hours()로 미리 조회해서 넘긴다(al02_pipeline은 DB를 직접 안 건드리는
    기존 구조 유지). open_tm/close_tm은 event_op_hour 실제 컬럼 타입인 "HH:MM" 문자열.

    판정 규칙(2026-09-10 정책 정정 — 아래 ⚠ 참고):
      - 그 날짜 요일에 매칭되는 event_op_hour "행 자체가 없으면" 영업시간 정보가 없는
        것으로 보고 필터를 적용하지 않는다(통과) — 데이터 미입력을 휴무로 오인하지 않는다.
      - 행은 있는데 open_tm/close_tm이 "둘 다" None이면 그때만 진짜 휴무로 간주 → 배정 불가.
      - 행이 있고 open_tm/close_tm 중 하나만 None인(비정상 데이터) 경우도, 겹침을 판단할
        유효한 구간이 없으므로 판단을 보류하고 통과시킨다(휴무 처리하지 않음) — "데이터가
        불완전하면 필터를 안 건다"는 같은 원칙의 연장.
      - 영업시간이 둘 다 있으면 [open_tm, close_tm]과 [day_start_min, day_end_min]
        (trip.start_tm~end_tm, 트립 전체에 적용되는 단일 하루 활동 시간대)이 조금이라도
        겹치는지만 확인한다 — 겹침 체크는 실제 영업시간 데이터가 둘 다 있을 때만 수행한다.
        보수적으로 배제하지 않는다 — 교집합 길이가 0보다 크면 무조건 통과.

    ⚠ 2026-09-10 정정: 최초 구현은 "행 없음"과 "둘 다 None"을 구분하지 않고 똑같이
    휴무 취급했다. event_op_hour 커버리지를 실측해보니 "성지 음식점"/"성지 디저트·카페"/
    "기타 성지" 카테고리가 전부 0%라(반대로 "쇼핑"/"여행지"는 100%), 그 정책 그대로는
    성지류 후보가 전멸하는 부작용이 실제 트립(trip_no=38)에서 확인됨 — 팀이 정책을
    "행 없음=필터 미적용(통과), 행 있고 둘 다 NULL인 경우만 휴무"로 확정해 이 함수를
    그에 맞게 고쳤다. 트립 상세 화면(auth.py list_trip_routes의 business_hours 필드,
    has_data:false="데이터 미제공"과 is_closed:true="진짜 휴무"를 구분해서 내려주는 것)과
    이제 같은 방향의 구분이 됐다.

    business_hours_by_event가 None이면(al02_selftest.py 등 이 기능을 아직 안 쓰는 하위
    호환 호출) 필터를 아예 적용하지 않는다(전부 True) — 데이터가 있는데 특정 이벤트만
    조회 결과가 없는 것(빈 dict의 일부)과 결과적으로 같은 값(True)이지만 의미는 다르다."""
    n = len(events)
    n_days = len(date_list)
    open_matrix = np.ones((n, n_days), dtype=bool)
    if business_hours_by_event is None:
        return open_matrix

    for d, date_str in enumerate(date_list):
        weekday_kr = _weekday_kr(date_str)
        for i, ev in enumerate(events):
            day_hours = business_hours_by_event.get(ev.get("event_no")) or {}
            if weekday_kr not in day_hours:
                # 그 요일 행 자체가 없음 — 데이터 미입력, 필터 미적용(통과).
                open_matrix[i, d] = True
                continue
            open_tm, close_tm = day_hours[weekday_kr]
            if open_tm is None and close_tm is None:
                # 행은 있는데 둘 다 NULL — 진짜 휴무.
                open_matrix[i, d] = False
                continue
            if open_tm is None or close_tm is None:
                # 한쪽만 NULL(비정상 데이터) — 겹침 판단 불가, 통과.
                open_matrix[i, d] = True
                continue
            # 2026-09-10 재정정 — close_tm="00:00"(자정 마감) 보정: "00:00"을 문자 그대로
            # 분으로 바꾸면 0이 되어 open_tm(예: "10:30")보다 이르다고 오판, 실제로는 심야
            # 까지 영업 중인 곳(예: 올리브영 두타점, "10:30~00:00")이 매일 휴무로 잘못
            # 판정되던 별건 버그. open_tm이 있고 close_tm이 정확히 "00:00"일 때만 "23:59"
            # (그날 안에서 표현 가능한 가장 늦은 시각)로 바꿔서 겹침 비교에 쓴다 — 3단계
            # 판정 로직(행 없음/둘 다 NULL/한쪽만 NULL) 자체는 그대로, 이 보정은 "둘 다
            # 값 있음" 케이스의 겹침 비교 직전에만 끼워 넣는다. open_tm="00:00"(자정 오픈)
            # 등 다른 자정 케이스는 이번 범위 밖 — 손대지 않음.
            close_tm_effective = "23:59" if close_tm == "00:00" else close_tm
            open_min, close_min = _to_min(open_tm), _to_min(close_tm_effective)
            open_matrix[i, d] = max(open_min, day_start_min) < min(close_min, day_end_min)
    return open_matrix


def s3_greedy(candidates, frame, matrix, names, stays, concert_matrix_idx, W_rel=30.0,
              open_matrix=None, visit_max=None, concert_visit_max=None,
              events=None, caps=None, selected_ctg_nos=None, axes=ALL_DIVERSITY_AXES):
    """events/caps(2026-09-11, AL02_diversity_constraint_handover.md 5.6): events는
    matrix idx로 그대로 인덱싱 가능한 이벤트 dict 리스트(run()이 넘기는 것과 동일),
    caps는 al02_diversity.DiversityCaps — 둘 다 None이면(하위호환 호출, al02_selftest.py
    등) 다양성 제약을 아예 적용하지 않고 기존 동작 그대로 순수 relevance 그리디로 돈다.

    axes(2026-09-12 신규): 실제로 검사할 축만 좁힐 수 있다 — run()이 enable_hard_dedup만
    켜진 경우("동일 이벤트/브랜드/쇼핑 전체-여행 1회"만 항상 유지, 카테고리 Tier 시스템은
    끔) ("duplicate_event","brand_trip_cap","shopping_trip_cap")만 넘긴다. 기본값은
    다섯 축 전부(al02_diversity.ALL_DIVERSITY_AXES) — 기존 동작과 동일."""
    score_map = {int(i): float(s) for i, s in candidates}
    n_days = frame["n_days"]
    concert_day = frame["concert_day_idx"]
    selected_ctg_nos = selected_ctg_nos or []
    concert_idx_int = int(concert_matrix_idx) if concert_matrix_idx is not None else None
    diversity_on = caps is not None and events is not None

    day_plans = {d: [] for d in range(n_days)}
    if concert_day is not None and concert_matrix_idx is not None:
        day_plans[concert_day].append(int(concert_matrix_idx))
    assigned = set()
    if concert_matrix_idx is not None:
        assigned.add(int(concert_matrix_idx))

    # 방문 개수 상한(2026-09-10, 트립 밀도 프로파일 A/B/C별) — 안 넘겨받으면 B로 하위호환
    # (비공연일 5는 기존 MAX_PER_DAY_NORMAL과, 공연일 4는 기존 MAX_PER_DAY_CONCERT와 동일한 값).
    normal_limit = visit_max if visit_max is not None else VISIT_COUNT_PROFILES["B"]["max"]
    concert_limit = (concert_visit_max if concert_visit_max is not None
                     else CONCERT_VISIT_COUNT_PROFILES["B"]["max"])

    for idx, score in sorted(candidates, key=lambda x: x[1], reverse=True):
        idx = int(idx)
        if idx in assigned:
            continue

        # 다양성 선택 점수(2026-09-11, 문서 5.7) — "지금까지 배정된 전체"를 S로 보고
        # coverage_bonus - redundancy_penalty를 relevance에 더한다. 어느 날에 넣을지와
        # 무관한 값이라(day별로 안 바뀜) 후보당 한 번만 계산한다. route_fit(이동시간)은
        # 아래 gain 계산의 (after-before)가 그대로 담당 — 여기서 중복 계산 안 함.
        if diversity_on:
            exclude = {concert_idx_int} if concert_idx_int is not None else None
            trip_state = build_trip_diversity_state(day_plans.values(), events, exclude_idx=exclude)
            selected_events = [
                events[i] for v in day_plans.values() for i in v if i != concert_idx_int
            ]
            sel_score = calculate_selection_score(
                events[idx], score_map.get(idx, 0.0), selected_events, selected_ctg_nos, trip_state,
            )
        else:
            trip_state = None
            sel_score = score_map.get(idx, 0.0)

        best_day, best_gain = None, float("inf")
        for d in range(n_days):
            is_c = (d == concert_day)
            limit = concert_limit if is_c else normal_limit
            if len(day_plans[d]) >= limit:
                continue
            # 영업시간 필터(2026-09-10) — 콘서트(concert_matrix_idx)는 이 루프 자체에 안
            # 들어오므로(위에서 assigned 처리) 별도 예외 처리 불필요.
            if open_matrix is not None and not open_matrix[idx, d]:
                continue
            # 다양성 하드 제약(2026-09-11, 문서 5.5) — 1차 배정은 항상 완화 없이(cap=1)
            # 검사한다. 2차(빽빽한 비공연일 한정) 완화는 s3_fill_with_relaxation()에서
            # 별도로 처리한다.
            if diversity_on:
                day_state = build_day_diversity_state(day_plans[d], events)
                ok, _reason = can_insert_candidate(
                    events[idx], day_state, trip_state, caps,
                    is_concert_day=is_c, allow_day_category_relaxation=False, axes=axes,
                )
                if not ok:
                    continue
            c_idx = int(concert_matrix_idx) if is_c else None
            c_start = frame["concert_start_min"] if is_c else None
            c_dur = frame["concert_duration"] if is_c else None
            s_idx, e_idx = day_depots(d, n_days, frame)
            d_start, d_end = frame["day_budget"][d]
            before = _day_cost(day_plans[d], s_idx, e_idx, c_idx, matrix, names, stays, c_start, c_dur,
                                day_start=d_start, day_end=d_end)
            after = _day_cost(day_plans[d] + [idx], s_idx, e_idx, c_idx, matrix, names, stays, c_start, c_dur,
                               day_start=d_start, day_end=d_end)
            if after >= 99999:
                continue
            # W_rel: 이동시간 증가 - (다양성 반영) 선택 점수 (클수록 취향/커버리지 우선)
            gain = (after - before) - W_rel * sel_score
            if gain < best_gain:
                best_gain = gain
                best_day = d
        if best_day is not None:
            day_plans[best_day].append(idx)
            assigned.add(idx)
    return day_plans


def s3_local_search(day_plans, frame, matrix, names, stays, concert_matrix_idx, candidates=None,
                     W_rel=30.0, max_iter=10, open_matrix=None, visit_max=None,
                     concert_visit_max=None, events=None, caps=None, axes=ALL_DIVERSITY_AXES):
    score_map = {int(i): float(s) for i, s in (candidates or [])}
    n_days = frame["n_days"]
    concert_day = frame["concert_day_idx"]
    plans = {d: [int(p) for p in v] for d, v in day_plans.items()}
    concert_idx_int = int(concert_matrix_idx) if concert_matrix_idx is not None else None
    diversity_on = caps is not None and events is not None
    # 방문 개수 상한(2026-09-10) — s3_greedy와 동일한 프로파일 값/하위호환 기본값.
    normal_limit = visit_max if visit_max is not None else VISIT_COUNT_PROFILES["B"]["max"]
    concert_limit = (concert_visit_max if concert_visit_max is not None
                     else CONCERT_VISIT_COUNT_PROFILES["B"]["max"])

    def diversity_ok_for_move(moving_idx, dest_day, extra_exclude_from_dest=None):
        """poi(moving_idx)를 dest_day로 옮겨도 다양성 제약을 지키는지(2026-09-11).
        같은 트립 안에서 날짜만 옮기는 것이므로 트립 전체 카운트는 원래 안 바뀐다 —
        moving_idx 자신을 트립 상태에서 뺀 채로(마치 아직 안 넣은 것처럼) 다시
        can_insert_candidate()로 검사하면, 트립 전체 축은 항상 통과하고(제외 전에
        이미 상한 이내였으므로) 목적지 날짜의 category_day_cap만 실질적으로 갈린다 —
        s3_greedy와 같은 함수를 그대로 재사용해 로직이 갈라지지 않게 한다.

        extra_exclude_from_dest: Swap 전용 — 목적지 날짜에서 "같이 빠져나가는" 상대편
        아이템(예: pa<->pb 스왑에서 pb)을 목적지 날짜 카운트에서도 빼야 한다. 안 빼면
        "pb가 나가고 pa가 들어오는" 정상적인 스왑을, pb가 아직 그 자리에 있는 것처럼
        보고 category_day_cap을 잘못 초과 판정할 수 있다."""
        if not diversity_on:
            return True
        exclude = {moving_idx}
        if concert_idx_int is not None:
            exclude.add(concert_idx_int)
        trip_state = build_trip_diversity_state(plans.values(), events, exclude_idx=exclude)
        day_exclude = set(exclude) | (extra_exclude_from_dest or set())
        dest_day_state = build_day_diversity_state(plans[dest_day], events, exclude_idx=day_exclude)
        ok, _reason = can_insert_candidate(
            events[moving_idx], dest_day_state, trip_state, caps,
            is_concert_day=(dest_day == concert_day), allow_day_category_relaxation=False, axes=axes,
        )
        return ok

    def day_cost(d):
        c_idx = int(concert_matrix_idx) if d == concert_day else None
        c_start = frame["concert_start_min"] if d == concert_day else None
        c_dur = frame["concert_duration"] if d == concert_day else None
        s_idx, e_idx = day_depots(d, n_days, frame)
        d_start, d_end = frame["day_budget"][d]
        return _day_cost(plans[d], s_idx, e_idx, c_idx, matrix, names, stays, c_start, c_dur,
                          day_start=d_start, day_end=d_end)

    cost_cache = {d: day_cost(d) for d in range(n_days)}
    rel_cache = {d: sum(score_map.get(p, 0.0) for p in plans[d]) for d in range(n_days)}

    def comp(cost, rel):
        return cost - W_rel * rel

    cur = sum(comp(cost_cache[d], rel_cache[d]) for d in range(n_days))
    improved, it = True, 0
    while improved and it < max_iter:
        improved = False
        it += 1

        # Shift
        for da in range(n_days):
            for poi in list(plans[da]):
                if poi == concert_matrix_idx:
                    continue
                for db in range(n_days):
                    if da == db:
                        continue
                    if poi not in plans[da]:
                        continue
                    is_c = (db == concert_day)
                    lim = concert_limit if is_c else normal_limit
                    if len(plans[db]) >= lim:
                        continue
                    # 영업시간 필터(2026-09-10) — poi를 옮기려는 날(db)에 영업 안 하면 이동 금지.
                    if open_matrix is not None and not open_matrix[poi, db]:
                        continue
                    # 다양성 제약(2026-09-11) — 옮겨갈 날(db)에 이미 같은 카테고리가
                    # cap만큼 있으면 이동 금지(da는 poi를 빼는 방향이라 위반이 생길 수 없음).
                    if not diversity_ok_for_move(poi, db):
                        continue
                    plans[da].remove(poi)
                    plans[db].append(poi)
                    nra = rel_cache[da] - score_map.get(poi, 0.0)
                    nrb = rel_cache[db] + score_map.get(poi, 0.0)
                    na, nb = day_cost(da), day_cost(db)
                    nc = (cur - comp(cost_cache[da], rel_cache[da]) - comp(cost_cache[db], rel_cache[db])
                          + comp(na, nra) + comp(nb, nrb))
                    if nc < cur:
                        cur = nc
                        cost_cache[da], cost_cache[db] = na, nb
                        rel_cache[da], rel_cache[db] = nra, nrb
                        improved = True
                    else:
                        plans[db].remove(poi)
                        plans[da].append(poi)

        # Swap
        for da in range(n_days):
            for db in range(da + 1, n_days):
                for pa in list(plans[da]):
                    if pa == concert_matrix_idx:
                        continue
                    for pb in list(plans[db]):
                        if pb == concert_matrix_idx:
                            continue
                        if pa not in plans[da]:
                            continue
                        if pb not in plans[db]:
                            continue
                        # 영업시간 필터(2026-09-10) — pa는 db로, pb는 da로 옮겨가므로 각각
                        # 그 목적지 날짜에 영업하는지 확인.
                        if open_matrix is not None and (
                            not open_matrix[pa, db] or not open_matrix[pb, da]
                        ):
                            continue
                        # 다양성 제약(2026-09-11) — pa가 들어갈 db는 pb가 빠진 상태 기준,
                        # pb가 들어갈 da는 pa가 빠진 상태 기준으로 확인(extra_exclude_from_dest).
                        if (not diversity_ok_for_move(pa, db, extra_exclude_from_dest={pb})
                                or not diversity_ok_for_move(pb, da, extra_exclude_from_dest={pa})):
                            continue
                        plans[da].remove(pa)
                        plans[db].remove(pb)
                        plans[da].append(pb)
                        plans[db].append(pa)
                        nra = rel_cache[da] - score_map.get(pa, 0.0) + score_map.get(pb, 0.0)
                        nrb = rel_cache[db] - score_map.get(pb, 0.0) + score_map.get(pa, 0.0)
                        na, nb = day_cost(da), day_cost(db)
                        nc = (cur - comp(cost_cache[da], rel_cache[da]) - comp(cost_cache[db], rel_cache[db])
                              + comp(na, nra) + comp(nb, nrb))
                        if nc < cur:
                            cur = nc
                            cost_cache[da], cost_cache[db] = na, nb
                            rel_cache[da], rel_cache[db] = nra, nrb
                            improved = True
                        else:
                            plans[da].remove(pb)
                            plans[db].remove(pa)
                            plans[da].append(pa)
                            plans[db].append(pb)
    total_travel = sum(cost_cache.values())
    return plans, total_travel


def s3_fill_with_tier2(day_plans, remaining_candidates, frame, matrix, names, stays,
                        concert_matrix_idx, events, caps, target_visits_by_day, open_matrix=None):
    """2026-09-12 신규 — "2차 배정: Tier2 개방"(al02_policy.PREFERENCE_TIER_2 참고).

    1차(Tier1만, s3_greedy+s3_local_search, 완화 없음) 이후, 아직 target_visits_by_day에
    못 미친 날짜에 한해 Tier1 잔여 후보 + Tier2 후보를 relevance 내림차순으로 더 채운다.
    카테고리 하루 상한은 여기서도 1로 그대로 유지한다(allow_day_category_relaxation=False
    항상) — "완화"가 아니라 "그 날 아직 안 쓰인 다른 카테고리를 새로 연다"는 목적이라,
    Tier1이 이미 채운 카테고리와는 자연히 안 겹친다(각 ctg_no당 하루 1곳이라는 하드
    제약은 그대로 지키면서 서로 다른 카테고리를 옆에 채우는 것뿐). 브랜드/쇼핑/동일
    이벤트 전체-여행 1회 제약은 can_insert_candidate()가 항상 함께 검사하므로 여기서도
    그대로 유지된다 — 이 단계에서 별도로 완화하는 축은 없다.

    공연일도 대상이다(문서 5 "일반 POI 목표를 못 채우면" 요구사항 — 공연일이라고 Tier2를
    안 열면 balanced 프로파일에서 공연일이 공연 하나만 남는 사례가 실측으로 확인됐다,
    trip_no=43) — 다만 공연일의 목표치는 target_visits_by_day를 통해 호출부(run())가
    al02_policy.CONCERT_DAY_GENERAL_POI_TARGET(공연 제외 일반 POI 목표, 프로파일별
    1/2/3)로 이미 낮춰서 넘긴다. 완화(카테고리 하루 2곳 허용)는 공연일이든 아니든 이
    단계에서 하지 않는다 — 그건 s3_fill_with_relaxation(3차)만의 역할이다.

    반환: (day_plans, tier2_used_by_day) — tier2_used_by_day는 {day_index: [event_no,...]}
    로, 실제로 Tier2에서 채워진 항목만 담는다(진단/diversity 메타데이터용)."""
    concert_day = frame["concert_day_idx"]
    concert_idx_int = int(concert_matrix_idx) if concert_matrix_idx is not None else None
    n_days = frame["n_days"]
    tier2_used_by_day: dict[int, list] = {}
    if caps is None or events is None:
        return day_plans, tier2_used_by_day

    assigned = {i for v in day_plans.values() for i in v}
    remaining = sorted(
        ((int(i), s) for i, s in remaining_candidates if int(i) not in assigned),
        key=lambda x: x[1], reverse=True,
    )
    exclude = {concert_idx_int} if concert_idx_int is not None else None

    for d in range(n_days):
        target = target_visits_by_day.get(d)
        if target is None:
            continue
        is_c = (d == concert_day)
        for idx, score in remaining:
            if idx in assigned:
                continue
            if len(day_plans[d]) >= target:
                break
            if open_matrix is not None and not open_matrix[idx, d]:
                continue
            trip_state = build_trip_diversity_state(day_plans.values(), events, exclude_idx=exclude)
            day_state = build_day_diversity_state(day_plans[d], events)
            ok, _reason = can_insert_candidate(
                events[idx], day_state, trip_state, caps,
                is_concert_day=is_c, allow_day_category_relaxation=False, axes=CORE_AXES,
            )
            if not ok:
                continue
            c_idx = int(concert_matrix_idx) if is_c else None
            c_start = frame["concert_start_min"] if is_c else None
            c_dur = frame["concert_duration"] if is_c else None
            s_idx, e_idx = day_depots(d, n_days, frame)
            d_start, d_end = frame["day_budget"][d]
            before = _day_cost(day_plans[d], s_idx, e_idx, c_idx, matrix, names, stays, c_start, c_dur,
                                day_start=d_start, day_end=d_end)
            after = _day_cost(day_plans[d] + [idx], s_idx, e_idx, c_idx, matrix, names, stays, c_start, c_dur,
                               day_start=d_start, day_end=d_end)
            if after >= 99999:
                continue
            day_plans[d].append(idx)
            assigned.add(idx)
            if events[idx].get("preference_tier") == PREFERENCE_TIER_2:
                tier2_used_by_day.setdefault(d, []).append(events[idx].get("event_no"))

    return day_plans, tier2_used_by_day


def s3_fill_with_relaxation(day_plans, remaining_candidates, frame, matrix, names, stays,
                             concert_matrix_idx, events, caps, target_visits_by_day,
                             density_label, open_matrix=None):
    """2026-09-12 재정의(3차 배정으로 순번 변경 — Tier2 개방이 2차가 됨, s3_fill_with_tier2
    참고) — "제한적 완화". 1차(Tier1만) + 2차(Tier2 개방) 이후에만 호출한다. 아래 조건을
    모두 만족하는 날짜에서만, 남은(아직 배정 안 된, Tier1+Tier2 전부) 후보로 동일 ctg_no를
    caps.day_category_cap(기본 2)까지 허용해 채운다:
      - density_label == "dense"(문서 4.4/5.6/10 — balanced/relaxed는 대상 아님, al02_policy.
        DENSITY_LABEL_BY_WREL_KEY 주석 참고)
      - 공연일이 아님
      - 그 날 목표 방문 수(target_visits_by_day[d])가 7 이상
      - 1·2차 배정이 그 목표에 못 미침
      - 완화 없이 넣을 수 있는(다른 카테고리) 후보가 Tier1/Tier2 통틀어 하나도 안 남음
    쇼핑 전체 1곳/동일 브랜드 전체 1곳/동일 이벤트 전체 1회는 완화 플래그와 무관하게
    can_insert_candidate() 안에서 항상 그대로 검사된다(문서 5.6 "쇼핑·브랜드·동일 장소는
    3차에서도 절대 완화하지 않는다").

    이 단계 이후 s3_local_search를 다시 돌리지 않는다 — 재최적화 과정에서 방금 완화로
    허용한 항목이 비완화 규칙으로 재검사되어 다른 날로 밀려나거나 제거될 위험이 있어,
    "완화로 채운 자리는 그대로 확정"이 더 안전하다고 판단했다(완료 보고 참고).

    반환: (day_plans, relaxed_days) — relaxed_days는 실제로 완화를 적용해 최소 1곳을
    채운 날짜 인덱스 집합(조건은 만족했지만 채울 후보가 끝내 없었던 날은 제외)."""
    concert_day = frame["concert_day_idx"]
    concert_idx_int = int(concert_matrix_idx) if concert_matrix_idx is not None else None
    n_days = frame["n_days"]
    relaxed_days = set()
    if density_label != "dense" or caps is None or events is None:
        return day_plans, relaxed_days

    assigned = {i for v in day_plans.values() for i in v}
    remaining = sorted(
        ((int(i), s) for i, s in remaining_candidates if int(i) not in assigned),
        key=lambda x: x[1], reverse=True,
    )
    exclude = {concert_idx_int} if concert_idx_int is not None else None

    for d in range(n_days):
        if d == concert_day:
            continue
        target = target_visits_by_day.get(d)
        if target is None or target < 7:
            continue
        if len(day_plans[d]) >= target:
            continue

        trip_state = build_trip_diversity_state(day_plans.values(), events, exclude_idx=exclude)
        day_state = build_day_diversity_state(day_plans[d], events)
        has_non_relaxed_option = any(
            idx not in assigned
            and (open_matrix is None or open_matrix[idx, d])
            and can_insert_candidate(events[idx], day_state, trip_state, caps,
                                      is_concert_day=False, allow_day_category_relaxation=False,
                                      axes=CORE_AXES)[0]
            for idx, _ in remaining
        )
        if has_non_relaxed_option:
            continue  # 아직 비완화로 채울 수 있는 다른 카테고리 후보가 있음 — 완화 보류

        s_idx, e_idx = day_depots(d, n_days, frame)
        d_start, d_end = frame["day_budget"][d]
        for idx, score in remaining:
            if idx in assigned:
                continue
            if len(day_plans[d]) >= target:
                break
            if open_matrix is not None and not open_matrix[idx, d]:
                continue
            trip_state = build_trip_diversity_state(day_plans.values(), events, exclude_idx=exclude)
            day_state = build_day_diversity_state(day_plans[d], events)
            ok, _reason = can_insert_candidate(
                events[idx], day_state, trip_state, caps,
                is_concert_day=False, allow_day_category_relaxation=True, axes=CORE_AXES,
            )
            if not ok:
                continue
            before = _day_cost(day_plans[d], s_idx, e_idx, None, matrix, names, stays, None, None,
                                day_start=d_start, day_end=d_end)
            after = _day_cost(day_plans[d] + [idx], s_idx, e_idx, None, matrix, names, stays, None, None,
                               day_start=d_start, day_end=d_end)
            if after >= 99999:
                continue
            day_plans[d].append(idx)
            assigned.add(idx)
            relaxed_days.add(d)

    return day_plans, relaxed_days


# ---------------------------------------------------------------------------
# S5 — 검증
# ---------------------------------------------------------------------------


def verify(day_plans, frame, concert_matrix_idx, s4_results=None, visit_max=None,
           concert_visit_max=None, events=None, caps=None, relaxed_days=None):
    """events/caps/relaxed_days(2026-09-11, 문서 5.8 S5 최종 검증) — 셋 다 주어지면
    다양성 제약도 함께 검증한다. ⚠ 이 검증은 s3_greedy/s3_local_search/
    s3_fill_with_relaxation이 전부 can_insert_candidate()로 삽입 시점에 이미 막았어야
    하는 것들의 재확인(방어적 assertion)이다 — 통과한 항목만 day_plans에 들어가므로
    이 단계에서 새로 위반을 "고치는" 로직은 없다(수학적으로 실패할 수 없어야 정상 —
    실패하면 삽입 로직 자체의 버그를 의미하므로 조용히 넘기지 않고 그대로 checks에
    드러낸다)."""
    normal_limit = visit_max if visit_max is not None else VISIT_COUNT_PROFILES["B"]["max"]
    concert_limit = (concert_visit_max if concert_visit_max is not None
                     else CONCERT_VISIT_COUNT_PROFILES["B"]["max"])
    checks = {}
    all_pois = [p for v in day_plans.values() for p in v]
    checks["중복 없음"] = len(all_pois) == len(set(all_pois))
    for d, v in day_plans.items():
        is_c = (d == frame["concert_day_idx"])
        lim = concert_limit if is_c else normal_limit
        checks[f"Day {d+1} 상한 {lim}"] = len(v) <= lim
    if concert_matrix_idx is not None:
        cd = frame["concert_day_idx"]
        checks["공연 배정"] = int(concert_matrix_idx) in day_plans.get(cd, [])
        if s4_results and cd is not None and s4_results.get(cd):
            sched = s4_results[cd]
            if sched and sched["schedule"]:
                checks["공연 마지막"] = (sched["schedule"][-1]["idx"] == int(concert_matrix_idx))

    if caps is not None and events is not None:
        relaxed_days = relaxed_days or set()
        concert_idx_int = int(concert_matrix_idx) if concert_matrix_idx is not None else None
        exclude = {concert_idx_int} if concert_idx_int is not None else None
        trip_state = build_trip_diversity_state(day_plans.values(), events, exclude_idx=exclude)
        checks["다양성: 동일 브랜드 트립 상한"] = all(
            c <= caps.max_same_brand_per_trip for c in trip_state.trip_brand_counts.values()
        )
        checks["다양성: 쇼핑 트립 상한"] = trip_state.shopping_count <= caps.max_shopping_per_trip
        # 2026-09-12: category_trip_cap(선호 순위별 비율 상한)은 이번 작업 지시의
        # "항상 유지" 네 축(CORE_AXES)에 없어 더 이상 게이팅에 안 쓴다 — 검증에서도
        # 뺐다. Tier2가 같은 비선호 카테고리를 여러 날짜에 걸쳐 하루 1곳씩 채우는 건
        # 정상 동작이라(하루 상한만 지키면 됨), 예전 비율 상한 기준으로 검증하면
        # 허위 실패(validation_passed=False)가 난다.
        day_cat_ok = True
        for d, v in day_plans.items():
            day_state = build_day_diversity_state(v, events, exclude_idx=exclude)
            cap_for_day = caps.day_category_cap if d in relaxed_days else 1
            if any(c > cap_for_day for c in day_state.category_counts.values()):
                day_cat_ok = False
        checks["다양성: 카테고리 하루 상한"] = day_cat_ok

    return {"passed": all(checks.values()), "checks": checks}


# ---------------------------------------------------------------------------
# 통합 파이프라인
# ⚠ concert.event_no(DB ID) → concert_matrix_idx(배열 인덱스) 변환은 run() 내부에서 자동 수행.
# ---------------------------------------------------------------------------


class AL02Pipeline:
    # AL-02 전체 파이프라인.
    # user_input의 concert.event_no는 DB의 event_no.
    # depot(숙소)/출발핀/도착핀/날짜별 출발지 override는 augment_matrix로
    #   matrix 끝에 임시 노드로 추가된다.

    def __init__(self, score_range=(0.0, 100.0)):
        self.score_range = score_range
        self.policy = SCORING_POLICY
        # artist_group_map은 여기서 쓰지 않는다 — candidates 점수(relevance)는 run() 호출 전에
        # calc_relevance()로 이미 계산되어 들어오므로, 그룹 매핑은 S0/candidate 준비 단계의 책임이다.

    def run(self, candidates, matrix, events, user_input, W_rel=30.0, extra_travel_minutes=None,
            business_hours_by_event=None, visit_min=None, visit_max=None,
            concert_visit_min=None, concert_visit_max=None, enable_diversity=False,
            density_profile=None, enable_hard_dedup=False):
        """extra_travel_minutes: {role: {event_index: 분}} — 2026-09-09 이동시간 실 API
        연동. al02_candidates.build_extra_travel_minutes()로 미리 조회해서 넘긴다(role은
        아래 "depot"/"start_pin"/"end_pin"과 정확히 일치해야 함). None(기본값)이면 기존과
        동일하게 augment_matrix()가 haversine 근사를 쓴다(al02_selftest.py 등 DB 없이
        도는 합성 테스트는 이 경로를 그대로 탄다).

        business_hours_by_event: {event_no: {"월":(open_tm,close_tm), ...}} — 2026-09-10
        S3 영업시간 필터 신규. al02_candidates.fetch_business_hours()로 미리 조회해서
        넘긴다. None(기본값)이면 필터를 아예 적용하지 않는다(기존 동작 그대로 —
        al02_selftest.py 등 이 데이터가 없는 하위호환 호출용). build_open_matrix() 참고.

        visit_min/visit_max: 트립 밀도 프로파일(A/B/C, trip_density_no)의 비공연일 방문
        개수 하한/상한 — 호출부(auth.py)가 al02_policy.VISIT_COUNT_PROFILES[wrel_key]에서
        미리 뽑아 넘긴다(W_rel을 WREL_PROFILES에서 미리 뽑아 넘기는 것과 동일한 패턴).
        둘 다 None(기본값)이면 B 프로파일(4~5, 기존 고정 MAX_PER_DAY_NORMAL=5와 동일한
        상한)로 동작 — al02_selftest.py 등 프로파일 없이 부르는 하위호환 경로용.

        concert_visit_min/concert_visit_max: 2026-09-10 정정 — "공연일은 A/B/C 무관 고정
        4곳"이었던 이전 정책을 철회, 공연일도 al02_policy.CONCERT_VISIT_COUNT_PROFILES
        [wrel_key]에서 뽑은 프로파일별 범위(공연 포함 총 개수)를 쓴다. 공연 자체는
        여전히 항상 포함·마지막 방문 고정(변경 없음). 둘 다 None(기본값)이면 B(3~4,
        C의 4~4와 함께 기존 고정 MAX_PER_DAY_CONCERT=4와 동일한 상한)로 하위호환.

        enable_diversity/density_profile/enable_hard_dedup: 2026-09-11~12 신규 다양성 제약
        스위치 3단계(al02_diversity.py). 프론트 리포트(trip_no=43, balanced 프로파일에서
        1일차 1곳/2일차 공연만/3일차 0곳)를 실측 재현한 결과, trip_interest 실측(70개
        트립 중 66개가 카테고리 3개, 4개가 2개, 4개 이상 선택 0건)으로 "하루 동일 ctg_no
        최대 1곳" 하드 제약이 Tier1(선호 카테고리)만으로는 방문 목표를 구조적으로 못
        채우는 게 예외가 아니라 일반적인 상황임을 확인했다 — 그래서 카테고리 상한 시스템
        (Tier1→Tier2→3차 완화)과, 그와 무관하게 항상 유지해야 하는 하드 제약(동일 이벤트/
        브랜드/쇼핑 전체-여행 1회)을 분리했다:

        - enable_hard_dedup=True: 동일 event_no·브랜드(allow-list)·쇼핑 전체-여행 1회만
          적용(카테고리 하루/여행 상한은 전혀 안 봄). 올리브영 쏠림 재발 방지 목적으로
          항상 켜두는 게 안전하다.
        - enable_diversity=True: 위 하드 제약 + Tier1→Tier2→(dense 한정) 3차 완화까지 전부
          켠다. Tier1만으로 채우는 1차(s3_greedy/s3_local_search, 완화 없음) 이후
          s3_fill_with_tier2()로 비선호 카테고리까지 열어 목표를 채우고, 그래도(dense +
          비공연일 + 목표 7 + 새 카테고리 후보 없음) 부족하면 s3_fill_with_relaxation()으로
          하루 카테고리 상한을 2로 완화한다.
        - 둘 다 False(기본값)면 al02_selftest.py처럼 event_nm이 전부 "테스트 장소 N"류인
          합성 데이터·다른 하위호환 호출에 전혀 영향이 없다(기존 동작 그대로).
        - density_profile("A"/"B"/"C", trip_density_no 기준)은 캡 계산 자체엔 안 쓰이고
          (다양성 하드 제약은 프로파일 무관 항상 동일), Tier2/3차 배정의 날짜별 목표치
          산정(공연일은 al02_policy.CONCERT_DAY_GENERAL_POI_TARGET, 3차 완화는 dense만
          대상)에만 쓰인다."""
        frame = build_trip_frame(user_input)
        names, stays = build_cache(events)
        N = matrix.shape[0]
        score_map = {int(idx): float(s) for idx, s in candidates}
        selected_ctg_nos = user_input.get("ctg_nos") or []

        # ── depot(들)/핀/override를 matrix에 임시 노드로 추가 ────────────
        # role: al02_candidates.build_extra_travel_minutes()가 반환하는 dict의 키와
        # 맞춰야 한다(이동시간 실 API 조회 결과를 role로 매칭).
        # 2026-09-10: 숙소가 여러 곳이면(체크인/체크아웃 기간이 다른 숙소들) 각각을 별도
        # 노드로 추가한다 — day_depots()가 frame["accoms"]+날짜로 그때그때 맞는 노드를
        # 고른다(pick_depot_accom 참고). role은 "depot:{accom_no}"(accom_no가 없으면
        # "depot:None" — lodging 하나만 있던 하위호환 경로).
        extra = []
        depot_idx_by_accom_no = {}
        for accom in frame["accoms"]:
            extra.append({
                "lat": accom.get("lat"), "lon": accom.get("lon"),
                "role": f"depot:{accom.get('accom_no')}",
            })
            depot_idx_by_accom_no[accom.get("accom_no")] = N + len(extra) - 1
        start_pin_idx = None
        end_pin_idx = None
        sp = frame.get("start_pin")
        ep = frame.get("end_pin")
        if sp:
            extra.append({"lat": sp.get("latitude"), "lon": sp.get("longitude"), "role": "start_pin"})
            start_pin_idx = N + len(extra) - 1
        if ep:
            extra.append({"lat": ep.get("latitude"), "lon": ep.get("longitude"), "role": "end_pin"})
            end_pin_idx = N + len(extra) - 1

        # 날짜별 출발지 override: user_input["day_start_override"] = {날짜idx: {lat,lon}}
        # SC-02c 동선 수정 화면에서 중간날 출발지를 바꿀 때 사용.
        # ⚠ 이번 실 API 연동 범위 밖: override 지점은 role이 날짜마다 달라(캐시 키로 쓸
        # 안정적인 PK가 없음) extra_travel_minutes을 지원하지 않는다 — 항상 haversine.
        override_map = {}
        raw_override = user_input.get("day_start_override", {})
        for d, pt in raw_override.items():
            if pt and pt.get("latitude") is not None:
                extra.append({"lat": pt.get("latitude"), "lon": pt.get("longitude"),
                              "role": f"day_start_override:{d}"})
                override_map[int(d)] = N + len(extra) - 1

        aug_matrix, _ = augment_matrix(matrix, events, extra, extra_travel_minutes=extra_travel_minutes)
        frame["depot_idx_by_accom_no"] = depot_idx_by_accom_no
        frame["start_pin_idx"] = start_pin_idx
        frame["end_pin_idx"] = end_pin_idx
        frame["day_start_override"] = override_map

        pad = len(extra)
        pad_names = [f"숙소({a.get('accom_no')})" for a in frame["accoms"]]
        if sp:
            pad_names.append("출발핀")
        if ep:
            pad_names.append("도착핀")
        pad_names += [f"출발지(day{d})" for d in override_map]
        names = names + pad_names
        stays = np.concatenate([stays, np.zeros(pad, dtype=stays.dtype)])
        matrix = aug_matrix

        # ── event_no → matrix 인덱스 변환 ──────────────────
        concert_matrix_idx = None
        concert_event_no = frame.get("concert_event_no")
        if concert_event_no is not None:
            for ii, ev in enumerate(events):
                if ev.get("event_no") == concert_event_no:
                    concert_matrix_idx = ii
                    break
            if concert_matrix_idx is None:
                raise ValueError(
                    f"공연 event_no={concert_event_no}가 events에서 발견되지 않습니다."
                )

        # S3: 날짜 배정 (W_rel 반영) — 영업시간 필터(open_matrix)는 events/date_list 기준이라
        # augment_matrix로 붙는 depot/핀 노드와 무관하게 원본 events 순서 그대로 계산한다.
        open_matrix = build_open_matrix(
            events, frame["date_list"], business_hours_by_event,
            frame["day_start_min"], frame["day_end_min"],
        )
        resolved_visit_max = visit_max if visit_max is not None else VISIT_COUNT_PROFILES["B"]["max"]
        resolved_visit_min = visit_min if visit_min is not None else VISIT_COUNT_PROFILES["B"]["min"]
        resolved_concert_visit_max = (
            concert_visit_max if concert_visit_max is not None
            else CONCERT_VISIT_COUNT_PROFILES["B"]["max"]
        )
        resolved_concert_visit_min = (
            concert_visit_min if concert_visit_min is not None
            else CONCERT_VISIT_COUNT_PROFILES["B"]["min"]
        )
        # 다양성 제약(2026-09-11~12) — enable_hard_dedup/enable_diversity 둘 다 False면
        # caps=None이라 s3_greedy/s3_local_search가 완전히 기존 동작 그대로 돈다(하위호환).
        # CORE_AXES(문서 대신 이번 작업 지시 2번이 명시한 4축 — 동일 이벤트/브랜드/쇼핑
        # 전체-여행 1회 + 하루 동일 ctg_no 최대 1곳)는 enable_hard_dedup만으로도 켜지고,
        # enable_diversity가 그 위에 Tier2 개방 + (dense 한정) 3차 완화를 추가한다.
        density_label = DENSITY_LABEL_BY_WREL_KEY.get(density_profile) if density_profile else None
        diversity_active = enable_diversity or enable_hard_dedup
        caps = None
        if diversity_active:
            n_non_concert_target = compute_n_non_concert_target(
                frame["n_days"], frame["concert_day_idx"], resolved_visit_max, resolved_concert_visit_max,
            )
            caps = make_diversity_caps(selected_ctg_nos, n_non_concert_target)
        diversity_events = events if diversity_active else None
        stage1_axes = CORE_AXES if diversity_active else ALL_DIVERSITY_AXES

        # 1차 배정: Tier1(선호 카테고리)만 — 문서/작업지시 공통으로 "기존 하드제약 유지".
        # preference_tier가 없는 이벤트(al02_selftest.py 등 하위호환 합성 데이터, 또는
        # al02_candidates.fetch_candidates(include_tier2=False) 결과)는 전부 Tier1로 본다.
        tier1_candidates = [
            (i, s) for i, s in candidates
            if events[int(i)].get("preference_tier", PREFERENCE_TIER_1) == PREFERENCE_TIER_1
        ]
        tier2_candidates = [
            (i, s) for i, s in candidates
            if events[int(i)].get("preference_tier") == PREFERENCE_TIER_2
        ]

        day_plans = s3_greedy(tier1_candidates, frame, matrix, names, stays,
                               concert_matrix_idx, W_rel=W_rel, open_matrix=open_matrix,
                               visit_max=resolved_visit_max,
                               concert_visit_max=resolved_concert_visit_max,
                               events=diversity_events, caps=caps,
                               selected_ctg_nos=selected_ctg_nos, axes=stage1_axes)
        day_plans, total_cost = s3_local_search(
            day_plans, frame, matrix, names, stays, concert_matrix_idx,
            candidates=tier1_candidates, W_rel=W_rel, open_matrix=open_matrix,
            visit_max=resolved_visit_max, concert_visit_max=resolved_concert_visit_max,
            events=diversity_events, caps=caps, axes=stage1_axes)

        # 2차 배정: Tier2 개방(al02_policy.PREFERENCE_TIER_2) — enable_diversity일 때만.
        # 공연일 목표는 al02_policy.CONCERT_DAY_GENERAL_POI_TARGET(공연 제외 일반 POI,
        # 프로파일별 1/2/3)을 쓴다 — 기존 CONCERT_VISIT_COUNT_PROFILES(S4 트리밍/하루
        # 상한용)는 그대로 두고 이 목적에만 별도로 참조한다.
        tier2_used_by_day: dict[int, list] = {}
        diversity_relaxed_days = set()
        if enable_diversity:
            concert_general_target = CONCERT_DAY_GENERAL_POI_TARGET.get(
                density_profile, CONCERT_DAY_GENERAL_POI_TARGET["B"]
            )
            tier2_target_visits_by_day = {
                d: (concert_general_target if d == frame["concert_day_idx"] else resolved_visit_max)
                for d in range(frame["n_days"])
            }
            day_plans, tier2_used_by_day = s3_fill_with_tier2(
                day_plans, tier1_candidates + tier2_candidates, frame, matrix, names, stays,
                concert_matrix_idx, events, caps, tier2_target_visits_by_day, open_matrix=open_matrix,
            )

            # 3차 배정: 제한적 완화(문서 5.6) — dense(density_profile="C") 비공연일에서,
            # 목표가 7곳 이상인데 1·2차가 못 채웠고 Tier1/Tier2 통틀어 다른 카테고리
            # 후보도 안 남았을 때만.
            if density_label == "dense":
                relax_target_visits_by_day = {
                    d: (resolved_concert_visit_max if d == frame["concert_day_idx"] else resolved_visit_max)
                    for d in range(frame["n_days"])
                }
                day_plans, diversity_relaxed_days = s3_fill_with_relaxation(
                    day_plans, tier1_candidates + tier2_candidates, frame, matrix, names, stays,
                    concert_matrix_idx, events, caps, relax_target_visits_by_day, density_label,
                    open_matrix=open_matrix,
                )

        # 문서/작업지시 6번 — Tier2(+3차 완화)까지 다 쓰고도 목표 미달인 날짜가 있으면
        # insufficient_diverse_candidates 경고 대상(주의: S4의 시간초과 트리밍 "이전"
        # 상태 기준 — 다양성 후보 부족이 원인인지, 순수 시간 예산 문제인지 구분하기 위해
        # S4 실행 전에 미리 재둔다). 반복 쇼핑·동일 체인으로 억지로 채우지 않는다는
        # 원칙은 애초에 can_insert_candidate()가 항상 걸러서 여기까지 온 day_plans 자체가
        # 그 원칙을 어길 수 없다 — 부족하면 그냥 부족한 채로 두고 신호만 남긴다.
        diversity_incomplete_days = set()
        if enable_diversity:
            concert_general_target = CONCERT_DAY_GENERAL_POI_TARGET.get(
                density_profile, CONCERT_DAY_GENERAL_POI_TARGET["B"]
            )
            for d in range(frame["n_days"]):
                if d == frame["concert_day_idx"]:
                    non_concert_count = len(day_plans[d]) - (1 if concert_matrix_idx is not None else 0)
                    if non_concert_count < concert_general_target:
                        diversity_incomplete_days.add(d)
                elif len(day_plans[d]) < resolved_visit_min:
                    diversity_incomplete_days.add(d)

        # S4: 각 날짜 순서 확정 (시간 초과 시 자동 축소)
        days_output = []
        s4_results = {}
        all_dropped = []
        n_days = frame["n_days"]
        for d in range(n_days):
            is_c = (d == frame["concert_day_idx"])
            c_idx = int(concert_matrix_idx) if is_c else None
            c_start = frame["concert_start_min"] if is_c else None
            c_dur = frame["concert_duration"] if is_c else None
            s_idx, e_idx = day_depots(d, n_days, frame)
            d_start, d_end = frame["day_budget"][d]  # 2026-09-10: day_budget[d]로 통일(모든 날 동일값)

            result, dropped, under_min = s4_solve_fallback(
                day_plans[d], s_idx, e_idx, matrix, names, stays, score_map,
                concert_idx=c_idx, concert_start=c_start,
                concert_duration=c_dur,
                day_start=d_start,
                day_end=d_end,
                # 2026-09-10 정정: 공연 있는 날도 이제 프로파일 하한을 쓴다(공연 포함 총
                # 개수 기준 — CONCERT_VISIT_COUNT_PROFILES). 이전엔 공연일이 고정값이라
                # min 자체를 안 줬었다.
                min_places=resolved_concert_visit_min if is_c else resolved_visit_min,
            )
            s4_results[d] = result
            if under_min:
                logger.warning(
                    "Day %d: 시간 예산 때문에 방문 개수가 프로파일 하한(%s)보다 아래로 "
                    "떨어짐 — 시간 제약이 개수 범위보다 우선이라 허용된 극단적 케이스",
                    d + 1, resolved_visit_min,
                )
            # 제외된 장소는 day_plans에서도 빼고 기록
            for p in dropped:
                if p in day_plans[d]:
                    day_plans[d].remove(p)
                all_dropped.append({"day_index": d, "event_no": events[p].get("event_no"),
                                    "event_nm": events[p].get("event_nm")})

            schedule = []
            for s in result["schedule"]:
                ev = events[s["idx"]]
                schedule.append({
                    "event_no": ev.get("event_no"),
                    "event_nm": s["name"],
                    "ctg_nm": ev.get("ctg_nm"),
                    "travel_from_prev_min": s["travel"],
                    "arrive": s["arrive"],
                    "depart": s["depart"],
                    "stay_min": s["stay"],
                    "is_concert": (s["idx"] == c_idx),
                    # relevance — score_map(candidates 점수)에서 그대로 꺼내 응답에 노출.
                    # 프론트가 "왜 이 장소가 추천됐는지"(취향 반영도)를 보여줄 수 있게 함.
                    "relevance": round(score_map.get(s["idx"], 0.0), 4),
                })

            # start_point 표시: override > 출발핀 > 숙소
            if d in override_map and s_idx == override_map[d]:
                sp_label = "사용자 지정 출발지"
            elif d == 0 and s_idx == start_pin_idx:
                sp_label = "출발핀"
            else:
                sp_label = "숙소"

            days_output.append({
                "day_index": d,
                "date": frame["date_list"][d],
                "is_concert_day": is_c,
                "start_point": sp_label,
                "end_point": ("도착핀" if (d == n_days - 1 and e_idx == end_pin_idx) else "숙소"),
                "n_places": len(day_plans[d]),
                "travel_minutes": result["cost"] if result["order"] else 0,
                "dropped_for_time": [p for p in dropped],  # 시간초과로 제외된 장소
                "schedule": schedule,
            })

        verification = verify(day_plans, frame, concert_matrix_idx, s4_results,
                               visit_max=resolved_visit_max,
                               concert_visit_max=resolved_concert_visit_max,
                               events=diversity_events, caps=caps,
                               relaxed_days=diversity_relaxed_days)
        total_places = len([p for v in day_plans.values() for p in v])

        # 다양성 관찰 지표(문서 8.3 + 이번 작업지시 6번 — tier별 사용 현황/목표수/실제수/
        # 카테고리별 선택수/완화 여부) — caps가 없으면(enable_hard_dedup/enable_diversity
        # 둘 다 False) 전부 기본값.
        diversity_summary = None
        if caps is not None:
            exclude = {int(concert_matrix_idx)} if concert_matrix_idx is not None else None
            trip_state = build_trip_diversity_state(day_plans.values(), events, exclude_idx=exclude)
            tier1_used = sum(
                1 for v in day_plans.values() for i in v
                if i != concert_matrix_idx and events[i].get("preference_tier", PREFERENCE_TIER_1) == PREFERENCE_TIER_1
            )
            tier2_used = sum(len(v) for v in tier2_used_by_day.values())
            days_detail = [
                {
                    "day_index": d,
                    "target_visits": (
                        CONCERT_DAY_GENERAL_POI_TARGET.get(density_profile, CONCERT_DAY_GENERAL_POI_TARGET["B"])
                        if d == frame["concert_day_idx"] and enable_diversity
                        else resolved_visit_max
                    ),
                    "actual_visits": len(day_plans[d]) - (1 if d == frame["concert_day_idx"] and concert_matrix_idx is not None else 0),
                    "tier2_event_nos": tier2_used_by_day.get(d, []),
                    "relaxed": d in diversity_relaxed_days,
                }
                for d in range(frame["n_days"])
            ]
            diversity_summary = {
                "shopping_count": trip_state.shopping_count,
                "brand_counts": dict(trip_state.trip_brand_counts),
                "unique_categories": len(trip_state.trip_category_counts),
                "category_counts": dict(trip_state.trip_category_counts),
                "tier1_used": tier1_used,
                "tier2_used": tier2_used,
                "diversity_relaxed": bool(diversity_relaxed_days),
                "relaxed_days": sorted(diversity_relaxed_days),
                "insufficient_diverse_candidates": bool(diversity_incomplete_days),
                "incomplete_days": sorted(diversity_incomplete_days),
                "days_detail": days_detail,
            }

        return {
            "scoring_policy": SCORING_POLICY["policy_version"],
            "wrel_used": W_rel,
            "summary": {
                "total_places": total_places,
                "total_travel_minutes": sum(
                    d["travel_minutes"] for d in days_output),
                "validation_passed": verification["passed"],
                "validation_details": verification["checks"],
                "dropped_for_time": all_dropped,  # 시간 초과로 빠진 장소 전체
            },
            "days": days_output,
            "diversity": diversity_summary,
        }


def run_abc(pipeline, candidates, matrix, events, user_input):
    """A/B/C 세 동선을 한 번에 생성. 유저가 SC-03에서 비교/선택.

    schedule에 relevance가 실리므로(run() 참고) total_relevance/avg_relevance를 실제로
    집계해서 결과에 포함한다.

    2026-09-10 재정의: A/B/C의 핵심 체감 차이는 W_rel(이동시간-relevance 가중치)이 아니라
    "하루 방문 개수"라는 팀 판단에 따라, 각 프로파일의 al02_policy.VISIT_COUNT_PROFILES
    (min/max)를 W_rel과 함께 run()에 실어 보낸다 — 이전엔 W_rel만 다르고 방문 상한은
    셋 다 ROUTE_POLICY["MAX_PER_DAY_NORMAL"](고정 5)로 동일해서 total_places가 A/B/C
    사이에 사실상 안 갈렸다(2026-09-09 실측 확인). 이제는 total_places 자체가 프로파일별로
    갈리는 게 정상이다.

    ⚠ 알려진 한계(그대로 유지, 이번 범위 밖): "후보 풀에서 뭘 뽑을지"는 여전히 relevance
    내림차순 고정이라 W_rel은 "선택"이 아니라 "배치(어느 날에 넣을지)"에만 영향을 준다."""
    results = {}
    for key, prof in WREL_PROFILES.items():
        visit_profile = VISIT_COUNT_PROFILES[key]
        concert_visit_profile = CONCERT_VISIT_COUNT_PROFILES[key]
        r = pipeline.run(candidates, matrix, events, user_input, W_rel=prof["W_rel"],
                          visit_min=visit_profile["min"], visit_max=visit_profile["max"],
                          concert_visit_min=concert_visit_profile["min"],
                          concert_visit_max=concert_visit_profile["max"])
        all_sched = [s for d in r["days"] for s in d["schedule"]]
        total_rel = round(sum(s.get("relevance", 0.0) for s in all_sched), 4)
        avg_rel = round(total_rel / len(all_sched), 4) if all_sched else 0.0
        results[key] = {
            "label": prof["label"], "W_rel": prof["W_rel"],
            "visit_range": f"{visit_profile['min']}~{visit_profile['max']}",
            "concert_visit_range": f"{concert_visit_profile['min']}~{concert_visit_profile['max']}",
            "total_places": r["summary"]["total_places"],
            "total_travel_minutes": r["summary"]["total_travel_minutes"],
            "total_relevance": total_rel,
            "avg_relevance": avg_rel,
            "result": r,
        }
    return results


# ---------------------------------------------------------------------------
# AHP 가중치 재계산 (SCORING_POLICY["pairwise_judgments"] 검증/재산정용)
# ---------------------------------------------------------------------------


def recalculate_ahp_weights(judgments):
    criteria = sorted(set([k[0] for k in judgments] + [k[1] for k in judgments]))
    n = len(criteria)
    idx = {c: i for i, c in enumerate(criteria)}
    M = np.ones((n, n))
    for (r, c), v in judgments.items():
        M[idx[r], idx[c]] = v
        M[idx[c], idx[r]] = 1.0 / v
    eigvals, eigvecs = np.linalg.eig(M)
    max_i = np.argmax(eigvals.real)
    lam = eigvals.real[max_i]
    w = np.abs(eigvecs[:, max_i].real)
    w = w / w.sum()
    CI = (lam - n) / (n - 1)
    RI = {1: 0, 2: 0, 3: 0.58, 4: 0.90, 5: 1.12}.get(n, 1.12)
    CR = CI / RI if RI > 0 else 0.0
    return {"weights": {c: round(float(w[idx[c]]), 4) for c in criteria},
            "lambda_max": round(float(lam), 4),
            "CI": round(CI, 4), "CR": round(CR, 4), "consistent": CR < 0.10}
