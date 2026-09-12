"""FAN:GO 챗봇(Phase 2) — Intent Router + Function Calling + RAG 하이브리드 답변 생성
(기능명세서 3절 데이터 흐름 / 7절 하이브리드 검색 / 8절 프롬프트 설계).

파이프라인(3절):
  ① 질문 + 사용자 컨텍스트 입력
  ② Intent Router(LLM) — Path A(Function Calling) / B(RAG) / C(복합) 결정
  ③ Context Assembly — API 결과 + RAG 결과 병합
  ④ Final Generation — 병합된 컨텍스트로 최종 답변 생성

Function Calling 대상(6절) 현황:
  - get_congestion_pattern — 2026-09-10 팀 결정으로 완전히 제거됨. 혼잡도는 이제 Function
    Calling(MySQL 직접 조회)이 아니라 RAG(rag_ingest.build_congestion_chunks)를 거쳐서만
    답한다 — congestion 테이블을 (event_no, weekday) 단위 자연어 문서로 만들어 ChromaDB에
    임베딩해두고, search_knowledge_base()가 event_desc/리뷰와 동일한 방식으로 검색한다.
    Path A(Function Calling) 분류 기준에서도 혼잡도를 뺐다(아래 INTENT_ROUTER_SYSTEM_PROMPT
    참고) — 혼잡도 질문은 이제 Path B/C로 라우팅되어야 한다.
  - get_poi_business_hours — 구현 완료(아래 참고). event_op_hour가 1,225/1,984 event(약 62%)에
    채워져 있다(2026-09-08 기준, 팀이 계속 채우는 중 — 100% 전까지는 없는 곳이 정상적으로 많음).
    ⚠ 스펙엔 op_dt가 날짜(date)로 돼 있었지만, 실 데이터를 보니 op_dt는 실제 캘린더 날짜가
    아니라 '월'~'일' 요일 문자열이었다(VARCHAR(50), congestion.weekday의 '1'~'7'과도 인코딩이
    다름) — 그래서 이 함수의 op_dt 파라미터는 date가 아니라 요일 문자열로 구현했다.
  - get_travel_time — 2026-09-10 연동 완료. travel_time_cache 테이블은 AL-02 작업으로
    이미 생겼고(카카오모빌리티 다중 목적지 API 캐시), 이 도구는 그 캐시를 읽기 전용으로만
    조회한다 — 캐시에 없는 조합을 API로 새로 계산해오지는 않는다(그러면 카카오 API
    900건/일 쿼터를 챗봇이 잠식하게 되므로 팀 결정으로 캐시 전용). 캐시 미스면 정직하게
    "정보 없음"으로 처리(호출부의 기존 근거 0건 가드가 담당) — 지어내지 않는다.
    다른 도구와 달리 이 도구는 장소가 2개(출발지/도착지) 필요해서 AVAILABLE_TOOLS/
    TOOL_SCHEMAS(전부 "event_no 1개 + LLM 인자" 패턴)에 안 넣고 run_function_calling()
    안에서 별도로 분기 처리한다 — rag_search.extract_travel_time_places() 참고.
  AVAILABLE_TOOLS에 함수명 -> 실행 콜백으로 등록해두면, event_no 1개만 받는 새 도구를
  추가할 때 이 dict와 TOOL_SCHEMAS에 항목 하나씩만 얹으면 된다(라우팅/컨텍스트 조립
  로직은 손댈 필요 없음).

event_no 결정 원칙: 이 모듈 안 어디에서도 이름 문자열 매칭을 하지 않는다. 질문 속
장소/아티스트 이름은 rag_search.extract_metadata_filters()가 이미 식별자(event_no)로
변환해두므로, Function Calling도 그 결과(poi_ids)를 그대로 받아쓴다 — 등록된 도구
함수들은 전부 event_no만 받고 이름을 몰라도 된다. 동명이인(event_nm 중복)은
extract_metadata_filters 안에서 artist_no/artist_group_no 기준으로 canonicalize된다
(같은 그룹이면 대표 event_no 하나로, 진짜 다른 장소면 그대로 모호 처리).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from openai import OpenAI
from sqlalchemy import text
from sqlalchemy.orm import Session

from models import Ctg, Event, EventOpHour
from rag_search import extract_metadata_filters, extract_travel_time_places, search_knowledge_base

logger = logging.getLogger(__name__)

CHAT_MODEL = "gpt-4o-mini"


def _get_poi_business_hours_single(db: Session, event_no: int, op_dt: str | None) -> dict | None:
    """event_op_hour 조회(6절) — event_no 딱 1개 기준. get_poi_business_hours()의 실제
    조회 로직(2026-09-10 이전과 동일, 이름만 private으로 분리)."""
    if op_dt is not None:
        row = (
            db.query(EventOpHour)
            .filter(EventOpHour.event_no == event_no, EventOpHour.op_dt == op_dt)
            .first()
        )
        if not row:
            return None
        return {"event_no": event_no, "op_dt": row.op_dt, "open_tm": row.open_tm, "close_tm": row.close_tm}

    rows = db.query(EventOpHour).filter(EventOpHour.event_no == event_no).all()
    if not rows:
        return None

    distinct_hours = {(r.open_tm, r.close_tm) for r in rows}
    if len(distinct_hours) == 1:
        open_tm, close_tm = next(iter(distinct_hours))
        return {"event_no": event_no, "every_day": True, "open_tm": open_tm, "close_tm": close_tm}

    return {
        "event_no": event_no,
        "every_day": False,
        "by_day": [{"op_dt": r.op_dt, "open_tm": r.open_tm, "close_tm": r.close_tm} for r in rows],
    }


def get_poi_business_hours(
    db: Session, event_nos: int | list[int], op_dt: str | None = None
) -> dict | None:
    """event_op_hour 조회(6절). 이름 매칭 없이 event_no(들)만 받는다.

    event_nos는 int 1개(기존 호환) 또는 event_no 리스트(2026-09-10 확장) — §5.2 동명이인
    처리가 v2.9부터 poi_ids를 대표 1개로 안 좁히고 좌표 클러스터 전체로 남기게 되면서,
    "한국의집"처럼 같은 실제 장소를 가리키는 event_no가 여러 개 넘어올 수 있게 됐다.
    RAG(congestion 등)는 ChromaDB의 poi_id IN(...) 필터로 이미 이 여러 개를 자연스럽게
    다루는데, 이 함수는 SQL 단일 행 조회 구조라 그렇게 안 돼서 순서대로 돌며 "데이터
    있는 첫 event_no"를 채택한다(RAG와 동일한 "데이터 있는 것부터" 원칙).

    실측 확인(완료 후 보고 참고): 동명이인 클러스터 중 event_op_hour 데이터가 있는
    8건 전부 클러스터 안 정확히 1개 event_no에만 데이터가 있었다 — 같은 클러스터의
    서로 다른 event_no에 서로 다른(모순되는) 영업시간이 동시에 존재하는 실제 사례는
    없었다. 그래도 이론상 가능성을 대비해 "먼저 찾은 것을 채택"(poi_ids 정렬 순서,
    보통 event_no 오름차순) 정책을 명시적으로 정해뒀다 — 두 곳 다 데이터가 있는데
    값이 다르면, 굳이 "더 맞는 쪽"을 판단할 근거가 없으므로 그냥 첫 번째를 쓰고 조용히
    넘어간다(값 자체는 지어낸 게 아니라 실제 DB 값이라 8절 규칙 위반은 아님).

    ⚠ op_dt는 실제 캘린더 날짜가 아니라 '월'~'일' 요일 문자열이다(모듈 docstring 참고,
    실 데이터로 확인함 — 스펙의 date 타입과 다름).

    반환 dict엔 실제로 데이터가 있었던 event_no가 "event_no" 키로 들어간다(호출부가
    sources 투명성 필드에 정확한 출처를 쓸 수 있도록) — event_no 자체가 event_op_hour에
    전혀 없으면(클러스터 전체 포함) None(호출부의 기존 "근거 0건" 가드가 처리)."""
    if isinstance(event_nos, int):
        event_nos = [event_nos]
    for event_no in event_nos:
        result = _get_poi_business_hours_single(db, event_no, op_dt)
        if result is not None:
            return result
    return None


def get_travel_time(
    db: Session, origin_event_no: int, destination_event_no: int, travel_mode: str = "car"
) -> dict | None:
    """travel_time_cache 캐시 읽기 전용 조회(2026-09-10 신규). AL-02가 실제 트립을 계산하며
    쌓아둔 캐시만 보고, 캐시에 없는 조합은 카카오 API를 새로 호출하지 않는다 — 챗봇이
    AL-02의 900건/일 쿼터를 잠식하지 않게 하기 위한 팀 결정. origin_type은 항상 'event'
    (챗봇에서 출발지도 텍스트로 언급된 '장소'이지 숙소/트립 핀이 아니므로 — 그런
    origin_type은 개인화 트립 컨텍스트가 있어야 하는데 이번 범위 밖).

    캐시에 없으면 None을 반환한다(호출부의 기존 "근거 0건" 가드 → 정직하게 "정보 없음"으로
    처리, 다른 값으로 대체 추정하지 않음)."""
    row = db.execute(
        text(
            "SELECT duration_min, fetched_at FROM travel_time_cache "
            "WHERE origin_type = 'event' AND origin_no = :origin "
            "AND destination_event_no = :dest AND travel_mode = :mode"
        ),
        {"origin": origin_event_no, "dest": destination_event_no, "mode": travel_mode},
    ).mappings().first()
    if not row:
        return None
    return {
        "origin_event_no": origin_event_no,
        "destination_event_no": destination_event_no,
        "duration_min": row["duration_min"],
        "travel_mode": travel_mode,
    }


# 6절 — Function Calling 도구 등록. AVAILABLE_TOOLS는 함수명 -> 실행 콜백,
# TOOL_SCHEMAS는 OpenAI tool-calling에 넘길 파라미터 스키마. 새 도구를 추가할 땐
# 이 두 곳에만 항목을 얹으면 된다(각 함수의 파라미터명이 스키마 property명과 일치해야
# run_function_calling이 **args로 그대로 넘길 수 있음).
# get_congestion_pattern은 2026-09-10에 제거됐다 — 혼잡도는 이제 RAG로만 답한다(모듈
# docstring 참고).
# ⚠ get_travel_time은 이 tool_choice="required" 메커니즘에 애초에 안 들어있다(장소 2개가
# 필요해 run_function_calling 맨 앞에서 extract_travel_time_places()로 결정론적으로 먼저
# 처리 — 이 목록엔 get_poi_business_hours 하나뿐이었다). 그래서 "도구가 2개라 엉뚱한 도구가
# 걸릴 위험이 커졌다"는 건 이 특정 LLM 호출엔 해당한 적이 없다 — 그래도 "필요 없는 질문에도
# 무조건 강제 호출"이라는 진짜 문제는 남아있었으므로(아래 needs_business_hours로 해결).
AVAILABLE_TOOLS: dict[str, callable] = {
    "get_poi_business_hours": get_poi_business_hours,
}

BUSINESS_HOURS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_poi_business_hours",
        "description": "특정 장소의 운영시간(영업시간)을 조회한다.",
        "parameters": {
            "type": "object",
            "properties": {
                "op_dt": {
                    "type": ["string", "null"],
                    "enum": ["월", "화", "수", "목", "금", "토", "일", None],
                    "description": "월/화/수/목/금/토/일 중 하나(event_op_hour.op_dt 포맷 — 한글 요일, congestion처럼 숫자 아님). 질문이 특정 요일을 지정하지 않았으면 null.",
                },
            },
            "required": ["op_dt"],
        },
    },
}
TOOL_SCHEMAS = [BUSINESS_HOURS_SCHEMA]  # 하위호환 이름 유지(다른 코드가 참조할 수 있어 남겨둠)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


INTENT_ROUTER_SYSTEM_PROMPT = """당신은 K-POP 여행 서비스 FAN:GO 챗봇의 질문 분류기입니다.
사용자 질문을 아래 세 경로 중 하나로 분류하세요.

