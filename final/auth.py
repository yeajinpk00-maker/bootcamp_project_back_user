import os
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import al02_alternatives
import al02_candidates
import chatbot
import schemas
import security
import travel_time_service
from al02_pipeline import AL02Pipeline, DepotOverlapError, accoms_overlap
from al02_policy import (
    CONCERT_VISIT_COUNT_PROFILES,
    TRIP_DENSITY_TO_WREL_KEY,
    VISIT_COUNT_PROFILES,
    WREL_PROFILES,
)
from database import get_db
from deps import get_current_user
from models import (
    Accom,
    Artist,
    ArtistGroup,
    Auth,
    ChatMessage,
    ChatSession,
    Ctg,
    Event,
    EventOpHour,
    ExternalReview,
    FavoriteGroup,
    Lang,
    NatLang,
    Nationality,
    RefreshToken,
    Review,
    ReviewOpt,
    RuteArtistSelect,
    Trip,
    TripDensity,
    TripInterest,
    TripRoute,
    TripRouteEvent,
    UsageStatus,
    User,
    UserStatus,
    VisitFeedback,
)

router = APIRouter(tags=["auth"])

# 로컬 개발(http://localhost)에서는 secure 쿠키가 전송되지 않으므로 기본은 False.
# 배포 시 HTTPS 환경이면 .env에 COOKIE_SECURE=true 를 설정하세요.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

DEFAULT_AUTH_NM = "사용자"
DEFAULT_STATUS_NM = "활성"
WAITING_USAGE_STATUS_NM = "대기중"

# user_status.status_no 2=정지, 4=탈퇴 (ERD 확정값). 로그인 시 이 상태면 차단한다.
BLOCKED_STATUS_MESSAGES: dict[int, str] = {
    2: "정지된 계정입니다.",
    4: "탈퇴한 계정입니다.",
}
WITHDRAWN_STATUS_NO = 4

PROFILE_IMAGE_DIR = Path("uploads/profile_images")
PROFILE_IMAGE_URL_PREFIX = "/uploads/profile_images"
PROFILE_IMAGE_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PROFILE_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5MB


def _get_default_auth_no(db: Session) -> int:
    auth = db.query(Auth).filter(Auth.auth_nm == DEFAULT_AUTH_NM).first()
    if not auth:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"기본 권한('{DEFAULT_AUTH_NM}') 코드가 설정되어 있지 않습니다.",
        )
    return auth.auth_no


def _get_default_status_no(db: Session) -> int:
    user_status = db.query(UserStatus).filter(UserStatus.status_nm == DEFAULT_STATUS_NM).first()
    if not user_status:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"기본 상태('{DEFAULT_STATUS_NM}') 코드가 설정되어 있지 않습니다.",
        )
    return user_status.status_no


def _get_waiting_usage_status_no(db: Session) -> int:
    usage_status = (
        db.query(UsageStatus).filter(UsageStatus.usage_status_nm == WAITING_USAGE_STATUS_NM).first()
    )
    if not usage_status:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"기본 상태('{WAITING_USAGE_STATUS_NM}') 코드가 설정되어 있지 않습니다.",
        )
    return usage_status.usage_status_no


@router.get("/nationalities", response_model=list[schemas.NationalityOut], tags=["reference"])
def list_nationalities(db: Session = Depends(get_db)):
    # nat_lang은 모든 국적에 대해 매핑이 이미 채워져 있는 전제(없으면 English로
    # DB에 명시 저장됨)이므로 inner join으로 바로 lang_nm을 가져온다.
    rows = (
        db.query(Nationality, Lang.lang_no, Lang.lang_nm)
        .join(NatLang, NatLang.nationality_no == Nationality.nationality_no)
        .join(Lang, Lang.lang_no == NatLang.lang_no)
        .all()
    )
    return [
        schemas.NationalityOut(
            nationality_no=nationality.nationality_no,
            nationality_nm=nationality.nationality_nm,
            lang_no=lang_no,
            lang_nm=lang_nm,
        )
        for nationality, lang_no, lang_nm in rows
    ]


@router.get("/langs", response_model=list[schemas.LangOut], tags=["reference"])
def list_langs(db: Session = Depends(get_db)):
    return db.query(Lang).all()


# ctg_type_no=2 = '성지', 3 = '관광 명소' — 선호 카테고리 선택지는 이 두 타입뿐이다.
INTEREST_CTG_TYPE_NOS = (2, 3)


@router.get("/interests", response_model=list[schemas.InterestOut], tags=["reference"])
def list_interests(db: Session = Depends(get_db)):
    return (
        db.query(Ctg)
        .filter(Ctg.ctg_type_no.in_(INTEREST_CTG_TYPE_NOS))
        .order_by(Ctg.ctg_no)
        .all()
    )


@router.get("/review-opts", response_model=list[schemas.ReviewOptOut], tags=["reference"])
def list_review_opts(db: Session = Depends(get_db)):
    return db.query(ReviewOpt).order_by(ReviewOpt.opt_no).all()


def _serialize_artist_groups(db: Session, group_nos: list[int] | None = None) -> list[schemas.ArtistGroupOut]:
    """group_nos를 주면 그 그룹들만, 안 주면 전체 그룹을 멤버 목록과 함께 직렬화한다."""
    groups_query = db.query(ArtistGroup)
    if group_nos is not None:
        groups_query = groups_query.filter(ArtistGroup.artist_group_no.in_(group_nos))
    groups = groups_query.order_by(ArtistGroup.artist_group_no).all()

    members_query = db.query(Artist.artist_group_no, Artist.artist_nm)
    if group_nos is not None:
        members_query = members_query.filter(Artist.artist_group_no.in_(group_nos))
    members_by_group: dict[int, list[str]] = {}
    for group_no, artist_nm in members_query.order_by(Artist.artist_no).all():
        members_by_group.setdefault(group_no, []).append(artist_nm)

    return [
        schemas.ArtistGroupOut(
            artist_group_no=g.artist_group_no,
            group_nm=g.group_nm,
            agency=g.agency,
            debut_dt=g.debut_dt,
            fandom_nm=g.fandom_nm,
            members=members_by_group.get(g.artist_group_no, []),
        )
        for g in groups
    ]


@router.get("/artist-groups", response_model=list[schemas.ArtistGroupOut], tags=["reference"])
def list_artist_groups(db: Session = Depends(get_db)):
    return _serialize_artist_groups(db)


@router.get(
    "/artist-groups/{artist_group_no}",
    response_model=schemas.ArtistGroupOut,
    tags=["reference"],
)
def get_artist_group(artist_group_no: int, db: Session = Depends(get_db)):
    group = db.query(ArtistGroup).filter(ArtistGroup.artist_group_no == artist_group_no).first()
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="존재하지 않는 그룹입니다."
        )

    members = (
        db.query(Artist.artist_nm)
        .filter(Artist.artist_group_no == artist_group_no)
        .order_by(Artist.artist_no)
        .all()
    )

    return schemas.ArtistGroupOut(
        artist_group_no=group.artist_group_no,
        group_nm=group.group_nm,
        agency=group.agency,
        debut_dt=group.debut_dt,
        fandom_nm=group.fandom_nm,
        members=[m[0] for m in members],
    )


@router.get("/users/check-login-id", response_model=schemas.CheckLoginIdResponse, tags=["reference"])
def check_login_id(login_id: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    exists = db.query(User).filter(User.login_id == login_id).first() is not None
    return schemas.CheckLoginIdResponse(available=not exists)


@router.post("/signup", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def signup(payload: schemas.SignupRequest, db: Session = Depends(get_db)):
    if payload.login_pw != payload.login_pw_confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="비밀번호와 비밀번호 확인이 일치하지 않습니다.",
        )

    if db.query(User).filter(User.login_id == payload.login_id).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "이미 사용 중인 아이디입니다.", "fields": ["login_id"]},
        )

    default_auth_no = _get_default_auth_no(db)
    default_status_no = _get_default_status_no(db)

    try:
        user = User(
            login_id=payload.login_id,
            login_pw=security.hash_password(payload.login_pw),
            phone=payload.phone,
            nationality_no=payload.nationality_no,
            nickname=payload.nickname,
            auth_no=default_auth_no,
            lang_no=payload.lang_no,
            status_no=default_status_no,
            name=payload.name,
            gender=payload.gender,
            birth=payload.birth,
        )
        db.add(user)
        db.flush()  # user_no 채번

        for artist_group_no in payload.favorite_group_nos:
            db.add(FavoriteGroup(artist_group_no=artist_group_no, user_no=user.user_no))

        db.commit()
    except IntegrityError as e:
        db.rollback()
        print(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="입력값을 확인해주세요 (존재하지 않는 코드 값이거나 제약 조건을 위반했습니다).",
        )
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    return user


@router.post("/signin", response_model=schemas.Token)
def signin(
    credentials: schemas.UserLogin,
    response: Response,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.login_id == credentials.login_id).first()

    if not user or not security.verify_password(credentials.password, user.login_pw):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="아이디 또는 비밀번호가 올바르지 않습니다.",
        )

    blocked_message = BLOCKED_STATUS_MESSAGES.get(user.status_no)
    if blocked_message:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=blocked_message)

    access_token = security.create_access_token(data={"sub": str(user.user_no)})

    # 로그인 상태 유지: httpOnly 쿠키에 JWT 저장
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

    _issue_refresh_token(db, response, user.user_no)

    return schemas.Token(access_token=access_token)


def _issue_refresh_token(db: Session, response: Response, user_no: int) -> None:
    """유저당 세션 1개 — 기존 refresh_token 행이 있으면 덮어쓰고, 없으면 새로 만든다."""
    raw_token = security.generate_refresh_token()
    token_hash = security.hash_refresh_token(raw_token)
    expires_at = datetime.utcnow() + timedelta(days=security.REFRESH_TOKEN_EXPIRE_DAYS)

    existing = db.query(RefreshToken).filter(RefreshToken.user_no == user_no).first()
    if existing:
        existing.token_hash = token_hash
        existing.issued_at = datetime.utcnow()
        existing.expires_at = expires_at
        existing.revoked_at = None
    else:
        db.add(RefreshToken(user_no=user_no, token_hash=token_hash, expires_at=expires_at))
    db.commit()

    response.set_cookie(
        key="refresh_token",
        value=raw_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=security.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )


