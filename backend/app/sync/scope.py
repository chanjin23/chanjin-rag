"""Sync 수집 범위 (PRD 3장 수집 범위, TRD 6.1, 11장 업로드).

허용 폴더(`SYNC_ALLOWED_ROOT`), `sync_config`의 루트·제외 패턴·최대 크기를 한데 묶는다.
manifest 판정, 업로드·메타 등록 검증, `GET /sync/config`가 같은 값을 쓴다.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import SyncConfig
from app.ingest.scope import check_scope, in_roots, is_upload_extension


@dataclass(frozen=True)
class SyncScope:
    """수집 범위: 허용 폴더(`SYNC_ALLOWED_ROOT`), `sync_config`의 루트와 제외 패턴, 최대 크기."""

    allowed_root: str
    roots: list[str]
    excludes: list[str]
    max_file_mb: int

    @property
    def max_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def path_reason(self, path: str) -> str | None:
        """수집할 수 없는 이유(없으면 None). 확장자는 따로 본다."""
        reason = check_scope(path, self.allowed_root, self.excludes)
        if reason is not None:
            return reason
        if not in_roots(path, self.roots):
            return "outside_root"
        return None

    def file_reason(self, path: str) -> str | None:
        reason = self.path_reason(path)
        if reason is None and not is_upload_extension(path.replace("\\", "/").rsplit("/", 1)[-1]):
            return "unsupported_extension"
        return reason


async def load_scope(session: AsyncSession, settings: Settings) -> SyncScope:
    config = await session.get(SyncConfig, 1)
    return SyncScope(
        allowed_root=settings.sync_allowed_root,
        roots=list(config.roots) if config else [],
        excludes=list(config.excludes) if config else [],
        max_file_mb=config.max_file_mb if config else 10,
    )