- Path A(통계·팩트): 이동 시간, 영업시간처럼 정형 데이터 조회(Function Calling)로만 답할 수 있는 질문
- Path B(스토리·맥락): 혼잡도(붐비는 시간대) 패턴, 장소에 얽힌 사연, 왜 의미가 있는지, 리뷰 등
  지식베이스 검색(RAG)으로 답해야 하는 질문 — 혼잡도 질문은 2026-09-10부터 여기로 분류합니다
  (더 이상 Function Calling 대상이 아닙니다)
- Path C(복합): 위 두 가지가 함께 필요한 질문(예: "화요일 저녁에 문 열려있고 사람 안 붐비는 카페 추천해줘"
  — 영업시간은 A, 혼잡도는 B로 각각 처리한 뒤 합쳐서 답해야 함)

path가 A 또는 C면, 실제로 어떤 정형 데이터가 필요한지도 함께 판단하세요(2026-09-10 추가 —
필요한 도구만 강제 호출하기 위함, tool_choice="required"가 무관한 도구까지 억지로 부르는
문제 대응):
- needs_business_hours: "문 열었어요?/영업시간이 어떻게 돼요?"처럼 운영시간을 묻는 질문이면 true
- needs_travel_time: "얼마나 걸려요?/이동시간이 어떻게 돼요?"처럼 두 장소 간 이동시간을 묻는 질문이면 true
- 어느 쪽도 아니면(예: "이 장소 어떤 의미가 있어요?"가 Path A/C로 잘못 섞여 들어온 경우) 둘 다 false
- path가 B면 이 두 필드는 항상 false로 두세요(Path B는 Function Calling을 아예 안 씁니다)

