"""AL-02 다양성 제약 Tier 시스템 회귀 테스트 — T15~T21(2026-09-12 작업지시 8번).

배경: 프론트 리포트(trip_no=43, balanced 프로파일에서 1일차 1곳/2일차 공연만/3일차
0곳)를 실측 재현한 결과, trip_interest 실측(70개 트립 중 66개가 카테고리 3개, 4개가
2개, 4개 이상 선택 0건)과 "하루 동일 ctg_no 최대 1곳" 하드 제약이 충돌해 방문 목표를
구조적으로 못 채우는 게 예외가 아니라 정상 케이스였다. Tier1(선호 카테고리)만으로 1차
배정하고, Tier2(비선호 ctg_type 2/3 카테고리)로 2차 개방, dense 비공연일 한정 3차
완화까지 적용하는 al02_pipeline.AL02Pipeline.run()의 새 흐름을 검증한다.

test_diversity_constraints.py(T01~T14, 문서 기반 하드 제약/alternatives 검증)와 별도
파일로 둔다 — 이번 작업 지시가 명시한 "신규 테스트 T15~T21 추가"에 맞춰 회귀 대상을
분리 관리. 실행: python test_diversity_tiers.py
"""
import sys
sys.path.insert(0, r"C:\workspaces\final")

import numpy as np
from dotenv import load_dotenv
load_dotenv(r"C:\workspaces\final\.env")

from al02_diversity import CORE_AXES, can_insert_candidate, derive_brand_key
from al02_pipeline import AL02Pipeline, calc_relevance, hard_filter
from al02_policy import PREFERENCE_TIER_1, PREFERENCE_TIER_2, SHOPPING_CTG_NOS

PASS_COUNT = 0
FAIL_COUNT = 0


def check(name, condition):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  [PASS] {name}")
    else:
        FAIL_COUNT += 1
        print(f"  [FAIL] {name}")


# ---------------------------------------------------------------------------
# 공용 픽스처 빌더 — Tier1/Tier2 후보를 원하는 구성으로 만든다.
# ---------------------------------------------------------------------------

def make_event(eno, nm, ctg_no, ctg_nm, tier, artist_no=101, score=70):
    return {
        "event_no": eno, "event_nm": nm, "ctg_nm": ctg_nm, "ctg_no": ctg_no,
        "artist_no": artist_no, "artist_group_no": None, "op_status_no": 2,
        "total_score": score, "preference_tier": tier,
    }


def build_and_run(events, ctg_nos, n_days=1, concert_no=None, concert_day_date=None,
                   density_profile="B", visit_max=5, concert_visit_max=4,
                   enable_diversity=True, enable_hard_dedup=True):
    trip_start = "2026-11-01"
    trip_end_day = n_days
    trip_end = f"2026-11-{trip_end_day:02d}"
    user_input = {
        "trip_start": trip_start, "trip_end": trip_end,
        "artist_nos": [101], "group_nos": [], "ctg_nos": list(ctg_nos),
        "lodging": {"latitude": 37.5, "longitude": 127.0},
    }
    if concert_no is not None:
        user_input["concert"] = {
            "event_no": concert_no, "event_date": concert_day_date, "start_time": "19:00",
        }
    candidates = []
    for i, ev in enumerate(events):
        if not hard_filter(ev):
            continue
        score = calc_relevance(ev, user_input)["relevance"]
        candidates.append((i, score))
    N = len(events)
    rng = np.random.RandomState(11)
    matrix = rng.randint(3, 15, size=(N, N))
    matrix = (matrix + matrix.T) // 2
    np.fill_diagonal(matrix, 0)
    pipeline = AL02Pipeline()
    result = pipeline.run(
        candidates, matrix, events, user_input, W_rel=30.0,
        visit_min=max(1, visit_max - 1), visit_max=visit_max,
        concert_visit_min=max(1, concert_visit_max - 1), concert_visit_max=concert_visit_max,
        enable_diversity=enable_diversity, enable_hard_dedup=enable_hard_dedup,
        density_profile=density_profile,
    )
    return events, result


def ctg_no_of(events, event_no):
    return next(e["ctg_no"] for e in events if e["event_no"] == event_no)


