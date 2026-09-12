"""AL-02 동선 최적화 엔진 — 정책/가중치 상수.

가중치·정책 값은 반드시 이 모듈에서만 정의한다(하드코딩 금지). 다른 모듈
(al02_pipeline.py, al02_candidates.py 등)은 이 모듈에서 import해서만 쓴다.

원본: AL02_pipeline_v4_final.ipynb — 실행 검증 완료(2026-09-09), v4 정정
(artist_match 우선순위 3단: 선택 멤버 본인 1.0 / 그룹 전체 태그 0.7(폴백) / 그 외 0.0) 반영.
"""

# ── S2 채점 가중치 (AHP 쌍대비교 + 고유벡터법) ────────────────────────
SCORING_POLICY = {
    "policy_version": "ahp_v1_20260907",
    "decision_method": "AHP pairwise comparison (Saaty scale) + eigenvalue method",
    "weights": {"artist_match": 0.6483, "category_fitness": 0.2297, "place_quality": 0.1220},
    "pairwise_judgments": {
        ("artist_match", "category_fitness"): 3.0,
        ("artist_match", "place_quality"): 5.0,
        ("category_fitness", "place_quality"): 2.0,
    },
    "consistency": {"lambda_max": 3.0037, "CI": 0.0018, "RI": 0.58, "CR": 0.0032},
    "rationale": "K-POP 팬 여행 타겟의 성향 기준",
    "revision_trigger": ["total_score 산식 변경", "장소-멤버 연결 테이블 추가", "실사용자 데이터 축적 시"],
}

# ── S3/S4 동선 제약 ──────────────────────────────────────────────────
ROUTE_POLICY = {
    "CONCERT_BUFFER_MIN": 150,
    "CONCERT_DURATION_MIN": 90,
    "DAY_START_MIN": 540,
    "DAY_END_MIN": 1260,
}

# 2026-09-10: A/B/C 트립 밀도(trip_density_no 1/2/3) 프로파일 재정의. 기존엔 A/B/C가
# W_rel(이동시간-relevance 가중치)만 다르고 비공연일 방문 상한은 ROUTE_POLICY
# ["MAX_PER_DAY_NORMAL"]=5로 셋이 전부 동일했다 — 실측 결과(2026-09-09) 그 때문에
# A/B/C 최종 방문 개수가 사실상 똑같이 나오는 문제가 있었음. "세밀한 동선 형태 차이보다
# 하루 방문 개수 차이가 체감 포인트"라는 팀 재정의에 따라 그 고정 상수를 폐기하고
# 프로파일별 개수 범위로 대체한다. min/max는 al02_pipeline의 S4 "시간 예산 안에서
# relevance 최하위부터 제거" 트리밍 로직의 하한/상한으로 쓰인다 — max는 S3가 그 날에
# 배정할 수 있는 상한이기도 하다(공연 있는 날은 이 표 대상이 아니고 아래
# CONCERT_VISIT_COUNT_PROFILES를 따로 쓴다). 시간 예산이 min에서도 안 맞는 극단적
# 케이스는 시간 제약이 개수 범위보다 우선이라 min 아래로 더 빠질 수 있다
# (al02_pipeline.s4_solve_fallback 참고).
VISIT_COUNT_PROFILES = {
    "A": {"min": 3, "max": 4},
    "B": {"min": 4, "max": 5},
    "C": {"min": 5, "max": 7},
}

# 2026-09-10 정정: "공연일은 A/B/C 무관 고정 4곳(최대 3곳+공연 1곳)"이었던 직전 정책을
# 철회 — 공연일도 비공연일처럼 trip_density_no 기준 프로파일별 범위를 적용한다. min/max
# 둘 다 "공연 포함" 총 방문 개수 기준(VISIT_COUNT_PROFILES와 같은 형태, 다만 이쪽은
# 공연 1개가 항상 포함된 총합이라는 점만 다름). 공연 자체는 여전히 항상 포함되고
# 마지막 방문으로 고정된다(al02_pipeline.s4_solve의 "공연은 마지막 고정" 로직 불변) —
# 이 표는 그 날 "총 개수"만 정한다. C는 이전 고정값(4)과 동일하게 유지.
CONCERT_VISIT_COUNT_PROFILES = {
    "A": {"min": 2, "max": 3},
    "B": {"min": 3, "max": 4},
    "C": {"min": 4, "max": 4},
}

