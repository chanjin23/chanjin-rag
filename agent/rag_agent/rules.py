"""수집 범위와 제외 규칙 (PRD 3장 수집 범위, TRD 6.1 Agent 쪽 규칙).

- 허용 폴더(`allowed_root`, 운영 `C:/Chanjin/dev`) 기준 상대 경로(`a/b.txt`)에 규칙을 적용한다.
- 고정 제외 `/project/`, `/backup/`은 코드에 두고 서버 설정과 상관없이 항상 적용한다.
  앞의 `/`가 있어 허용 폴더 바로 아래만 빼고, 하위의 같은 이름 폴더는 빼지 않는다.
- 제외 패턴은 gitignore 문법(pathspec `GitIgnoreSpec`)이다. Windows 경로이므로 대소문자를 무시한다.
- 지원 확장자만 manifest에 넣는다(대소문자 무시). 그 밖의 형식은 형식별 건너뛴 수만 센다.
"""

import os
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import pathspec

FIXED_EXCLUDES: tuple[str, ...] = ("/project/", "/backup/")
DEFAULT_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".txt", ".md", ".docx", ".hwp", ".hwpx", ".xlsx", ".pptx"}
)


def _norm(path: str) -> str:
    """비교용: NFC, 역슬래시→`/`, 소문자, 끝 `/` 제거."""
    s = unicodedata.normalize("NFC", path).replace("\\", "/")
    return unicodedata.normalize("NFC", s.lower()).rstrip("/")


def relative_to(path: str, root: str) -> str | None:
    """`root` 기준 상대 경로(소문자, `/` 구분, 앞 `/` 없음). 같으면 ``""``, 밖이면 None."""
    p, r = _norm(path), _norm(root)
    if not r:
        return None
    if ".." in p.split("/"):
        return None
    if p == r:
        return ""
    if not p.startswith(r + "/"):
        return None
    return p[len(r) + 1 :]


def is_under(path: str, root: str) -> bool:
    return relative_to(path, root) is not None


def extension(name: str) -> str:
    """소문자 확장자(`.pdf`). 없으면 빈 문자열(`.gitignore` 같은 점 파일도 빈 문자열)."""
    return os.path.splitext(name)[1].lower()


@dataclass
class SyncRules:
    """`GET /sync/config` 응답을 Agent가 쓰는 형태로 묶은 것."""

    allowed_root: str
    roots: list[str]
    excludes: list[str] = field(default_factory=list)
    extensions: frozenset[str] = DEFAULT_EXTENSIONS
    max_file_mb: int = 10

    def __post_init__(self) -> None:
        patterns = list(FIXED_EXCLUDES) + [p for p in self.excludes if p.strip()]
        lowered = [p.strip().replace("\\", "/").lower() for p in patterns]
        self._spec = pathspec.GitIgnoreSpec.from_lines(lowered)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SyncRules":
        excludes = [
            str(e["pattern"]) if isinstance(e, dict) else str(e) for e in config.get("excludes", [])
        ]
        exts = config.get("extensions")
        return cls(
            allowed_root=str(config["allowed_root"]),
            roots=[str(r) for r in config.get("roots", [])],
            excludes=excludes,
            extensions=frozenset(str(e).lower() for e in exts) if exts else DEFAULT_EXTENSIONS,
            max_file_mb=int(config.get("max_file_mb", 10)),
        )

    def to_config(self) -> dict[str, Any]:
        return {
            "allowed_root": self.allowed_root,
            "roots": self.roots,
            "excludes": [{"pattern": p, "fixed": False} for p in self.excludes],
            "extensions": sorted(self.extensions),
            "max_file_mb": self.max_file_mb,
        }

    @property
    def max_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def in_scope(self, path: str) -> bool:
        """허용 폴더 안이고 `roots` 중 하나의 하위인지(제외 여부는 따로 본다)."""
        if relative_to(path, self.allowed_root) is None:
            return False
        return any(is_under(path, root) for root in self.roots)

    def excluded(self, path: str, is_dir: bool = False) -> bool:
        """고정 제외·제외 패턴에 걸리는지. 허용 폴더 밖이면 True."""
        rel = relative_to(path, self.allowed_root)
        if rel is None:
            return True
        if rel == "":
            return False
        return bool(self._spec.match_file(rel + "/" if is_dir else rel))

    def supported(self, name: str) -> bool:
        return extension(name) in self.extensions

    def file_allowed(self, path: str) -> bool:
        """`reveal` 확인용: 범위 안, 제외 아님, 지원 확장자."""
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        return self.in_scope(path) and not self.excluded(path) and self.supported(name)