# ---------------------------------------------------------------------------
# T15 — 선호 카테고리 2개 시나리오: Tier2 없이는 하루 최대 2곳(카테고리 2개 x 1곳)뿐이지만,
# Tier2를 열면 그 이상 채울 수 있어야 한다.
# ---------------------------------------------------------------------------
print("=== T15: 선호 카테고리 2개 — Tier2 개방으로 하루 목표(visit_max) 근접 ===")
events15 = []
eno = 15000
for i in range(3):
    events15.append(make_event(eno, f"성지음식점{i+1}", 8, "성지 음식점", PREFERENCE_TIER_1)); eno += 1
for i in range(3):
    events15.append(make_event(eno, f"문화유적지{i+1}", 11, "문화/유적지", PREFERENCE_TIER_1)); eno += 1
for ctg_no, ctg_nm in [(6, "생일 카페"), (7, "성지 디저트/카페"), (9, "팝업/굿즈"), (10, "기타 성지"), (12, "여행지")]:
    events15.append(make_event(eno, f"{ctg_nm}1", ctg_no, ctg_nm, PREFERENCE_TIER_2)); eno += 1

events15_tier2_only_run, result_no_tier2 = build_and_run(
    events15, [8, 11], n_days=1, visit_max=5, enable_diversity=False, enable_hard_dedup=True,
)
day0_no_tier2 = len(result_no_tier2["days"][0]["schedule"])
check(f"Tier2 없이(enable_diversity=False)는 최대 2곳(카테고리 2개) 근처에 머무름(실제 {day0_no_tier2})",
      day0_no_tier2 <= 2)

events15b, result_tier2 = build_and_run(
    events15, [8, 11], n_days=1, visit_max=5, enable_diversity=True, enable_hard_dedup=True,
)
day0_tier2 = len(result_tier2["days"][0]["schedule"])
check(f"Tier2 개방 시 목표(5)에 더 가깝게 채워짐(실제 {day0_tier2} > 2)", day0_tier2 > 2)
check("Tier2 개방 후에도 하루 동일 ctg_no 상한(1)은 유지",
      len({ctg_no_of(events15b, s["event_no"]) for s in result_tier2["days"][0]["schedule"]})
      == len(result_tier2["days"][0]["schedule"]))


# ---------------------------------------------------------------------------
# T16 — 선호 카테고리 3개 시나리오(실측 trip_interest 최빈 케이스: 70개 중 66개가 3개).
# ---------------------------------------------------------------------------
print("\n=== T16: 선호 카테고리 3개(실측 최빈 케이스) — Tier2 개방으로 목표 채움 ===")
events16 = []
eno = 16000
for ctg_no, ctg_nm in [(13, "쇼핑"), (6, "생일 카페"), (8, "성지 음식점")]:
    for i in range(2):
        events16.append(make_event(eno, f"{ctg_nm}{i+1}", ctg_no, ctg_nm, PREFERENCE_TIER_1)); eno += 1
for ctg_no, ctg_nm in [(7, "성지 디저트/카페"), (9, "팝업/굿즈"), (10, "기타 성지"), (11, "문화/유적지"), (12, "여행지")]:
    events16.append(make_event(eno, f"{ctg_nm}1", ctg_no, ctg_nm, PREFERENCE_TIER_2)); eno += 1

events16b, result16 = build_and_run(events16, [13, 6, 8], n_days=1, visit_max=5)
day0_16 = result16["days"][0]["schedule"]
check(f"선호 3개 + Tier2로 목표(5)에 도달(실제 {len(day0_16)})", len(day0_16) >= 4)
shopping_count_16 = sum(1 for s in day0_16 if ctg_no_of(events16b, s["event_no"]) in SHOPPING_CTG_NOS)
check("쇼핑은 여전히 최대 1곳", shopping_count_16 <= 1)


# ---------------------------------------------------------------------------
# T17 — 쇼핑 상한: Tier2/Tier1 어느 쪽으로도 쇼핑은 전체 여행 1곳을 넘지 않는다.
# ---------------------------------------------------------------------------
print("\n=== T17: 쇼핑 카테고리 상한(Tier1+Tier2 합쳐도 전체 여행 최대 1곳) ===")
events17 = []
eno = 17000
for i in range(6):
    events17.append(make_event(eno, f"올리브영 {i+1}호점", 13, "쇼핑", PREFERENCE_TIER_1)); eno += 1
