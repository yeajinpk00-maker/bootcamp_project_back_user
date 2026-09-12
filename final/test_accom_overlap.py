"""숙소(accoms) 겹침 판정 회귀 테스트 — 2026-09-11 버그 리포트(반열린 구간 수정) 대상.

DB 연결 없이 순수 함수만 검증한다(al02_selftest.py와 같은 스타일: pytest 없이
assert + PASS/FAIL 출력, `python test_accom_overlap.py`로 실행).

검증 대상:
  1) al02_pipeline.accoms_overlap()      — auth.py POST /trips 쓰기 시점 겹침 검증이 쓰는 함수
  2) al02_pipeline.pick_depot_accom()    — recommend 계산 시점 날짜별 숙소 소속 판정
     (accoms_overlap()과 반드시 같은 반열린 규칙으로 움직여야 함 — 두 함수가 따로
     놀면 "체크아웃일=체크인일" 데이터가 생성은 통과하고 recommend에서 뒤늦게 터진다)
"""

from datetime import date

from al02_pipeline import DepotOverlapError, accoms_overlap, pick_depot_accom

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


def expect_raises(name, fn, exc_type):
    global PASS_COUNT, FAIL_COUNT
    try:
        fn()
    except exc_type:
        PASS_COUNT += 1
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL_COUNT += 1
        print(f"  [FAIL] {name} (다른 예외 발생: {type(e).__name__}: {e})")
    else:
        FAIL_COUNT += 1
        print(f"  [FAIL] {name} ({exc_type.__name__} 발생 안 함)")


d = date


# ---------------------------------------------------------------------------
# 1) accoms_overlap() — 겹침 판정 자체
# ---------------------------------------------------------------------------
print("=== 1) accoms_overlap() ===")

# 버그 리포트 원 사례: A 09-13~09-17 체크아웃, B 09-17 체크인~09-20
check(
    "경계 케이스: A 체크아웃일 == B 체크인일 -> 겹침 아님 (버그 리포트 원 사례)",
    accoms_overlap(d(2026, 9, 13), d(2026, 9, 17), d(2026, 9, 17), d(2026, 9, 20)) is False,
)
check(
    "경계 케이스(인자 순서 반대): B/A 바꿔 호출해도 대칭적으로 겹침 아님",
    accoms_overlap(d(2026, 9, 17), d(2026, 9, 20), d(2026, 9, 13), d(2026, 9, 17)) is False,
)

# 실제 겹침: B가 A의 체크아웃 하루 전에 체크인 (하루 공유)
check(
    "실제 겹침 케이스: B 체크인이 A 체크아웃보다 하루 이름(1일 공유) -> 겹침",
    accoms_overlap(d(2026, 9, 13), d(2026, 9, 17), d(2026, 9, 16), d(2026, 9, 20)) is True,
)
# 완전 포함(A가 B를 통째로 감쌈)
check(
    "실제 겹침 케이스: A 기간이 B를 완전히 포함 -> 겹침",
    accoms_overlap(d(2026, 9, 10), d(2026, 9, 20), d(2026, 9, 13), d(2026, 9, 17)) is True,
)

# 완전 분리: 사이에 빈 날짜(gap)가 있음
check(
    "완전 분리 케이스: 체크아웃과 다음 체크인 사이에 빈 날짜 있음 -> 겹침 아님",
    accoms_overlap(d(2026, 9, 13), d(2026, 9, 15), d(2026, 9, 17), d(2026, 9, 20)) is False,
)
# 완전 분리: 아예 멀리 떨어진 기간
check(
    "완전 분리 케이스: 서로 겹칠 여지가 없는 별개 기간 -> 겹침 아님",
    accoms_overlap(d(2026, 9, 1), d(2026, 9, 3), d(2026, 10, 1), d(2026, 10, 5)) is False,
)

# check_in_dt == check_out_dt(0박, 당일치기)인 두 숙소가 같은 날짜라도, 반열린 구간
# [check_in, check_out)은 길이 0인 빈 구간이라 수학적으로 그 무엇과도 겹치지 않는다
# (이 0박 케이스 자체를 허용할지는 이번 버그 리포트 범위 밖 — 여기선 반열린 정의를
# 일관되게 적용했을 때의 결과만 확인).
check(
    "0박(check_in_dt==check_out_dt) 두 숙소가 같은 날짜여도 빈 구간이라 겹침 아님",
    accoms_overlap(d(2026, 9, 15), d(2026, 9, 15), d(2026, 9, 15), d(2026, 9, 15)) is False,
)


# ---------------------------------------------------------------------------
# 2) pick_depot_accom() — accoms_overlap()과 같은 반열린 규칙으로 동작해야 함
# ---------------------------------------------------------------------------
print("\n=== 2) pick_depot_accom() (accoms_overlap()과 규칙 일치 확인) ===")

accom_a = {"accom_no": 1, "check_in_dt": "2026-09-13", "check_out_dt": "2026-09-17"}
accom_b = {"accom_no": 2, "check_in_dt": "2026-09-17", "check_out_dt": "2026-09-20"}
transition_accoms = [accom_a, accom_b]

check(
    "전환일(2026-09-17, A 체크아웃=B 체크인) 이전 날짜는 A",
    pick_depot_accom("2026-09-15", transition_accoms)["accom_no"] == 1,
)
check(
    "전환일(2026-09-17) 당일은 B(체크인하는 쪽)를 depot으로 선택 — DepotOverlapError 안 남",
    pick_depot_accom("2026-09-17", transition_accoms)["accom_no"] == 2,
)
check(
    "전환일 다음 날짜(2026-09-18)는 B",
    pick_depot_accom("2026-09-18", transition_accoms)["accom_no"] == 2,
)

# 마지막 날 폴백: 이 accom 이후 다음 숙소가 아예 없는 경우, 체크아웃 당일도 그 숙소로 폴백되어야 함
single_accom = [{"accom_no": 1, "check_in_dt": "2026-09-13", "check_out_dt": "2026-09-17"}]
check(
    "숙소가 하나뿐이고 그 체크아웃 당일(뒤에 다음 숙소 없음)도 폴백으로 그 숙소 선택",
    pick_depot_accom("2026-09-17", single_accom)["accom_no"] == 1,
)

# 실제 겹침(진짜 겹침 데이터, accoms_overlap()이라면 True로 걸러졌어야 할 케이스)이
# pick_depot_accom()에도 그대로 들어오면 DepotOverlapError로 명확히 실패해야 한다
overlapping_accoms = [
    {"accom_no": 1, "check_in_dt": "2026-09-10", "check_out_dt": "2026-09-17"},
    {"accom_no": 2, "check_in_dt": "2026-09-13", "check_out_dt": "2026-09-20"},
]
expect_raises(
    "진짜 겹침 데이터(레거시)가 들어오면 DepotOverlapError 발생(하나를 임의로 고르지 않음)",
    lambda: pick_depot_accom("2026-09-14", overlapping_accoms),
    DepotOverlapError,
)


print(f"\n=== 결과: {PASS_COUNT} PASS / {FAIL_COUNT} FAIL ===")
if FAIL_COUNT:
    raise SystemExit(1)
