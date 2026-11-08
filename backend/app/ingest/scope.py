"""수집 범위 검증 (PRD 3장 수집 범위, TRD 6.1).

- 수집 허용 최상위 폴더(`SYNC_ALLOWED_ROOT`, 운영 `C:\\Chanjin\\dev`) 밖의 경로는 받지 않는다.
- 고정 제외 `/project/`, `/backup/`은 허용 폴더 기준이다(앞의 `/`가 있어 하위의 같은 이름 폴더는
  빼지 않는다). 웹에서 지울 수 없으므로 코드에 둔다.
- 제외 패턴(`sync_config.excludes`)은 gitignore 문법의 단순한 부분만 지원한다:
  `이름/`(어느 깊이든 그 이름의 폴더), `/경로/`(허용 폴더 기준 고정 위치),
  그 밖에는 파일 이름에 대한 glob(`~$*`, `*.tmp`). Agent도 같은 규칙으로 걸러서 보낸다.
- 비교는 NFC 정규화 + `\\`→`/` + 대소문자 무시로 한다(Windows 경로, TRD 5.1 `path_key`와 같은 기준).
  서버(Linux)에서도 Windows 경로 문자열을 그대로 검사할 수 있게 문자열로만 비교한다.
"""

import fnmatch
import unicodedata
from collections.abc import Iterable
from typing import Literal

# 고정 제외(허용 폴더 기준). 웹에서 지울 수 없다.
FIXED_EXCLUDES: tuple[str, ...] = ("/project/", "/backup/")

# Agent가 manifest에 넣는 확장자(TRD 6.1). HWP·HWPX·XLSX는 받아서 `unsupported_format`으로 기록.
UPLOAD_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".txt", ".md", ".docx", ".hwp", ".hwpx", ".xlsx", ".pptx"}
)

ScopeReason = Literal["outside_root", "fixed_exclude", "excluded", "relative_path"]


def _norm(path: str) -> str:
    s = unicodedata.normalize("NFC", path).replace("\\", "/")
    while "//" in s[1:]:
        s = s[0] + s[1:].replace("//", "/")
    return unicodedata.normalize("NFC", s.lower()).rstrip("/")


def relative_path(path: str, allowed_root: str) -> str | None:
    """허용 폴더 기준 상대 경로(`/a/b.txt`, 비교용 소문자). 밖이면 None. 허용 폴더 자신은 `/`."""
    p, root = _norm(path), _norm(allowed_root)
    if not root:
        return None
    parts = p.split("/")
    if ".." in parts or "." in parts:
        return None
    if p == root:
        return "/"
    if not p.startswith(root + "/"):
        return None
    return p[len(root) :]


def _matches(rel: str, pattern: str) -> bool:
    pat = _norm(pattern.strip()) + ("/" if pattern.strip().endswith(("/", "\\")) else "")
    if not pat.strip("/"):
        return False
    rel_dirs = rel.rsplit("/", 1)[0] + "/"  # 파일이 들어 있는 폴더들(`/a/b/`)
    if pat.endswith("/"):
        if pat.startswith("/"):
            return rel_dirs.startswith(pat) or (rel + "/").startswith(pat)
        name = pat.strip("/")
        return any(fnmatch.fnmatchcase(seg, name) for seg in rel_dirs.strip("/").split("/") if seg)
    if pat.startswith("/"):
        return fnmatch.fnmatchcase(rel, pat)
    return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pat)


def check_scope(path: str, allowed_root: str, excludes: Iterable[str] = ()) -> ScopeReason | None:
    """수집할 수 없는 이유(없으면 None). 폴더와 파일 경로 모두에 쓴다."""
    if not path or (not path.startswith(("/", "\\")) and ":" not in path[:3]):
        return "relative_path"
    rel = relative_path(path, allowed_root)
    if rel is None:
        return "outside_root"
    probe = rel if rel.endswith("/") else rel + "/"
    if any(probe.startswith(fixed) for fixed in FIXED_EXCLUDES):
        return "fixed_exclude"
    if rel != "/" and any(_matches(rel, pattern) for pattern in excludes):
        return "excluded"
    return None


def is_upload_extension(filename: str) -> bool:
    dot = filename.rfind(".")
    return dot >= 0 and filename[dot:].lower() in UPLOAD_EXTENSIONS