@router.post("/auth/refresh", response_model=schemas.Token)
def refresh_access_token(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """refresh_token 쿠키로 새 access_token을 재발급한다. refresh_token 자체는
    회전(재발급)하지 않고 그대로 둔다 — 필요해지면 나중에 rotation 방식으로 바꿀 수 있음."""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="리프레시 토큰이 필요합니다."
        )

    token_hash = security.hash_refresh_token(refresh_token)
    row = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
    if not row or row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 리프레시 토큰입니다."
        )
    if row.expires_at < datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="만료된 리프레시 토큰입니다."
        )

    user = db.query(User).filter(User.user_no == row.user_no).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자를 찾을 수 없습니다."
        )
    blocked_message = BLOCKED_STATUS_MESSAGES.get(user.status_no)
    if blocked_message:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=blocked_message)

    access_token = security.create_access_token(data={"sub": str(user.user_no)})
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return schemas.Token(access_token=access_token)


@router.post("/signout")
def signout(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    if refresh_token:
        token_hash = security.hash_refresh_token(refresh_token)
        row = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
        if row and row.revoked_at is None:
            row.revoked_at = datetime.utcnow()
            db.commit()

    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"message": "로그아웃되었습니다."}


def _build_profile(db: Session, user: User) -> schemas.UserProfileOut:
    favorite_group_nos = [
        fg.artist_group_no
        for fg in db.query(FavoriteGroup).filter(FavoriteGroup.user_no == user.user_no).all()
    ]
    favorite_groups = _serialize_artist_groups(db, favorite_group_nos) if favorite_group_nos else []
    return schemas.UserProfileOut(
        user_no=user.user_no,
        login_id=user.login_id,
        nickname=user.nickname,
        nationality_no=user.nationality_no,
        lang_no=user.lang_no,
        profile_img=user.profile_img,
        favorite_groups=favorite_groups,
    )


@router.get("/me", response_model=schemas.UserProfileOut)
def read_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """마이 페이지 표시 + 정보수정 화면 초기값 채우기 겸용."""
    return _build_profile(db, current_user)


@router.patch("/me", response_model=schemas.UserProfileOut)
def update_me(
    payload: schemas.MeUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.nickname is not None:
        current_user.nickname = payload.nickname
    if payload.nationality_no is not None:
        current_user.nationality_no = payload.nationality_no
    if payload.lang_no is not None:
        current_user.lang_no = payload.lang_no

    if payload.favorite_group_nos is not None:
        db.query(FavoriteGroup).filter(FavoriteGroup.user_no == current_user.user_no).delete()
        for artist_group_no in payload.favorite_group_nos:
            db.add(FavoriteGroup(artist_group_no=artist_group_no, user_no=current_user.user_no))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "입력값을 확인해주세요 (존재하지 않는 코드 값이거나 제약 조건을 위반했습니다).",
                "fields": [],
            },
        )
    except Exception:
        db.rollback()
        raise

    db.refresh(current_user)
    return _build_profile(db, current_user)


@router.post("/me/profile-image", response_model=schemas.UserProfileOut)
def upload_profile_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in PROFILE_IMAGE_ALLOWED_EXTENSIONS:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "지원하지 않는 이미지 형식입니다 (jpg/jpeg/png/webp만 가능).",
            ["file"],
        )

    content = file.file.read()
    if len(content) > PROFILE_IMAGE_MAX_BYTES:
        raise _err(status.HTTP_400_BAD_REQUEST, "이미지 용량은 5MB를 넘을 수 없습니다.", ["file"])

    PROFILE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    (PROFILE_IMAGE_DIR / filename).write_bytes(content)

    old_path = current_user.profile_img
    current_user.profile_img = f"{PROFILE_IMAGE_URL_PREFIX}/{filename}"
    db.commit()
    db.refresh(current_user)

    if old_path and old_path.startswith(PROFILE_IMAGE_URL_PREFIX):
        old_file = PROFILE_IMAGE_DIR / Path(old_path).name
        old_file.unlink(missing_ok=True)

    return _build_profile(db, current_user)


@router.post("/me/withdraw")
def withdraw_me(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """소프트 삭제 — status_no만 4(탈퇴)로 바꾸고 row/연관 데이터는 그대로 둔다."""
    current_user.status_no = WITHDRAWN_STATUS_NO
    db.query(RefreshToken).filter(RefreshToken.user_no == current_user.user_no).update(
        {"revoked_at": datetime.utcnow()}
    )
    db.commit()
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"message": "탈퇴 처리되었습니다."}


@router.get("/me/favorite-groups", response_model=list[schemas.ArtistGroupOut], tags=["reference"])
def list_my_favorite_groups(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    favorite_group_nos = [
        fg.artist_group_no
        for fg in db.query(FavoriteGroup)
        .filter(FavoriteGroup.user_no == current_user.user_no)
        .all()
    ]
    if not favorite_group_nos:
        return []
    return _serialize_artist_groups(db, favorite_group_nos)


# ctg_type_no=1 = '메인이벤트'(행사). event -> ctg -> ctg_type로 한 단계 거쳐야 한다
# (event에 ctg_type_no 직접 FK 없음).
MAIN_EVENT_CTG_TYPE_NO = 1

# trip_density_no -> al02_policy.WREL_PROFILES 키는 al02_policy.TRIP_DENSITY_TO_WREL_KEY로
# 이동함(2026-09-11) — al02_alternatives.py도 같은 매핑이 필요해져 공용 정책 모듈로 옮김.

# 2026-09-12 — 프론트 리포트(trip_no=43, balanced에서 1일차 1곳/2일차 공연만/3일차 0곳)
# 실측 재현 결과, "하루 동일 ctg_no 최대 1곳" 하드 제약은 trip_interest 실측(70개 트립 중
# 66개가 카테고리 3개, 4개가 2개, 4개 이상 선택 0건)과 맞물려 Tier1(선호 카테고리)만으로는
# 방문 목표를 구조적으로 못 채우는 게 일반적인 상황임을 확인했다. Tier1→Tier2→(dense
# 한정) 3차 완화까지 구현·al02_selftest.py 회귀 없음·T01~T21 전부 PASS까지 확인했지만,
# 작업 지시에 따라 enable_diversity(Tier2 개방 + 3차 완화)는 일단 프로덕션에서 꺼둔다 —
# 동일 이벤트/브랜드/쇼핑 전체-여행 1회 + 하루 동일 ctg_no 최대 1곳(ENABLE_HARD_DEDUP)은
# 계속 유지한다. 사용자가 최종 확인 후 아래 값을 True로 바꾸면 Tier2/3차 완화가 함께
# 켜진다(fetch_candidates의 include_tier2도 이 값에 맞춰 같이 조절됨 — 따로 안 건드려도 됨).
ENABLE_DIVERSITY_TIERS = False
ENABLE_HARD_DEDUP = True  # 동일 이벤트/브랜드(allow-list)/쇼핑 전체-여행 1회 — 항상 유지


@router.get("/events/main", response_model=list[schemas.EventOut], tags=["trip"])
def list_main_events(
    artist_group_no: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    favorite_group_nos = [
        fg.artist_group_no
        for fg in db.query(FavoriteGroup)
        .filter(FavoriteGroup.user_no == current_user.user_no)
        .all()
    ]

    if artist_group_no is not None:
        if artist_group_no not in favorite_group_nos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="즐겨찾기한 그룹이 아닙니다.",
            )
        target_group_nos = [artist_group_no]
    else:
        target_group_nos = favorite_group_nos

    if not target_group_nos:
        return []

    today_start = datetime.combine(date.today(), datetime.min.time())
    return (
        db.query(Event)
        .join(Ctg, Event.ctg_no == Ctg.ctg_no)
        .filter(
            Ctg.ctg_type_no == MAIN_EVENT_CTG_TYPE_NO,
            Event.artist_group_no.in_(target_group_nos),
            Event.end_dt >= today_start,
        )
        .order_by(Event.start_dt)
        .all()
    )


@router.get(
    "/events/{event_no}/members", response_model=list[schemas.ArtistOut], tags=["trip"]
)
def list_event_group_members(event_no: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.event_no == event_no).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="존재하지 않는 이벤트입니다."
        )

    return (
        db.query(Artist)
        .filter(Artist.artist_group_no == event.artist_group_no)
        .order_by(Artist.artist_no)
        .all()
    )


def _err(status_code: int, message: str, fields: list[str], **extra):
    return HTTPException(status_code=status_code, detail={"message": message, "fields": fields, **extra})


