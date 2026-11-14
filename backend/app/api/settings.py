"""동기화 범위 설정 API (F-01, TRD 6.5, 8.3). 웹 세션 + CSRF.

- `GET /settings/sync`: `allowed_root`, `roots`, `excludes`(`[{pattern, fixed}]`, 고정 제외가 앞),
  `max_file_mb`, `updated_at`.
- `POST /settings/sync/preview`: 바꿀 `roots`, `excludes` → 범위에서 빠질 문서 수(`count`)와
  예시 경로 10개(`examples`), 정리된 설정. 저장하지 않는다.
- `PUT /settings/sync`: 저장. 범위에서 빠지는 문서를 바로 영구 삭제하고(인용 가림)
  `purged`를 돌려준다.
  선택 값 `expected_purge`(미리보기의 `count`)가 실제 삭제 수와 다르면 409 `preview_outdated`
  (미리보기 뒤에 문서가 바뀜. 확인받지 않은 수만큼 지우지 않는다).
- 오류: 400 `outside_root`(허용 폴더 밖), `relative_path`, `fixed_exclude`(고정 제외 아래 루트),
  `invalid_root`, `invalid_exclude`, `too_many_roots`, `too_many_excludes`.
- 로그: `settings.sync.read`, `settings.sync.previewed`,
  `settings.sync.updated`(개수만, 경로·패턴 원문 없음),
  `documents.purged`.
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import SyncConfig
from app.ingest.purge import purge_documents
from app.ingest.scope import FIXED_EXCLUDES
from app.sync.scope import SyncScope, load_scope
from app.sync.settings import (
    InvalidSetting,
    normalize_excludes,
    normalize_roots,
    save_config,
    scope_change,
)

router = APIRouter(prefix="/settings", tags=["settings"])
log = get_logger(__name__)

_MESSAGES = {
    "outside_root": "수집 루트는 허용 폴더 하위만 지정할 수 있습니다",
    "relative_path": "수집 루트는 절대 경로여야 합니다",
    "fixed_exclude": "고정 제외 폴더(/project/, /backup/)는 수집 루트로 지정할 수 없습니다",
    "invalid_root": "수집 루트 값이 올바르지 않습니다",
    "invalid_exclude": "제외 패턴 값이 올바르지 않습니다",
    "too_many_roots": "수집 루트가 너무 많습니다",
    "too_many_excludes": "제외 패턴이 너무 많습니다",
}


class ExcludeItem(BaseModel):
    pattern: str
    fixed: bool  # 고정 제외(웹에서 지울 수 없음)


class SyncSettingsOut(BaseModel):
    allowed_root: str
    roots: list[str]
    excludes: list[ExcludeItem]
    max_file_mb: int
    updated_at: datetime | None


class SyncSettingsIn(BaseModel):
    roots: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)  # 편집 가능한 패턴만(고정 제외는 무시)


class SyncSettingsUpdateIn(SyncSettingsIn):
    expected_purge: int | None = Field(default=None, ge=0)


class PreviewOut(BaseModel):
    count: int
    examples: list[str]
    roots: list[str]
    excludes: list[str]


class SyncSettingsSaved(SyncSettingsOut):
    purged: int


def _out(scope: SyncScope, updated_at: datetime | None) -> SyncSettingsOut:
    excludes = [ExcludeItem(pattern=p, fixed=True) for p in FIXED_EXCLUDES]
    excludes += [ExcludeItem(pattern=p, fixed=False) for p in scope.excludes]
    return SyncSettingsOut(
        allowed_root=scope.allowed_root,
        roots=scope.roots,
        excludes=excludes,
        max_file_mb=scope.max_file_mb,
        updated_at=updated_at,
    )


def _new_scope(current: SyncScope, body: SyncSettingsIn) -> SyncScope:
    try:
        roots = normalize_roots(body.roots, current.allowed_root)
        excludes = normalize_excludes(body.excludes)
    except InvalidSetting as e:
        log.info("settings.sync.rejected", reason=e.code)
        message = _MESSAGES.get(e.code, "설정 값이 올바르지 않습니다")
        raise ApiError(400, e.code, message, extra={"value": e.value}) from e
    return SyncScope(
        allowed_root=current.allowed_root,
        roots=roots,
        excludes=excludes,
        max_file_mb=current.max_file_mb,
    )


@router.get("/sync")
async def get_sync_settings(db: DbSession) -> SyncSettingsOut:
    scope = await load_scope(db, get_settings())
    config = await db.get(SyncConfig, 1)
    log.info("settings.sync.read", roots=len(scope.roots), excludes=len(scope.excludes))
    return _out(scope, config.updated_at if config else None)


@router.post("/sync/preview")
async def preview_sync_settings(body: SyncSettingsIn, db: DbSession) -> PreviewOut:
    current = await load_scope(db, get_settings())
    new = _new_scope(current, body)
    change = await scope_change(db, current, new)
    log.info(
        "settings.sync.previewed",
        count=change.count,
        roots=len(new.roots),
        excludes=len(new.excludes),
    )
    return PreviewOut(
        count=change.count, examples=change.examples, roots=new.roots, excludes=new.excludes
    )


@router.put("/sync")
async def update_sync_settings(body: SyncSettingsUpdateIn, db: DbSession) -> SyncSettingsSaved:
    current = await load_scope(db, get_settings())
    new = _new_scope(current, body)
    change = await scope_change(db, current, new)
    if body.expected_purge is not None and body.expected_purge != change.count:
        log.info(
            "settings.sync.rejected",
            reason="preview_outdated",
            expected=body.expected_purge,
            count=change.count,
        )
        raise ApiError(
            409,
            "preview_outdated",
            "미리보기 이후 삭제될 문서 수가 바뀌었습니다. 다시 확인해 주세요",
            extra={"count": change.count},
        )
    purged = await purge_documents(db, change.ids, reason="scope") if change.ids else None
    config = await save_config(db, new.roots, new.excludes)
    await db.commit()
    log.info(
        "settings.sync.updated",
        roots=len(new.roots),
        excludes=len(new.excludes),
        roots_added=len(set(new.roots) - set(current.roots)),
        roots_removed=len(set(current.roots) - set(new.roots)),
        excludes_added=len(set(new.excludes) - set(current.excludes)),
        excludes_removed=len(set(current.excludes) - set(new.excludes)),
        purged=purged.documents if purged else 0,
        masked_messages=purged.masked_messages if purged else 0,
    )
    await db.refresh(config)
    out = _out(new, config.updated_at)
    return SyncSettingsSaved(**out.model_dump(), purged=purged.documents if purged else 0)
