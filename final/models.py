from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)

from database import Base

# MySQL에서는 BIGINT로, SQLite(로컬 테스트)에서는 INTEGER로 컴파일된다.
# SQLite는 정확히 "INTEGER PRIMARY KEY"일 때만 자동증가(rowid)로 취급하기 때문.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class Nationality(Base):
    __tablename__ = "nationality"

    nationality_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    nationality_nm = Column(String(50))


class NatLang(Base):
    """국적별 기본 언어 매핑 (nationality_no 1:1 -> lang_no)."""

    __tablename__ = "nat_lang"

    nat_lang_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    nationality_no = Column(
        BigInteger, ForeignKey("nationality.nationality_no"), unique=True, nullable=False
    )
    lang_no = Column(BigInteger, ForeignKey("lang.lang_no"), nullable=False)


class Auth(Base):
    __tablename__ = "auth"

    auth_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    auth_nm = Column(String(50))


class Lang(Base):
    __tablename__ = "lang"

    lang_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    lang_nm = Column(String(50))


class UserStatus(Base):
    __tablename__ = "user_status"

    status_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    status_nm = Column(String(50))


class ArtistGroup(Base):
    __tablename__ = "artist_group"

    artist_group_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    group_nm = Column(String(100))
    agency = Column(String(100))
    debut_dt = Column(Date)
    fandom_nm = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())


class Artist(Base):
    __tablename__ = "artist"

    artist_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    artist_nm = Column(String(100), nullable=False)
    artist_group_no = Column(
        BigInteger, ForeignKey("artist_group.artist_group_no"), nullable=False
    )
    birth = Column(Date)
    real_nm = Column(String(100))
    gender = Column(String(10))
    origin = Column(String(100))
    agency = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())
    nationality_no = Column(BigInteger, ForeignKey("nationality.nationality_no"), nullable=False)


class User(Base):
    __tablename__ = "user"

    user_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    login_id = Column(String(50), unique=True, nullable=False, index=True)
    login_pw = Column(String(255), nullable=False)
    phone = Column(String(20))
    nationality_no = Column(BigInteger, ForeignKey("nationality.nationality_no"))
    nickname = Column(String(50), nullable=False, index=True)
    auth_no = Column(BigInteger, ForeignKey("auth.auth_no"))
    lang_no = Column(BigInteger, ForeignKey("lang.lang_no"))
    status_no = Column(BigInteger, ForeignKey("user_status.status_no"))
    name = Column(String(50))
    gender = Column(String(10))
    birth = Column(Date)
    created_at = Column(DateTime, server_default=func.now())
    # 마이그레이션(migration_user_profile.sql)으로 신규 추가되는 컬럼.
    profile_img = Column(String(255))


class RefreshToken(Base):
    """유저당 세션 1개 — 로그인할 때마다 기존 행을 덮어쓴다(재로그인하면 이전 세션은 끊김).
    원문 토큰은 서버에 저장하지 않고 해시만 들고 있다가 대조한다.
    migration_refresh_token.sql로 신규 생성되는 테이블."""

    __tablename__ = "refresh_token"

    refresh_token_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    user_no = Column(BigInteger, ForeignKey("user.user_no"), nullable=False, unique=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    issued_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime)


