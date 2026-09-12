import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# DB 접속 정보는 코드에 직접 적지 말고 .env / 환경변수로 관리하세요.
# 예: DATABASE_URL=mysql+pymysql://admin:qwer1234@team2.xxxx.ap-northeast-2.rds.amazonaws.com/team2
DB_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://user:password@localhost:3306/team2",
)

engine = create_engine(DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI Depends용 DB 세션 제공자."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
