import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import jwt, JWTError

# 반드시 .env 등 환경변수로 주입하고, 기본값은 로컬 개발용으로만 사용하세요.
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 14

# bcrypt는 72바이트까지만 처리하므로 그 이상은 미리 자른다.
_BCRYPT_MAX_BYTES = 72


def hash_password(plain_password: str) -> str:
    password_bytes = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    password_bytes = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def generate_refresh_token() -> str:
    """access token(JWT)과 달리 자체 검증 정보가 없는 불투명한 랜덤 문자열 —
    서버가 DB에 해시로 들고 있다가 대조하는 방식이라 이걸로 충분하고 더 간단하다."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw_token: str) -> str:
    """비밀번호와 달리 무작위 고엔트로피 토큰이라 별도 salt 없이 SHA-256 해시만으로 충분."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
