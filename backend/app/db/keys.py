"""문서 경로 키 (TRD 5.1 `documents.path_key`).

Windows 경로는 대소문자를 구분하지 않고, 한글 파일명은 NFC/NFD 두 형태로 올 수 있다.
그래서 NFC로 정규화한 뒤 소문자로 바꾼 값을 unique 키로 쓴다. 원래 경로는 `path`에 그대로 둔다.
"""

import unicodedata


def make_path_key(path: str) -> str:
    # 소문자 변환이 결합 문자를 만들 수 있어(예: 'İ') 한 번 더 NFC로 맞춘다
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).lower())