@router.post("/trips", response_model=schemas.TripOut, status_code=status.HTTP_201_CREATED, tags=["trip"])
def create_trip(
    payload: schemas.TripCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """'어떤 이벤트에 참여하시나요' ~ '확인(동선 만들기)' 전 구간에서 모은 값을 한 번에 받아
    trip + 자식 행(accom/trip_interest/rute_artist_select)을 한 트랜잭션으로 생성한다.
    중간 화면들은 아무것도 저장하지 않고 프론트가 값을 들고 있다가 여기서 한 번만 호출한다."""

    # 1) 이벤트 + 날짜
    event = (
        db.query(Event)
        .join(Ctg, Event.ctg_no == Ctg.ctg_no)
        .filter(Event.event_no == payload.event_no, Ctg.ctg_type_no == MAIN_EVENT_CTG_TYPE_NO)
        .first()
    )
    if not event:
        raise _err(status.HTTP_404_NOT_FOUND, "존재하지 않는 메인 이벤트입니다.", ["event_no"])

    event_start = event.start_dt.date() if event.start_dt else None
    event_end = event.end_dt.date() if event.end_dt else None
    if event_start is None or event_end is None or not (event_start <= payload.event_date <= event_end):
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "event_date가 이벤트 진행 기간 범위를 벗어났습니다.",
            ["event_date"],
            allowed_range={
                "min": event_start.isoformat() if event_start else None,
                "max": event_end.isoformat() if event_end else None,
            },
        )

    # 2) 여행 기간
    event_date = payload.event_date
    start_min, start_max = event_date - timedelta(days=1), event_date
    end_min, end_max = event_date, event_date + timedelta(days=1)

    if not (start_min <= payload.start_dt <= start_max):
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "시작일이 허용 범위를 벗어났습니다.",
            ["start_dt"],
            allowed_range={"min": start_min.isoformat(), "max": start_max.isoformat()},
        )
    if not (end_min <= payload.end_dt <= end_max):
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "완료일이 허용 범위를 벗어났습니다.",
            ["end_dt"],
            allowed_range={"min": end_min.isoformat(), "max": end_max.isoformat()},
        )
    if payload.start_dt > payload.end_dt:
        raise _err(
            status.HTTP_400_BAD_REQUEST, "시작일은 완료일보다 늦을 수 없습니다.", ["start_dt", "end_dt"]
        )

    # 3) 숙소 (0개 이상)
    accom_min, accom_max = event_date - timedelta(days=2), event_date + timedelta(days=2)
    for idx, accom in enumerate(payload.accoms):
        if not (accom_min <= accom.check_in_dt <= accom_max):
            raise _err(
                status.HTTP_400_BAD_REQUEST,
                f"{idx}번째 숙소의 체크인 날짜가 허용 범위를 벗어났습니다.",
                [f"accoms[{idx}].check_in_dt"],
                allowed_range={"min": accom_min.isoformat(), "max": accom_max.isoformat()},
            )
        if not (accom_min <= accom.check_out_dt <= accom_max):
            raise _err(
                status.HTTP_400_BAD_REQUEST,
                f"{idx}번째 숙소의 체크아웃 날짜가 허용 범위를 벗어났습니다.",
                [f"accoms[{idx}].check_out_dt"],
                allowed_range={"min": accom_min.isoformat(), "max": accom_max.isoformat()},
            )
        # 2026-09-10 추가: 체크인이 체크아웃보다 늦을 수 없다 — 이게 뚫려 있으면
        # AL02Pipeline.pick_depot_accom()의 "check_in_dt <= 날짜 <= check_out_dt" 판정이
        # 애초에 항상 거짓이 되어 그 숙소가 어느 날짜에도 유효하지 않게 조용히 무시된다.
        if accom.check_in_dt > accom.check_out_dt:
            raise _err(
                status.HTTP_400_BAD_REQUEST,
                f"{idx}번째 숙소의 체크인 날짜가 체크아웃 날짜보다 늦습니다.",
                [f"accoms[{idx}].check_in_dt", f"accoms[{idx}].check_out_dt"],
            )

    # 2026-09-10 추가, 2026-09-11 반열린 구간으로 정정: 같은 trip 안에서 숙소끼리
    # 체크인~체크아웃 기간이 겹치면 안 된다(팀 확정 정책 — 같은 날짜에 유효한 숙소는
    # 항상 1곳이어야 함. 동일 숙소 연박은 되고 날짜별로 A->B->C처럼 숙소가 바뀌는 것도
    # 되지만, 두 숙소 기간이 실제로 겹치는 입력 자체는 막는다). 판정 기준은
    # accoms_overlap() 참고 — "A 체크아웃일 = B 체크인일"은 겹침이 아니다.
    # trip_no=32~36에서 이 검증 없이 실제로 겹치는 데이터가 생성된 적이 있었음(조사 결과,
    # 아래 완료 보고 참고) — 프론트 폼단 검증은 별도(이번 범위 밖), 여기가 최종 방어선.
    for i in range(len(payload.accoms)):
        for j in range(i + 1, len(payload.accoms)):
            a, b = payload.accoms[i], payload.accoms[j]
            if accoms_overlap(a.check_in_dt, a.check_out_dt, b.check_in_dt, b.check_out_dt):
                raise _err(
                    status.HTTP_400_BAD_REQUEST,
                    f"{i}번째 숙소와 {j}번째 숙소의 체크인~체크아웃 기간이 겹칩니다. "
                    "같은 날짜에는 숙소가 하나만 유효해야 합니다.",
                    [f"accoms[{i}]", f"accoms[{j}]"],
                    overlapping={
                        f"accoms[{i}]": {"check_in_dt": a.check_in_dt.isoformat(),
                                         "check_out_dt": a.check_out_dt.isoformat()},
                        f"accoms[{j}]": {"check_in_dt": b.check_in_dt.isoformat(),
                                         "check_out_dt": b.check_out_dt.isoformat()},
                    },
                )

    # 4) 선호 카테고리 (최소 1개, 순서 = 우선순위)
    if len(set(payload.ctg_nos)) != len(payload.ctg_nos):
        raise _err(status.HTTP_400_BAD_REQUEST, "중복된 ctg_no가 있습니다.", ["ctg_nos"])
    found_ctg_nos = {
        row.ctg_no
        for row in db.query(Ctg.ctg_no)
        .filter(Ctg.ctg_no.in_(payload.ctg_nos), Ctg.ctg_type_no.in_(INTEREST_CTG_TYPE_NOS))
        .all()
    }
    missing_ctgs = [n for n in payload.ctg_nos if n not in found_ctg_nos]
    if missing_ctgs:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "존재하지 않는 ctg_no가 있습니다.",
            ["ctg_nos"],
            invalid=missing_ctgs,
        )

    # 5) 멤버 선택 — 그룹 전체 아니면 개별 멤버, 배타적으로 정확히 하나만 선택해야 한다.
    # 그룹 전체를 고르면 실제로는 그 그룹 멤버 전원을 개별 row로 확장해서 저장한다
    # (rute_artist_select엔 group_no 컬럼이 없어서, "그룹 선택"이라는 의사 자체는
    # DB에 남지 않고 그 시점 멤버 명단만 스냅샷으로 남는다).
    group_selected = payload.artist_group_no is not None
    member_selected = len(payload.artist_nos) > 0

    if group_selected and member_selected:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "그룹 선택과 멤버 선택을 동시에 할 수 없습니다.",
            ["artist_group_no", "artist_nos"],
        )
    if not group_selected and not member_selected:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "그룹 전체 또는 멤버 중 하나는 선택해야 합니다.",
            ["artist_group_no", "artist_nos"],
        )

    if group_selected:
        if payload.artist_group_no != event.artist_group_no:
            raise _err(
                status.HTTP_400_BAD_REQUEST,
                "이벤트로 확정된 그룹이 아닙니다.",
                ["artist_group_no"],
            )
        selected_artist_nos = [
            row.artist_no
            for row in db.query(Artist.artist_no)
            .filter(Artist.artist_group_no == event.artist_group_no)
            .order_by(Artist.artist_no)
            .all()
        ]
    else:
        if len(set(payload.artist_nos)) != len(payload.artist_nos):
            raise _err(status.HTTP_400_BAD_REQUEST, "중복된 artist_no가 있습니다.", ["artist_nos"])
        found_artist_nos = {
            row.artist_no
            for row in db.query(Artist.artist_no)
            .filter(
                Artist.artist_no.in_(payload.artist_nos),
                Artist.artist_group_no == event.artist_group_no,
            )
            .all()
        }
        missing_artists = [n for n in payload.artist_nos if n not in found_artist_nos]
        if missing_artists:
            raise _err(
                status.HTTP_400_BAD_REQUEST,
                "이벤트 그룹 소속이 아니거나 존재하지 않는 artist_no가 있습니다.",
                ["artist_nos"],
                invalid=missing_artists,
            )
        selected_artist_nos = payload.artist_nos

    # 6) 동선 스타일 — trip.trip_density_no. trip_type_no는 대응 테이블이 삭제된 죽은
    # 컬럼이라 쓰지 않는다.
    if not db.query(TripDensity).filter(TripDensity.trip_density_no == payload.trip_density_no).first():
        raise _err(
            status.HTTP_400_BAD_REQUEST, "존재하지 않는 trip_density_no입니다.", ["trip_density_no"]
        )

    try:
        trip = Trip(
            user_no=current_user.user_no,
            event_no=payload.event_no,
            event_date=payload.event_date,
            start_dt=payload.start_dt,
            end_dt=payload.end_dt,
            start_tm=payload.start_tm,
            end_tm=payload.end_tm,
            start_place=payload.start_place,
            start_place_lat=payload.start_place_lat,
            start_place_lon=payload.start_place_lon,
            end_place=payload.end_place,
            end_place_lat=payload.end_place_lat,
            end_place_lon=payload.end_place_lon,
            trip_density_no=payload.trip_density_no,
        )
        db.add(trip)
        db.flush()  # trip_no 채번

        accom_rows = [
            Accom(
                trip_no=trip.trip_no,
                accom_nm=accom.accom_nm,
                add=accom.add,
                accom_lat=accom.accom_lat,
                accom_lon=accom.accom_lon,
                check_in_dt=accom.check_in_dt,
                check_out_dt=accom.check_out_dt,
                created_by=current_user.login_id,
            )
            for accom in payload.accoms
        ]
        db.add_all(accom_rows)

        interest_rows = [
            TripInterest(trip_no=trip.trip_no, ctg_no=ctg_no, rank=rank)
            for rank, ctg_no in enumerate(payload.ctg_nos, start=1)
        ]
        db.add_all(interest_rows)

        artist_rows = [
            RuteArtistSelect(trip_no=trip.trip_no, artist_no=artist_no)
            for artist_no in selected_artist_nos
        ]
        db.add_all(artist_rows)

        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "입력값을 확인해주세요 (제약 조건을 위반했습니다).", "fields": []},
        )
    except Exception:
        db.rollback()
        raise

    db.refresh(trip)
    return schemas.TripOut(
        trip_no=trip.trip_no,
        event_no=trip.event_no,
        event_date=trip.event_date,
        start_dt=trip.start_dt,
        end_dt=trip.end_dt,
        start_tm=trip.start_tm,
        end_tm=trip.end_tm,
        start_place=trip.start_place,
        start_place_lat=float(trip.start_place_lat) if trip.start_place_lat is not None else None,
        start_place_lon=float(trip.start_place_lon) if trip.start_place_lon is not None else None,
        end_place=trip.end_place,
        end_place_lat=float(trip.end_place_lat) if trip.end_place_lat is not None else None,
        end_place_lon=float(trip.end_place_lon) if trip.end_place_lon is not None else None,
        trip_density_no=trip.trip_density_no,
        accoms=accom_rows,
        interests=[
            schemas.TripInterestOut(ctg_no=row.ctg_no, rank=row.rank) for row in interest_rows
        ],
        artist_nos=selected_artist_nos,
    )


