"""AL-02 파이프라인 자체 검증 스크립트 — AL02_pipeline_v4_final.ipynb 9번 셀(전체 테스트)을
그대로 옮긴 것. DB 연결 없이 합성 데이터로 돈다 (S0 SQL 자체는 al02_candidates.py 쪽,
실 DB 연결 확인은 이 스크립트가 아니라 별도로 진행).

실행: python al02_selftest.py
"""

import time

import numpy as np

from al02_pipeline import (
    AL02Pipeline,
    artist_match,
    build_selected_group_nos,
    calc_relevance,
    hard_filter,
    run_abc,
)
from al02_policy import STAY_MAP


def main():
    np.random.seed(42)
    N = 15
    ctg_list = list(STAY_MAP.keys())
    events = []
    for i in range(N):
        events.append({
            "event_no": 1000 + i, "event_nm": f"테스트 장소 {i+1}",
            "ctg_nm": ctg_list[i % len(ctg_list)],
            # v4 테스트 데이터: 3단 검증용
            #   i=0~4  (5개): 선택 멤버(101) 본인 개별 이벤트           -> 기대 1.0
            #   i=5~7  (3개): 그룹1 "전체" 태그 이벤트(artist_no 없음)  -> 기대 0.7 (폴백)
            #   i=8~10 (3개): 같은 그룹 다른 멤버(102) 개별 이벤트      -> 기대 0.0 (v4에서 정정된 부분)
            #   i=11~14(4개): 다른 그룹(999) 이벤트                     -> 기대 0.0
            "artist_no": (101 if i < 5 else (None if i < 8 else (102 if i < 11 else 999))),
            "artist_group_no": (1 if 5 <= i < 8 else None),
            "ctg_no": (i % 13) + 1, "op_status_no": 2,
            "total_score": 70 + (i * 2) % 30,
        })

    matrix = np.random.randint(5, 60, size=(N, N))
    matrix = (matrix + matrix.T) // 2
    np.fill_diagonal(matrix, 0)

    # 사용자는 "그룹 전체"가 아니라 "멤버 101만" 개별 선택(그룹 미선택, 화면 UX와 동일 패턴)
    user_input = {
        "trip_start": "2026-09-10", "trip_end": "2026-09-12",
        "artist_nos": [101], "group_nos": [], "ctg_nos": [5, 8, 11],
        "concert": {"event_no": 1014, "event_date": "2026-09-11", "start_time": "19:00"},
    }

    # S0 단계에서 DB(artist 테이블)로 미리 조회해뒀다고 가정하는 매핑.
    # 101,102는 그룹1(선택 멤버 101과 같은 그룹) 소속, 999는 그룹2(무관) 소속.
    artist_group_map = {101: 1, 102: 1, 999: 2}

    candidates = []
    for i, ev in enumerate(events):
        if not hard_filter(ev):
            continue
        score = calc_relevance(ev, user_input, artist_group_map=artist_group_map)
        candidates.append((i, score["relevance"]))

    print(f"후보: {len(candidates)}개")
    print("\n[v4 검증] 선택멤버 우선(1.0) / 그룹전체 폴백(0.7) / 다른개별멤버 무관(0.0) 3단 확인:")
    selected_group_nos = build_selected_group_nos([], [101], artist_group_map)
    all_ok = True
    expected = [1.0] * 5 + [0.7] * 3 + [0.0] * 3
    for i, ev in enumerate(events[:11]):
        am = artist_match(ev["artist_no"], ev["artist_group_no"], [101], selected_group_nos)
        ok = (am == expected[i])
        all_ok = all_ok and ok
        mark = "OK" if ok else "FAIL"
        print(f"  [{mark}] event_no={ev['event_no']} artist_no={ev['artist_no']} "
              f"artist_group_no={ev['artist_group_no']} -> artist_match={am} (기대 {expected[i]})")
    print(f"  3단 검증 전체: {'PASS' if all_ok else 'FAIL'}")

    engine = AL02Pipeline()
    t0 = time.perf_counter()
    result = engine.run(candidates, matrix, events, user_input)
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"\n실행 시간: {elapsed:.0f}ms (목표 3초 이하)")
    print(f"검증: {'PASS' if result['summary']['validation_passed'] else 'FAIL'}")
    print(f"총 이동: {result['summary']['total_travel_minutes']}분")
    print(f"총 방문: {result['summary']['total_places']}곳")

    for day in result["days"]:
        is_c = " [공연일]" if day["is_concert_day"] else ""
        print(f"\n  -- Day {day['day_index']+1} ({day['date']}){is_c} --")
        print(f"     {day['n_places']}곳 | 이동 {day['travel_minutes']}분")
        for s in day["schedule"]:
            mark = " *" if s["is_concert"] else "  "
            print(f"     {mark} {s['arrive']}~{s['depart']} ({s['stay_min']:3d}분) "
                  f"relevance={s.get('relevance', 0.0):.2f}  {s['event_nm']}")

    print("\n검증 상세:")
    for check, passed in result["summary"]["validation_details"].items():
        icon = "OK" if passed else "FAIL"
        print(f"  [{icon}] {check}")

    print("\n\n=== A/B/C 동선 비교 (run_abc) ===")
    abc = run_abc(engine, candidates, matrix, events, user_input)
    for key, v in abc.items():
        print(f"  [{key}] {v['label']:16s} W_rel={v['W_rel']:3d}  "
              f"장소 {v['total_places']}곳  이동 {v['total_travel_minutes']}분  "
              f"total_relevance={v['total_relevance']}  avg_relevance={v['avg_relevance']}")

    overall_pass = all_ok and result["summary"]["validation_passed"]
    print(f"\n=== 종합 결과: {'PASS' if overall_pass else 'FAIL'} ===")
    return overall_pass


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
