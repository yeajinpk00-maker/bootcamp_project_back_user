from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class UserLogin(BaseModel):
    login_id: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    user_no: int
    login_id: str
    nickname: str

    class Config:
        from_attributes = True


class SignupRequest(BaseModel):
    login_id: str = Field(min_length=3, max_length=50)
    login_pw: str = Field(min_length=8, max_length=128)
    login_pw_confirm: str
    nickname: str = Field(min_length=1, max_length=50)
    phone: str
    nationality_no: int
    lang_no: int
    name: str | None = None
    gender: str | None = None
    birth: date | None = None
    favorite_group_nos: list[int] = Field(min_length=1, max_length=2)

    @field_validator("favorite_group_nos")
    @classmethod
    def _no_duplicate_favorite_groups(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("중복된 artist_group_no가 있습니다.")
        return v


class NationalityOut(BaseModel):
    nationality_no: int
    nationality_nm: str
    lang_no: int
    lang_nm: str

    class Config:
        from_attributes = True


class LangOut(BaseModel):
    lang_no: int
    lang_nm: str

    class Config:
        from_attributes = True


class InterestOut(BaseModel):
    ctg_no: int
    ctg_nm: str

    class Config:
        from_attributes = True


class ArtistGroupOut(BaseModel):
    artist_group_no: int
    group_nm: str
    agency: str | None = None
    debut_dt: date | None = None
    fandom_nm: str | None = None
    members: list[str]

    class Config:
        from_attributes = True


class UserProfileOut(BaseModel):
    """마이 페이지 조회 + 정보수정 화면 초기값 채우기용."""

    user_no: int
    login_id: str  # 이메일
    nickname: str
    nationality_no: int
    lang_no: int
    profile_img: str | None = None
    favorite_groups: list[ArtistGroupOut]


class MeUpdateRequest(BaseModel):
    """넘어온 필드만 갱신 — None(미포함)이면 기존 값 유지.
    favorite_group_nos는 넘어오면 통째로 교체(기존 삭제 후 재생성).
    login_id(이메일)는 로그인 식별자라 수정 대상에서 제외 — 필드 자체가 없어서
    요청에 실려 와도 Pydantic이 조용히 무시한다(extra 필드 기본 동작)."""

    nickname: str | None = Field(default=None, min_length=1, max_length=50)
    nationality_no: int | None = None
    lang_no: int | None = None
    favorite_group_nos: list[int] | None = Field(default=None, min_length=1, max_length=2)

    @field_validator("favorite_group_nos")
    @classmethod
    def _no_duplicate_favorite_groups(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("중복된 artist_group_no가 있습니다.")
        return v


class CheckLoginIdResponse(BaseModel):
    available: bool


class EventOut(BaseModel):
    event_no: int
    event_nm: str
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    add: str
    event_lat: float
    event_lon: float
    event_desc: str | None = None
    # 그룹 전체 대상 이벤트는 artist_group_no만, 특정 멤버 대상 이벤트는 artist_no만
    # 채워지는 배타적 패턴이라 항상 값이 있다고 가정하면 안 된다(2026-09-10, event_no=671
    # 500 에러로 실제 확인됨 — EventDetailOut과 동일한 원인이라 여기도 함께 고침).
    artist_group_no: int | None = None
    artist_no: int | None = None

    class Config:
        from_attributes = True


class ArtistOut(BaseModel):
    artist_no: int
    artist_nm: str
    real_nm: str | None = None
    gender: str | None = None
    birth: date | None = None

    class Config:
        from_attributes = True


class ExternalReviewOut(BaseModel):
    rating: float | None = None
    total_score: float | None = None


class EventDetailOut(BaseModel):
    """장소 상세 — GET /events/main의 카드 정보 + 외부 리뷰(FAN:GO 추천점수 등)."""

    event_no: int
    event_nm: str
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    add: str
    event_lat: float
    event_lon: float
    event_desc: str | None = None
    event_img_url: str | None = None  # 2026-09-10 추가, DB 컬럼명 그대로 — NULL이면 null 그대로 반환
    # 그룹 전체 대상 이벤트는 artist_group_no만, 특정 멤버 대상 이벤트는 artist_no만
    # 채워지는 배타적 패턴 — 여기를 int(필수)로 잘못 선언해서 individual-member 이벤트를
    # 조회하면 응답 검증 단계에서 터져 500이 났다(2026-09-10, event_no=671/456/560/613/718
    # 실측으로 확인). 둘 다 nullable로 완화하고 artist_no도 함께 내려주도록 수정.
    artist_group_no: int | None = None
    artist_no: int | None = None
    external_reviews: list[ExternalReviewOut]


class VisitFeedbackOut(BaseModel):
    trip_route_event_no: int
    liked: bool


class BusinessHoursOut(BaseModel):
    has_data: bool  # False면 event_op_hour에 이 장소 자체가 없음(정보 없음, 휴무 아님)
    is_closed: bool  # True면 그 요일 행은 있는데 open_tm/close_tm이 둘 다 NULL(휴무)
    open_tm: str | None = None  # "HH:MM"
    close_tm: str | None = None  # "HH:MM"


class FixedScheduleOut(BaseModel):
    start_tm: str  # "HH:MM" — event.start_dt의 시각 부분
    end_tm: str | None = None  # "HH:MM" — event.end_dt 있으면, 없으면 null(시작시간만 있는 행사가 많음)


class TripRouteEventListItemOut(BaseModel):
    trip_route_event_no: int
    event_no: int
    event_nm: str
    seq: int | None = None
    liked: bool
    # trip.start_dt + visit_day로 실제 방문 날짜를 못 구하면(visit_day 미배정 등) None —
    # 그 외에는 항상 채워짐(event_op_hour에 데이터가 없어도 has_data:false로 채워서 반환).
    business_hours: BusinessHoursOut | None = None
    # ctg_type_no=1(행사)이고 event.start_dt가 있을 때만 채워짐 — 이 경우 business_hours는
    # 계산하지 않고 null로 둔다(요일별 운영시간이 아니라 행사 자체의 고정 시작/종료 시각).
    fixed_schedule: FixedScheduleOut | None = None


class TripRouteListItemOut(BaseModel):
    trip_route_no: int
    visit_day: int | None = None
    usage_status_no: int
    events: list[TripRouteEventListItemOut]


class AccomIn(BaseModel):
    accom_nm: str
    add: str
    accom_lat: float
    accom_lon: float
    check_in_dt: date
    check_out_dt: date


class AccomOut(BaseModel):
    accom_no: int
    accom_nm: str | None = None
    add: str | None = None
    accom_lat: float | None = None
    accom_lon: float | None = None
    check_in_dt: date | None = None
    check_out_dt: date | None = None

    class Config:
        from_attributes = True


class TripInterestOut(BaseModel):
    ctg_no: int
    rank: int

    class Config:
        from_attributes = True


class TripCreateRequest(BaseModel):
    event_no: int
    event_date: date
    start_dt: date
    end_dt: date
    start_tm: datetime | None = None
    end_tm: datetime | None = None
    # 출발지점(첫날)/완료지점(마지막날). 좌표는 프론트가 지도 SDK로 계산해서 그대로 보낸다 —
    # 서버는 지오코딩을 하지 않는다(accom_lat/lon, event_lat/lon과 동일한 기존 패턴).
    start_place: str | None = None
    start_place_lat: float | None = None
    start_place_lon: float | None = None
    end_place: str | None = None
    end_place_lat: float | None = None
    end_place_lon: float | None = None
    accoms: list[AccomIn] = []
    # 배열 순서 그대로가 우선순위(rank 1..N)가 된다.
    ctg_nos: list[int] = Field(min_length=1)
    # artist_group_no(그룹 전체) 또는 artist_nos(개별 멤버) 중 정확히 하나만 채워야 한다 — 배타적.
    artist_group_no: int | None = None
    artist_nos: list[int] = []
    trip_density_no: int


class TripOut(BaseModel):
    trip_no: int
    event_no: int
    event_date: date
    start_dt: date
    end_dt: date
    start_tm: datetime | None = None
    end_tm: datetime | None = None
    start_place: str | None = None
    start_place_lat: float | None = None
    start_place_lon: float | None = None
    end_place: str | None = None
    end_place_lat: float | None = None
    end_place_lon: float | None = None
    trip_density_no: int | None = None
    accoms: list[AccomOut]
    interests: list[TripInterestOut]
    artist_nos: list[int]


class RecommendScheduleItemOut(BaseModel):
    """AL02Pipeline.run()의 schedule 항목 그대로 — POST /trips/{trip_no}/recommend 응답용."""

    event_no: int | None = None
    event_nm: str
    ctg_nm: str | None = None
    travel_from_prev_min: int
    arrive: str
    depart: str
    stay_min: int
    is_concert: bool
    relevance: float


class RecommendDroppedOut(BaseModel):
    """시간 초과로 그 날 동선에서 제외된 장소(al02_pipeline.s4_solve_fallback 참고)."""

    day_index: int
    event_no: int | None = None
    event_nm: str | None = None


class RecommendSummaryOut(BaseModel):
    total_places: int
    total_travel_minutes: int
    validation_passed: bool
    validation_details: dict[str, bool]
    dropped_for_time: list[RecommendDroppedOut]


class RecommendDayOut(BaseModel):
    # 아직 저장 전이라 trip_route_no는 항상 null — 프론트가 이 결과를 편집해서
    # 기존 POST /trip-routes 바디({trip_no, routes:[{visit_day, event_no}]})로 변환해 저장한다.
    trip_route_no: int | None = None
    visit_day: int
    date: str
    is_concert_day: bool
    schedule: list[RecommendScheduleItemOut]


class RecommendDayDiversityOut(BaseModel):
    """2026-09-12 신규 — 날짜별 다양성 배정 상세(작업지시 6번 "tier별 사용 현황/목표수/
    실제수... 포함")."""

    day_index: int
    target_visits: int
    actual_visits: int  # 공연일은 공연 제외 순수 POI 개수
    tier2_event_nos: list[int]  # 그 날 Tier2(비선호 카테고리)로 채워진 event_no들
    relaxed: bool  # 3차(하루 카테고리 상한 2 완화)가 적용된 날짜인지


class RecommendDiversityOut(BaseModel):
    """2026-09-11~12 신규(다양성 제약 관찰 지표) — 이번 추천 결과의 다양성 제약 적용
    현황. QA/디버깅·프론트 참고용, 필수 소비 필드 아님."""

    shopping_count: int
    brand_counts: dict[str, int]
    unique_categories: int
    category_counts: dict[int, int]  # ctg_no -> 여행 전체 선택 개수
    tier1_used: int  # 선호 카테고리(Tier1)로 채택된 곳 수(공연 제외)
    tier2_used: int  # 비선호 카테고리(Tier2)로 채택된 곳 수
    diversity_relaxed: bool
    relaxed_days: list[int]  # 1일차=0 기준 day_index. 완화가 실제로 적용된 날짜만.
    # Tier2(+3차 완화)까지 다 쓰고도 목표 미달 날짜가 있으면 True(작업지시 6번,
    # warning code "insufficient_diverse_candidates"와 짝을 이룸).
    insufficient_diverse_candidates: bool
    incomplete_days: list[int]
    days_detail: list[RecommendDayDiversityOut]


class TripRecommendOut(BaseModel):
    trip_no: int
    scoring_policy: str
    summary: RecommendSummaryOut
    days: list[RecommendDayOut]
    # 후보 3개 미만일 때만 채워짐(기능명세서 6.3절) — 나머지는 요청 실패 없이 그냥 생략.
    warning: str | None = None
    # enable_diversity=False로 계산된 결과라면(현재는 항상 True) None.
    diversity: RecommendDiversityOut | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    # null 또는 미전달 = 새 세션 생성. 값을 주면 그 세션(본인 소유여야 함)의 기존 대화를
    # 이어간다(WBS 5.4.1).
    chat_session_no: int | None = None


class ChatOut(BaseModel):
    answer: str
    sources: list[int] = []
    is_fallback: bool = False
    chat_session_no: int


class ChatSessionListItemOut(BaseModel):
    chat_session_no: int
    created_at: datetime
    last_message_at: datetime | None = None
    # 세션의 첫 user 메시지를 짧게 잘라 보여주는 목록용 미리보기. 없으면(이론상 발생 안 함) null.
    preview: str | None = None


class ChatMessageOut(BaseModel):
    role: str
    content: str
    sources: list[int] | None = None
    is_fallback: bool | None = None
    created_at: datetime


class TripRouteItemIn(BaseModel):
    visit_day: int
    event_no: int


class TripRouteCreateRequest(BaseModel):
    trip_no: int
    routes: list[TripRouteItemIn] = Field(min_length=1)


class TripRouteEventOut(BaseModel):
    trip_route_event_no: int
    event_no: int
    seq: int | None = None

    class Config:
        from_attributes = True


class TripRouteOut(BaseModel):
    trip_route_no: int
    trip_no: int
    visit_day: int | None = None
    usage_status_no: int
    events: list[TripRouteEventOut]

    class Config:
        from_attributes = True


class AlternativeScoreBreakdownOut(BaseModel):
    artist_match: float
    category_fitness: float
    place_quality: float
    travel_fit: float
    diversity_fit: float


class AlternativeConstraintCheckOut(BaseModel):
    """2026-09-11~12 신규 — 다섯 다양성 축 + 시간/공연 버퍼, 전부 정보 표시용(문서 6.4).
    category_day_cap_ok/category_trip_cap_ok/shopping_trip_cap_ok는 alternatives
    후보 필터링에는 실제로 안 쓰인다(후보가 전부 교체 대상과 같은 ctg_no라 그 세 축은
    어떤 후보를 고르든 값이 똑같이 나오는 수학적 불변 — al02_diversity._diversity_checks
    참고) — 그래도 "이 트립/이 날짜가 이미 그 상한을 넘었는지" 자체는 유용한 정보라
    그대로 내려준다. 실제 필터링에 쓰이는 축은 duplicate_event_ok/brand_trip_cap_ok."""
    duplicate_event_ok: bool
    brand_trip_cap_ok: bool
    shopping_trip_cap_ok: bool
    category_day_cap_ok: bool
    category_trip_cap_ok: bool
    time_budget_ok: bool
    concert_buffer_ok: bool


class AlternativeEventOut(BaseModel):
    """al02_alternatives.get_alternatives() 결과 — GET /trips/{trip_no}/routes/{event_no}
    /alternatives 응답용(2026-09-10 신규, 2026-09-11~12 다양성 재검증/replacement_score
    확장). 정렬은 항상 distance_km 오름차순(요청 사양, relevance는 표시용일 뿐 정렬
    기준 아님)."""
    event_no: int
    event_nm: str
    ctg_no: int | None = None
    ctg_nm: str | None = None
    distance_km: float
    relevance: float  # base_relevance와 동일 — 기존 필드명 그대로 유지(응답 호환성)
    is_open: bool  # §4.4와 동일한 3단계 판정 — False면 그 날짜 영업 안 함(휴무), 후보 목록엔 남김
    base_relevance: float  # 취향 적합도(거리 미포함) — relevance와 값 동일
    replacement_score: float  # 0.60*base_relevance + 0.25*travel_fit + 0.15*diversity_fit
    display_score: int  # replacement_score*100, 반올림 — 프론트 표시용
    added_travel_min: int  # 교체 전후 그 날짜 실제 총 이동시간 차이(분), 음수면 오히려 단축
    # true면 위 added_travel_min이 travel_time_cache 미스로 haversine 근사가 섞인 값 —
    # 프론트는 "예상 추가 이동시간"처럼 근사치임을 표시하는 문구를 쓸 것.
    added_travel_min_is_approximate: bool
    score_breakdown: AlternativeScoreBreakdownOut
    constraint_check: AlternativeConstraintCheckOut


class TripListItemOut(BaseModel):
    trip_no: int
    event_nm: str
    # event 테이블에 지역 전용 컬럼이 없어 주소 원문을 그대로 내려준다 (예: "서울 송파구...").
    event_add: str
    start_dt: date
    end_dt: date
    status: str  # "완료" | "진행중" | "예정" — 저장값이 아니라 조회 시점마다 계산
    rating: float | None = None
    place_count: int
    # event_nm 파싱 대신 식별자 기반으로 그룹명을 내려준다. 개별 멤버 지정 이벤트는
    # artist_group_no가 없어 둘 다 None일 수 있다.
    artist_group_no: int | None = None
    group_nm: str | None = None


class ReviewOptOut(BaseModel):
    opt_no: int
    opt_nm: str
    sentiment: str

    class Config:
        from_attributes = True


class ReviewCreateRequest(BaseModel):
    """rating/opt_no/review_content 전부 선택사항 — 사용자가 원하는 항목만 채워서 보낸다."""

    opt_no: int | None = None
    rating: float | None = Field(default=None, ge=0.5, le=5.0)
    review_content: str | None = None

    @field_validator("rating")
    @classmethod
    def _rating_half_step(cls, v: float | None) -> float | None:
        if v is not None and (v * 2) % 1 != 0:
            raise ValueError("rating은 0.5 단위여야 합니다.")
        return v

    @model_validator(mode="after")
    def _at_least_one_field(self):
        content = (self.review_content or "").strip()
        if self.rating is None and self.opt_no is None and not content:
            raise ValueError("rating/opt_no/review_content 중 최소 하나는 값이 있어야 합니다.")
        return self


class ReviewOut(BaseModel):
    review_no: int
    trip_no: int
    opt_no: int | None = None
    rating: float | None = None
    review_content: str | None = None
    written_at: datetime

    class Config:
        from_attributes = True