@router.post(
    "/trips/{trip_no}/recommend",
    response_model=schemas.TripRecommendOut,
    tags=["trip"],
)
def recommend_trip_route(
    trip_no: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """trip에 이미 저장된 정보(콘서트/기간/숙소/선호 카테고리/선택 멤버/동선 스타일)만으로
    AL-02 파이프라인(al02_candidates.fetch_candidates + AL02Pipeline.run)을 돌려 추천 동선을
    만들어 그대로 반환한다. DB에 저장하지 않는다(trip_route/trip_route_event insert 없음) —
    프론트가 이 결과를 편집해서 기존 POST /trip-routes 바디로 변환해 저장하는 흐름을 전제로 함."""
    trip = db.query(Trip).filter(Trip.trip_no == trip_no).first()
    if not trip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="존재하지 않는 여행입니다."
        )
    if trip.user_no != current_user.user_no:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 여행이 아닙니다.")

    wrel_key = TRIP_DENSITY_TO_WREL_KEY.get(trip.trip_density_no)
    if wrel_key is None:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "여행에 동선 스타일(trip_density_no)이 설정되어 있지 않습니다. 먼저 스타일을 선택해주세요.",
            ["trip_density_no"],
        )
    w_rel = WREL_PROFILES[wrel_key]["W_rel"]
    # 2026-09-10: A/B/C 밀도 프로파일의 방문 개수 범위(al02_policy.VISIT_COUNT_PROFILES) —
    # W_rel과 같은 wrel_key(trip_density_no 1/2/3 -> A/B/C)로 뽑아 그대로 같이 넘긴다.
    visit_profile = VISIT_COUNT_PROFILES[wrel_key]
    # 2026-09-10 정정: 공연일도 이제 프로파일별 범위(공연 포함 총 개수) — 이전엔 공연일
    # 고정값이라 이 조회가 없었음.
    concert_visit_profile = CONCERT_VISIT_COUNT_PROFILES[wrel_key]

    # trip.event_no는 NOT NULL FK라 정상 흐름에선 항상 존재 — 없으면 방어적으로 400.
    concert_event = db.query(Event).filter(Event.event_no == trip.event_no).first()
    if not concert_event:
        raise _err(status.HTTP_400_BAD_REQUEST, "여행에 연결된 이벤트를 찾을 수 없습니다.", ["event_no"])

    ctg_nos = [
        row.ctg_no
        for row in db.query(TripInterest)
        .filter(TripInterest.trip_no == trip_no)
        .order_by(TripInterest.rank)
        .all()
    ]
    artist_nos = [
        row.artist_no
        for row in db.query(RuteArtistSelect.artist_no)
        .filter(RuteArtistSelect.trip_no == trip_no)
        .all()
    ]

    # 숙소(depot) — 2026-09-10: 등록된 숙소 전부를 가져온다(체크인 이른 순).
    # 이전엔 첫 번째 숙소 1곳만 트립 전체 기간에 고정으로 썼음(버그) — 이제
    # AL02Pipeline.day_depots()가 날짜별로 그날 체크인~체크아웃이 유효한 숙소를 고른다
    # (§4.2 우선순위: 1순위 공연 마지막 방문 / 2순위 첫날 출발핀·마지막날 도착핀 / 기본값
    # 그날 유효한 숙소 — al02_pipeline.pick_depot_accom 참고).
    accoms_all = (
        db.query(Accom)
        .filter(Accom.trip_no == trip_no)
        .order_by(Accom.check_in_dt, Accom.accom_no)
        .all()
    )
    accoms = [a for a in accoms_all if a.accom_lat is not None and a.accom_lon is not None]
    if not accoms:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "좌표가 등록된 숙소가 없어 동선을 만들 수 없습니다.",
            ["accoms"],
        )
    accom = accoms[0]  # S0 후보 검색 반경 중심 등 "대표 숙소 1곳"이 필요한 곳에서 그대로 사용
    accoms_input = [
        {
            "accom_no": a.accom_no, "lat": float(a.accom_lat), "lon": float(a.accom_lon),
            "check_in_dt": a.check_in_dt.isoformat() if a.check_in_dt else None,
            "check_out_dt": a.check_out_dt.isoformat() if a.check_out_dt else None,
        }
        for a in accoms
    ]

    user_input = {
        "trip_start": trip.start_dt.isoformat(),
        "trip_end": trip.end_dt.isoformat(),
        "artist_nos": artist_nos,
        # rute_artist_select는 그룹 전체 선택도 그 시점 멤버 전원을 개별 row로 풀어서 저장하므로
        # (POST /trips 참고) group_nos는 항상 비워도 된다 — build_selected_group_nos가
        # artist_nos + artist_group_map만으로 소속 그룹을 그대로 복원한다.
        "group_nos": [],
        "ctg_nos": ctg_nos,
        "concert": {
            "event_no": trip.event_no,
            "event_date": trip.event_date.isoformat(),
            "start_time": (
                concert_event.start_dt.strftime("%H:%M") if concert_event.start_dt else "19:00"
            ),
        },
        "lodging": {"latitude": float(accom.accom_lat), "longitude": float(accom.accom_lon)},
        "accoms": accoms_input,
    }
    # 출발핀/도착핀 — trip 생성 시 저장된 값이 있으면 그대로 태운다(al02_pipeline이
    # 이미 지원하는 필드라 값이 있는데 빼면 오히려 정확도가 떨어짐).
    if trip.start_place_lat is not None and trip.start_place_lon is not None:
        user_input["start_pin"] = {
            "latitude": float(trip.start_place_lat), "longitude": float(trip.start_place_lon)
        }
    if trip.end_place_lat is not None and trip.end_place_lon is not None:
        user_input["end_pin"] = {
            "latitude": float(trip.end_place_lat), "longitude": float(trip.end_place_lon)
        }
    # 하루 활동 시간대(2026-09-10 추가) — trip.start_tm/end_tm은 트립 전체에 적용되는
    # 단일 "하루 시작~종료 시각"이고(날짜 부분은 무시, 시:분만 씀), DB엔 DATETIME으로
    # 저장돼 있다. 여태 여기서 안 읽어서 al02_pipeline이 계속 기본값(09:00~21:00)만
    # 쓰고 있었음 — S3 영업시간 필터(build_open_matrix)가 실제로 이 값과 비교해야 해서
    # 이번에 같이 연결한다. 둘 중 하나라도 없으면(트립 생성 시 안 받은 레거시 데이터)
    # 기존 기본값 그대로 둔다.
    if trip.start_tm is not None:
        user_input["day_start_time"] = trip.start_tm.strftime("%H:%M")
    if trip.end_tm is not None:
        user_input["day_end_time"] = trip.end_tm.strftime("%H:%M")

    # 이동시간 실 API 연동(2026-09-09): "고정 지점"(숙소 각각/출발핀/도착핀)마다 후보
    # 이벤트까지의 실제 이동시간을 미리 조회해 AL02Pipeline.run()에 넘긴다. role 키
    # ("depot:{accom_no}"/"start_pin"/"end_pin")는 al02_pipeline.run()이 augment_matrix에
    # 넘기는 extra_points의 role과 반드시 일치해야 한다 — al02_candidates.
    # build_extra_travel_minutes() 참고. 2026-09-10: 숙소가 여러 곳이면 전부 넣는다
    # (다중 숙소 depot 지원 — travel_time_cache는 accom_no별로 캐시하므로 자연스럽게 지원됨).
    extra_origins = {
        f"depot:{a['accom_no']}": {"origin_type": "accom", "origin_no": a["accom_no"],
                                    "lat": a["lat"], "lon": a["lon"]}
        for a in accoms_input
    }
    if "start_pin" in user_input:
        extra_origins["start_pin"] = {
            "origin_type": "trip_start_pin", "origin_no": trip_no,
            "lat": user_input["start_pin"]["latitude"], "lon": user_input["start_pin"]["longitude"],
        }
    if "end_pin" in user_input:
        extra_origins["end_pin"] = {
            "origin_type": "trip_end_pin", "origin_no": trip_no,
            "lat": user_input["end_pin"]["latitude"], "lon": user_input["end_pin"]["longitude"],
        }

    try:
        candidates, events, matrix = al02_candidates.fetch_candidates(
            db,
            user_input,
            lodging_lat=user_input["lodging"]["latitude"],
            lodging_lon=user_input["lodging"]["longitude"],
            include_tier2=ENABLE_DIVERSITY_TIERS,
        )
        warning = "insufficient_candidates" if len(candidates) < 3 else None

        extra_travel_minutes = al02_candidates.build_extra_travel_minutes(
            db, extra_origins, events,
        )
        # S3 영업시간 필터(2026-09-10 신규) — 후보 event_no 전부에 대해 event_op_hour을
        # 한 번에 조회해서 넘긴다. 실 사용 흐름에선 이 인자를 항상 명시적으로 넘기므로
        # (al02_selftest.py처럼 None을 넘겨 필터를 끄는 경로가 아님) 데이터가 없는
        # 이벤트는 al02_pipeline.build_open_matrix()가 그대로 휴무 취급한다.
        business_hours_by_event = al02_candidates.fetch_business_hours(
            db, [ev["event_no"] for ev in events],
        )

        engine = AL02Pipeline()
        result = engine.run(candidates, matrix, events, user_input, W_rel=w_rel,
                             extra_travel_minutes=extra_travel_minutes,
                             business_hours_by_event=business_hours_by_event,
                             visit_min=visit_profile["min"], visit_max=visit_profile["max"],
                             concert_visit_min=concert_visit_profile["min"],
                             concert_visit_max=concert_visit_profile["max"],
                             # 2026-09-11~12: 다양성 제약 스위치 — enable_hard_dedup(동일
                             # 이벤트/브랜드/쇼핑 전체-여행 1회 + 하루 동일 ctg_no 최대
                             # 1곳)은 항상 켜고, enable_diversity(Tier2 개방 + dense 3차
                             # 완화)는 이 파일 상단의 ENABLE_DIVERSITY_TIERS로 제어한다
                             # (al02_selftest.py 등 합성 데이터 호출은 둘 다 안 넘겨서
                             # 영향 없음).
                             enable_hard_dedup=ENABLE_HARD_DEDUP,
                             enable_diversity=ENABLE_DIVERSITY_TIERS, density_profile=wrel_key)
    except travel_time_service.TravelTimeQuotaExceeded as e:
        # 일 900건 하드 락 — haversine 등으로 폴백하지 않고 명확히 실패 처리(요청 사양).
        raise _err(status.HTTP_503_SERVICE_UNAVAILABLE, str(e), [])
    except travel_time_service.TravelTimeAPIError as e:
        raise _err(status.HTTP_502_BAD_GATEWAY, f"이동시간 조회에 실패했습니다: {e}", [])
    except DepotOverlapError as e:
        # 숙소 체크인~체크아웃 기간이 겹치는 데이터(정책 위반) — POST /trips가 2026-09-10부터
        # 새로 막지만, 그 이전에 생성된 레거시 데이터는 여전히 여기서 걸릴 수 있다.
        # 조용히 하나를 골라 넘어가지 않고 500으로 명확히 실패 처리(al02_pipeline.
        # pick_depot_accom 참고) — 클라이언트 요청 자체는 잘못이 없어 4xx가 아니라 5xx.
        raise _err(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e), ["accoms"])

    # 2026-09-12: Tier2(+3차 완화)까지 다 쓰고도 목표를 못 채운 날짜가 있으면(작업지시
    # 6번) "insufficient_candidates"(후보 자체가 3개 미만)보다 더 구체적인 이 코드를
    # 우선한다 — 둘 다 해당될 수 있는 상황에서 프론트가 "왜 부족한지"를 더 정확히
    # 알 수 있게. enable_diversity가 꺼져 있으면(diversity가 None이거나 그 필드가
    # False) 기존 동작(insufficient_candidates만) 그대로다.
    if result.get("diversity") and result["diversity"].get("insufficient_diverse_candidates"):
        warning = "insufficient_diverse_candidates"

    # 공연이 실제로 어느 날의 schedule에도 안 실렸으면(마감 전 도착 불가 + 재배정으로도 실패,
    # al02_pipeline.s4_solve_fallback이 공연만 남기고도 포기한 경우) 200으로 어설프게
    # 돌려주지 않고 422로 명확히 알린다.
    concert_scheduled = any(s["is_concert"] for day in result["days"] for s in day["schedule"])
    if not concert_scheduled:
        raise _err(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "공연 시작 전까지 도착 가능한 동선을 만들 수 없습니다.",
            [],
        )

    response = schemas.TripRecommendOut(
        trip_no=trip_no,
        scoring_policy=result["scoring_policy"],
        summary=schemas.RecommendSummaryOut(**result["summary"]),
        days=[
            schemas.RecommendDayOut(
                trip_route_no=None,
                visit_day=day["day_index"] + 1,  # trip_route.visit_day와 동일하게 1부터 시작
                date=day["date"],
                is_concert_day=day["is_concert_day"],
                schedule=day["schedule"],
            )
            for day in result["days"]
        ],
        warning=warning,
        diversity=(
            schemas.RecommendDiversityOut(**result["diversity"])
            if result.get("diversity") is not None else None
        ),
    )
    return response


