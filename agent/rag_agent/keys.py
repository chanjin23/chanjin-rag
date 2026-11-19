"""경로 키 (TRD 5.1 `documents.path_key`, 10.2 `reveal`의 `k`).

- `path_key`: NFC 정규화 → 소문자 → 다시 NFC. 백엔드 `app.db.keys.make_path_key`와 같은 식이다
  (Agent는 백엔드 패키지에 의존하지 않으므로 같은 식을 여기 둔다. 테스트가 둘이 같은지 확인한다).
- `reveal_key`: `path_key`의 SHA-256(64자리 소문자 16진수).
  웹의 "폴더에서 보기"가 URL에 이 값만 넣는다.
"""

import hashlib
import unicodedata


def nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def make_path_key(path: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).lower())


def reveal_key(path: str) -> str:
    return hashlib.sha256(make_path_key(path).encode("utf-8")).hexdigest()