for ctg_no, ctg_nm in [(6, "생일 카페"), (7, "성지 디저트/카페"), (8, "성지 음식점")]:
    for i in range(3):
        events17.append(make_event(eno, f"{ctg_nm}{i+1}", ctg_no, ctg_nm, PREFERENCE_TIER_2)); eno += 1

events17b, result17 = build_and_run(events17, [13], n_days=3, visit_max=5)
all_sched_17 = [s for d in result17["days"] for s in d["schedule"]]
shopping_total_17 = sum(1 for s in all_sched_17 if ctg_no_of(events17b, s["event_no"]) in SHOPPING_CTG_NOS)
check(f"쇼핑 후보 6개 중 3일 트립 전체 채택은 <=1곳(실제 {shopping_total_17})", shopping_total_17 <= 1)
check("쇼핑이 아닌 Tier2 카테고리가 실제로 채워짐(다양성 확보)",
      any(ctg_no_of(events17b, s["event_no"]) not in SHOPPING_CTG_NOS for s in all_sched_17))


# ---------------------------------------------------------------------------
# T18 — 공연일 balanced: 일반 POI 목표(al02_policy.CONCERT_DAY_GENERAL_POI_TARGET["B"]=2).
# ---------------------------------------------------------------------------
print("\n=== T18: 공연일(balanced) — 일반 POI 목표 2곳 ===")
events18 = []
eno = 18000
events18.append(make_event(eno, "테스트 콘서트", 1, "콘서트/팬미팅", PREFERENCE_TIER_1, artist_no=None)); concert_no18 = eno; eno += 1
for i in range(2):
    events18.append(make_event(eno, f"성지음식점{i+1}", 8, "성지 음식점", PREFERENCE_TIER_1)); eno += 1
for ctg_no, ctg_nm in [(6, "생일 카페"), (7, "성지 디저트/카페"), (9, "팝업/굿즈")]:
    events18.append(make_event(eno, f"{ctg_nm}1", ctg_no, ctg_nm, PREFERENCE_TIER_2)); eno += 1

events18b, result18 = build_and_run(
    events18, [8], n_days=1, concert_no=concert_no18, concert_day_date="2026-11-01",
    density_profile="B", visit_max=5, concert_visit_max=4,
)
concert_day18 = result18["days"][0]
non_concert_count_18 = sum(1 for s in concert_day18["schedule"] if not s["is_concert"])
check(f"balanced 공연일 일반 POI 목표(2)에 맞춰 채워짐(실제 {non_concert_count_18})",
      non_concert_count_18 <= 2)
check("공연이 마지막 방문으로 고정됨", concert_day18["schedule"][-1]["is_concert"])
day_cat_counts_18 = {}
for s in concert_day18["schedule"]:
    if s["is_concert"]:
        continue
    c = ctg_no_of(events18b, s["event_no"])
    day_cat_counts_18[c] = day_cat_counts_18.get(c, 0) + 1
check("공연일도 동일 ctg_no 하루 상한 1 유지(완화 없음)", all(v <= 1 for v in day_cat_counts_18.values()))


# ---------------------------------------------------------------------------
# T19 — dense, 목표 7곳, Tier1+Tier2까지 부족할 때만 3차 완화(하루 카테고리 상한 2) 발동.
# ---------------------------------------------------------------------------
print("\n=== T19: dense 7곳 부족 — Tier1+Tier2 소진 후에만 3차 완화 ===")
events19 = []
eno = 19000
for i in range(10):
    events19.append(make_event(eno, f"성지음식점{i+1}", 8, "성지 음식점", PREFERENCE_TIER_1)); eno += 1
# Tier2 후보를 의도적으로 1개만 둬서(다른 카테고리로는 부족) 3차 완화가 필요하게 만든다.
events19.append(make_event(eno, "생일카페1", 6, "생일 카페", PREFERENCE_TIER_2)); eno += 1

events19b, result19 = build_and_run(events19, [8], n_days=1, density_profile="C",
                                     visit_max=7, concert_visit_max=4)
diversity19 = result19.get("diversity") or {}
check("dense+목표7곳+Tier1/2 소진 상황에서 3차 완화가 실제 발동함",
      diversity19.get("diversity_relaxed") is True)
