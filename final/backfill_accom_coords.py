"""1회성 스크립트 — accom.add(주소)로 카카오 지오코딩 재조회해서 accom_lat/accom_lon을
복구한다. DECIMAL(10,8) 오버플로우 버그(migration_accom_lon_precision.sql 참고)로 경도가
99.99999999 -> (컬럼 확장 마이그레이션의 반올림으로) 100.000000이 되어버린 기존 행 대상.

⚠ 서버 상시 로직이 아니다. auth.py/al02_*.py 등 앱 코드에서 이 스크립트나 지오코딩 API를
import/호출하지 않는다("서버는 지오코딩을 하지 않는다" 기존 정책 유지) — 딱 이번 데이터
복구 1회만 수동 실행하는 용도.

사용법:
  1) .env에 KAKAO_REST_API_KEY=<카카오 REST API 키> 추가
  2) python backfill_accom_coords.py            # dry-run(기본) — API 조회만 하고 DB는 안 건드림
  3) python backfill_accom_coords.py --apply     # 실제로 UPDATE까지 실행

동작:
  - accom_lon = 100.000000인 행만 대상으로 삼는다(이 버그로 유실된 행의 표식 — 정상적인
    경도값이 정확히 100.000000일 일은 없다고 봐도 된다. accom_lonlat_backfill_backup_20260909.json에
    저장해 둔 13행 백업과 대조해서 개수가 다르면 경고만 하고 계속 진행).
  - 카카오 주소 검색 API로 add(주소)를 좌표로 재조회.
  - 새로 받아온 위도가 기존 accom_lat(이 버그의 영향을 안 받아 원래부터 정상이던 값)과
    너무 다르면(0.05도, 대략 5.5km 초과) 주소-좌표 매칭이 잘못됐을 가능성이 있다고 보고
    그 행은 건너뛴다(수동 확인 필요 목록에 남김) — 절대 의심스러운 값으로 덮어쓰지 않는다.
  - 통과한 행만 accom_lat/accom_lon을 갱신하고, 그 외 컬럼은 손대지 않는다.
  - 실행 결과(성공/스킵/실패, 이전 값 -> 이후 값)를 accom_backfill_result_<타임스탬프>.json로 저장.
"""

import json
import os
import sys
from datetime import datetime
from decimal import Decimal

import requests
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from database import SessionLocal  # noqa: E402

KAKAO_GEOCODE_URL = "https://dapi.kakao.com/v2/local/search/address.json"
BUGGED_SENTINEL_LON = Decimal("100.000000")
LAT_SANITY_THRESHOLD = Decimal("0.05")  # 약 5.5km — 이보다 차이나면 자동 적용 안 하고 스킵


def geocode_address(api_key: str, address: str) -> tuple[Decimal, Decimal] | None:
    """카카오 주소 검색 API. 반환: (lat, lon) 또는 매칭 실패 시 None."""
    resp = requests.get(
        KAKAO_GEOCODE_URL,
        headers={"Authorization": f"KakaoAK {api_key}"},
        params={"query": address},
        timeout=10,
    )
    resp.raise_for_status()
    docs = resp.json().get("documents", [])
    if not docs:
        return None
    doc = docs[0]
    return Decimal(doc["y"]), Decimal(doc["x"])  # y=위도, x=경도


def main():
    apply = "--apply" in sys.argv
    api_key = os.getenv("KAKAO_REST_API_KEY")
    if not api_key:
        print("KAKAO_REST_API_KEY가 .env에 없습니다. 카카오 개발자 콘솔에서 REST API 키를 "
              "발급받아 .env에 추가한 뒤 다시 실행하세요.")
        sys.exit(1)

    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT accom_no, trip_no, accom_nm, `add`, accom_lat, accom_lon "
                 "FROM accom WHERE accom_lon = :sentinel ORDER BY accom_no"),
            {"sentinel": BUGGED_SENTINEL_LON},
        ).mappings().all()

        print(f"대상 행 {len(rows)}개 (백업 파일 기준 예상: 13개)")
        if len(rows) != 13:
            print("⚠ 예상(13)과 다릅니다 — accom_lonlat_backfill_backup_20260909.json과 대조해서 "
                  "새로 생긴 문제 행인지 확인하세요. 그대로 진행은 합니다.")

        results = []
        for row in rows:
            old_lat, old_lon = row["accom_lat"], row["accom_lon"]
            try:
                geocoded = geocode_address(api_key, row["add"])
            except Exception as e:
                results.append({**dict(row), "status": "error", "error": str(e)})
                print(f"[ERROR] accom_no={row['accom_no']} 지오코딩 실패: {e}")
                continue

            if geocoded is None:
                results.append({**dict(row), "status": "no_match"})
                print(f"[NO_MATCH] accom_no={row['accom_no']} add='{row['add']}' — 검색 결과 없음")
                continue

            new_lat, new_lon = geocoded
            if old_lat is not None and abs(new_lat - old_lat) > LAT_SANITY_THRESHOLD:
                results.append({
                    **dict(row), "status": "skipped_sanity_check",
                    "geocoded_lat": str(new_lat), "geocoded_lon": str(new_lon),
                })
                print(f"[SKIP] accom_no={row['accom_no']} 기존 accom_lat({old_lat})과 지오코딩 결과"
                      f"({new_lat}) 차이가 너무 큼 — 수동 확인 필요, 자동 적용 안 함")
                continue

            if apply:
                db.execute(
                    text("UPDATE accom SET accom_lat=:lat, accom_lon=:lon WHERE accom_no=:no"),
                    {"lat": new_lat, "lon": new_lon, "no": row["accom_no"]},
                )
            results.append({
                **dict(row), "status": "updated" if apply else "dry_run_would_update",
                "accom_lat_before": str(old_lat), "accom_lon_before": str(old_lon),
                "accom_lat_after": str(new_lat), "accom_lon_after": str(new_lon),
            })
            print(f"[{'UPDATED' if apply else 'DRY-RUN'}] accom_no={row['accom_no']} "
                  f"'{row['add']}' -> lat={new_lat} lon={new_lon}")

        if apply:
            db.commit()
            print("\n커밋 완료.")
        else:
            print("\ndry-run — DB는 변경하지 않았습니다. 결과 확인 후 --apply로 재실행하세요.")

        out_path = f"accom_backfill_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"apply": apply, "results": results}, f, ensure_ascii=False, indent=2, default=str)
        print(f"결과 저장: {out_path}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
