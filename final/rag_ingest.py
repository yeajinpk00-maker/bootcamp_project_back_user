"""FAN:GO 챗봇 RAG 지식베이스 인제스천 파이프라인 (기능명세서 4.2·5절).

원천 데이터:
  - event.event_desc / event.event_dtl — 장소 설명(공식/큐레이션 텍스트) → evidence_tier="A" 고정.
    (명세서 5.1절은 모든 청크에 evidence_tier를 요구하지만, 이 원천은 리뷰가 아니라
    FAN:GO가 직접 정리한 장소 정보라 total_score 게이트 대상이 아니다. 리뷰처럼 품질이
    들쭉날쭉한 사용자 생성 콘텐츠가 아니므로 A로 고정하는 게 합리적이라고 판단했다 —
    이 판단은 명세서에 명시돼 있지 않으니 확인 부탁.)
  - external_review.review — 리뷰 원문 → evidence_tier는 total_score 기준 A/B/C 게이트(5.2절) 통과분만.
  - congestion (2026-09-10 신규) — 혼잡도. get_congestion_pattern Function Calling을 완전히
    대체하기로 팀 결정, 이제는 이 테이블 데이터를 자연어 문서로 변환해 RAG 경유로만 답한다
    (아래 build_congestion_chunks 참고). event_desc와 마찬가지로 사용자 생성 콘텐츠가 아니라
    통계 집계값이라 evidence_tier="A" 고정.

이 저장소엔 AL-02(동선 최적화 엔진) 코드가 없어(grep 확인함, 다른 팀/모듈 소관) 등급 임계값을
문서 5.2절 그대로 재구현했다 — 새로 설계한 게 아니라 명세서에 이미 확정된 임계값을 옮긴 것.

배치 스케줄(매일 03:00 KST)은 batch.py의 기존 APScheduler 인스턴스에 얹는다(별도 스케줄러 안 만듦).
"""

from __future__ import annotations

from itertools import groupby
from typing import Iterable

import chromadb
from sentence_transformers import SentenceTransformer

from database import SessionLocal
from models import Artist, Congestion, Ctg, Event, ExternalReview

EMBEDDING_MODEL_NAME = "snunlp/KR-SBERT-V40K-klueNLI-augSTS"
CHROMA_PERSIST_DIR = "./rag_store/chroma"
COLLECTION_NAME = "fango_knowledge_base"

# 5.2절 — AL-02 등급 정책(4.5 이상=A, 3.5~4.5 미만=B, 3.5 미만/NULL=C). C는 적재 제외.
GRADE_A_THRESHOLD = 4.5
GRADE_B_THRESHOLD = 3.5

# congestion → 자연어 변환용 라벨(db_data_dictionary.md의 cong_level 정의와 동일: 0한산 1보통
# 2혼잡 3매우혼잡). WEEKDAY_LABELS_KR은 congestion.weekday('1'~'7', ISO-8601 월=1) 기준.
CONGESTION_LEVEL_LABELS = {0: "한산", 1: "보통", 2: "혼잡", 3: "매우혼잡"}
WEEKDAY_LABELS_KR = {
    "1": "월요일", "2": "화요일", "3": "수요일", "4": "목요일",
    "5": "금요일", "6": "토요일", "7": "일요일",
}

_embedder: SentenceTransformer | None = None
_chroma_client = None


def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _embedder


def get_chroma_collection():
    """코사인 유사도 검색을 위해 hnsw:space=cosine으로 명시 생성(기본값은 l2라 반드시 지정 필요)."""
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    return _chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def evidence_tier_from_total_score(total_score) -> str | None:
    """5.2절 등급 게이트. None(C등급)이면 적재하지 않는다."""
    if total_score is None:
        return None
    score = float(total_score)
    if score >= GRADE_A_THRESHOLD:
        return "A"
    if score >= GRADE_B_THRESHOLD:
        return "B"
    return None


def _group_member_artist_nos(db, artist_group_no: int) -> list[int]:
    rows = db.query(Artist.artist_no).filter(Artist.artist_group_no == artist_group_no).all()
    return [r.artist_no for r in rows]


