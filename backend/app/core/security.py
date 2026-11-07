"""비밀번호 해시와 난수 토큰 (TRD 11장).

- 비밀번호: argon2id(argon2-cffi 기본값).
- 세션 쿠키·Agent 토큰: 32바이트 난수(URL-safe base64). DB에는 SHA-256 16진수(64자)만 저장한다.
"""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # 기본 type=Argon2id

# 없는 사용자로 로그인할 때도 같은 시간이 걸리게 비교용 해시를 하나 만들어 둔다.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def burn_password_check(password: str) -> None:
    """사용자가 없을 때 응답 시간으로 존재 여부가 드러나지 않게 같은 비용의 검증을 한다."""
    verify_password(_DUMMY_HASH, password)


def new_token() -> str:
    """32바이트 난수 토큰(쿠키 값, Agent 토큰)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