# ctg_nm -> 기본 체류시간(분). team2 ctg 테이블의 12개 ctg_nm과 1:1 대응(2026-09-09 확인).
STAY_MAP = {
    "콘서트/팬미팅": 120, "팬사인회": 90, "공연/행사": 120,
    "팝업": 60, "기타": 40, "생일 카페": 60,
    "성지 디저트/카페": 60, "성지 음식점": 60, "팝업/굿즈": 45,
    "기타 성지": 40, "문화/유적지": 90, "여행지": 90, "쇼핑": 60,
}

CTG_TYPE = {1: "행사", 2: "성지", 3: "관광 명소"}
ALLOWED_PREFERENCE_CTG_TYPES = [2, 3]  # trip_interest/선호 카테고리로 고를 수 있는 ctg_type_no
ALLOWED_OP_STATUS = [1, 2]  # hard_filter 통과 기준(운영 정상 상태만)

# A/B/C 동선 비교(SC-03)용 W_rel 프로파일.
# ⚠ 15/30/60은 실측 전 임시값 — 실제 relevance/이동시간 분포로 재보정 필요(다음 라운드).
WREL_PROFILES = {
    "A": {"W_rel": 15, "label": "여유 우선 (이동 최소)"},
    "B": {"W_rel": 30, "label": "균형 (기본)"},
    "C": {"W_rel": 60, "label": "많이 보기 (취향 우선)"},
}

# ── 2026-09-11 다양성 제약(TTDP/MMR/xQuAD 근거) — AL02_diversity_constraint_handover.md ──
# 배경: 선호 카테고리에 "쇼핑"이 있으면 1일차 대부분이 동일 체인(예: 올리브영) 매장으로만
# 채워지는 버그가 있었다. 최초 대응(2026-09-11 오전, 이제 폐기됨)은 event_nm 첫 토큰
# 기반 "같은 상호 하루 최대 2개" 캡이었으나, 핸드오버 문서(2026-09-11 오후)가 학술
# 근거(MMR/xQuAD/TTDP)를 붙여 트립 전체 기준 하드 제약 + 카테고리 상한 체계로 대체했다
# (al02_diversity.py 참고). MAX_SAME_BRAND_PER_DAY는 이 정책으로 완전히 대체되어 삭제됨
# — 이제 브랜드는 "하루 N개"가 아니라 "여행 전체 1개"로 관리된다.

# trip.trip_density_no(1/2/3) -> WREL_PROFILES/VISIT_COUNT_PROFILES 등의 프로파일 키(A/B/C).
# auth.py에만 있던 매핑을 al02_alternatives.py도 함께 써야 해서(공용 정책값 원칙) 이리로 이동.
TRIP_DENSITY_TO_WREL_KEY = {1: "A", 2: "B", 3: "C"}

# 프로파일 키(A/B/C) -> 핸드오버 문서 4.5/5.6이 쓰는 밀도 라벨. C(많이 보기)만 "dense"로
# 취급 — 문서 4.4(표)는 "기본 비공연일(B)도 후보 부족 시 완화 가능"이라고 서술하지만,
# 문서 5.6의 실제 구현 pseudocode와 10절 완료 기준 체크리스트("dense 비공연일의 후보
# 부족 상황에서만 허용")는 dense(C)만 명시한다 — 서술(4.4)과 pseudocode/완료기준(5.6/10)이
# 문서 안에서 서로 다른데, 검증 가능한 쪽(pseudocode+완료기준)을 따랐다.
DENSITY_LABEL_BY_WREL_KEY = {"A": "relaxed", "B": "balanced", "C": "dense"}

# 문서 5.3 SQL(SELECT ctg_no, ctg_nm, ctg_type_no, ctg_type_nm FROM ctg JOIN ctg_type ...)을
# 실 DB에 그대로 돌려 확인(2026-09-11): 쇼핑은 ctg_no=13("쇼핑", ctg_type_no=3 "관광 명소")
# 단 하나뿐이었다. 문서가 "SHOPPING_CTG_NOS = {...}  # 실제 값으로 확정할 것"이라고 비워둔
# 부분을 이 값으로 채운다.
SHOPPING_CTG_NOS = {13}

