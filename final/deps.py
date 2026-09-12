from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

import security
from database import get_db
from models import User

# auth.py의 BLOCKED_STATUS_MESSAGES와 같은 값. auth.py -> deps.py 방향 import라
# 순환참조를 피하려고 여기서 다시 정의한다(둘 다 바뀔 일이 거의 없는 코드값).
_BLOCKED_STATUS_MESSAGES: dict[int, str] = {
    2: "정지된 계정입니다.",
    4: "탈퇴한 계정입니다.",
}


def get_current_user(
    access_token: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """쿠키(access_token) 또는 Authorization: Bearer 헤더로 로그인 상태를 확인."""
    token = access_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다."
        )

    payload = security.decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 토큰입니다."
        )

    user = db.query(User).filter(User.user_no == int(payload["sub"])).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자를 찾을 수 없습니다."
        )

    # 로그인 이후 정지/탈퇴 처리된 계정이면, 만료 전까지 유효한 기존 토큰이라도 여기서 막는다.
    blocked_message = _BLOCKED_STATUS_MESSAGES.get(user.status_no)
    if blocked_message:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=blocked_message)

    return user
