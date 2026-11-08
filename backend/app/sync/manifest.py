"""manifest 판정 (TRD 6.1 판정표). 변경 판정은 서버가 한다.

Agent가 보낸 전체 목록(`path`, `size`, `mtime`, `sha256`)과
`scan_errors`(읽지 못한 폴더·잠긴 파일)를 `documents`와 비교한다.
항목마다 판정표를 위에서부터 순서대로 적용한다.

| 경우 | 판정 |
| --- | --- |
| 새 경로, 같은 해시의 기존 문서 경로가 manifest에 없음 | `moved`: 기존 문서의 경로만 바꿈 |
| 새 경로, 같은 해시의 기존 문서 경로도 manifest에 있음 | `copied`: sections·chunks·임베딩 복사 |
| 새 경로이거나 해시가 다름 | 10MB 이하면 `upload`, 초과면 `meta_only` |
| 해시가 같음 | 수정 시각만 갱신 |
| `needs_upload=true` | `upload` |
| DB에 있고 manifest에 없음(`scan_errors` 아래 아님) | `deleted`: `locally_deleted=true` |
| DB에 있고 manifest에 없음(`scan_errors` 아래) | 판정 보류 |
| `locally_deleted`인데 다시 나타남 | `restored`: 표시 해제 |

- 이동·복사의 원본 후보는 같은 해시·같은 확장자의 문서 중 (로컬 삭제 아님, ID 작은 순)으로 고른다.
  확장자가 다르면 파서가 달라지고 임시 원본 이름(`{id}-{sha}{ext}`)도 달라져 업로드로 판정한다.
  이동한 문서는 새 경로가 manifest에 있으므로, 같은 해시의 다음 새 경로는 그 문서에서 복사한다.
- 옛 경로가 `scan_errors` 아래인 문서는 이동 원본으로 고르지 않는다(읽지 못했을 뿐 사라진 것이
  아님). 이때 새 경로는 복사(추출이 끝났으면) 또는 업로드로 판정한다.
- 복사할 원본이 아직 추출을 끝내지 않았으면(대기·추출 중) 복사 대신 업로드로 판정한다.
- 삭제 판정 대상은 로컬 삭제 표시가 없고 수집 범위 안(허용 폴더 하위, `roots` 하위, 제외 패턴·고정
  제외에 걸리지 않음)인 문서다. 범위에서 빠진 문서는 설정을 저장할 때 영구 삭제한다(TRD 6.5).
- 대량 삭제 보호: 새 삭제 판정 수가 범위 안 활성 문서 수의 `MASS_DELETE_GUARD_RATIO`(20%)를 넘으면
  삭제 판정을 모두 보류하고 실행의 `stats.deletion_held`에 기록한다.
- 범위 밖·고정 제외·지원하지 않는 확장자 항목은 판정하지 않고 `rejected`로 돌려준다(Agent 규칙의
  서버 측 검증).
- `upload`·`meta_only` 판정은 문서를 만들지 않는다. 업로드(`PUT /sync/files`)나 메타 등록
  (`POST /sync/files/meta`)이 오면 그때 접수한다. 업로드가 실패하면 다음 manifest에서 다시 `upload`.
- 로그 `sync.manifest.judged`는 건수만 남긴다(경로·파일명 없음).
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.keys import make_path_key
from app.db.models import Document, SyncRun
from app.ingest.intake import FileMeta
from app.ingest.scope import under_any
from app.sync.documents import copy_document, copyable, relocate
from app.sync.scope import SyncScope

log = get_logger(__name__)

# stats에 남기는 보류 경로 예시 수(동기화 화면 표시용)
HELD_SAMPLE_SIZE = 20


@dataclass
class ManifestResult:
    upload: list[str] = field(default_factory=list)
    meta_only: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    moved: list[dict[str, str]] = field(default_factory=list)
    copied: list[dict[str, str]] = field(default_factory=list)
    deletion_held: list[str] = field(default_factory=list)
    scan_error_held: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    unchanged: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "upload": len(self.upload),
            "meta_only": len(self.meta_only),
            "deleted": len(self.deleted),
            "restored": len(self.restored),
            "moved": len(self.moved),
            "copied": len(self.copied),
            "deletion_held": len(self.deletion_held),
            "scan_error_held": len(self.scan_error_held),
            "rejected": len(self.rejected),
            "unchanged": self.unchanged,
        }


async def judge_manifest(
    session: AsyncSession,
    run: SyncRun,
    items: list[FileMeta],
    scan_errors: list[str],
    *,
    scope: SyncScope,
    guard_ratio: float,
    now: datetime,
) -> ManifestResult:
    """판정표를 적용하고 결과 목록을 돌려준다. 이동·복사·삭제·복원은 DB에 바로 반영한다."""
    result = ManifestResult()
    docs = list((await session.scalars(select(Document).order_by(Document.id))).all())
    by_key: dict[str, Document] = {d.path_key: d for d in docs}

    # 판정할 항목(범위 검증, 같은 경로 중복 제거)
    accepted: dict[str, FileMeta] = {}
    for item in items:
        reason = scope.file_reason(item.path)
        key = make_path_key(item.path)
        if reason is None and key in accepted:
            reason = "duplicate"
        if reason is not None:
            result.rejected.append({"path": item.path, "reason": reason})
            continue
        accepted[key] = item
    manifest_keys = set(accepted)

    # 판정 전의 범위 안 활성 문서(대량 삭제 보호의 분모, 삭제 판정 후보)
    active_in_scope = [
        d for d in docs if not d.locally_deleted and scope.path_reason(d.path) is None
    ]

    by_hash: dict[str, list[Document]] = {}
    for d in docs:
        by_hash.setdefault(d.sha256, []).append(d)
    for group in by_hash.values():
        group.sort(key=lambda d: (d.locally_deleted, d.id))

    seen: set[int] = set()  # manifest에 나타난 문서(이동 원본 포함)

    def want_upload(meta: FileMeta) -> None:
        if meta.size > scope.max_bytes:
            result.meta_only.append(meta.path)
        else:
            result.upload.append(meta.path)

    for key, meta in accepted.items():
        doc = by_key.get(key)
        if doc is None:
            same = [d for d in by_hash.get(meta.sha256, []) if d.ext == meta.ext]
            moving = next(
                (
                    d
                    for d in same
                    if d.path_key not in manifest_keys and not under_any(d.path, scan_errors)
                ),
                None,
            )
            if moving is not None:
                old_path, old_key = moving.path, moving.path_key
                was_deleted = moving.locally_deleted
                await relocate(session, moving, meta)
                moving.locally_deleted = False
                moving.deleted_at = None
                by_key.pop(old_key, None)
                by_key[key] = moving
                seen.add(moving.id)
                result.moved.append({"from": old_path, "to": meta.path})
                if was_deleted:
                    result.restored.append(meta.path)
                continue
            source = next((d for d in same if copyable(d)), None)
            if source is not None:
                copy = await copy_document(session, source, meta, now)
                by_key[key] = copy
                by_hash.setdefault(meta.sha256, []).append(copy)
                seen.add(copy.id)
                result.copied.append({"from": source.path, "to": meta.path})
                continue
            want_upload(meta)
            continue

        seen.add(doc.id)
        if doc.locally_deleted:
            doc.locally_deleted = False
            doc.deleted_at = None
            result.restored.append(meta.path)
        if doc.sha256 != meta.sha256:
            want_upload(meta)
        elif doc.needs_upload:
            want_upload(meta)
        else:
            if doc.path != meta.path:
                await relocate(session, doc, meta)  # 대소문자·정규화만 바뀐 경로 표기
            doc.mtime = meta.mtime
            result.unchanged += 1

    # 삭제 판정
    missing = [d for d in active_in_scope if d.id not in seen]
    held_by_scan = [d for d in missing if under_any(d.path, scan_errors)]
    to_delete = [d for d in missing if not under_any(d.path, scan_errors)]
    result.scan_error_held = [d.path for d in held_by_scan]

    held: dict[str, Any] = {}
    total = len(active_in_scope)
    ratio = len(to_delete) / total if total else 0.0
    if to_delete and ratio > guard_ratio:
        result.deletion_held = [d.path for d in to_delete]
        held["mass_delete"] = {
            "count": len(to_delete),
            "total": total,
            "ratio": round(ratio, 4),
            "threshold": guard_ratio,
            "sample": result.deletion_held[:HELD_SAMPLE_SIZE],
        }
        log.warning(
            "sync.deletion.held",
            run_id=run.id,
            reason="mass_delete_guard",
            count=len(to_delete),
            total=total,
            ratio=round(ratio, 4),
            threshold=guard_ratio,
        )
    else:
        for d in to_delete:
            d.locally_deleted = True
            d.deleted_at = now
            result.deleted.append(d.path)
    if held_by_scan:
        held["scan_error"] = {
            "count": len(held_by_scan),
            "sample": result.scan_error_held[:HELD_SAMPLE_SIZE],
        }
        log.info(
            "sync.deletion.held",
            run_id=run.id,
            reason="scan_error",
            count=len(held_by_scan),
            scan_errors=len(scan_errors),
        )
    await session.flush()

    counts = result.counts()
    rejected_reasons = Counter(r["reason"] for r in result.rejected)
    run.stats = {
        **run.stats,
        "manifest": {
            "files": len(items),
            "scan_errors": len(scan_errors),
            **counts,
            "rejected_reasons": dict(rejected_reasons),
        },
        "deletion_held": held,
    }
    run.heartbeat_at = now
    await session.flush()
    log.info(
        "sync.manifest.judged",
        run_id=run.id,
        files=len(items),
        scan_errors=len(scan_errors),
        **counts,
    )
    return result