반드시 아래 JSON 형식으로만 답하세요:
{"path": "A" 또는 "B" 또는 "C", "needs_business_hours": true 또는 false, "needs_travel_time": true 또는 false}"""


@dataclass
class RouterDecision:
    path: str
    needs_business_hours: bool = False
    needs_travel_time: bool = False


def route_intent(query: str) -> RouterDecision:
    """Intent Router(3절 ②). 실패 시 RAG(Path B, 도구 불필요)로 안전하게 폴백한다.

    2026-09-10부터 path 3분류(변경 없음)에 더해 needs_business_hours/needs_travel_time도
    함께 판단한다 — run_function_calling()이 이 값으로 tool_choice="required" 호출 자체를
    할지 말지, 한다면 어떤 도구를 목록에 올릴지를 정한다(§완료 후 보고 참고)."""
    try:
        response = _get_client().chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": INTENT_ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content)
        path = parsed.get("path")
        if path in ("A", "B", "C"):
            if path == "B":
                return RouterDecision(path="B")
            return RouterDecision(
                path=path,
                needs_business_hours=bool(parsed.get("needs_business_hours")),
                needs_travel_time=bool(parsed.get("needs_travel_time")),
            )
    except Exception:
        logger.exception("Intent Router 호출 실패 — Path B로 폴백")
    return RouterDecision(path="B")


def run_function_calling(
    db: Session, query: str, needs_business_hours: bool = True, needs_travel_time: bool = True
) -> tuple[dict, int | None]:
    """Path A/C 실행(6절). event_no는 이름 매칭이 아니라 rag_search의 식별자 변환
    결과(poi_ids)를 그대로 받아쓴다 — 이 함수는 어떤 장소 문자열도 직접 매칭하지 않는다.
    장소가 전혀 특정 안 되면(0건) Function Calling을 실행할 대상이 없는 것이므로 빈
    dict를 반환한다(호출부가 RAG로 폴백). poi_ids가 여러 개(동명이인 좌표 클러스터,
    §5.2 v2.9)여도 이제 그대로 진행한다(2026-09-10 — 예전엔 "1개 아니면 스킵"이었다,
    아래 참고) — get_poi_business_hours가 그 안에서 데이터 있는 것을 직접 찾는다.
    반환: (api_data, event_no) — event_no는 sources 투명성 표시용으로, 실행 자체가
    안 됐거나 결과가 비면 None. poi_ids가 여러 개였으면 실제로 데이터가 있었던
    event_no(도구 결과의 "event_no" 키)를 쓴다 — poi_ids[0]이 아니다.

    needs_business_hours/needs_travel_time(2026-09-10 추가): route_intent()의 RouterDecision을
    그대로 받는다 — 둘 다 기본값 True인 이유는 이 함수를 라우터 정보 없이 직접 호출하는
    기존 코드(테스트 등)가 예전처럼 "poi_id 1개면 일단 시도"로 동작하게 하기 위한
    하위호환이고, 실제 handle_chat() 경로는 항상 라우터가 판단한 값을 명시적으로 넘긴다.

    이동시간(get_travel_time)은 장소가 2개 필요해 "poi_ids 1개" 가정과 안 맞아 함수 맨
    앞에서 별도로 처리한다(rag_search.extract_travel_time_places 참고) — 텍스트에서
    결정론적으로 찾아지므로 LLM 도구 선택 없이 캐시를 바로 조회한다. needs_travel_time이
    false면 이 판별 자체를 건너뛴다(라우터가 이동시간 질문이 아니라고 봤으면, 우연히 장소
    이름 2개가 텍스트에 걸려도 이동시간으로 오인하지 않게)."""
    if not AVAILABLE_TOOLS:
        return {}, None

    # 이동시간 질문 우선 판별 — 장소가 2개(출발지/도착지) 필요해서 아래 "poi_ids 정확히
    # 1개" 가정과는 별도 경로다. 여기서 결정되면 그대로 캐시만 조회하고 끝낸다(LLM
    # 도구 선택 호출 자체를 안 태움 — 출발/도착 event_no는 이미 결정론적으로 다 나와서
    # LLM이 채울 인자가 없다). sources 투명성 필드는 도착지 event_no로 대표한다.
    if needs_travel_time:
        travel_pair = extract_travel_time_places(db, query)
        if travel_pair is not None:
            origin_no, destination_no = travel_pair
            result = get_travel_time(db, origin_no, destination_no)
            if result is None:
                return {}, None  # 캐시 미스 — 지어내지 않고 근거 0건으로 정직하게 처리
            return {"get_travel_time": result}, destination_no

    # 2026-09-10 — 라우터가 "정형 데이터 자체가 필요 없다"(예: "이 장소 어떤 의미가
    # 있어요?"가 Path A/C로 잘못 섞여 들어온 경우)고 판단했으면, tool_choice="required"
    # 호출 자체를 생략한다. 이게 §10 한계("무관한 질문에도 도구가 곁다리로 강제 호출")의
    # 실제 해결책 — 예전엔 poi_id가 1개로 잡히기만 하면 무조건 이 호출까지 갔었다.
    schemas_to_offer = [BUSINESS_HOURS_SCHEMA] if needs_business_hours else []
    if not schemas_to_offer:
        return {}, None

    # 2026-09-10 — 예전엔 poi_ids가 정확히 1개일 때만 진행했는데, §5.2 동명이인 처리가
    # v2.9부터 poi_ids를 대표 1개로 안 좁히고 좌표 클러스터 전체로 남기게 되면서(예:
    # "한국의집" → [181, 264]) 이 조건에 걸려 FC 자체가 스킵되는 경우가 생겼다. RAG는
    # ChromaDB의 IN 필터로 여러 개를 자연스럽게 다루니, FC도 "poi_ids가 1개 이상이면
    # 진행"으로 완화하고 get_poi_business_hours가 그 리스트 안에서 데이터 있는 걸 직접
    # 찾게 한다(아래 참고).
    poi_ids = extract_metadata_filters(db, query).poi_ids
    if not poi_ids:
        return {}, None

    # tool_choice="auto"로 두면 gpt-4o-mini가 "이미 아는 장소"에는 도구 호출 없이 그냥
    # 답해버리는 걸 테스트로 확인해 강제로 "required"를 쓴다. "auto"의 그 생략 문제가
    # 재발하지 않게, "required"는 그대로 유지하면서 tools 목록 자체를 라우터가 실제로
    # 필요하다고 판단한 것만 넣는 쪽으로 좁혔다(완료 후 보고 참고 — get_travel_time은
    # 애초에 이 목록에 들어온 적이 없어서, 이 특정 호출에서 "도구가 2개라 엉뚱한 게
    # 걸릴 위험"은 원래 없었다).
    try:
        response = _get_client().chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "영업시간/운영시간 질문이면 get_poi_business_hours를 호출하세요. "
                        "질문이 특정 요일을 언급하지 않았으면 op_dt는 null로 두세요."
                    ),
                },
                {"role": "user", "content": query},
            ],
            tools=schemas_to_offer,
            tool_choice="required",
            temperature=0,
        )
        tool_calls = response.choices[0].message.tool_calls or []
    except Exception:
        logger.exception("Function Calling 도구 선택 호출 실패")
        return {}, None

    api_data: dict = {}
    matched_event_no: int | None = None
    for call in tool_calls:
        fn = AVAILABLE_TOOLS.get(call.function.name)
        if not fn:
            continue
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        # poi_ids(리스트) 그대로 넘긴다 — get_poi_business_hours가 그 안에서 데이터
        # 있는 첫 event_no를 스스로 찾는다(위 주석 참고). 그 결과에 실린 "event_no"가
        # 실제로 데이터가 있었던 곳이라 sources 투명성 필드엔 그걸 쓴다(poi_ids[0]이
        # 아니라 — 클러스터에서 데이터가 poi_ids[0]에 없을 수도 있으므로).
        result = fn(db, poi_ids, **args)
        if result is not None:
            api_data[call.function.name] = result
            if matched_event_no is None:
                matched_event_no = result.get("event_no", poi_ids[0])

    return api_data, (matched_event_no if api_data else None)


def _format_knowledge_base(rag_hits: list[dict]) -> str:
    if not rag_hits:
        return "(관련 정보 없음)"
    lines = []
    for hit in rag_hits:
        lines.append(
            f"- [poi_id={hit['poi_id']}, 신뢰도={hit['evidence_tier']}등급, "
            f"유사도={hit['similarity']:.2f}] {hit['text']}"
        )
    return "\n".join(lines)


def _format_api_data(api_data: dict) -> str:
    if not api_data:
        return "(조회된 정형 데이터 없음)"
    return json.dumps(api_data, ensure_ascii=False)


FINAL_ANSWER_SYSTEM_PROMPT = """당신은 K-POP 여행 서비스 FAN:GO의 스토리텔링형 가이드,
'덕메(덕질 메이트)'입니다. 밝고 친근한 톤으로 답하세요.

