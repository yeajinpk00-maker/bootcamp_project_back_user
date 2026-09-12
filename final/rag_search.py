"""FAN:GO 챗봇 하이브리드 검색 — 카테고리·아티스트 메타데이터 필터 + 벡터 검색 (기능명세서 4.3·7절).

절차:
  1) 질문 텍스트에서 멤버 이름/그룹명/카테고리명/장소명을 찾아 artist_no/ctg_no/event_no로 변환
     (반드시 event_nm 같은 문자열이 아니라 식별자로 이후 조인·필터한다 — event_nm 자체는
     그 event 하나를 특정하는 최초 매칭에만 쓰고, 이후 필터링은 전부 event_no 기준).
  2) 그 식별자를 ChromaDB where 절 메타데이터 필터로 우선 적용.
  3) 코사인 유사도 임계값 이상인 것들 중 상위 Top-3만 반환.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rag_ingest import get_chroma_collection, _get_embedder
from models import Artist, ArtistGroup, Ctg, Event

# 명세서 7절 원안은 0.75였으나, 실측 결과(KR-SBERT 임베딩 + 짧은 한국어 텍스트 조합에서
# 실제로 관련 있는 매칭들이 0.4~0.6대에 몰려 있음) 0.75는 사실상 항상 0건을 반환해
# 그 구간 중간값인 0.45로 재보정(2026-09-07, 사용자 확인 후 반영).
# external_review 데이터가 쌓여 청크 분포가 달라지면 재실측 후 조정할 것.
SIMILARITY_THRESHOLD = 0.45
TOP_K = 3
# 후보 사전조회 개수 — 임계값 미달분을 걸러낸 뒤에도 top-3가 남도록 여유 있게 가져온다.
CANDIDATE_POOL_SIZE = 20

# congestion 문서(rag_ingest.build_congestion_chunks)의 weekday 메타데이터('1'~'7') 매핑.
# 2026-09-10 추가 — 요일별 문서 7개가 구조상 서로 거의 동일한 템플릿 문장이라(장소명·시간대
# 숫자만 다름) 임베딩 유사도만으로는 "화요일" 문서가 "수요일" 문서보다 반드시 더 위로
# 오지 않는 것을 실측으로 확인했다(완료 후 보고 참고). event_no 필터와 같은 방식으로
# 요일도 식별자화해서 사전 필터링하면 이 문제가 해결된다.
_WEEKDAY_NAME_TO_NO = {
    "월요일": ["1"], "화요일": ["2"], "수요일": ["3"], "목요일": ["4"],
    "금요일": ["5"], "토요일": ["6"], "일요일": ["7"],
    "평일": ["1", "2", "3", "4", "5"], "주말": ["6", "7"],
}


@dataclass
class MetadataFilters:
    matched_artist_nm: list[str] = field(default_factory=list)
    matched_group_nm: list[str] = field(default_factory=list)
    matched_ctg_nm: list[str] = field(default_factory=list)
    matched_event_nm: list[str] = field(default_factory=list)
    poi_ids: list[int] = field(default_factory=list)  # 아티스트/장소명 매칭 결과(식별자)
    ctg_nos: list[int] = field(default_factory=list)  # 카테고리 매칭 결과(식별자)
    congestion_weekdays: list[str] = field(default_factory=list)  # 혼잡도 문서용 요일 식별자('1'~'7')


def _extract_congestion_weekdays(query: str) -> list[str]:
    """질문에 '화요일'처럼 완전한 요일 단어(또는 '평일'/'주말')가 있으면 congestion.weekday
    값으로 변환한다. 단일 글자('월','화'...)는 다른 단어에 흔히 섞여 있어(예: '일단',
    '화면') 오탐이 크므로 완전한 단어만 매칭 대상으로 삼는다 — 못 찾으면 필터 없이
    기존 동작(유사도 기준 정렬)으로 그대로 폴백."""
    weekdays: set[str] = set()
    for name, nos in _WEEKDAY_NAME_TO_NO.items():
        if name in query:
            weekdays.update(nos)
    return sorted(weekdays)


def _name_candidates(name: str | None) -> list[str]:
    """artist_nm/group_nm이 '한글이름 (영문이름)' 형식(예: '정국 (Jung Kook)', '방탄소년단 (BTS)')이라
    한글/영문 둘 다 별도 매칭 후보로 쪼갠다 — 괄호 포함 전체 문자열은 사용자가 그대로 입력할 일이 없다."""
    if not name:
        return []
    candidates = []
    if "(" in name:
        korean, _, rest = name.partition("(")
        korean = korean.strip()
        english = rest.rstrip(")").strip()
        if korean:
            candidates.append(korean)
        if english:
            candidates.append(english)
    else:
        candidates.append(name.strip())
    return [c.replace(" ", "") for c in candidates if c]


def _canonicalize_event_nos(
    db, event_nos: set[int], resolved_artist_nos: set[int], resolved_group_nos: set[int]
) -> list[int]:
    """event_nm이 같아서 여러 event_no가 걸렸을 때, DA 쪽 event_nm 정리를 기다리지 않고
    매칭 로직에서 흡수한다. event_nm이 같아도 artist_group_no/artist_no가 다르면 실제로는
    다른 장소(아티스트별로 별개의 성지)이고, 그것까지 같으면 같은 장소가 중복 저장된
    것뿐이다(한 event는 둘 중 하나만 채워짐 — 채워진 값이 그룹 키).

    - 전부 같은 그룹이면, 그리고 질문에서 추출된 아티스트/그룹(resolved_*)으로 여러 그룹
      중 하나로 좁혀지면, 그 그룹에 속한 event_no 전체를 poi_id 후보로 남긴다(2026-09-10
      수정 — 예전엔 여기서 대표 event_no(그룹 내 MIN) 하나로 좁혔다). 대표 하나로 좁히지
      않는 이유: "한국의집"/"한국의 집"(좌표 완전 동일, 같은 artist_group_no) 실측에서
      congestion 데이터가 두 event_no 중 하나(264)에만 있는데, MIN(=181, 데이터 없음)이
      뽑히면서 검색이 미스되는 걸 확인했다 — 좌표가 서로 같든 다르든(후자는 같은 그룹
      소속의 실제로 다른 지점) 대표를 하나로 확정할 근거가 없기는 마찬가지라, v2.5에서
      "그룹이 여럿이라 못 좁힌" 케이스에 적용한 것과 동일하게(§완료 후 보고 참고)
      "안전하게 전부 후보로 남긴다" 원칙을 여기도 그대로 적용한다.
    - 그래도 여럿이면(v2.5, 2026-09-10) 좌표가 같든 다르든 마찬가지로 전체를 poi_id
      후보로 남긴다 — "에스엠엔터테인먼트"류처럼 멤버마다 개인 성지로 등록돼 그룹만
      24개인 경우, 그리고 좌표까지 다른 진짜 별개 장소인 경우 둘 다 같은 처리다.
    """
    if len(event_nos) <= 1:
        return sorted(event_nos)

    rows = (
        db.query(Event.event_no, Event.artist_no, Event.artist_group_no)
        .filter(Event.event_no.in_(event_nos))
        .all()
    )
    groups: dict[tuple, list[int]] = {}
    for event_no, artist_no, artist_group_no in rows:
        if artist_group_no is not None:
            key = ("group", artist_group_no)
        elif artist_no is not None:
            key = ("artist", artist_no)
        else:
            key = ("none", event_no)  # 둘 다 없으면 그룹핑 불가 — 자기 자신만의 그룹
        groups.setdefault(key, []).append(event_no)

    if len(groups) == 1:
        return sorted(next(iter(groups.values())))

    if resolved_group_nos:
        narrowed = {k: v for k, v in groups.items() if k[0] == "group" and k[1] in resolved_group_nos}
        if len(narrowed) == 1:
            return sorted(next(iter(narrowed.values())))
    if resolved_artist_nos:
        narrowed = {k: v for k, v in groups.items() if k[0] == "artist" and k[1] in resolved_artist_nos}
        if len(narrowed) == 1:
            return sorted(next(iter(narrowed.values())))

    # 그룹/힌트로도 안 좁혀짐 — 진짜 다른 장소들이 이름만 같은 경우(또는 그 이하로는
    # 판단 근거가 없는 경우) 전체를 그대로 후보로 남긴다.
    return sorted(event_nos)


def extract_metadata_filters(db, query: str) -> MetadataFilters:
    """질문 텍스트를 알려진 엔티티 이름(멤버/그룹/카테고리/장소)과 대조해 식별자로 변환한다.
    지금은 간단한 부분 문자열 매칭이다 — 나중에 Intent Router(LLM)가 엔티티를 뽑아주게 되면
    이 함수를 "이름 목록 매칭" 대신 "이미 추출된 엔티티 → 식별자 변환"으로 바꿔 재사용하면 된다."""
    filters = MetadataFilters()
    q = query.replace(" ", "").lower()  # 한국어 띄어쓰기 편차 방지 + 영문 대소문자 무시

    artist_event_nos: set[int] = set()
    resolved_artist_nos: set[int] = set()
    resolved_group_nos: set[int] = set()

    # 1) 멤버 이름 매칭 → 그 멤버가 걸린 event만 (artist_nm이 "정국 (Jung Kook)"처럼
    #    한글/영문 병기라 둘 중 아무 후보나 매칭되면 인정)
    artists = db.query(Artist).all()
    for artist in sorted(artists, key=lambda a: len(a.artist_nm or ""), reverse=True):
        if any(cand.lower() in q for cand in _name_candidates(artist.artist_nm)):
            filters.matched_artist_nm.append(artist.artist_nm)
            resolved_artist_nos.add(artist.artist_no)
            rows = (
                db.query(Event.event_no)
                .filter(Event.artist_no == artist.artist_no)
                .all()
            )
            artist_event_nos.update(r.event_no for r in rows)

    # 2) 그룹 이름 매칭 → 그 그룹 전체 대상 event만(그룹 지정 이벤트는 artist_group_no로 저장됨)
    groups = db.query(ArtistGroup).all()
    for group in sorted(groups, key=lambda g: len(g.group_nm or ""), reverse=True):
        if any(cand.lower() in q for cand in _name_candidates(group.group_nm)):
            filters.matched_group_nm.append(group.group_nm)
            resolved_group_nos.add(group.artist_group_no)
            rows = (
                db.query(Event.event_no)
                .filter(Event.artist_group_no == group.artist_group_no)
                .all()
            )
            artist_event_nos.update(r.event_no for r in rows)

    if filters.matched_artist_nm or filters.matched_group_nm:
        filters.poi_ids = sorted(artist_event_nos)

    # 3) 장소명 직접 매칭 → 특정 event로 좁힘(가장 구체적이므로 poi_ids를 이걸로 덮어씀).
    #    동명이인(event_nm 중복)은 artist_group_no/artist_no 기준으로 canonicalize한다.
    events = db.query(Event.event_no, Event.event_nm).all()
    matched_event_nos: set[int] = set()
    for event_no, event_nm in sorted(events, key=lambda e: len(e[1] or ""), reverse=True):
        if event_nm and event_nm.replace(" ", "").lower() in q:
            filters.matched_event_nm.append(event_nm)
            matched_event_nos.add(event_no)
    if matched_event_nos:
        filters.poi_ids = _canonicalize_event_nos(
            db, matched_event_nos, resolved_artist_nos, resolved_group_nos
        )

    # 4) 카테고리 이름 매칭 — ctg_nm이 여러 단어를 '/'로 묶고 있어 부분 매칭도 허용
    #    (예: 질문 "카페" ⊂ ctg_nm "생일 카페"/"성지 디저트/카페")
    ctgs = db.query(Ctg).all()
    matched_ctg_nos: set[int] = set()
    for ctg in ctgs:
        if not ctg.ctg_nm:
            continue
        keywords = [k for k in ctg.ctg_nm.replace(" ", "").split("/") if k]
        if ctg.ctg_nm.replace(" ", "") in q or any(k in q for k in keywords):
            filters.matched_ctg_nm.append(ctg.ctg_nm)
            matched_ctg_nos.add(ctg.ctg_no)
    filters.ctg_nos = sorted(matched_ctg_nos)
    filters.congestion_weekdays = _extract_congestion_weekdays(query)

    return filters


# 2026-09-10 추가 — get_travel_time(챗봇 FC) 전용 출발지/도착지 추출. 이동시간은 태생적으로
# 장소가 2개 필요해서, "poi_ids 정확히 1개"를 요구하는 extract_metadata_filters()의 기존
# 가정과는 별도 함수로 뗐다(무리하게 끼워 넣으면 poi_ids가 2개인 경우를 "모호함"으로
# 오판하는 기존 로직과 충돌한다).
_TRAVEL_TIME_KEYWORDS = [
    "까지", "걸려요", "걸리나요", "걸리는지", "이동시간", "이동 시간", "가는데", "가는 데", "얼마나걸",
]


def extract_travel_time_places(db, query: str) -> tuple[int, int] | None:
    """질문에 서로 다른 장소 이름이 정확히 2개 등장하고 이동시간을 묻는 문구가 있으면
    (origin_event_no, destination_event_no)를 반환한다 — 텍스트에 먼저 나오는 이름을
    출발지로 본다("A에서 B까지"). 장소가 2개가 아니거나(1개뿐/3개 이상), 어느 한쪽이
    동명이인으로 여전히 모호하면 None — 잘못 짚느니 호출부가 "정보 없음"으로 정직하게
    답하게 둔다."""
    q = query.replace(" ", "").lower()
    if not any(kw.replace(" ", "") in q for kw in _TRAVEL_TIME_KEYWORDS):
        return None

    events = db.query(Event.event_no, Event.event_nm).all()
    found: dict[str, set[int]] = {}
    for event_no, event_nm in events:
        if not event_nm:
            continue
        norm = event_nm.replace(" ", "").lower()
        if norm and norm in q:
            found.setdefault(norm, set()).add(event_no)

    if len(found) != 2:
        return None

    ordered = sorted(found.keys(), key=lambda nm: q.find(nm))
    origin_candidates = _canonicalize_event_nos(db, found[ordered[0]], set(), set())
    dest_candidates = _canonicalize_event_nos(db, found[ordered[1]], set(), set())
    if len(origin_candidates) != 1 or len(dest_candidates) != 1:
        return None
    return origin_candidates[0], dest_candidates[0]


def _build_chroma_where(filters: MetadataFilters, exclude_poi_ids: list[int] | None = None) -> dict | None:
    clauses: list[dict] = []
    if exclude_poi_ids:
        # 2026-09-10 추가 — 대화 지시어("거기 말고 다른 데는?") 해석용. chatbot.py가
        # 직전 턴에서 얘기하던 event_no를 여기로 넘기면, 그 문서만 후보에서 뺀 채로
        # 나머지(같은 카테고리 등)를 검색한다. poi_ids/ctg_nos 필터와는 독립적으로 항상
        # 적용 — "그 장소 자체를 다시 찾는" 게 아니라 "그 장소 말고"가 목적이므로.
        clauses.append({"poi_id": {"$nin": exclude_poi_ids}})
    if filters.poi_ids:
        clauses.append({"poi_id": {"$in": filters.poi_ids}})
    # 2026-09-10 v2.4 — poi_ids가 있으면 ctg_nos는 건너뛴다. poi_id가 이미 장소를
    # 유일하게(또는 동명이인 후보로) 특정한 상태라 ctg_no는 군더더기일 뿐 아니라,
    # 질문 속 글자가 우연히 다른 카테고리명과 겹치면(예: "문화비축기지"의 "문화"가
    # "문화/유적지"에 매칭) AND 결합 때문에 실제로는 존재하는 문서가 0건으로
    # 사라지는 실측 버그가 있었다(완료 후 보고 참고) — poi_id 단독으로도 충분히
    # 구체적인 필터이므로 ctg_no로 더 좁힐 이유가 없다. ctg_no는 장소가 특정 안 되고
    # 카테고리로만 좁히는 질문(poi_ids가 빈 경우)에서만 의미가 있다.
    elif filters.ctg_nos:
        clauses.append({"ctg_no": {"$in": filters.ctg_nos}})
    if filters.congestion_weekdays:
        # 2026-09-10 v2.3 — 하드 필터로 강화. 이전엔 여기에 ""(전체 요일 요약 문서)를 항상
        # 끼워 넣어 안전망으로 남겨뒀는데, 실측(20건 배치 검증) 결과 그 안전망이 실제
        # 실패 원인이 아니었다는 게 드러났다 — 진짜 원인은 (1) 동명이인 이벤트가 많아
        # poi_id 자체가 여러 개로 모호하게 풀리는 경우, (2) poi_id+ctg_no가 AND로 묶여
        # 카테고리 키워드가 우연히 다른 값으로 잡히면 결과가 0건이 되는 경우였다(둘 다
        # weekday 필터와 무관, 완료 후 보고 참고). 그래도 지시대로 "요일이 확정되면 그
        # 요일 문서만" 원칙을 그대로 지키기 위해, 여기서는 ""를 더 이상 끼우지 않는다 —
        # 요일을 못 찾았을 때(if 분기 자체를 안 타는 경우)는 원래도 전체 요약 문서가
        # 그대로 후보에 남으므로 폴백은 여전히 동작한다. event_desc/리뷰는 weekday
        # 메타데이터가 아예 없는 문서라 "$ne: congestion" 쪽으로 걸려 이 필터의 영향을
        # 받지 않는다.
        allowed = filters.congestion_weekdays
        clauses.append(
            {"$or": [{"weekday": {"$in": allowed}}, {"source": {"$ne": "congestion"}}]}
        )

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def search_knowledge_base(
    db,
    query: str,
    top_k: int = TOP_K,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    exclude_poi_ids: list[int] | None = None,
) -> list[dict]:
    """반환 항목: {poi_id, ctg_no, artist_ids, evidence_tier, source, similarity, text}.
    유사도 임계값 미만은 제외하고, 남은 것 중 유사도 내림차순 top_k만 돌려준다.

    exclude_poi_ids(2026-09-10 추가): 지시어 질문("거기 말고 다른 데는?") 처리용 — chatbot.
    resolve_reference()가 직전 턴 장소를 제외 대상으로 판단했을 때만 넘어온다. 일반 질문은
    항상 None이라 기존 동작 그대로다."""
    filters = extract_metadata_filters(db, query)
    where = _build_chroma_where(filters, exclude_poi_ids)

    collection = get_chroma_collection()
    if collection.count() == 0:
        return []

    embedder = _get_embedder()
    query_embedding = embedder.encode([query], normalize_embeddings=True).tolist()

    query_kwargs = dict(
        query_embeddings=query_embedding,
        n_results=min(CANDIDATE_POOL_SIZE, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    if where is not None:
        query_kwargs["where"] = where

    result = collection.query(**query_kwargs)

    hits: list[dict] = []
    docs = result.get("documents") or [[]]
    metas = result.get("metadatas") or [[]]
    dists = result.get("distances") or [[]]
    for doc, meta, dist in zip(docs[0], metas[0], dists[0]):
        # cosine space에서 chroma의 distance = 1 - cosine_similarity
        similarity = 1 - dist
        if similarity < similarity_threshold:
            continue
        hits.append({**meta, "similarity": similarity, "text": doc})

    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits[:top_k]