def _validate_trip_routes_request(
    db: Session, trip_no: int, routes: list[schemas.TripRouteItemIn], user_no: int
) -> Trip:
    """POST(생성)/PUT(전체 수정)이 공통으로 쓰는 검증: trip 소유 확인 + event_no 존재 확인."""
    trip = db.query(Trip).filter(Trip.trip_no == trip_no).first()
    if not trip:
        raise _err(status.HTTP_404_NOT_FOUND, "존재하지 않는 여행입니다.", ["trip_no"])
    if trip.user_no != user_no:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 여행이 아닙니다.")

    requested_event_nos = {item.event_no for item in routes}
    found_event_nos = {
        row.event_no
        for row in db.query(Event.event_no).filter(Event.event_no.in_(requested_event_nos)).all()
    }
    missing_events = sorted(requested_event_nos - found_event_nos)
    if missing_events:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "존재하지 않는 event_no가 있습니다.",
            ["routes"],
            invalid=missing_events,
        )
    return trip


def _serialize_trip_routes(db: Session, trip_no: int) -> list[schemas.TripRouteOut]:
    """trip_no의 일자(TripRoute) + 그 안의 이벤트(TripRouteEvent)를 묶어 응답 형태로 조립한다.
    POST/PUT /trip-routes가 공통으로 쓴다."""
    day_rows = (
        db.query(TripRoute)
        .filter(TripRoute.trip_no == trip_no)
        .order_by(TripRoute.visit_day, TripRoute.trip_route_no)
        .all()
    )
    day_nos = [d.trip_route_no for d in day_rows]
    events_by_day: dict[int, list[TripRouteEvent]] = {}
    if day_nos:
        event_rows = (
            db.query(TripRouteEvent)
            .filter(TripRouteEvent.trip_route_no.in_(day_nos))
            .order_by(TripRouteEvent.trip_route_no, TripRouteEvent.seq, TripRouteEvent.trip_route_event_no)
            .all()
        )
        for ev in event_rows:
            events_by_day.setdefault(ev.trip_route_no, []).append(ev)

    return [
        schemas.TripRouteOut(
            trip_route_no=day.trip_route_no,
            trip_no=day.trip_no,
            visit_day=day.visit_day,
            usage_status_no=day.usage_status_no,
            events=[
                schemas.TripRouteEventOut(
                    trip_route_event_no=ev.trip_route_event_no, event_no=ev.event_no, seq=ev.seq
                )
                for ev in events_by_day.get(day.trip_route_no, [])
            ],
        )
        for day in day_rows
    ]


@router.get(
    "/trips/{trip_no}/routes/{event_no}/alternatives",
    response_model=list[schemas.AlternativeEventOut],
    tags=["trip"],
)
def get_route_alternatives(
    trip_no: int,
    event_no: int,
    visit_day: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """'현재 장소 주변 대체 장소' 추천(2026-09-10 신규). event_no가 그 트립의 어느
    날짜(TripRoute)에도 없으면 빈 리스트를 반환한다(에러 아님 — "대체할 게 없다"는
    정상 상태). visit_day는 같은 event_no가 여러 날짜에 중복 등장할 때만 필요.

    al02_alternatives.get_alternatives() 참고 — 반경 5km 고정, 같은 ctg_no만, 거리순
    top5, 콘서트/그 trip 전체 동선(모든 day)에 이미 포함된 장소는 후보에서 제외."""
    trip = db.query(Trip).filter(Trip.trip_no == trip_no).first()
    if not trip:
        raise _err(status.HTTP_404_NOT_FOUND, "존재하지 않는 여행입니다.", ["trip_no"])
    if trip.user_no != current_user.user_no:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 여행이 아닙니다.")

    alternatives = al02_alternatives.get_alternatives(db, trip_no, event_no, visit_day=visit_day)
    return [schemas.AlternativeEventOut(**a) for a in alternatives]


@router.post(
    "/trip-routes",
    response_model=list[schemas.TripRouteOut],
    status_code=status.HTTP_201_CREATED,
    tags=["trip"],
)
def create_trip_routes(
    payload: schemas.TripRouteCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """'동선 직접 고치기' 화면에서 '동선 저장' 버튼을 누른 시점에 1~N일차 전체를
    한 번에 받는다. 요청 바디는 여전히 {trip_no, routes: [{visit_day, event_no}]} 평탄한
    형태지만, 내부적으로는 visit_day별로 TripRoute(일자) 로우를 하나 만들고 그 아래
    TripRouteEvent(이벤트) 로우들을 매단다. usage_status_no는 항상 '대기중'.
    이미 저장된 동선이 있는 trip에 또 호출하면 기존 행 위에 그대로 추가되니(삭제 안 함),
    재수정은 PUT /trip-routes를 쓸 것."""
    _validate_trip_routes_request(db, payload.trip_no, payload.routes, current_user.user_no)
    waiting_usage_status_no = _get_waiting_usage_status_no(db)

    by_day: dict[int, list[schemas.TripRouteItemIn]] = {}
    for item in payload.routes:
        by_day.setdefault(item.visit_day, []).append(item)

    # 2026-09-11 추가: trip_route/trip_route_event.created_at이 계속 NULL로 남던 버그 —
    # 모델(models.py)엔 server_default=func.now()가 선언돼 있지만, 실제 DB 컬럼 자체는
    # DEFAULT NULL이라(SHOW CREATE TABLE로 실측 확인, 53/53·197/197행 전부 NULL) 이
    # server_default가 실제로는 아무 효과가 없었다 — SQLAlchemy가 마이그레이션을
    # 대신 해주지 않는다. DB 컬럼 자체를 고치는 건 스키마 변경이라 이번 요청 범위 밖으로
    # 판단, 이미 이 파일의 ChatMessage 저장부(POST /chat)가 쓰던 것과 동일한 패턴
    # (요청 하나당 now를 한 번 잡아서 그 요청에서 생기는 모든 행에 그대로 씀)으로
    # 애플리케이션 레벨에서 명시적으로 채운다.
    now = datetime.now()
    try:
        for visit_day, items in by_day.items():
            day_row = TripRoute(
                trip_no=payload.trip_no,
                visit_day=visit_day,
                usage_status_no=waiting_usage_status_no,
                created_at=now,
            )
            db.add(day_row)
            db.flush()  # trip_route_no 채번
            db.add_all(
                TripRouteEvent(trip_route_no=day_row.trip_route_no, event_no=item.event_no, seq=seq,
                               created_at=now)
                for seq, item in enumerate(items, start=1)
            )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "입력값을 확인해주세요 (제약 조건을 위반했습니다).", "fields": []},
        )
    except Exception:
        db.rollback()
        raise

    return _serialize_trip_routes(db, payload.trip_no)