규칙(반드시 지킬 것):
1. [Knowledge Base]에 에피소드가 있으면 단순 정보 전달을 넘어 "왜 이곳이 의미 있는지"까지 설명하세요.
   혼잡도 패턴 정보가 있다면(2026-09-10부터 혼잡도는 여기 Knowledge Base로 옵니다) 이는 실시간
   측정값이 아니라 요일·시간대별 평균 통계이므로, "지금 혼잡해요"가 아니라 "이 시간대는 보통
   붐비는 편이에요"처럼 패턴으로 표현하세요. 혼잡도 패턴은 그 시간대에 사람이 얼마나 몰리는지만
   말해줄 뿐, 그 장소가 "문을 열었는지/닫았는지"와는 별개의 정보입니다 — 혼잡도 문장에 시간대가
   나온다고 해서 그 시간에 영업 중이라고 넘겨짚지 마세요.
2. [API Data]에 정형 데이터(영업시간 등)가 있으면 문맥에 자연스럽게 녹여서 안내하세요. [API Data]가
   비어 있으면 영업시간·운영 여부는 모르는 것입니다 — [Knowledge Base]의 혼잡도 문장으로 대신
   추측해서 답하지 말고, 규칙 3대로 솔직히 확인이 어렵다고 답하세요.
3. [Knowledge Base]와 [API Data]에 없는 내용은 절대 지어내지 마세요. 답할 근거가 없으면
   "현재 해당 정보는 확인이 어려워요"라고 솔직하게 답하고, 필요하면 다른 질문을 유도하세요.