class FavoriteGroup(Base):
    __tablename__ = "favorite_group"

    favorite_group_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    artist_group_no = Column(
        BigInteger, ForeignKey("artist_group.artist_group_no"), nullable=False
    )
    user_no = Column(BigInteger, ForeignKey("user.user_no"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class OpStatus(Base):
    __tablename__ = "op_status"

    op_status_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    op_status_nm = Column(String(50), nullable=False)


class CtgType(Base):
    __tablename__ = "ctg_type"

    ctg_type_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    ctg_type_nm = Column(String(50), nullable=False)


class Ctg(Base):
    __tablename__ = "ctg"

    ctg_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    ctg_nm = Column(String(50), nullable=False)
    ctg_type_no = Column(BigInteger, ForeignKey("ctg_type.ctg_type_no"), nullable=False)


class Event(Base):
    __tablename__ = "event"

    event_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    event_nm = Column(String(255), nullable=False)
    start_dt = Column(DateTime)
    end_dt = Column(DateTime)
    event_desc = Column(Text)
    event_dtl = Column(Text)
    # 2026-09-09 신규: 이벤트 대표 이미지 URL(migration_event_img_url.sql). 도입 당시엔
    # "컬럼 생성까지만, 백필은 범위 밖"이었으나 이후 DA 쪽에서 대부분 채움(2026-09-10 기준
    # 1,928건 중 1,735건 값 있음) — 남은 NULL은 GET /events/{event_no}에서 그대로 null 반환.
    event_img_url = Column(String(500))
    ctg_no = Column(BigInteger, ForeignKey("ctg.ctg_no"), nullable=False)
    add = Column(String(255), nullable=False)
    post = Column(String(10))
    event_lat = Column(Numeric(9, 6), nullable=False)
    event_lon = Column(Numeric(9, 6), nullable=False)
    op_status_no = Column(BigInteger, ForeignKey("op_status.op_status_no"), nullable=False)
    artist_no = Column(BigInteger, ForeignKey("artist.artist_no"), nullable=False)
    artist_group_no = Column(
        BigInteger, ForeignKey("artist_group.artist_group_no"), nullable=False
    )
    created_at = Column(DateTime, server_default=func.now())


class Congestion(Base):
    """혼잡도 — 다른 팀(AL-02) 관리 데이터. 한 event_no당 weekday(1~7) × hour_of_day(0~23)
    조합 수만큼 행이 있는 게 정상 구조다(그 장소의 요일·시간대별 평균 혼잡도 패턴).
    day_ratio/cong_level은 DB GENERATED STORED 컬럼이라 여기서 값을 쓰지 않는다 — MySQL이
    day_avg/week_avg로부터 자동 계산."""

    __tablename__ = "congestion"

    cong_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    event_no = Column(BigInteger, ForeignKey("event.event_no"), nullable=False)
    weekday = Column(String(2), nullable=False)
    hour_of_day = Column(Integer, nullable=False)
    day_avg = Column(Numeric(4, 1), nullable=False)
    week_avg = Column(Numeric(4, 1))
    day_ratio = Column(Numeric(4, 2))
    cong_level = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())


class EventOpHour(Base):
    """운영시간 — 다른 팀 관리 데이터. op_dt는 컬럼명과 달리 실제 캘린더 날짜가 아니라
    '월'~'일' 요일 문자열이다(실 데이터 확인함, VARCHAR(50)) — congestion.weekday는
    '1'~'7' 숫자 문자열인데 이 테이블은 한글 요일이라 인코딩이 서로 다르다. 한 event_no당
    요일 7개가 다 채워지거나 아예 하나도 없는 것만 실측됨(부분만 채워진 경우는 없었음)."""

    __tablename__ = "event_op_hour"

    op_hour_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    open_tm = Column(String(5))
    close_tm = Column(String(5))
    event_no = Column(BigInteger, ForeignKey("event.event_no"), nullable=False)
    op_dt = Column(String(50))
    created_at = Column(DateTime)


class TripDensity(Base):
    __tablename__ = "trip_density"

    trip_density_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    trip_density_nm = Column(String(50), nullable=False)


class Trip(Base):
    """여행 초안. 이벤트 선택 + 여행 기간이 모두 정해진 시점에 행 하나가 생성된다
    (중간 상태를 DB에 걸쳐 두지 않음). trip_density_no/trip_type_no는 이후 단계
    (동선 스타일 선택)에서 채워지므로 이 시점엔 NULL — migration_trip_draft.sql로
    nullable 처리해야 INSERT가 성공한다."""

    __tablename__ = "trip"

    trip_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    start_dt = Column(Date, nullable=False)
    end_dt = Column(Date, nullable=False)
    start_place = Column(String(255))
    end_place = Column(String(255))
    event_no = Column(BigInteger, ForeignKey("event.event_no"), nullable=False)
    # 멀티데이 이벤트 중 사용자가 고른 특정 날짜. 마이그레이션으로 신규 추가되는 컬럼.
    event_date = Column(Date, nullable=False)
    user_no = Column(BigInteger, ForeignKey("user.user_no"), nullable=False)
    trip_density_no = Column(BigInteger, ForeignKey("trip_density.trip_density_no"))
    start_tm = Column(DateTime)
    end_tm = Column(DateTime)
    start_place_lat = Column(Numeric(9, 6))
    start_place_lon = Column(Numeric(9, 6))
    end_place_lat = Column(Numeric(9, 6))
    end_place_lon = Column(Numeric(9, 6))
    # trip_type 테이블이 DB에서 이미 삭제된 상태라 FK를 걸지 않는다 (팀원 작업 진행 중으로 보임).
    # 죽은 컬럼으로 간주 — 어떤 코드에서도 값을 쓰지 않는다.
    trip_type_no = Column(BigInteger)
    created_at = Column(DateTime, server_default=func.now())


class TripInterest(Base):
    """여행 초안이 선택한 선호 카테고리(ctg_type_no 2/3 소속 ctg). rank는 프론트가
    보낸 배열 순서(1부터)."""

    __tablename__ = "trip_interest"

    trip_interest_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    ctg_no = Column(BigInteger, ForeignKey("ctg.ctg_no"), nullable=False)
    trip_no = Column(BigInteger, ForeignKey("trip.trip_no"), nullable=False)
    rank = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())