@router.put(
    "/trip-routes",
    response_model=list[schemas.TripRouteOut],
    tags=["trip"],
)
def replace_trip_routes(
    payload: schemas.TripRouteCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """여행 중에도 동선을 몇 번이든 다시 저장할 수 있는 '전체 교체' API — 이력은 안 남기고
    항상 최신 상태로 덮어쓴다. trip_no 하나에 대해 1~N일차 전체를 통째로 다시 보내야 한다
    (부분 일자만 보내면 그 일자만 남기고 나머지 일자는 지워짐 — 화면이 항상 전체 목록을
    들고 있다가 보내는 걸 전제로 함).

    단순 delete-all-then-insert가 아니라 (visit_day, event_no) 조합이 그대로인
    TripRouteEvent 행은 안 건드리는 diff 방식이다 — 그 행에 걸린 visit_feedback(좋아요)이
    그대로인 장소를 매번 날리는 게 이상하기 때문. 실제로 바뀐 이벤트만 지우고(그때는
    딸린 visit_feedback도 같이 정리) 새로 생긴 조합만 추가한다. 완전히 빠진 일자는
    TripRoute(일자) 로우 자체를 삭제하고, 새로 등장한 일자는 새로 만든다.

    날짜 제한 없음 — 이미 지난 날짜(진행완료)의 동선도 그대로 수정 가능하게 열어뒀다.
    이력을 안 남기기로 한 이상 과거/미래를 구분할 이유가 약하다고 판단."""
    trip = _validate_trip_routes_request(db, payload.trip_no, payload.routes, current_user.user_no)

    existing_day_rows = db.query(TripRoute).filter(TripRoute.trip_no == trip.trip_no).all()
    day_by_visit_day: dict[int | None, TripRoute] = {d.visit_day: d for d in existing_day_rows}

    existing_events = (
        db.query(TripRouteEvent, TripRoute.visit_day)
        .join(TripRoute, TripRouteEvent.trip_route_no == TripRoute.trip_route_no)
        .filter(TripRoute.trip_no == trip.trip_no)
        .all()
    )
    existing_by_key: dict[tuple[int | None, int], list[TripRouteEvent]] = {}
    for ev, visit_day in existing_events:
        existing_by_key.setdefault((visit_day, ev.event_no), []).append(ev)

    new_by_day: dict[int, list[schemas.TripRouteItemIn]] = {}
    for item in payload.routes:
        new_by_day.setdefault(item.visit_day, []).append(item)

    remaining_new_counts: dict[tuple[int, int], int] = {}
    for item in payload.routes:
        key = (item.visit_day, item.event_no)
        remaining_new_counts[key] = remaining_new_counts.get(key, 0) + 1

    events_to_delete: list[TripRouteEvent] = []
    for key, old_rows in existing_by_key.items():
        keep_count = min(len(old_rows), remaining_new_counts.get(key, 0))
        events_to_delete.extend(old_rows[keep_count:])  # 안 남는 만큼만 삭제 대상
        if key in remaining_new_counts:
            remaining_new_counts[key] -= keep_count  # 나머지는 신규 insert로 채움

    waiting_usage_status_no = _get_waiting_usage_status_no(db)
    final_visit_days = set(new_by_day.keys())

    # 2026-09-10 신규 — 이 PUT으로 사실상 바뀌는 날짜만 al02_alternatives.verify_swap()으로
    # 재검증한다(그대로인 날짜까지 매번 카카오 API를 다시 태울 이유가 없음). "old->new
    # 단일 교체"로 좁히지 않고 "그 날짜의 최종 이벤트 목록 전체"를 넘기는 형태라, 한 번에
    # 여러 곳이 바뀌는 이 엔드포인트의 실제 구조에 그대로 맞는다(al02_alternatives.
    # verify_swap 참고). 안 맞으면 DB에 아무것도 안 쓰고(아직 db.add 자체를 안 한 시점)
    # 422로 실패 처리한다 — "조용히 저장은 되는데 실제로는 시간 안에 못 들어가는 동선"을
    # 막기 위함.
    existing_order_by_day: dict[int, list[int]] = {}
    for ev, visit_day in existing_events:
        if visit_day is not None:
            existing_order_by_day.setdefault(visit_day, []).append((ev.seq or 0, ev.event_no))
    existing_order_by_day = {
        d: [no for _, no in sorted(pairs)] for d, pairs in existing_order_by_day.items()
    }
    for visit_day, items in new_by_day.items():
        new_order = [item.event_no for item in items]
        if new_order == existing_order_by_day.get(visit_day):
            continue  # 이 날짜는 실제로 안 바뀜 — 재검증 생략
        verify_result = al02_alternatives.verify_swap(db, trip.trip_no, visit_day, new_order)
        if not verify_result["ok"]:
            raise _err(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{visit_day}일차: {verify_result['reason']}",
                ["routes"],
            )

    # 2026-09-11 추가: trip_route/trip_route_event.created_at NULL 버그(POST /trip-routes와
    # 동일 원인 — models.py의 server_default=func.now()가 실제 DB 컬럼엔 반영 안 돼 있음).
    # 같은 요청에서 새로 생기는 행은 전부 이 시각으로 통일.
    now = datetime.now()

    try:
        if events_to_delete:
            delete_event_nos = [e.trip_route_event_no for e in events_to_delete]
            db.query(VisitFeedback).filter(
                VisitFeedback.trip_route_event_no.in_(delete_event_nos)
            ).delete(synchronize_session=False)
            db.query(TripRouteEvent).filter(
                TripRouteEvent.trip_route_event_no.in_(delete_event_nos)
            ).delete(synchronize_session=False)

        # 새로 등장한 일자만 TripRoute(일자) 로우 생성
        for visit_day in final_visit_days:
            if visit_day not in day_by_visit_day:
                new_day = TripRoute(
                    trip_no=trip.trip_no,
                    visit_day=visit_day,
                    usage_status_no=waiting_usage_status_no,
                    created_at=now,
                )
                db.add(new_day)
                db.flush()
                day_by_visit_day[visit_day] = new_day

        # 새 요청에서 완전히 빠진 일자는 로우 자체를 삭제(그 일자의 이벤트는 위에서 이미 다 지워짐)
        for visit_day in list(day_by_visit_day.keys()):
            if visit_day not in final_visit_days:
                db.delete(day_by_visit_day.pop(visit_day))

        # 부족한 만큼만 신규 이벤트 삽입 — seq는 그 날짜에 남아있는 최대값 다음부터 이어서 부여
        next_seq_by_day: dict[int, int] = {}
        new_event_rows: list[TripRouteEvent] = []
        for (visit_day, event_no), count in remaining_new_counts.items():
            if count <= 0:
                continue
            day_row = day_by_visit_day[visit_day]
            if day_row.trip_route_no not in next_seq_by_day:
                current_max = (
                    db.query(func.max(TripRouteEvent.seq))
                    .filter(TripRouteEvent.trip_route_no == day_row.trip_route_no)
                    .scalar()
                )
                next_seq_by_day[day_row.trip_route_no] = (current_max or 0) + 1
            for _ in range(count):
                new_event_rows.append(
                    TripRouteEvent(
                        trip_route_no=day_row.trip_route_no,
                        event_no=event_no,
                        seq=next_seq_by_day[day_row.trip_route_no],
                        created_at=now,
                    )
                )
                next_seq_by_day[day_row.trip_route_no] += 1
        if new_event_rows:
            db.add_all(new_event_rows)

        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "입력값을 확인해주세요 (제약 조건을 위반했습니다).", "fields": []},
        )
    except Exception:
        db.rollback()
        raise

    return _serialize_trip_routes(db, trip.trip_no)


TRIP_LIST_TABS = {"all", "past", "upcoming"}


def _trip_status(trip: Trip, today: date) -> str:
    if trip.end_dt < today:
        return "완료"
    if trip.start_dt <= today <= trip.end_dt:
        return "진행중"
    return "예정"


