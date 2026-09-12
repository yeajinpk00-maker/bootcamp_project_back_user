"""trip_route.usage_status_no를 매일 자정(KST)에 갱신하는 배치.

여행이 시작된(또는 이미 끝난) trip만 대상으로, 오늘이 여행 며칠차인지(day_index)를 계산해서
그날짜 이전 방문지는 진행완료(3), 그날짜 방문지는 진행중(2), 그 이후는 대기중(1)으로 맞춘다.
여행이 끝났으면(오늘 > end_dt) 전부 진행완료(3)로 처리한다.

같은 날짜 기준으로 몇 번을 다시 돌려도 결과가 같다(멱등) — 이미 목표 상태인 row는 건드리지 않는다.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import SessionLocal
from models import Trip, TripRoute
from rag_ingest import run_rag_ingestion

KST = ZoneInfo("Asia/Seoul")

WAITING_USAGE_STATUS_NO = 1  # 대기중
IN_PROGRESS_USAGE_STATUS_NO = 2  # 진행중
DONE_USAGE_STATUS_NO = 3  # 진행완료


def update_trip_route_statuses(db, today: date | None = None) -> int:
    """오늘(기본값: KST 기준 오늘) 기준으로 trip_route.usage_status_no를 갱신하고,
    실제로 값이 바뀐 row 수를 반환한다."""
    if today is None:
        today = datetime.now(KST).date()

    updated = 0
    trips = db.query(Trip).filter(Trip.start_dt <= today).all()

    for trip in trips:
        # 여행이 끝났으면(day_index=None) 전부 진행완료, 아니면 오늘이 며칠차인지 계산.
        day_index = None if today > trip.end_dt else (today - trip.start_dt).days + 1

        routes = db.query(TripRoute).filter(TripRoute.trip_no == trip.trip_no).all()
        for route in routes:
            if route.visit_day is None:
                continue  # 정상 플로우에선 발생하지 않지만, nullable 컬럼이라 방어적으로 스킵

            if day_index is None or route.visit_day < day_index:
                target = DONE_USAGE_STATUS_NO
            elif route.visit_day == day_index:
                target = IN_PROGRESS_USAGE_STATUS_NO
            else:
                target = WAITING_USAGE_STATUS_NO

            if route.usage_status_no != target:
                route.usage_status_no = target
                updated += 1

    db.commit()
    return updated


def run_daily_trip_route_update() -> None:
    db = SessionLocal()
    try:
        updated = update_trip_route_statuses(db)
        print(f"[trip_route batch] {datetime.now(KST).isoformat()} — {updated}건 갱신", flush=True)
    except Exception as e:
        db.rollback()
        print(f"[trip_route batch] 실패: {e}", flush=True)
        raise
    finally:
        db.close()


def run_daily_rag_ingestion() -> None:
    """FAN:GO 챗봇 RAG 지식베이스 인제스천 — 매일 03:00 KST(기능명세서 5.3절).
    실시간 반영이 아니라 하루 1회 배치이므로 이 스케줄러에 얹는다."""
    try:
        result = run_rag_ingestion()
        print(f"[rag ingest] {datetime.now(KST).isoformat()} — {result}", flush=True)
    except Exception as e:
        print(f"[rag ingest] 실패: {e}", flush=True)
        raise


_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    """앱 시작 시 한 번 호출. 이미 떠있으면 그대로 반환(재시작 방지)."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=KST)
    _scheduler.add_job(
        run_daily_trip_route_update,
        trigger=CronTrigger(hour=0, minute=0, timezone=KST),
        id="trip_route_daily_status_update",
        replace_existing=True,
    )
    _scheduler.add_job(
        run_daily_rag_ingestion,
        trigger=CronTrigger(hour=3, minute=0, timezone=KST),
        id="rag_daily_ingestion",
        replace_existing=True,
    )
    _scheduler.start()
    print("[trip_route batch] 스케줄러 시작 — 매일 00:00(Asia/Seoul)", flush=True)
    print("[rag ingest] 스케줄러 등록 — 매일 03:00(Asia/Seoul)", flush=True)
    return _scheduler