class RuteArtistSelect(Base):
    """여행에서 실제로 보러 갈 멤버 선택. PK가 select_no만 bigint가 아니라 int로 되어 있다."""

    __tablename__ = "rute_artist_select"

    select_no = Column(Integer, primary_key=True, autoincrement=True)
    artist_no = Column(BigInteger, ForeignKey("artist.artist_no"))
    trip_no = Column(BigInteger, ForeignKey("trip.trip_no"))


class Accom(Base):
    __tablename__ = "accom"

    accom_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    accom_nm = Column(String(100))
    # 2026-09-09: DECIMAL(10,8)이던 시절 정수부 2자리라 한국 경도(126~129대, 3자리 필요)가
    # 조용히 99.99999999로 클리핑되던 버그가 있었다(migration_accom_lon_precision.sql로 수정).
    # 최종 스펙은 DECIMAL(11,8) — event_lat/lon, trip.start_place_lat/lon 등 다른 좌표
    # 컬럼(9,6)과는 다르게, accom만 소수부 8자리까지 유지하는 쪽으로 확정됨(의도적 선택,
    # 2026-09-09). 기존 13행 중 이 버그로 잘못 저장된 값은(테스트 데이터로 확인, 복구 불필요)
    # 재지오코딩 스크립트(backfill_accom_coords.py)를 준비해뒀으나 이번 라운드엔 실행 안 함.
    accom_lon = Column(Numeric(11, 8))
    accom_lat = Column(Numeric(11, 8))
    trip_no = Column(BigInteger, ForeignKey("trip.trip_no"), nullable=False)
    check_in_dt = Column(Date)
    check_out_dt = Column(Date)
    add = Column(String(255))
    created_by = Column(String(50))
    created_at = Column(DateTime, server_default=func.now())


class UsageStatus(Base):
    __tablename__ = "usage_status"

    usage_status_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    usage_status_nm = Column(String(50))