def _artist_ids_for_event(db, event: Event, _group_cache: dict[int, list[int]]) -> list[int]:
    """이벤트가 걸린 아티스트 범위 — 개별 멤버 지정이면 그 1명, 그룹 지정이면 그룹 멤버 전원."""
    if event.artist_no is not None:
        return [event.artist_no]
    if event.artist_group_no is not None:
        if event.artist_group_no not in _group_cache:
            _group_cache[event.artist_group_no] = _group_member_artist_nos(db, event.artist_group_no)
        return _group_cache[event.artist_group_no]
    return []


def _event_chunk_text(event: Event) -> str | None:
    parts = [p for p in (event.event_desc, event.event_dtl) if p and p.strip()]
    if not parts:
        return None
    return "\n\n".join(parts)


def _format_hour_kr(h: int) -> str:
    """0~23 → '오전/오후 N시(H시)' — 사용자가 '오후 3시'처럼 말하는 표현과 '15시' 같은
    24시간제 표현 둘 다를 문서 텍스트에 심어서, 어느 쪽으로 질문해도 임베딩 유사도로
    걸릴 확률을 높인다."""
    ampm = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    return f"{ampm} {h12}시({h}시)"


def _format_hour_range_kr(start: int, end: int) -> str:
    if start == end:
        return _format_hour_kr(start)
    return f"{_format_hour_kr(start)}~{_format_hour_kr(end)}"


def _format_hour_range_narrative(start: int, end: int) -> str:
    """'오전 11시부터 오후 2시까지' — _format_hour_range_kr과 달리 24시간제 괄호 표기를
    안 붙인다. 실측(완료 후 보고 참고): 괄호 표기(예: '오전 11시(11시)~오후 2시(14시)')를
    끼워 넣으면 숫자가 반복돼 문장이 목록처럼 읽히면서 코사인 유사도가 오히려 떨어졌다
    (0.40대→0.35대) — 서술형 문장(_congestion_day_text)에서만 이 포맷을 쓰고, 24시간제
    표기가 유용한 다른 곳(_congestion_overall_text의 단일 시각 언급)엔 그대로 둔다."""
    def ap(h: int) -> str:
        ampm = "오전" if h < 12 else "오후"
        h12 = h % 12 or 12
        return f"{ampm} {h12}시"
    if start == end:
        return ap(start)
    return f"{ap(start)}부터 {ap(end)}까지"


def _weekday_tagged_kr(weekday: str) -> str:
    """'화요일(평일)'/'토요일(주말)' — "주말 저녁 붐벼요?" 같은 질문의 '주말'이라는
    단어가 문서 텍스트 안에도 그대로 등장하게 해서 임베딩 유사도를 끌어올린다."""
    base = WEEKDAY_LABELS_KR.get(weekday, f"{weekday}요일")
    tag = "주말" if weekday in ("6", "7") else "평일"
    return f"{base}({tag})"


