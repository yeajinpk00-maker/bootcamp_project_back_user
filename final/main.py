import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import models
from auth import router as auth_router
from batch import start_scheduler
from database import SessionLocal, engine

# 이미 ERD 기준으로 테이블이 만들어져 있다면 이 줄은 없어도 됩니다.
# (없는 테이블만 생성하며, 기존 테이블은 건드리지 않습니다.)
models.Base.metadata.create_all(bind=engine)


def seed_defaults():
    """auth/user_status 기본 코드값이 비어 있으면 채워 넣는다."""
    db = SessionLocal()
    try:
        if not db.query(models.Auth).first():
            db.add_all(
                [
                    models.Auth(auth_no=1, auth_nm="사용자"),
                    models.Auth(auth_no=2, auth_nm="총괄관리자"),
                    models.Auth(auth_no=3, auth_nm="중간관리자"),
                    models.Auth(auth_no=4, auth_nm="직원"),
                ]
            )
        if not db.query(models.UserStatus).first():
            # status_no 2=정지, 4=탈퇴는 로그인 차단 로직(auth.py)에서 하드코딩으로 참조한다.
            db.add_all(
                [
                    models.UserStatus(status_no=1, status_nm="활성"),
                    models.UserStatus(status_no=2, status_nm="정지"),
                    models.UserStatus(status_no=3, status_nm="삭제요청"),
                    models.UserStatus(status_no=4, status_nm="탈퇴"),
                ]
            )
        # nationality_no/lang_no는 회원가입 필수값이라, 비어 있으면 가입 자체가
        # FK 제약(R_39 등)에 걸려 항상 실패한다. 최소 시작 데이터만 넣어둔다 —
        # 실제 목록은 팀에서 필요에 맞게 추가/수정하면 된다.
        if not db.query(models.Nationality).first():
            db.add_all(
                [
                    models.Nationality(nationality_no=1, nationality_nm="대한민국"),
                    models.Nationality(nationality_no=2, nationality_nm="미국"),
                    models.Nationality(nationality_no=3, nationality_nm="일본"),
                    models.Nationality(nationality_no=4, nationality_nm="중국"),
                    models.Nationality(nationality_no=5, nationality_nm="기타"),
                ]
            )
        if not db.query(models.Lang).first():
            db.add_all(
                [
                    models.Lang(lang_no=1, lang_nm="한국어"),
                    models.Lang(lang_no=2, lang_nm="English"),
                    models.Lang(lang_no=3, lang_nm="日本語"),
                ]
            )
        # 국적별 기본 언어 매핑. 모든 국적이 매핑을 가져야 하며(/nationalities가
        # inner join으로 조회), 별도 매핑이 없는 국적은 English(lang_no=2)로 채운다.
        if not db.query(models.NatLang).first():
            db.add_all(
                [
                    models.NatLang(nationality_no=1, lang_no=1),  # 대한민국 -> 한국어
                    models.NatLang(nationality_no=2, lang_no=2),  # 미국 -> English
                    models.NatLang(nationality_no=3, lang_no=3),  # 일본 -> 日本語
                    models.NatLang(nationality_no=4, lang_no=2),  # 중국 -> English
                    models.NatLang(nationality_no=5, lang_no=2),  # 기타 -> English
                ]
            )
        db.commit()
    finally:
        db.close()


seed_defaults()

app = FastAPI()
app.include_router(auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://192.168.0.191:5173",
    ],  # 프론트 개발 서버 주소
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 프로필 사진 등 업로드 파일을 그대로 서빙 (auth.py의 PROFILE_IMAGE_DIR/URL_PREFIX와 짝).
os.makedirs("uploads/profile_images", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


@app.on_event("startup")
def _start_batch_scheduler():
    # 서버 프로세스 하나당 한 번만 떠야 한다 — uvicorn을 --workers 여러 개로 띄우면
    # 워커마다 스케줄러가 따로 돌아 같은 배치가 중복 실행된다(멱등해서 결과는 같지만
    # 낭비). 지금은 단일 워커 개발 서버라 문제 없음.
    start_scheduler()


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)