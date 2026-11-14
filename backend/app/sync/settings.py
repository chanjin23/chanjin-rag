"""동기화 범위 설정 변경 (F-01, PRD 3장 수집 범위·서버 데이터 삭제, TRD 6.5, 8.3).

- 루트는 허용 폴더(`SYNC_ALLOWED_ROOT`, 운영 `C:\\Chanjin\\dev`) 하위(또는 그 자신)만 받는다.
  고정 제외(`/project/`, `/backup/`) 아래 폴더도 루트로 받지 않는다.
- 제외 패턴은 편집 가능한 것만 저장한다. 고정 제외는 코드에 있어 늘 적용되므로, 요청에 들어 있어도
  저장하지 않는다(웹에서 지우거나 바꿀 수 없음).
- 범위에서 빠지는 문서 = 지금 설정으로 범위 안이던 문서 중 새 설정으로는 범위 밖인 문서.
  저장할 때 이 문서들을 영구 삭제하고(`purge_documents(reason="scope")`, 인용 가림),
  미리보기는 같은 계산으로 개수와 예시만 돌려준다(저장 안 함).
"""

import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.keys import make_path_key
from app.db.models import Document, SyncConfig
from app.ingest.scope import FIXED_EXCLUDES, check_scope
from app.sync.scope import SyncScope

PREVIEW_EXAMPLES = 10
MAX_ROOTS = 50
MAX_EXCLUDES = 200
MAX_PATTERN_LENGTH = 1024


class InvalidSetting(Exception):
    """설정 값 오류. `code`는 API 오류 코드, `value`는 문제가 된 값."""

    def __init__(self, code: str, value: str) -> None:
        super().__init__(code)
        self.code = code
        self.value = value


def _strip_trailing_separator(path: str) -> str:
    out = path
    while len(out) > 1 and out[-1] in "/\\" and not (len(out) == 3 and out[1] == ":"):
        out = out[:-1]
    return out


def normalize_roots(roots: list[str], allowed_root: str) -> list[str]:
    """앞뒤 공백·끝 구분자를 정리하고, 허용 폴더 하위인지 검사하고, 같은 경로는 하나만 남긴다."""
    if len(roots) > MAX_ROOTS:
        raise InvalidSetting("too_many_roots", str(len(roots)))
    out: list[str] = []
    keys: set[str] = set()
    for raw in roots:
        root = _strip_trailing_separator(unicodedata.normalize("NFC", raw.strip()))
        if not root:
            continue
        if len(root) > MAX_PATTERN_LENGTH:
            raise InvalidSetting("invalid_root", root[:80])
        reason = check_scope(root, allowed_root)
        if reason is not None:
            raise InvalidSetting(reason, root)
        key = make_path_key(root)
        if key not in keys:
            keys.add(key)
            out.append(root)
    return out


def is_fixed_exclude(pattern: str) -> bool:
    norm = pattern.strip().replace("\\", "/").lower()
    return any(norm in (fixed, fixed.rstrip("/")) for fixed in FIXED_EXCLUDES)


def normalize_excludes(excludes: list[str]) -> list[str]:
    """빈 값·중복·고정 제외를 뺀 편집 가능한 제외 패턴 목록."""
    if len(excludes) > MAX_EXCLUDES:
        raise InvalidSetting("too_many_excludes", str(len(excludes)))
    out: list[str] = []
    for raw in excludes:
        pattern = unicodedata.normalize("NFC", raw.strip())
        if not pattern or is_fixed_exclude(pattern):
            continue
        if len(pattern) > MAX_PATTERN_LENGTH or any(c in pattern for c in "\r\n\t"):
            raise InvalidSetting("invalid_exclude", pattern[:80])
        if pattern not in out:
            out.append(pattern)
    return out


@dataclass(frozen=True)
class ScopeChange:
    """새 설정으로 범위에서 빠지는 문서."""

    ids: list[int]
    examples: list[str]

    @property
    def count(self) -> int:
        return len(self.ids)


async def scope_change(session: AsyncSession, current: SyncScope, new: SyncScope) -> ScopeChange:
    rows = (await session.execute(select(Document.id, Document.path))).all()
    leaving = sorted(
        (
            (make_path_key(path), int(doc_id), path)
            for doc_id, path in rows
            if current.path_reason(path) is None and new.path_reason(path) is not None
        ),
    )
    return ScopeChange(
        ids=[doc_id for _, doc_id, _ in leaving],
        examples=[path for _, _, path in leaving[:PREVIEW_EXAMPLES]],
    )


async def save_config(session: AsyncSession, roots: list[str], excludes: list[str]) -> SyncConfig:
    config = await session.get(SyncConfig, 1, with_for_update=True)
    if config is None:
        config = SyncConfig(id=1, roots=roots, excludes=excludes)
        session.add(config)
    else:
        config.roots = roots
        config.excludes = excludes
    await session.flush()
    return config