@router.get("/trips", response_model=list[schemas.TripListItemOut], tags=["trip"])
def list_my_trips(
    tab: str = Query(default="all"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """'나의 일정' 목록. 상태(완료/진행중/예정)는 DB에 저장하지 않고 매 조회 시
    start_dt/end_dt와 오늘 날짜를 비교해서 계산한다."""
    if tab not in TRIP_LIST_TABS:
        raise _err(
            status.HTTP_400_BAD_REQUEST, "tab은 all/past/upcoming 중 하나여야 합니다.", ["tab"]
        )

    rows = (
        db.query(Trip, Event.event_nm, Event.add, Review.rating, Event.artist_group_no, ArtistGroup.group_nm)
        .join(Event, Trip.event_no == Event.event_no)
        .outerjoin(Review, Review.trip_no == Trip.trip_no)
        .outerjoin(ArtistGroup, Event.artist_group_no == ArtistGroup.artist_group_no)
        .filter(Trip.user_no == current_user.user_no)
        .all()
    )

    trip_nos = [trip.trip_no for trip, _, _, _, _, _ in rows]
    place_counts: dict[int, int] = {}
    if trip_nos:
        # place_count는 "이벤트(장소)" 개수다 — trip_route는 이제 일자 단위 로우라
        # 자식 테이블(trip_route_event)을 세야 한다.
        place_counts = dict(
            db.query(TripRoute.trip_no, func.count(TripRouteEvent.trip_route_event_no))
            .join(TripRouteEvent, TripRouteEvent.trip_route_no == TripRoute.trip_route_no)
            .filter(TripRoute.trip_no.in_(trip_nos))
            .group_by(TripRoute.trip_no)
            .all()
        )

    today = date.today()
    items = [
        schemas.TripListItemOut(
            trip_no=trip.trip_no,
            event_nm=event_nm,
            event_add=event_add,
            start_dt=trip.start_dt,
            end_dt=trip.end_dt,
            status=_trip_status(trip, today),
            rating=float(rating) if rating is not None else None,
            place_count=place_counts.get(trip.trip_no, 0),
            artist_group_no=artist_group_no,
            group_nm=group_nm,
        )
        for trip, event_nm, event_add, rating, artist_group_no, group_nm in rows
    ]

    if tab == "past":
        items = [item for item in items if item.status == "완료"]
    elif tab == "upcoming":
        items = [item for item in items if item.status in ("진행중", "예정")]

    status_order = {"진행중": 0, "예정": 1, "완료": 2}

    def sort_key(item: schemas.TripListItemOut):
        if item.status == "예정":
            secondary = item.start_dt.toordinal()
        elif item.status == "완료":
            secondary = -item.end_dt.toordinal()
        else:
            secondary = 0
        return (status_order[item.status], secondary)

    items.sort(key=sort_key)
    return items


@router.get(
    "/events/{event_no}",
    response_model=schemas.EventDetailOut,
    tags=["trip"],
)
def get_event_detail(event_no: int, db: Session = Depends(get_db)):
    """장소 상세. FAN:GO 추천점수(total_score)는 external_review에 이미 계산되어 있는
    값을 그대로 내려준다 — 서버에서 별도로 계산/가공하지 않는다. 리뷰가 여러 행일 수
    있어 배열로 반환한다(출처 라벨은 review_source 테이블 삭제로 더 이상 없음)."""
    event = db.query(Event).filter(Event.event_no == event_no).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="존재하지 않는 이벤트입니다."
        )

    reviews = db.query(ExternalReview).filter(ExternalReview.event_no == event_no).all()

    return schemas.EventDetailOut(
        event_no=event.event_no,
        event_nm=event.event_nm,
        start_dt=event.start_dt,
        end_dt=event.end_dt,
        add=event.add,
        event_lat=event.event_lat,
        event_lon=event.event_lon,
        event_desc=event.event_desc,
        event_img_url=event.event_img_url,
        artist_group_no=event.artist_group_no,
        artist_no=event.artist_no,
        external_reviews=[
            schemas.ExternalReviewOut(
                rating=float(er.rating) if er.rating is not None else None,
                total_score=float(er.total_score) if er.total_score is not None else None,
            )
            for er in reviews
        ],
    )


def _get_owned_trip_route_event(db: Session, trip_route_event_no: int, user_no: int) -> TripRouteEvent:
    event_row = (
        db.query(TripRouteEvent).filter(TripRouteEvent.trip_route_event_no == trip_route_event_no).first()
    )
    if not event_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="존재하지 않는 장소입니다."
        )
    day_row = db.query(TripRoute).filter(TripRoute.trip_route_no == event_row.trip_route_no).first()
    trip = db.query(Trip).filter(Trip.trip_no == day_row.trip_no).first() if day_row else None
    if not trip or trip.user_no != user_no:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 여행이 아닙니다.")
    return event_row


KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]  # date.weekday(): 월=0 ... 일=6


def _visit_op_dt(trip_start_dt: date, visit_day: int | None) -> str | None:
    """trip.start_dt + visit_day(1부터)로 실제 방문 날짜를 구해 한글 요일로 변환.
    visit_day가 아직 안 배정됐으면(None) 요일을 알 수 없으므로 None."""
    if visit_day is None:
        return None
    visit_date = trip_start_dt + timedelta(days=visit_day - 1)
    return KOREAN_WEEKDAYS[visit_date.weekday()]


@router.get("/trips/{trip_no}/routes", response_model=list[schemas.TripRouteListItemOut], tags=["trip"])
def list_trip_routes(
    trip_no: int,
    visit_day: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """특정 여행(또는 특정 일자)의 동선 — 일자별로 그 날의 장소 목록(장소별 좋아요 여부
    liked, 방문 요일 기준 오픈/마감 시간 business_hours, 행사 고정 시작/종료 시각
    fixed_schedule 포함)을 묶어서 내려준다."""
    trip = db.query(Trip).filter(Trip.trip_no == trip_no).first()
    if not trip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="존재하지 않는 여행입니다."
        )
    if trip.user_no != current_user.user_no:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 여행이 아닙니다.")

    day_query = db.query(TripRoute).filter(TripRoute.trip_no == trip_no)
    if visit_day is not None:
        day_query = day_query.filter(TripRoute.visit_day == visit_day)
    day_rows = day_query.order_by(TripRoute.visit_day, TripRoute.trip_route_no).all()

    day_nos = [d.trip_route_no for d in day_rows]
    events_by_day: dict[int, list[tuple[TripRouteEvent, str]]] = {}
    liked_event_nos: set[int] = set()
    op_dt_by_day: dict[int, str | None] = {
        day.trip_route_no: _visit_op_dt(trip.start_dt, day.visit_day) for day in day_rows
    }
    # event_no -> {op_dt: EventOpHour} — 이 여행에 등장하는 event_no 전체를 한 번에 조회
    # (요청하신 대로 event_op_hour 조회 로직은 새로 안 만들고 EventOpHour 모델 그대로 재사용).
    hours_by_event: dict[int, dict[str, EventOpHour]] = {}
    # event_no -> (ctg_type_no, start_dt, end_dt) — 행사(ctg_type_no=1) 고정 일정 판단용
    event_meta: dict[int, tuple[int, datetime | None, datetime | None]] = {}
    if day_nos:
        event_rows = (
            db.query(TripRouteEvent, Event.event_nm)
            .join(Event, TripRouteEvent.event_no == Event.event_no)
            .filter(TripRouteEvent.trip_route_no.in_(day_nos))
            .order_by(TripRouteEvent.trip_route_no, TripRouteEvent.seq, TripRouteEvent.trip_route_event_no)
            .all()
        )
        for ev, event_nm in event_rows:
            events_by_day.setdefault(ev.trip_route_no, []).append((ev, event_nm))

        event_nos_all = [ev.trip_route_event_no for ev, _ in event_rows]
        if event_nos_all:
            liked_event_nos = {
                vf.trip_route_event_no
                for vf in db.query(VisitFeedback)
                .filter(VisitFeedback.trip_route_event_no.in_(event_nos_all))
                .all()
            }

        distinct_event_nos = {ev.event_no for ev, _ in event_rows}
        if distinct_event_nos:
            for hour_row in (
                db.query(EventOpHour).filter(EventOpHour.event_no.in_(distinct_event_nos)).all()
            ):
                hours_by_event.setdefault(hour_row.event_no, {})[hour_row.op_dt] = hour_row

            for event_no, ctg_type_no, start_dt, end_dt in (
                db.query(Event.event_no, Ctg.ctg_type_no, Event.start_dt, Event.end_dt)
                .join(Ctg, Event.ctg_no == Ctg.ctg_no)
                .filter(Event.event_no.in_(distinct_event_nos))
                .all()
            ):
                event_meta[event_no] = (ctg_type_no, start_dt, end_dt)

    MAIN_EVENT_CTG_TYPE_NO = 1

    def _fixed_schedule(event_no: int) -> schemas.FixedScheduleOut | None:
        meta = event_meta.get(event_no)
        if not meta:
            return None
        ctg_type_no, start_dt, end_dt = meta
        if ctg_type_no != MAIN_EVENT_CTG_TYPE_NO or start_dt is None:
            return None
        # start_dt가 00:00:00이고 end_dt가 23:59:59면 실제 공연 시각이 아니라 "진행 기간"을
        # 날짜만으로 표현한 것뿐이다(멀티데이 행사에서 흔함) — 이 경우 시간 정보 없음으로 취급.
        is_date_range_placeholder = (
            start_dt.time() == time(0, 0, 0)
            and end_dt is not None
            and end_dt.time() == time(23, 59, 59)
        )
        if is_date_range_placeholder:
            return None
        return schemas.FixedScheduleOut(
            start_tm=start_dt.strftime("%H:%M"),
            end_tm=end_dt.strftime("%H:%M") if end_dt is not None else None,
        )

    def _business_hours(event_no: int, op_dt: str | None) -> schemas.BusinessHoursOut | None:
        if op_dt is None:
            return None
        day_map = hours_by_event.get(event_no)
        if day_map is None:
            return schemas.BusinessHoursOut(has_data=False, is_closed=False)
        hour_row = day_map.get(op_dt)
        if hour_row is None:
            # 실측상 한 event_no에 행이 있으면 요일 7개가 항상 다 있어 이 분기는 정상적으론
            # 발생하지 않는다 — 혹시 모를 부분 데이터 대비 방어적으로 "정보 없음" 처리.
            return schemas.BusinessHoursOut(has_data=False, is_closed=False)
        is_closed = hour_row.open_tm is None and hour_row.close_tm is None
        return schemas.BusinessHoursOut(
            has_data=True,
            is_closed=is_closed,
            open_tm=hour_row.open_tm,
            close_tm=hour_row.close_tm,
        )

    return [
        schemas.TripRouteListItemOut(
            trip_route_no=day.trip_route_no,
            visit_day=day.visit_day,
            usage_status_no=day.usage_status_no,
            events=[
                schemas.TripRouteEventListItemOut(
                    trip_route_event_no=ev.trip_route_event_no,
                    event_no=ev.event_no,
                    event_nm=event_nm,
                    seq=ev.seq,
                    liked=ev.trip_route_event_no in liked_event_nos,
                    fixed_schedule=(fixed := _fixed_schedule(ev.event_no)),
                    # fixed_schedule이 채워지면 business_hours는 계산하지 않고 null(요청하신 대로).
                    business_hours=(
                        None if fixed is not None
                        else _business_hours(ev.event_no, op_dt_by_day[day.trip_route_no])
                    ),
                )
                for ev, event_nm in events_by_day.get(day.trip_route_no, [])
            ],
        )
        for day in day_rows
    ]