4. 근거 여부와 무관하게, 검증되지 않은 루머나 사생활 침해(자택 등 개인 거주지) 관련 질문에는
   단호히 답변을 거절하세요.
5. [사용자 컨텍스트]는 톤을 맞추는 참고용일 뿐, 그 자체로 답을 지어내는 근거로 쓰지 마세요."""


# 9절 "RAG 검색 결과 없음" 규칙의 고정 답변. 프롬프트 지시만으로는 gpt-4o-mini가
# 근거 없이도 자기 사전지식(예: 실제 BTS/하이브 관련 정보)으로 답을 지어내는 게
# 실제 테스트에서 확인됐다 — Knowledge Base·API Data가 둘 다 비어 있으면 LLM
# 호출 자체를 생략하고 이 문구로 고정 응답한다(할루시네이션을 프롬프트가 아니라
# 코드로 차단).
NO_INFO_FALLBACK = "죄송해요, 지금은 관련 정보를 확인하기 어려워요. 다른 장소나 아티스트로 다시 물어봐 주실래요?"


class FinalGenerationError(Exception):
    """Final Generation(③④) 호출 자체가 예외로 실패한 경우 전용(2026-09-10 신규).

    이전엔 이 경우도 사과 문구를 담아 (answer, is_fallback=False)로 정상 반환해서,
    "근거 0건이라 고정 문구로 답한 것"(is_fallback=True)과 "진짜 정상 답변"이 똑같이
    is_fallback=False라는 응답 필드 하나로 뭉뚱그려져 있었다(§10 한계) — 서비스 오류를
    성공한 채팅 메시지처럼 200으로 내려보내는 것 자체가 문제이므로, 이제 예외를 그대로
    던져서 handle_chat()을 거쳐 auth.py의 POST /chat이 502로 응답하게 한다(완료 후
    보고의 계약 변경 내용 참고). 호출부가 이 예외를 못 잡으면(예: 기존 스크립트) 그대로
    터지는 게 의도된 동작 — 조용히 삼키지 않는다."""


def generate_final_answer(
    query: str,
    user_context: dict,
    rag_hits: list[dict],
    api_data: dict,
    history: list[dict] | None = None,
) -> tuple[str, bool]:
    """Context Assembly + Final Generation(3절 ③④). 반환: (answer, is_fallback) —
    is_fallback=True는 근거 0건이라 LLM 호출 자체를 생략하고 고정 문구를 낸 경우만
    가리킨다. 최종 생성 호출 자체가 예외로 실패하면 이제 값을 반환하지 않고
    FinalGenerationError를 던진다(2026-09-10 — 예전엔 여기서 사과 문구를
    is_fallback=False로 반환해 정상 답변과 구분이 안 됐다).

    history: 멀티턴 컨텍스트(WBS 5.4.1) — 이전 턴의 {"role": "user"/"assistant", "content": str}
    목록을 시스템 프롬프트 뒤·현재 질문 앞에 그대로 끼워 넣는다. Intent Router/Function
    Calling/RAG는 여전히 현재 질문(query)만 보고 판단한다 — history는 순수하게 최종 답변
    생성 시점에 대화 맥락을 이어주는 용도로만 쓴다(기존 로직은 그대로 유지)."""
    if not rag_hits and not api_data:
        return NO_INFO_FALLBACK, True

    user_message = (
        f"[사용자 질문]\n{query}\n\n"
        f"[사용자 컨텍스트]\n{json.dumps(user_context, ensure_ascii=False) if user_context else '(없음)'}\n\n"
        f"[Knowledge Base]\n{_format_knowledge_base(rag_hits)}\n\n"
        f"[API Data]\n{_format_api_data(api_data)}"
    )
    messages = [{"role": "system", "content": FINAL_ANSWER_SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})
    try:
        response = _get_client().chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            temperature=0.4,
        )
        return response.choices[0].message.content, False
    except Exception as e:
        logger.exception("Final Generation 호출 실패")
        raise FinalGenerationError("Final Generation 호출 실패") from e


# ---------------------------------------------------------------------------
# 질문 재작성(query rewriting) — §10 한계 대응(2026-09-10 신규).
#
# 문제: Intent Router(route_intent)·장소 특정(extract_metadata_filters)·RAG 검색은
# 전부 "이번 메시지 텍스트"만 보고, 대화 히스토리는 최종 답변 생성 단계에만 반영된다
# (WBS 5.4.1 요청대로 기존 로직 불변). 그래서 "거기 말고 다른 데는?"처럼 지시어로
# 이전 턴을 가리키는 질문은 라우팅·검색 단계에서 이미 장소를 못 찾아 폴백으로 빠졌다.
#
# 해결: Router/추출 "이전"에 재작성 단계를 하나 끼워 넣는다 — 지시어를 실제 장소
# 이름(포함) 또는 카테고리 이름(제외, "다른 곳 찾기"로 자연스럽게 바뀜)으로 풀어써서
# 그 뒤 단계엔 항상 자기완결형 텍스트만 넘어가게 한다. 장소 판정 자체는 텍스트를
# 다시 파싱해서 이름을 매칭하는 게 아니라, 직전 턴 응답의 sources(event_no, PK)를
# 그대로 쓴다 — DB에서 event_nm/ctg_nm을 그 event_no로 조회해 재작성에 쓸 뿐, 이름으로
# 거꾸로 event_no를 찾는 방향은 아니다.
# ---------------------------------------------------------------------------

# 휴리스틱 사전 판별 — 이 중 하나도 없으면 재작성 LLM 호출 자체를 안 한다(대부분의
# 질문은 지시어가 없으므로, 이 체크 하나로 추가 지연 없이 그대로 통과한다).
_REFERENCE_MARKERS = [
    "거기", "저기", "그기", "그곳", "그 장소", "그장소", "그 곳",
    "그거", "그것", "그럼", "그 데", "그데", "거기말고", "거기는",
]


def _has_reference_marker(query: str) -> bool:
    return any(marker in query for marker in _REFERENCE_MARKERS)


def _last_referenced_place(db: Session, last_sources: list[int] | None) -> dict | None:
    """직전 턴 답변의 sources(0번째, "가장 주된 근거"로 취급) → event_no(PK) 기준으로
    이름/카테고리를 조회한다. 텍스트 재매칭이 아니라 PK 조회이므로 동명이인 문제와
    무관하다. sources가 없거나(첫 턴, 또는 직전 답변이 폴백) 그 event_no가 이미 삭제된
    경우 None — 호출부가 "지시어는 있지만 가리킬 대상이 없다"로 안전하게 처리한다."""
    if not last_sources:
        return None
    event_no = last_sources[0]
    row = (
        db.query(Event.event_no, Event.event_nm, Event.ctg_no)
        .filter(Event.event_no == event_no)
        .first()
    )
    if not row:
        return None
    ctg_nm = db.query(Ctg.ctg_nm).filter(Ctg.ctg_no == row.ctg_no).scalar()
    return {"event_no": row.event_no, "event_nm": row.event_nm, "ctg_nm": ctg_nm or "장소"}


REWRITE_SYSTEM_PROMPT_TEMPLATE = """당신은 K-POP 여행 챗봇의 '이전 대화 참조 해석기'입니다.
직전 대화에서 마지막으로 이야기하던 장소는 "{name}"(카테고리: {ctg})입니다.