def _run_length_segments(hourly_levels: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """[(hour, level), ...] → 연속된 동일 레벨을 [(start_hour, end_hour, level), ...]로 묶는다."""
    segments = []
    for level, group in groupby(hourly_levels, key=lambda x: x[1]):
        hours = [h for h, _ in group]
        segments.append((hours[0], hours[-1], level))
    return segments


def _congestion_level_phrase(level: int, branch: str) -> str:
    """레벨(0~3)과 문장 내 역할(branch)에 맞는 자연스러운 서술어를 고른다.
    '보통'은 '보통하다/보통해집니다'가 성립하지 않는 한국어라 레벨 1만 별도 처리한다."""
    if level == 1:
        return {
            "first": "보통 수준이다가", "peak": "가장 붐비는 보통 수준입니다",
            "last_repeat": "다시 보통 수준으로 돌아갑니다", "last": "보통 수준입니다",
            "solo": "하루 종일 보통 수준입니다", "mid": "보통 수준이고",
        }[branch]
    adj = {0: "한산", 2: "혼잡", 3: "매우 혼잡"}[level]
    return {
        "first": f"{adj}하다가", "peak": f"가장 {adj}합니다",
        "last_repeat": f"다시 {adj}해집니다", "last": f"{adj}해집니다",
        "solo": f"하루 종일 {adj}한 편입니다", "mid": f"{adj} 수준이고",
    }[branch]


def _congestion_day_text(event_nm: str, weekday: str, hourly_levels: list[tuple[int, int]]) -> str:
    """hourly_levels: [(hour_of_day, cong_level), ...] 24개, hour_of_day 오름차순.

    2026-09-10 실측(완료 후 보고 참고): 처음엔 '몇 시~몇 시는 한산, 몇 시~몇 시는 보통, ...'
    식으로 구간을 나열만 했는데, 이 형태는 event_no 필터로 정확한 문서까지 좁혀놓고도
    실제 질문("화요일 오후 3시에 얼마나 붐벼요?")과의 코사인 유사도가 0.34~0.37 안팎으로
    SIMILARITY_THRESHOLD(0.45)를 넘지 못해 검색 결과가 0건이 되는 걸 확인했다 — 나열형
    문장은 어휘가 단조롭고 질문에 실제 쓰이는 동사('붐비다', '얼마나')와 겹치지 않았던
    것으로 보인다(임계값 자체는 그대로 두고 텍스트 쪽을 개선했다). 최고 혼잡 구간을
    "가장 혼잡합니다"로 짚어주고, 처음 봤던 레벨이 다시 나오면 "다시 한산해집니다"처럼
    자연스러운 흐름으로 이어붙이는 서술형으로 바꾸고, 24시간제 괄호 병기와 "언제
    붐비고 언제 한산할까요?" 같은 중복 서두를 걷어내자 유사도가 0.5대로 올라 임계값을
    넉넉히 넘기는 것을 확인(중간 과정의 시행착오 포함 완료 후 보고 참고)."""
    segments = _run_length_segments(hourly_levels)
    weekday_tagged = _weekday_tagged_kr(weekday)
    weekday_kr = weekday_tagged.split("(")[0]

    if len(segments) == 1:
        _, _, lv = segments[0]
        body = _congestion_level_phrase(lv, "solo")
    else:
        max_level = max(lv for _, _, lv in segments)
        seen_levels: set[int] = set()
        parts = []
        for i, (s, e, lv) in enumerate(segments):
            is_last = i == len(segments) - 1
            if i == 0:
                branch = "first"
            elif lv == max_level and lv != 0:
                branch = "peak"
            elif is_last:
                branch = "last_repeat" if lv in seen_levels else "last"
            else:
                branch = "mid"
            joiner = "에는" if branch in ("first", "mid") else "에"
            parts.append(f"{_format_hour_range_narrative(s, e)}{joiner} {_congestion_level_phrase(lv, branch)}")
            seen_levels.add(lv)
        body = " ".join(parts)

    return f"{event_nm}의 {weekday_tagged} 붐빔 정도: {body} 실시간이 아닌 {weekday_kr} 평균 통계입니다."


def _congestion_overall_text(event_nm: str, all_rows: list[tuple[str, int, int]]) -> str:
    """all_rows: [(weekday, hour_of_day, cong_level), ...] 그 event_no의 전체 168행(있는 만큼).
    요일을 특정하지 않은 질문("아원고택 언제 붐벼요?")에 답하기 위한 event_no당 1개짜리
    주간 전체 요약 문서 — 기존 get_congestion_pattern이 weekday/hour_of_day를 둘 다
    안 받았을 때 반환하던 '가장 붐비는/한산한 슬롯 + 요일별 평균' 요약과 같은 역할을
    RAG 쪽에서 대신한다."""
    if not all_rows:
        return ""
    busiest = sorted(all_rows, key=lambda r: r[2], reverse=True)[0]
    quietest = sorted(all_rows, key=lambda r: r[2])[0]

    by_weekday: dict[str, list[int]] = {}
    for wd, _, lv in all_rows:
        by_weekday.setdefault(wd, []).append(lv)
    weekday_avg = {wd: sum(lvs) / len(lvs) for wd, lvs in by_weekday.items()}
    weekend_avg = sum(weekday_avg.get(wd, 0) for wd in ("6", "7")) / 2 if weekday_avg else 0
    weekday_only_avg = (
        sum(weekday_avg.get(wd, 0) for wd in ("1", "2", "3", "4", "5")) / 5 if weekday_avg else 0
    )
    weekend_vs_weekday = (
        "주말이 평일보다 더 붐비는 편" if weekend_avg > weekday_only_avg
        else "평일이 주말보다 더 붐비는 편" if weekday_only_avg > weekend_avg
        else "평일과 주말의 붐비는 정도가 비슷한 편"
    )

    busiest_wd, busiest_h, busiest_lv = busiest
    quietest_wd, quietest_h, quietest_lv = quietest
    return (
        f"{event_nm} 전체 요일 혼잡도 요약 — 언제 가장 붐비고 언제 가장 한산할까요? "
        f"{event_nm}은(는) {WEEKDAY_LABELS_KR.get(busiest_wd, busiest_wd)} {_format_hour_kr(busiest_h)}쯤에 "
        f"{CONGESTION_LEVEL_LABELS.get(busiest_lv, '정보없음')} 수준으로 가장 붐비고, "
        f"{WEEKDAY_LABELS_KR.get(quietest_wd, quietest_wd)} {_format_hour_kr(quietest_h)}쯤이 "
        f"{CONGESTION_LEVEL_LABELS.get(quietest_lv, '정보없음')} 수준으로 가장 한산합니다. "
        f"전반적으로 {weekend_vs_weekday}입니다. "
        f"이 정보는 실시간 혼잡도가 아니라 요일·시간대별 평균 통계입니다."
    )


def build_congestion_chunks(db, event_by_no: dict[int, Event], group_cache: dict[int, list[int]]):
    """congestion 테이블을 (event_no, weekday) 단위 문서로 변환한다(2026-09-10 신규 —
    get_congestion_pattern Function Calling 완전 대체, 팀 결정).

    event_no 하나로 묶지 않고 event_no × weekday로 나눈 이유: 168개(7일×24시간) 슬롯을
    한 문서에 다 넣으면 임베딩이 요일 하나에 집중된 질문("화요일 오후 3시 어때요")과
    거리가 멀어진다 — 요일별로 쪼개면 각 문서가 그 요일 얘기만 하게 되어, 질문에 담긴
    요일 표현과의 의미적 유사도가 더 또렷해진다(실측은 완료 후 보고 참고). 문서 수는
    event당 최대 8개(요일별 최대 7개 + 요일 미지정 질문용 전체 요약 1개)로, event_desc
    1개짜리 문서보다는 늘지만 168개짜리보다는 훨씬 적다.

    메타데이터는 기존 event_desc/리뷰 청크와 동일한 필터링 패턴(poi_id=event_no)을
    그대로 재사용하고, weekday는 추가 정보로만 얹는다 — extract_metadata_filters()가
    요일까지 추출해 where 절에 넣도록 확장하는 건 이번 범위에 넣지 않았다(완료 후
    보고에서 별도로 안내)."""
    rows = (
        db.query(Congestion.event_no, Congestion.weekday, Congestion.hour_of_day, Congestion.cong_level)
        .order_by(Congestion.event_no, Congestion.weekday, Congestion.hour_of_day)
        .all()
    )

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for event_no, day_rows in groupby(rows, key=lambda r: r.event_no):
        event = event_by_no.get(event_no)
        if event is None:
            continue  # FK상 이론적으로 없어야 하지만 방어적으로 스킵
        artist_ids = _artist_ids_for_event(db, event, group_cache)
        all_rows_for_event: list[tuple[str, int, int]] = []

        for weekday, weekday_rows in groupby(day_rows, key=lambda r: r.weekday):
            weekday_rows = list(weekday_rows)
            hourly_levels = [(r.hour_of_day, r.cong_level or 0) for r in weekday_rows]
            all_rows_for_event.extend((weekday, r.hour_of_day, r.cong_level or 0) for r in weekday_rows)
            if not hourly_levels:
                continue
            text = _congestion_day_text(event.event_nm, weekday, hourly_levels)
            ids.append(f"congestion:{event_no}:{weekday}")
            documents.append(text)
            metadatas.append(
                {
                    "source": "congestion",
                    "poi_id": event_no,
                    "ctg_no": event.ctg_no,
                    "artist_ids": ",".join(str(a) for a in artist_ids),
                    "evidence_tier": "A",
                    "weekday": weekday,
                }
            )

        # 요일을 특정하지 않은 질문("아원고택 언제 붐벼요?")용 event_no당 1개짜리 주간 요약
        # 문서(2026-09-10 추가) — 위 7개 요일별 문서만으로는 이런 질문에 어느 한 요일
        # 문서가 특별히 더 유사하다고 보기 어려워(실측 확인), 옛 get_congestion_pattern의
        # "weekday/hour_of_day 둘 다 없으면 최번/한산 슬롯+요일평균 요약" 동작을 대신한다.
        overall_text = _congestion_overall_text(event.event_nm, all_rows_for_event)
        if overall_text:
            ids.append(f"congestion:{event_no}:overall")
            documents.append(overall_text)
            metadatas.append(
                {
                    "source": "congestion",
                    "poi_id": event_no,
                    "ctg_no": event.ctg_no,
                    "artist_ids": ",".join(str(a) for a in artist_ids),
                    "evidence_tier": "A",
                    "weekday": "",
                }
            )

    return ids, documents, metadatas


def build_chunks(db) -> tuple[list[str], list[str], list[dict]]:
    """(ids, documents, metadatas) 튜플 반환 — event_desc/dtl + 게이트 통과한 리뷰."""
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    group_cache: dict[int, list[int]] = {}

    events = db.query(Event).all()
    event_by_no = {e.event_no: e for e in events}

    for event in events:
        text = _event_chunk_text(event)
        if not text:
            continue
        artist_ids = _artist_ids_for_event(db, event, group_cache)
        ids.append(f"event:{event.event_no}")
        documents.append(text)
        metadatas.append(
            {
                "source": "event",
                "poi_id": event.event_no,
                "ctg_no": event.ctg_no,
                "artist_ids": ",".join(str(a) for a in artist_ids),
                "evidence_tier": "A",
            }
        )

    reviews = db.query(ExternalReview).all()
    for review in reviews:
        if not review.review or not review.review.strip():
            continue
        tier = evidence_tier_from_total_score(review.total_score)
        if tier is None:
            continue  # C등급(또는 NULL) — 적재 금지
        event = event_by_no.get(review.event_no)
        artist_ids = _artist_ids_for_event(db, event, group_cache) if event else []
        ids.append(f"review:{review.external_review_no}")
        documents.append(review.review)
        metadatas.append(
            {
                "source": "external_review",
                "poi_id": review.event_no,
                "ctg_no": event.ctg_no if event else -1,
                "artist_ids": ",".join(str(a) for a in artist_ids),
                "evidence_tier": tier,
            }
        )

    cong_ids, cong_documents, cong_metadatas = build_congestion_chunks(db, event_by_no, group_cache)
    ids.extend(cong_ids)
    documents.extend(cong_documents)
    metadatas.extend(cong_metadatas)

    return ids, documents, metadatas


def ingest_once(db) -> dict:
    """지식베이스 전체를 다시 임베딩해서 upsert한다. event/external_review에 변경분을
    구분할 타임스탬프 컬럼이 없어(둘 다 updated_at 없음) 매번 전체를 다시 계산 —
    현재 규모(수백 건)에서는 충분히 빠르고, upsert라 안전(idempotent)하다."""
    ids, documents, metadatas = build_chunks(db)

    result = {
        "event_chunks": sum(1 for m in metadatas if m["source"] == "event"),
        "review_chunks": sum(1 for m in metadatas if m["source"] == "external_review"),
        "review_grade_a": sum(1 for m in metadatas if m["source"] == "external_review" and m["evidence_tier"] == "A"),
        "review_grade_b": sum(1 for m in metadatas if m["source"] == "external_review" and m["evidence_tier"] == "B"),
        "congestion_chunks": sum(1 for m in metadatas if m["source"] == "congestion"),
        "total_upserted": len(ids),
    }

    if not ids:
        return result

    embedder = _get_embedder()
    embeddings = embedder.encode(documents, normalize_embeddings=True).tolist()

    collection = get_chroma_collection()
    collection.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)

    return result


def run_rag_ingestion() -> dict:
    """배치 진입점 — 매일 03:00 KST(batch.py 스케줄러에서 호출)."""
    db = SessionLocal()
    try:
        return ingest_once(db)
    finally:
        db.close()


if __name__ == "__main__":
    print(run_rag_ingestion())