class TripRoute(Base):
    """확정된 동선의 일자 단위 로우 — 하루당 1개. 그 날 방문하는 이벤트들은
    TripRouteEvent에 자식으로 딸린다(하루에 보통 4~5개). usage_status_no는 생성
    시점엔 항상 '대기중', 배치(batch.py)가 날짜 경과에 따라 자동 전환한다."""

    __tablename__ = "trip_route"

    trip_route_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    trip_no = Column(BigInteger, ForeignKey("trip.trip_no"), nullable=False)
    visit_day = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())
    usage_status_no = Column(BigInteger, ForeignKey("usage_status.usage_status_no"), nullable=False)


class TripRouteEvent(Base):
    """하루 동선(TripRoute) 안의 개별 이벤트. seq는 그 날 안에서의 방문 순서(1부터)."""

    __tablename__ = "trip_route_event"

    trip_route_event_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    trip_route_no = Column(BigInteger, ForeignKey("trip_route.trip_route_no"), nullable=False)
    event_no = Column(BigInteger, ForeignKey("event.event_no"), nullable=False)
    seq = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())


class ReviewOpt(Base):
    __tablename__ = "review_opt"

    opt_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    opt_nm = Column(String(50), nullable=False)
    sentiment = Column(String(10), nullable=False)


class Review(Base):
    """trip.trip_no에 UNIQUE 제약이 걸려있어 trip당 리뷰는 최대 1개."""

    __tablename__ = "review"

    review_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    review_content = Column(Text)
    opt_no = Column(BigInteger, ForeignKey("review_opt.opt_no"))
    written_at = Column(DateTime, nullable=False)
    rating = Column(Numeric(2, 1))
    trip_no = Column(BigInteger, ForeignKey("trip.trip_no"), nullable=False, unique=True)
    created_at = Column(DateTime, server_default=func.now())


class VisitFeedback(Base):
    """장소(그 날의 특정 이벤트)별 '좋아요' — reaction 테이블이 삭제되면서 온/오프
    단순 모델로 바뀜. 행이 있으면 좋아요, 없으면 아니다. trip_route가 일자 단위로
    바뀌면서 좋아요 대상은 TripRouteEvent(이벤트 단위)를 가리키도록 변경됨."""

    __tablename__ = "visit_feedback"

    visit_feedback_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    trip_route_event_no = Column(
        BigInteger, ForeignKey("trip_route_event.trip_route_event_no"), nullable=False
    )
    created_at = Column(DateTime, server_default=func.now())


class ExternalReview(Base):
    """장소(이벤트) 하나에 여러 리뷰 행이 있을 수 있다. review_source 테이블(구글맵/
    카카오맵/캐치테이블 라벨)은 팀에서 삭제했고 source_no 컬럼도 함께 없어졌다 —
    출처 라벨은 더 이상 DB에 없다."""

    __tablename__ = "external_review"

    external_review_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    review = Column(Text)
    event_no = Column(BigInteger, ForeignKey("event.event_no"), nullable=False)
    rating = Column(Numeric(2, 1))
    fake_score = Column(Numeric(5, 2))
    total_score = Column(Numeric(5, 2))
    author_id = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())


class ChatSession(Base):
    """챗봇 대화 세션(WBS 5.4.1). 세션 하나 = 연속된 멀티턴 대화 한 묶음."""

    __tablename__ = "chat_session"

    chat_session_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    user_no = Column(BigInteger, ForeignKey("user.user_no"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    last_message_at = Column(DateTime)


class ChatMessage(Base):
    """세션 내 개별 메시지. sources/is_fallback은 role='assistant' 행에만 채워지고,
    role='user' 행은 둘 다 NULL(적용 대상이 아님)."""

    __tablename__ = "chat_message"

    chat_message_no = Column(BigIntPK, primary_key=True, autoincrement=True)
    chat_session_no = Column(BigInteger, ForeignKey("chat_session.chat_session_no"), nullable=False)
    role = Column(String(10), nullable=False)  # 'user' | 'assistant'
    content = Column(Text, nullable=False)
    sources = Column(JSON)
    is_fallback = Column(Boolean)
    created_at = Column(DateTime, server_default=func.now())