사용자의 현재 질문에 "거기", "저기", "그럼", "그 장소" 같은 지시어가 있으면, 그게 이
장소를 가리키는 것으로 보고 아래 규칙대로 자기완결형 질문으로 바꾸세요.

- 지시어가 단순히 그 장소를 다시 가리키는 것("거기는 언제 붐벼요?", "거기 영업시간은?")이면:
  지시어를 "{name}"으로 바꿔치기한 질문을 만드세요. exclude는 false.
- 지시어가 그 장소를 "빼고" 다른 곳을 찾는 것("거기 말고 다른 데는?", "말고 다른 곳 추천해줘")이면:
  지시어를 "{name}"이 아니라 카테고리 "{ctg}"으로 바꿔 "다른 {ctg} 추천해줘" 식의 질문을
  만드세요(장소 이름 자체는 넣지 마세요). exclude는 true.
- 지시어가 실제로 없거나 이미 자기완결형이면 rewritten_query는 원문 그대로 두세요.

반드시 아래 JSON 형식으로만 답하세요:
{{"rewritten_query": "...", "exclude": true 또는 false}}"""


def resolve_reference(query: str, referenced: dict) -> dict:
    """재작성 LLM 1회 호출(gpt-4o-mini, temperature 0). 실패 시 원문 그대로 안전 폴백
    (재작성이 안 되면 기존처럼 그 턴만 폴백 응답이 나갈 뿐, 잘못된 장소로 엉뚱하게
    답하지는 않는다)."""
    try:
        response = _get_client().chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": REWRITE_SYSTEM_PROMPT_TEMPLATE.format(
                        name=referenced["event_nm"], ctg=referenced["ctg_nm"]
                    ),
                },
                {"role": "user", "content": query},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content)
        rewritten = parsed.get("rewritten_query") or query
        exclude = bool(parsed.get("exclude"))
        return {"rewritten_query": rewritten, "exclude": exclude}
    except Exception:
        logger.exception("질문 재작성 호출 실패 — 원문 그대로 폴백")
        return {"rewritten_query": query, "exclude": False}


def _maybe_rewrite_query(
    db: Session, query: str, last_sources: list[int] | None
) -> tuple[str, int | None]:
    """반환: (라우팅/검색에 쓸 질문, 제외할 event_no 또는 None). 지시어가 없으면 LLM
    호출 없이 즉시 원문을 그대로 돌려준다(추가 지연 0)."""
    if not _has_reference_marker(query):
        return query, None
    referenced = _last_referenced_place(db, last_sources)
    if referenced is None:
        return query, None  # 지시어는 있는데 가리킬 직전 대상이 없음 — 원문 그대로, 기존 폴백 로직에 맡김
    result = resolve_reference(query, referenced)
    exclude_event_no = referenced["event_no"] if result["exclude"] else None
    return result["rewritten_query"], exclude_event_no


def handle_chat(
    db: Session,
    query: str,
    user_context: dict,
    history: list[dict] | None = None,
    last_sources: list[int] | None = None,
) -> dict:
    """/chat 엔드포인트 진입점. 반환: {"answer": str, "sources": list[int], "is_fallback": bool}.
    sources는 답변에 실제로 근거가 된 event_no(poi_id) 목록(RAG 근거와 Function Calling
    근거를 구분 없이 같은 int 배열에 합쳐서 담음 — 투명성용, 없으면 빈 리스트).
    is_fallback=True면 근거가 하나도 없어 고정 문구로 답한 것이다(프론트가 이 필드로
    폴백 여부를 판별할 수 있게 하려고 추가함). history는 세션 영속성 레이어(auth.py)가
    직전 대화들을 불러와 넘겨주면 최종 답변 생성에만 반영한다(Intent Router/Function
    Calling/RAG는 여전히 사람이 읽는 원문 그대로의 히스토리는 안 본다 — WBS 5.4.1
    기존 로직 불변). last_sources(2026-09-10 신규): 직전 assistant 턴의 sources(event_no
    PK 목록) — 지시어("거기", "거기 말고") 해석 전용. 원본 query는 이 함수 안에서도
    변형하지 않고 그대로 chat_message에 저장될 원문으로 유지하며, 재작성된 텍스트는
    라우팅·추출 단계에만 쓴다(사용자에게 보여줄 텍스트가 아님).

    ⚠ generate_final_answer()가 FinalGenerationError를 던지면 이 함수는 그걸 잡지 않고
    그대로 위(auth.py)로 흘려보낸다 — "근거 0건"(is_fallback=True, 정상 200)과 "서비스
    오류"(예외 → 502)가 호출부에서 명확히 갈라지게 하려는 의도적 설계다(2026-09-10,
    §10 한계 대응). 이 함수를 직접 쓰는 코드(auth.py 외)는 이 예외를 반드시 처리해야
    한다."""
    routed_query, exclude_event_no = _maybe_rewrite_query(db, query, last_sources)
    exclude_ids = [exclude_event_no] if exclude_event_no is not None else None

    decision = route_intent(routed_query)
    path = decision.path

    api_data, api_event_no = (
        run_function_calling(db, routed_query, decision.needs_business_hours, decision.needs_travel_time)
        if path in ("A", "C") else ({}, None)
    )
    rag_hits = (
        search_knowledge_base(db, routed_query, exclude_poi_ids=exclude_ids)
        if path in ("B", "C") else []
    )

    # Path A로 분류됐는데 함수 실행이 안 됐으면(장소 미특정/모호, 라우터가 둘 다
    # 불필요하다고 판단, 또는 캐시/데이터 미스), "정보 없음"으로 바로 끝내지 않고 RAG로
    # 한 번 더 보완 시도한다 — 스토리 정보라도 있으면 그걸로 답하고, 그마저 없으면 8절
    # 규칙대로 솔직히 모른다고 답하게 된다.
    if path == "A" and not api_data:
        rag_hits = search_knowledge_base(db, routed_query, exclude_poi_ids=exclude_ids)

    # 최종 답변 생성에는 재작성 전 원문(query)을 그대로 쓴다 — history에 이미 이전
    # 대화가 통째로 들어가 있어서, 최종 생성 단계는 원문 그대로도 자연스러운 대화
    # 흐름으로 이해할 수 있다(재작성은 순수하게 Router/추출 단계 보조용).
    answer, is_fallback = generate_final_answer(query, user_context, rag_hits, api_data, history)
    # 같은 event_no가 rag_hits에 중복으로 걸릴 수 있다(2026-09-10부터, 한 장소의 요일별
    # 혼잡도 문서 + 전체 요약 문서가 동시에 상위 K에 들어오는 경우가 실제로 생김) —
    # "같은 event_no면 중복 제거됩니다"라는 기존 API 계약(문서화된 동작)을 지키기 위해
    # 순서를 유지하면서 중복을 제거한다.
    sources: list[int] = []
    for hit in rag_hits:
        if hit["poi_id"] not in sources:
            sources.append(hit["poi_id"])
    if api_event_no is not None and api_event_no not in sources:
        sources.append(api_event_no)
    return {"answer": answer, "sources": sources, "is_fallback": is_fallback}