@router.post(
    "/trip-route-events/{trip_route_event_no}/like",
    response_model=schemas.VisitFeedbackOut,
    status_code=status.HTTP_201_CREATED,
    tags=["trip"],
)
def like_trip_route_event(
    trip_route_event_no: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """따봉 누르기. 이미 눌러져 있으면 그대로 유지(멱등) — 중복 행을 만들지 않는다.
    trip_route가 일자 단위로 바뀌면서 좋아요 대상은 그 날의 개별 이벤트(trip_route_event)다."""
    _get_owned_trip_route_event(db, trip_route_event_no, current_user.user_no)

    if not db.query(VisitFeedback).filter(
        VisitFeedback.trip_route_event_no == trip_route_event_no
    ).first():
        db.add(VisitFeedback(trip_route_event_no=trip_route_event_no))
        db.commit()

    return schemas.VisitFeedbackOut(trip_route_event_no=trip_route_event_no, liked=True)


@router.delete(
    "/trip-route-events/{trip_route_event_no}/like",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["trip"],
)
def unlike_trip_route_event(
    trip_route_event_no: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """따봉 취소. 애초에 안 눌려있었어도 그냥 성공(멱등)."""
    _get_owned_trip_route_event(db, trip_route_event_no, current_user.user_no)

    db.query(VisitFeedback).filter(VisitFeedback.trip_route_event_no == trip_route_event_no).delete()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/trips/{trip_no}/review",
    response_model=schemas.ReviewOut,
    status_code=status.HTTP_201_CREATED,
    tags=["trip"],
)
def create_review(
    trip_no: int,
    payload: schemas.ReviewCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """여행 리뷰 작성 — 여행당 1개(review.trip_no UNIQUE). 수정 API는 없음(생성만)."""
    trip = db.query(Trip).filter(Trip.trip_no == trip_no).first()
    if not trip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="존재하지 않는 여행입니다."
        )
    if trip.user_no != current_user.user_no:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 여행이 아닙니다.")

    if payload.opt_no is not None and not db.query(ReviewOpt).filter(
        ReviewOpt.opt_no == payload.opt_no
    ).first():
        raise _err(status.HTTP_400_BAD_REQUEST, "존재하지 않는 opt_no입니다.", ["opt_no"])

    review = Review(
        trip_no=trip_no,
        opt_no=payload.opt_no,
        rating=payload.rating,
        review_content=payload.review_content,
        written_at=datetime.now(),
    )
    db.add(review)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 리뷰를 작성한 여행입니다.",
        )
    except Exception:
        db.rollback()
        raise

    db.refresh(review)
    return review


# WBS 5.4.1 — 컨텍스트에 넣을 이전 대화 개수. 토큰 비용 때문에 무제한으로 못 넣어서
# "최근 N턴(사용자+어시스턴트 쌍)"으로 제한한다. 3턴(메시지 6개)이면 대부분의 짧은
# 후속 질문("그거 말고 다른 데는?" 등) 맥락은 충분히 잡히면서 매 요청 프롬프트가
# 과도하게 커지지 않는다 — 프론트/사용량 보고 조정 가능(단일 상수라 바꾸기 쉬움).
CHAT_HISTORY_TURNS = 3
CHAT_HISTORY_MESSAGES = CHAT_HISTORY_TURNS * 2


@router.post("/chat", response_model=schemas.ChatOut, tags=["chatbot"])
def chat(
    payload: schemas.ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """FAN:GO 챗봇(Phase 2) — Intent Router로 Path A(Function Calling)/B(RAG)/C(복합)를
    정하고, 그 결과를 8절 페르소나 규칙대로 합성해 답한다. 사용자 컨텍스트(최애 아티스트)는
    로그인한 유저의 favorite_group에서 서버가 직접 조회한다 — 클라이언트가 보내는 값을
    신뢰하지 않는다.

    멀티턴(WBS 5.4.1): chat_session_no가 없으면 새 세션을 만들고, 있으면 본인 소유 세션인지
    확인한 뒤 그 세션의 최근 대화(CHAT_HISTORY_MESSAGES개)를 불러와 답변 생성에 반영한다.
    Intent Router/Function Calling/RAG 판단 자체는 여전히 이번 메시지만 본다."""
    if payload.chat_session_no is not None:
        session = (
            db.query(ChatSession)
            .filter(ChatSession.chat_session_no == payload.chat_session_no)
            .first()
        )
        if not session:
            raise _err(
                status.HTTP_404_NOT_FOUND, "존재하지 않는 대화 세션입니다.", ["chat_session_no"]
            )
        if session.user_no != current_user.user_no:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 대화 세션이 아닙니다.")
    else:
        session = ChatSession(user_no=current_user.user_no)
        db.add(session)
        db.flush()  # chat_session_no 채번

    history_rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_session_no == session.chat_session_no)
        .order_by(ChatMessage.chat_message_no.desc())
        .limit(CHAT_HISTORY_MESSAGES)
        .all()
    )
    history = [
        {"role": row.role, "content": row.content} for row in reversed(history_rows)
    ]
    # 2026-09-10 신규 — 지시어("거기", "거기 말고") 해석용. history_rows는 이미
    # chat_message_no DESC(최신순)라 첫 assistant 행이 바로 직전 턴의 답변이다.
    last_assistant_sources = next(
        (row.sources for row in history_rows if row.role == "assistant" and row.sources),
        None,
    )

    favorite_groups = (
        db.query(ArtistGroup.group_nm)
        .join(FavoriteGroup, FavoriteGroup.artist_group_no == ArtistGroup.artist_group_no)
        .filter(FavoriteGroup.user_no == current_user.user_no)
        .all()
    )
    user_context = {"favorite_groups": [g.group_nm for g in favorite_groups]}

    # 2026-09-10 — Final Generation(LLM 최종 답변 생성) 호출 자체가 실패하면 chatbot이
    # FinalGenerationError를 던진다. "근거 0건이라 고정 문구로 답함"(is_fallback=True,
    # 200 정상)과 "서비스 오류"를 응답 필드 하나로 뭉뚱그리지 않기 위해, 여기서는 200 +
    # 가짜 assistant 메시지 대신 502로 실패시킨다 — user/assistant 메시지 둘 다
    # chat_message에 저장하지 않는다(아래 db.add를 아예 못 타고 예외가 먼저 나가므로,
    # 세션이 새로 만들어졌던 경우에도 커밋 전이라 그대로 롤백된다 — 실패한 시도를
    # 대화 이력에 남기지 않는 쪽이 자연스럽다는 판단).
    try:
        result = chatbot.handle_chat(db, payload.message, user_context, history, last_assistant_sources)
    except chatbot.FinalGenerationError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="답변을 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        )

    now = datetime.now()
    db.add(
        ChatMessage(
            chat_session_no=session.chat_session_no,
            role="user",
            content=payload.message,
            created_at=now,
        )
    )
    db.add(
        ChatMessage(
            chat_session_no=session.chat_session_no,
            role="assistant",
            content=result["answer"],
            sources=result["sources"],
            is_fallback=result["is_fallback"],
            created_at=now,
        )
    )
    session.last_message_at = now
    db.commit()

    return schemas.ChatOut(**result, chat_session_no=session.chat_session_no)


CHAT_PREVIEW_MAX_LEN = 40


def _chat_session_previews(db: Session, session_nos: list[int]) -> dict[int, str]:
    """세션별 첫 user 메시지를 목록용 미리보기로 짧게 잘라 반환. 세션당 정확히 하나만
    쓰도록 MIN(chat_message_no)로 첫 user 메시지를 먼저 특정한 뒤 그 행만 다시 읽는다."""
    if not session_nos:
        return {}
    first_message_nos = dict(
        db.query(ChatMessage.chat_session_no, func.min(ChatMessage.chat_message_no))
        .filter(ChatMessage.chat_session_no.in_(session_nos), ChatMessage.role == "user")
        .group_by(ChatMessage.chat_session_no)
        .all()
    )
    if not first_message_nos:
        return {}
    rows = (
        db.query(ChatMessage.chat_session_no, ChatMessage.content)
        .filter(ChatMessage.chat_message_no.in_(first_message_nos.values()))
        .all()
    )
    previews = {}
    for session_no, content in rows:
        previews[session_no] = (
            content if len(content) <= CHAT_PREVIEW_MAX_LEN else content[:CHAT_PREVIEW_MAX_LEN] + "…"
        )
    return previews


@router.get("/chat/sessions", response_model=list[schemas.ChatSessionListItemOut], tags=["chatbot"])
def list_chat_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """로그인 유저 소유의 대화 세션 목록, last_message_at 최신순. preview는 그 세션의
    첫 user 메시지를 짧게 자른 것(프론트 채팅 목록 UI용)."""
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.user_no == current_user.user_no)
        .order_by(func.coalesce(ChatSession.last_message_at, ChatSession.created_at).desc())
        .all()
    )
    previews = _chat_session_previews(db, [s.chat_session_no for s in sessions])
    return [
        schemas.ChatSessionListItemOut(
            chat_session_no=s.chat_session_no,
            created_at=s.created_at,
            last_message_at=s.last_message_at,
            preview=previews.get(s.chat_session_no),
        )
        for s in sessions
    ]


@router.get(
    "/chat/sessions/{chat_session_no}/messages",
    response_model=list[schemas.ChatMessageOut],
    tags=["chatbot"],
)
def list_chat_messages(
    chat_session_no: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """특정 세션의 메시지 전체를 시간순(생성 순서)으로 반환. POST /chat과 동일하게
    본인 소유 세션인지 확인한다."""
    session = (
        db.query(ChatSession).filter(ChatSession.chat_session_no == chat_session_no).first()
    )
    if not session:
        raise _err(
            status.HTTP_404_NOT_FOUND, "존재하지 않는 대화 세션입니다.", ["chat_session_no"]
        )
    if session.user_no != current_user.user_no:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 대화 세션이 아닙니다.")

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_session_no == chat_session_no)
        .order_by(ChatMessage.chat_message_no)
        .all()
    )
    return [
        schemas.ChatMessageOut(
            role=m.role,
            content=m.content,
            sources=m.sources,
            is_fallback=m.is_fallback,
            created_at=m.created_at,
        )
        for m in messages
    ]