# 문서 5.2 — 정식 브랜드 테이블이 없는 현 단계의 "런타임 보조 키". allow-list에 없는
# 상호는 절대 브랜드로 묶지 않는다(오분류보다 미탐지가 안전하다는 문서 5.2 필수조건).
# 이전(al02_pipeline._brand_key, 첫 공백 토큰 전부를 브랜드로 간주하던 방식)은 al02_selftest.py
# 처럼 event_nm이 전부 "테스트 장소 N"류인 합성 데이터에서 15개 전부가 "테스트"라는 같은
# 브랜드로 묶이는 등 오분류 위험이 있었다 — allow-list 방식으로 교체해 이 문제 자체가 사라짐.
KNOWN_CHAIN_PREFIXES = {
    "올리브영": "oliveyoung",
    "다이소": "daiso",
    "스타벅스": "starbucks",
}

# ── 문서 4.2 하드 제약 ──────────────────────────────────────────────
MAX_SAME_EVENT_PER_TRIP = 1
MAX_SAME_BRAND_PER_TRIP = 1       # brand_key를 신뢰성 있게 얻은 경우만 적용(al02_diversity.derive_brand_key)
MAX_SHOPPING_PER_TRIP = 1
MAX_SAME_CATEGORY_PER_DAY = 1     # 기본(비완화) 하루 상한
RELAXED_SAME_CATEGORY_PER_DAY = 2  # 문서 4.4/5.6 — dense 비공연일, 후보 부족 시에만 적용

# ── 2026-09-12 신규 — 선호 카테고리 Tier(프론트 리포트 대응) ────────────
# 배경: trip_interest 실측 결과 70개 트립 중 4개가 카테고리 2개, 66개가 3개만 선택
# (4개 이상 선택한 트립 0건) — 선호 카테고리 후보만으로는(Tier1) 하루 카테고리 상한 1과
# 부딪혀 방문 목표를 못 채우는 게 예외가 아니라 사실상 전 트립의 정상 케이스였다
# (실측: trip_no=43, balanced 프로파일에서 1일차 1곳/2일차 공연만/3일차 0곳). S0가
# 원래 트립이 고른 ctg_no만 후보로 가져오던 것을 넓혀서, "선호하진 않았지만 ctg_type
# 2/3 안의 다른 카테고리"도 2순위 후보 풀(Tier2)로 쓴다 — category_fitness는 al02_pipeline.
# category_fitness()의 기존 MISS=0.3 그대로 자동 적용되고(선택 안 한 카테고리라 그
# 함수가 이미 그렇게 계산함), 새 공식은 만들지 않는다.
#   Tier 1: trip_interest 1·2·3순위로 고른 ctg_no (기존 S0 그대로)
#   Tier 2: ctg_type_no IN ALLOWED_PREFERENCE_CTG_TYPES 안의 비선호 ctg_no
#           (운영상태/반경/영업시간 조건은 Tier1과 동일하게 적용)
#   Tier 3: 이번 작업 범위에서는 비활성(자리만 마련 — 도입 시 ctg_type_no=1 등 확장 검토)
PREFERENCE_TIER_1 = 1
PREFERENCE_TIER_2 = 2
PREFERENCE_TIER_3_ENABLED = False  # 문서 지시 — 이번 작업 범위 밖, 켜지 않음

# 공연일 "일반 POI"(공연 제외) 목표 개수 — 프로파일별로 명시된 값을 그대로 쓴다.
# 기존 CONCERT_VISIT_COUNT_PROFILES(공연 포함 총 개수의 min/max, S4 트리밍 하한/S3 하루
# 상한에 계속 쓰임)는 건드리지 않는다 — 이 상수는 "Tier2까지 열어서 몇 곳을 목표로
# 채울지"를 판단하는 별도 목적으로만 s3_fill_with_tier2()가 참조한다.
CONCERT_DAY_GENERAL_POI_TARGET = {"A": 1, "B": 2, "C": 3}