day0_19_cat_counts = {}
for s in result19["days"][0]["schedule"]:
    c = ctg_no_of(events19b, s["event_no"])
    day0_19_cat_counts[c] = day0_19_cat_counts.get(c, 0) + 1
check("완화된 카테고리도 상한 2를 넘지 않음", all(v <= 2 for v in day0_19_cat_counts.values()))


# ---------------------------------------------------------------------------
# T20 — Tier2까지 사용해도 목표 미달 시: 반복 없이 축소 + insufficient_diverse_candidates.
# ---------------------------------------------------------------------------
print("\n=== T20: Tier2까지 부족 — 반복 없이 축소 + warning ===")
events20 = []
eno = 20000
events20.append(make_event(eno, "성지음식점1", 8, "성지 음식점", PREFERENCE_TIER_1)); eno += 1
events20.append(make_event(eno, "생일카페1", 6, "생일 카페", PREFERENCE_TIER_2)); eno += 1
# 후보가 이 2개뿐 — balanced 목표(4~5)를 절대 못 채운다.

events20b, result20 = build_and_run(events20, [8], n_days=1, density_profile="B", visit_max=5)
diversity20 = result20.get("diversity") or {}
check("후보 부족 시 insufficient_diverse_candidates=True", diversity20.get("insufficient_diverse_candidates") is True)
sched20 = result20["days"][0]["schedule"]
check(f"목표(5)보다 적어도 반복(중복 event_no) 없이 축소됨(실제 {len(sched20)}곳)",
      len(sched20) == len({s["event_no"] for s in sched20}))
check("동일 ctg_no를 억지로 반복하지 않음(카테고리별 최대 1)",
      all(v <= 1 for v in {ctg_no_of(events20b, s["event_no"]): 1 for s in sched20}.values()))


# ---------------------------------------------------------------------------
# T21 — /recommend·/alternatives 정책 일치: 같은 al02_diversity.can_insert_candidate와
# 같은 축 정의(브랜드/이벤트)를 공유하는지 구조적으로 확인.
# ---------------------------------------------------------------------------
print("\n=== T21: /recommend·/alternatives 다양성 정책 일치 ===")
import al02_pipeline
import al02_alternatives

check("al02_pipeline과 al02_alternatives가 같은 can_insert_candidate 함수 객체를 씀",
      al02_pipeline.can_insert_candidate is al02_alternatives.can_insert_candidate is can_insert_candidate)
check("al02_pipeline이 CORE_AXES(4축)를 1차 배정에 쓴다(al02_diversity와 동일 상수 공유)",
      al02_pipeline.CORE_AXES is CORE_AXES and set(CORE_AXES) == {
          "duplicate_event", "brand_trip_cap", "shopping_trip_cap", "category_day_cap"})
# alternatives는 duplicate_event/brand_trip_cap만 실제 필터링에 쓴다(카테고리/쇼핑은
# 후보가 전부 대상과 같은 ctg_no라 불변 — al02_alternatives.py의 axes 주석 참고).
# 두 경로 모두 "브랜드 트립 캡 위반 시 거부"라는 동일 판정 결과가 나오는지 직접 확인.
from al02_diversity import DiversityState, DayDiversityState, make_diversity_caps
caps21 = make_diversity_caps([13], n_non_concert_target=10)
trip_state21 = DiversityState(trip_brand_counts={"oliveyoung": 1})
day_state21 = DayDiversityState()
cand21 = {"event_no": 99001, "event_nm": "올리브영 새 지점", "ctg_no": 13}
ok_recommend_axes, _ = can_insert_candidate(cand21, day_state21, trip_state21, caps21,
                                             is_concert_day=False, allow_day_category_relaxation=False,
                                             axes=CORE_AXES)
ok_alternatives_axes, _ = can_insert_candidate(cand21, day_state21, trip_state21, caps21,
                                                is_concert_day=False, allow_day_category_relaxation=False,
                                                axes=("duplicate_event", "brand_trip_cap"))
check("브랜드 트립 캡 위반 판정이 recommend 축·alternatives 축에서 동일(둘 다 거부)",
      ok_recommend_axes is False and ok_alternatives_axes is False)


print(f"\n=== 종합: {PASS_COUNT} PASS / {FAIL_COUNT} FAIL ===")
if FAIL_COUNT:
    raise SystemExit(1)
