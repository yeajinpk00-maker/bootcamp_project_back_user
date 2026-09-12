"""AL-02 추천 다양성 제약 — 공용 컨텍스트/제약/점수 함수.

AL02_diversity_constraint_handover.md(2026-09-11) 3~8절 설계를 그대로 구현한다.
/recommend(al02_pipeline.py의 S3)와 /alternatives(al02_alternatives.py)가 이 모듈의
함수를 그대로 공유한다 — 문서 5.1의 핵심 요구사항("정책 로직이 두 경로에서 갈라지는
것을 막기 위함", alternatives의 artist_group_map={} 결함이 그 실패 사례로 언급됨).

⚠ eventnm(장소명 문자열)은 절대 동일 장소 판정·조인·저장에 쓰지 않는다(문서 2.2 필수
조건) — 동일 장소는 반드시 event_no(PK)로만 판정한다. event_nm은 여기서 브랜드 키를
추정하는 런타임 보조 용도로만 쓴다.

정책 상수(비율/절대상한/보너스/가중치 등)는 전부 al02_policy.py에서만 가져온다 —
이 모듈에 숫자를 직접 박아넣지 않는다(하드코딩 금지 원칙, al02_pipeline.py와 동일).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from al02_policy import (
    CATEGORY_TRIP_CAP_POLICY,
    COVERAGE_BONUS_BY_RANK,
    KNOWN_CHAIN_PREFIXES,
    MAX_SAME_BRAND_PER_TRIP,
    MAX_SHOPPING_PER_TRIP,
    MMR_REDUNDANCY_WEIGHT,
    RELAXED_SAME_CATEGORY_PER_DAY,
    REPLACEMENT_SCORE_WEIGHTS,
    SHOPPING_CTG_NOS,
    SIMILARITY_SCORE,
    TRAVEL_FIT_BEYOND,
    TRAVEL_FIT_TABLE,
)

# ---------------------------------------------------------------------------
# 5.2 — 런타임 브랜드 키 (allow-list, eventnm 첫 토큰 전체를 브랜드로 보지 않음)
# ---------------------------------------------------------------------------


def normalize_event_name(event_nm: str | None) -> str:
    """브랜드 접두어 매칭 전 최소 정규화 — 앞뒤 공백만 제거한다.
    한글 상호명은 대소문자 구분이 없어 casefold 등은 불필요."""
    if not event_nm:
        return ""
    return event_nm.strip()


def derive_brand_key(event_nm: str | None) -> str | None:
    """문서 5.2 — 명확한 체인만 allow-list로 판별한다. event_nm의 첫 토큰을 전부
    브랜드로 간주하지 않는다(문서 5.2 필수조건) — al02_policy.KNOWN_CHAIN_PREFIXES에
    없는 상호는 항상 None(브랜드 제약 미적용)이다. eventnm으로 조인/저장/동일장소
    판정을 하지 않는다는 원칙과도 무관하게, 여기서는 오직 "다양성 제약을 적용할지"
    판단용 파생값만 만든다."""
    normalized = normalize_event_name(event_nm)
    if not normalized:
        return None
    for prefix, key in KNOWN_CHAIN_PREFIXES.items():
        if normalized.startswith(prefix):
            return key
    return None


# ---------------------------------------------------------------------------
# 4.3 — 전체 일정 카테고리 상한 U_c = min(A_c, max(1, ceil(r_c * N_non_concert_target)))
# ---------------------------------------------------------------------------


def category_rank(ctg_no: int | None, selected_ctg_nos: list[int]) -> int | None:
    """tripinterest.rank 기준 순위(1부터). 선택 안 했으면 None."""
    if ctg_no is None or not selected_ctg_nos or ctg_no not in selected_ctg_nos:
        return None
    return selected_ctg_nos.index(ctg_no) + 1


def category_trip_cap(ctg_no: int | None, selected_ctg_nos: list[int], n_non_concert_target: int) -> int:
    """카테고리 ctg_no의 여행 전체 상한 U_c.

    쇼핑(SHOPPING_CTG_NOS)은 tripinterest 순위와 무관하게 항상 CATEGORY_TRIP_CAP_POLICY
    ["shopping"] 행을 쓴다(A_c=1이라 결과는 항상 1 — 문서 4.4 "쇼핑은 절대 완화 안 함"과
    정합). 그 외는 순위 1/2/3이면 해당 행, 4순위 이하 또는 미선택이면 "unselected" 행.
    ctg_no가 None이면(방어적) 가장 보수적인 unselected 상한을 반환한다."""
    if ctg_no is not None and ctg_no in SHOPPING_CTG_NOS:
        policy = CATEGORY_TRIP_CAP_POLICY["shopping"]
    else:
        rank = category_rank(ctg_no, selected_ctg_nos)
        policy = CATEGORY_TRIP_CAP_POLICY.get(rank, CATEGORY_TRIP_CAP_POLICY["unselected"])
    r, a = policy["r"], policy["A"]
    return min(a, max(1, math.ceil(r * n_non_concert_target)))


def compute_n_non_concert_target(n_days: int, concert_day_idx: int | None,
                                  visit_max: int, concert_visit_max: int) -> int:
    """문서 4.5 N_non_concert_target — 공연을 제외한 여행 전체 "목표" 방문 수.
    이 코드베이스엔 별도 "목표값" 개념이 없어(al02_policy.VISIT_COUNT_PROFILES의
    min/max 범위만 있음), S3가 실제로 채우려 시도하는 상한(visit_max/concert_visit_max
    — s3_greedy가 매일 그 개수까지 그리디로 채움)을 목표로 그대로 쓴다. 공연일은
    concert_visit_max에 공연 1개가 이미 포함된 값이므로 -1로 순수 POI 개수만 센다."""
    total = 0
    for d in range(n_days):
        if d == concert_day_idx:
            total += max(0, concert_visit_max - 1)
        else:
            total += visit_max
    return total


# ---------------------------------------------------------------------------
# 5.4 — 다양성 컨텍스트
# ---------------------------------------------------------------------------


@dataclass
class DiversityState:
    selected_event_nos: set[int] = field(default_factory=set)
    trip_category_counts: Counter = field(default_factory=Counter)
    trip_brand_counts: Counter = field(default_factory=Counter)
    shopping_count: int = 0


@dataclass
class DayDiversityState:
    category_counts: Counter = field(default_factory=Counter)
    selected_event_nos: set[int] = field(default_factory=set)


@dataclass
class DiversityCaps:
    """문서 5.4 DiversityCaps를 그대로 따르되, trip_category_caps는 미리 계산한
    dict 대신 selected_ctg_nos + n_non_concert_target을 들고 있다가 category_trip_cap()
    으로 그때그때 계산한다 — recommend는 후보 ctg_no가 항상 selected_ctg_nos의
    부분집합이지만(S0 SQL이 ctg_no IN selected_ctg_nos로 필터링), alternatives는
    "교체 대상과 같은 ctg_no"만 후보로 잡아 그 ctg_no가 트립의 선호 목록에 아예
    없을 수도 있다(문서에 없는 케이스) — 미리 "가능한 모든 ctg_no"의 우주를 알아야
    하는 대신, 필요한 시점에 계산하는 쪽이 두 호출부 모두에 안전하다."""
    day_category_cap: int = RELAXED_SAME_CATEGORY_PER_DAY
    selected_ctg_nos: list[int] = field(default_factory=list)
    n_non_concert_target: int = 0
    max_shopping_per_trip: int = MAX_SHOPPING_PER_TRIP
    max_same_brand_per_trip: int = MAX_SAME_BRAND_PER_TRIP


def make_diversity_caps(selected_ctg_nos: list[int], n_non_concert_target: int) -> DiversityCaps:
    return DiversityCaps(
        day_category_cap=RELAXED_SAME_CATEGORY_PER_DAY,
        selected_ctg_nos=list(selected_ctg_nos or []),
        n_non_concert_target=n_non_concert_target,
        max_shopping_per_trip=MAX_SHOPPING_PER_TRIP,
        max_same_brand_per_trip=MAX_SAME_BRAND_PER_TRIP,
    )


def build_trip_diversity_state(day_event_lists, events_by_idx: dict[int, dict] | list[dict],
                                exclude_idx: set[int] | None = None) -> DiversityState:
    """day_event_lists: 날짜별 matrix idx 리스트들의 iterable(dict.values() 등).
    events_by_idx: idx -> event dict(event_no/ctg_no/event_nm 포함, 보통 al02_pipeline의
    events 리스트를 그대로 넘긴다 — 인덱스 그대로 접근 가능). exclude_idx는 alternatives가
    교체 대상 자리를 "이미 선택된 상태"에서 빼고 계산할 때 쓴다(문서 6.2)."""
    exclude_idx = exclude_idx or set()
    state = DiversityState()
    for day_list in day_event_lists:
        for idx in day_list:
            if idx in exclude_idx:
                continue
            ev = events_by_idx[idx]
            eno = ev.get("event_no")
            if eno is not None:
                state.selected_event_nos.add(eno)
            ctg_no = ev.get("ctg_no")
            if ctg_no is not None:
                state.trip_category_counts[ctg_no] += 1
                if ctg_no in SHOPPING_CTG_NOS:
                    state.shopping_count += 1
            brand_key = derive_brand_key(ev.get("event_nm"))
            if brand_key:
                state.trip_brand_counts[brand_key] += 1
    return state


def build_day_diversity_state(day_event_list, events_by_idx: dict[int, dict] | list[dict],
                               exclude_idx: set[int] | None = None) -> DayDiversityState:
    exclude_idx = exclude_idx or set()
    state = DayDiversityState()
    for idx in day_event_list:
        if idx in exclude_idx:
            continue
        ev = events_by_idx[idx]
        ctg_no = ev.get("ctg_no")
        if ctg_no is not None:
            state.category_counts[ctg_no] += 1
        eno = ev.get("event_no")
        if eno is not None:
            state.selected_event_nos.add(eno)
    return state


# ---------------------------------------------------------------------------
# 5.5 — 삽입 가능 여부: 단일 공용 함수
# ---------------------------------------------------------------------------

ALL_DIVERSITY_AXES: tuple[str, ...] = (
    "duplicate_event", "brand_trip_cap", "shopping_trip_cap", "category_day_cap", "category_trip_cap",
)

# 2026-09-12 신규 — 이번 Tier 작업(프론트 리포트 대응)이 실제로 "항상 유지"하라고 명시한
# 네 축만. 이전 문서(AL02_diversity_constraint_handover.md)의 category_trip_cap(선호
# 순위별 비율 r_c/절대상한 A_c) 축은 이번 작업 지시에 명시적으로 포함되지 않았다 —
# trip_interest 실측(카테고리 2~3개만 선택하는 게 사실상 전 트립의 정상 케이스)과 맞물려
# 방문 목표를 더 어렵게 만드는 축이라, 이번 작업 범위에서는 뺐다. category_trip_cap 자체
# 함수는 여전히 존재하고(alternatives의 diagnose_candidate 등 정보 표시용 호출에 남음),
# 이 CORE_AXES만 실제 게이팅(can_insert_candidate)에 쓰인다.
CORE_AXES: tuple[str, ...] = (
    "duplicate_event", "brand_trip_cap", "shopping_trip_cap", "category_day_cap",
)


def _diversity_checks(
    candidate: dict,
    day_state: DayDiversityState,
    trip_state: DiversityState,
    caps: DiversityCaps,
    *,
    is_concert_day: bool,
    allow_day_category_relaxation: bool,
    axes: tuple[str, ...] = ALL_DIVERSITY_AXES,
) -> list[tuple[str, bool]]:
    """문서 5.5의 다섯 축을 전부 평가해 (이름, 통과여부) 순서 리스트로 반환한다 —
    can_insert_candidate()(첫 실패에서 멈춤)와 diagnose_candidate()(전부 보여줌)가
    이 하나의 목록을 공유해, "어느 쪽에서 봐도 같은 판정"이 보장된다.

    axes: 실제로 걸러야 하는 축만 골라서 받을 수 있다(2026-09-11, al02_alternatives.py
    전용) — alternatives 후보는 SQL이 항상 "교체 대상과 같은 ctg_no"만 가져오므로,
    day_state/trip_state를 교체 대상 제외 기준으로 만들어도 category_day_cap/
    category_trip_cap/shopping_trip_cap 세 축은 "어떤 후보를 고르든 교체 전후로 그
    카테고리 개수가 똑같다"는 수학적 이유로 모든 후보에 대해 항상 같은 값(전부 통과
    또는 전부 실패)이 나온다 — 실제로 사전 배정된 트립(예: "성지 음식점" 4곳짜리
    맛집 투어 하루)에서 이미 그 카테고리가 하루 상한을 넘어 있으면, 이 세 축을 그대로
    걸었을 때 대체 후보가 무조건 0건이 되는 회귀를 실측으로 확인했다(trip_no=43).
    그래서 alternatives는 실제로 후보마다 다르게 갈리는 duplicate_event/brand_trip_cap
    두 축만 걸러내는 데 쓰고, 나머지는 diagnose_candidate()로 "정보 표시"만 한다."""
    event_no = candidate.get("event_no")
    ctg_no = candidate.get("ctg_no")
    brand_key = derive_brand_key(candidate.get("event_nm"))

    duplicate_ok = not (event_no is not None and event_no in trip_state.selected_event_nos)
    brand_ok = not (brand_key and trip_state.trip_brand_counts[brand_key] >= caps.max_same_brand_per_trip)
    shopping_ok = not (ctg_no in SHOPPING_CTG_NOS and trip_state.shopping_count >= caps.max_shopping_per_trip)

    # 문서 5.5 "구현 주의": day_cap 기본값은 1 — allow_day_category_relaxation이 True이고
    # 공연일이 아닐 때만 caps.day_category_cap(완화값, 기본 2)로 올라간다.
    day_cap = 1
    if allow_day_category_relaxation and not is_concert_day:
        day_cap = caps.day_category_cap
    category_day_ok = not (ctg_no is not None and day_state.category_counts[ctg_no] >= day_cap)

    category_trip_ok = True
    if ctg_no is not None:
        trip_cap = category_trip_cap(ctg_no, caps.selected_ctg_nos, caps.n_non_concert_target)
        category_trip_ok = trip_state.trip_category_counts[ctg_no] < trip_cap

    all_checks = {
        "duplicate_event": duplicate_ok,
        "brand_trip_cap": brand_ok,
        "shopping_trip_cap": shopping_ok,
        "category_day_cap": category_day_ok,
        "category_trip_cap": category_trip_ok,
    }
    return [(name, all_checks[name]) for name in axes]


def can_insert_candidate(
    candidate: dict,
    day_state: DayDiversityState,
    trip_state: DiversityState,
    caps: DiversityCaps,
    *,
    is_concert_day: bool,
    allow_day_category_relaxation: bool,
    axes: tuple[str, ...] = ALL_DIVERSITY_AXES,
) -> tuple[bool, str | None]:
    """후보(candidate: event dict, event_no/ctg_no/event_nm 포함)가 다양성 제약을
    통과하는지 판정한다. 시간 예산·공연 버퍼 검사는 이 함수 밖(al02_pipeline의
    _day_cost/s4_solve, al02_alternatives의 verify_swap)에서 별도로 한다 — 문서 5.5
    docstring 그대로. 첫 실패 축에서 바로 멈춘다(S3 그리디처럼 통과여부만 필요한
    호출부용 — 전체 축을 다 보고 싶으면 diagnose_candidate() 사용).

    axes: _diversity_checks() 참고 — S3(al02_pipeline.py)는 기본값(다섯 축 전부)을
    그대로 쓰고, alternatives(al02_alternatives.py)만 실제로 후보마다 갈리는 두 축
    (duplicate_event, brand_trip_cap)으로 좁혀서 부른다.

    반환 reason은 로그·QA·API 디버깅용(문서 8.3 excluded_reason_counts와 값 일치)."""
    for name, ok in _diversity_checks(candidate, day_state, trip_state, caps,
                                       is_concert_day=is_concert_day,
                                       allow_day_category_relaxation=allow_day_category_relaxation,
                                       axes=axes):
        if not ok:
            return False, name
    return True, None


def diagnose_candidate(
    candidate: dict,
    day_state: DayDiversityState,
    trip_state: DiversityState,
    caps: DiversityCaps,
    *,
    is_concert_day: bool,
    allow_day_category_relaxation: bool = False,
) -> dict[str, bool]:
    """alternatives 응답의 constraint_check(문서 6.4) — 다섯 축을 전부 개별 True/False로
    보여준다(can_insert_candidate처럼 첫 실패에서 멈추지 않음). 항상 다섯 축 전부를
    "정보 표시"용으로 보여준다 — category_day_cap/category_trip_cap/shopping_trip_cap은
    alternatives 필터링에는 안 쓰이지만(_diversity_checks의 axes 설명 참고), 그 날/그
    트립이 이미 정책 상한을 넘어선 상태라는 사실 자체는 여전히 유용한 정보라 감추지
    않는다."""
    checks = _diversity_checks(candidate, day_state, trip_state, caps,
                                is_concert_day=is_concert_day,
                                allow_day_category_relaxation=allow_day_category_relaxation)
    return {f"{name}_ok": ok for name, ok in checks}


# ---------------------------------------------------------------------------
# 5.7 — 선택 점수: relevance 유지 + coverage - redundancy (+ route_fit은 호출부가 더함)
# ---------------------------------------------------------------------------


def similarity(candidate: dict, selected: dict) -> float:
    """문서 5.7 유사도 표. 값은 al02_policy.SIMILARITY_SCORE에서만 가져온다."""
    if candidate.get("event_no") is not None and candidate.get("event_no") == selected.get("event_no"):
        return SIMILARITY_SCORE["same_event"]

    c_brand = derive_brand_key(candidate.get("event_nm"))
    s_brand = derive_brand_key(selected.get("event_nm"))
    if c_brand and c_brand == s_brand:
        return SIMILARITY_SCORE["same_brand"]

    c_ctg, s_ctg = candidate.get("ctg_no"), selected.get("ctg_no")
    if c_ctg in SHOPPING_CTG_NOS and s_ctg in SHOPPING_CTG_NOS:
        return SIMILARITY_SCORE["both_shopping"]

    if c_ctg is not None and c_ctg == s_ctg:
        return SIMILARITY_SCORE["same_ctg"]

    c_ctg_type, s_ctg_type = candidate.get("ctg_type_no"), selected.get("ctg_type_no")
    if c_ctg_type is not None and c_ctg_type == s_ctg_type:
        return SIMILARITY_SCORE["same_ctg_type"]

    return SIMILARITY_SCORE["unrelated"]


def coverage_bonus(candidate_ctg_no: int | None, selected_ctg_nos: list[int],
                    trip_state: DiversityState) -> float:
    """아직 여행 전체에 하나도 없는 선호 카테고리(1/2/3순위)에 넣으면 그 순위의 보너스.
    이미 그 카테고리가 1개 이상 있으면(coverage 이미 충족) 보너스 없음 — xQuAD의
    "아직 충족되지 않은 aspect"만 보상하는 원리(문서 3.3)."""
    if candidate_ctg_no is None:
        return 0.0
    rank = category_rank(candidate_ctg_no, selected_ctg_nos)
    if rank is None or rank not in COVERAGE_BONUS_BY_RANK:
        return 0.0
    if trip_state.trip_category_counts[candidate_ctg_no] > 0:
        return 0.0  # 이미 커버된 선호 — 보너스 대상 아님
    return COVERAGE_BONUS_BY_RANK[rank]


def redundancy_penalty(candidate: dict, selected_events: list[dict]) -> float:
    """이미 선택된 장소들과의 최대 유사도 * MMR_REDUNDANCY_WEIGHT (문서 5.7/3.2 MMR)."""
    if not selected_events:
        return 0.0
    max_sim = max(similarity(candidate, s) for s in selected_events)
    return MMR_REDUNDANCY_WEIGHT * max_sim


def diversity_fit(candidate: dict, selected_events: list[dict]) -> float:
    """alternatives의 score_breakdown.diversity_fit(문서 6.4) — "이미 고른 것들과
    얼마나 다른가"를 0~1로 표현한다. redundancy_penalty와 같은 similarity()를 재사용해
    별도 유사도 개념을 새로 만들지 않는다(1 - 최대유사도)."""
    if not selected_events:
        return 1.0
    max_sim = max(similarity(candidate, s) for s in selected_events)
    return round(1.0 - max_sim, 4)


def calculate_selection_score(candidate: dict, relevance: float, selected_events: list[dict],
                               selected_ctg_nos: list[int], trip_state: DiversityState) -> float:
    """문서 5.7 selection_score = relevance + coverage_bonus - redundancy_penalty.
    route_fit(이동시간 적합도)은 여기 안 넣는다 — al02_pipeline.s3_greedy가 기존
    "삽입 증가 이동시간(after-before)" 계산 결과를 gain 산식에서 그대로 반영한다
    (문서 5.7 "route_fit: 기존 로직 재사용, 중복 구현 금지")."""
    cb = coverage_bonus(candidate.get("ctg_no"), selected_ctg_nos, trip_state)
    rp = redundancy_penalty(candidate, selected_events)
    return relevance + cb - rp


# ---------------------------------------------------------------------------
# 6.3 — alternatives 전용: replacement_score = 0.60*base_relevance + 0.25*travel_fit + 0.15*diversity_fit
# ---------------------------------------------------------------------------


def travel_fit(added_travel_min: float) -> float:
    """added_travel_min(교체 전후 실제 추가 이동시간, 분) -> travel_fit(0~1).
    al02_policy.TRAVEL_FIT_TABLE의 (상한분, fit) 오름차순 표를 그대로 따른다."""
    for upper_bound, fit in TRAVEL_FIT_TABLE:
        if added_travel_min <= upper_bound:
            return fit
    return TRAVEL_FIT_BEYOND


def replacement_score(base_relevance: float, travel_fit_value: float, diversity_fit_value: float) -> float:
    w = REPLACEMENT_SCORE_WEIGHTS
    return round(
        w["base_relevance"] * base_relevance
        + w["travel_fit"] * travel_fit_value
        + w["diversity_fit"] * diversity_fit_value,
        4,
    )