# ── 문서 4.3 — 전체 일정 카테고리 상한: 비율 r_c + 절대상한 A_c ──────────
# U_c = min(A_c, max(1, ceil(r_c * N_non_concert_target)))
# rank는 tripinterest.rank(사용자가 고른 선호 카테고리 순서, 1부터) 기준. 쇼핑(SHOPPING_CTG_NOS)은
# 몇 순위로 선택했든 항상 이 표의 "shopping" 행을 쓴다(문서 4.4 "쇼핑은 절대 완화 안 함"과
# 정합 — A_c=1이라 어차피 공식상 결과는 항상 1). 4순위 이하로 선택했거나 아예 선택하지
# 않은 카테고리는 "unselected" 행(문서가 명시한 "1·2·3순위 이외"의 기본값).
CATEGORY_TRIP_CAP_POLICY = {
    1: {"r": 0.35, "A": 2},
    2: {"r": 0.30, "A": 2},
    3: {"r": 0.25, "A": 2},
    "unselected": {"r": 0.20, "A": 1},
    "shopping": {"r": 0.20, "A": 1},
}

# ── 문서 5.7 — selection_score = relevance + coverage_bonus - redundancy_penalty ──
# (route_fit은 기존 S3 삽입-증가-이동시간 계산을 그대로 재사용 — 별도 상수 없음)
COVERAGE_BONUS_BY_RANK = {1: 0.15, 2: 0.10, 3: 0.05}
MMR_REDUNDANCY_WEIGHT = 0.10

# 문서 5.7 유사도 표 — redundancy_penalty·diversity_fit 계산에 공용으로 쓴다.
SIMILARITY_SCORE = {
    "same_event": 1.0,
    "same_brand": 1.0,
    "both_shopping": 0.8,
    "same_ctg": 0.6,
    "same_ctg_type": 0.3,
    "unrelated": 0.0,
}

# ── 문서 6.3 — alternatives 전용 replacement_score ──────────────────
REPLACEMENT_SCORE_WEIGHTS = {"base_relevance": 0.60, "travel_fit": 0.25, "diversity_fit": 0.15}

# added_travel_min(분, 교체 전후 실제 이동시간 차이) -> travel_fit. (상한분, fit) 오름차순 —
# added_travel_min이 그 상한 "이하"면 해당 fit, 마지막 구간(30분) 초과는 TRAVEL_FIT_BEYOND.
TRAVEL_FIT_TABLE = [(0, 1.00), (10, 0.85), (20, 0.65), (30, 0.40)]
TRAVEL_FIT_BEYOND = 0.00

# ── S0 후보 검색(al02_candidates.py) 기본값 ──────────────────────────
# 2026-09-10: 200 -> 300으로 소폭 상향(안전장치). radius_km(10km) 안 실제 이벤트 수가
# 서울 8개 지역 전수 조사 결과 전부 200을 초과(395~655건)해서, distance_km 단독 정렬 +
# LIMIT 200이 relevance 계산 전에 이미 후보를 거리순으로만 잘라내고 있었음. 근본 대응은
# ORDER BY를 "아티스트/그룹 매칭 우선 -> distance_km"로 바꾸는 것(CANDIDATE_SEARCH_SQL
# 참고)이고, LIMIT 300은 그 대응과 별개로 얹는 여유분이다 — radius_km(10km, 고정)는
# 이번에도 안 건드림.
CANDIDATE_SEARCH_ROW_LIMIT = 300  # SQL LIMIT
CANDIDATE_SEARCH_DEFAULT_RADIUS_KM = 10.0
CANDIDATE_TOP_N_MIN = 40  # 재축소 하한 가이드(강제 아님 — 풀이 더 작으면 있는 만큼만 사용)
# 2026-09-10: 80 -> 20으로 하향. 이동시간 실 API 연동 후 요청 1건당 API 호출이 최대
# 153건까지 나와 일 900건 한도를 몇 번 만에 소진하는 문제가 있었음(2026-09-09 조사).
# 18개 시뮬레이션 트립(haversine 근사, API 호출 0건) 실측 결과 실제 채택된 이벤트가
# 전부 1~20등 안에서만 나와서(21등 이하 채택 0건) top_n=20 하향에 따른 품질 저하 위험은
# 낮다고 판단 — CANDIDATE_TOP_N_MIN(40)보다 작아졌으므로 그 하한 가이드는 사실상 무의미해짐
# (강제 아님이라 문제는 없으나, 다음에 재조정할 때 같이 검토할 것).
CANDIDATE_TOP_N_MAX = 20  # 재축소 상한(기본 top_n)
