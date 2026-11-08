"""업로드 수신 (TRD 6.1, 8.2, 11장 업로드).

- 경로 검증: 허용 폴더(`SYNC_ALLOWED_ROOT`)와 `sync_config.roots` 안이고,
  고정 제외·제외 패턴에 걸리지 않으며, 확장자가 화이트리스트(대소문자 무시)에 있어야 한다.
  Agent도 같은 규칙으로 거르지만 서버가 다시 검증한다.
- 받은 내용을 `UPLOAD_TMP_DIR`의 임시 파일에 쓰면서 SHA-256과 크기를 센다. 크기가 `max_file_mb`를
  넘으면 바로 멈춘다. 해시가 Agent가 보낸 값과 다르면(업로드 사이에 파일이 바뀜) 저장하지 않는다.
- 통과하면 `intake.accept_upload`로 임시 원본을 두고 `extract` 작업을 등록한다(T06).
"""

import hashlib
import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

_READ_CHUNK = 1024 * 1024


class AsyncReadable(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


class FileTooLarge(Exception):
    pass


def incoming_path() -> Path:
    """받는 중인 파일의 임시 위치. 검증이 끝나면 `intake`가 버전별 이름으로 옮긴다."""
    folder = get_settings().upload_tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"incoming-{uuid.uuid4().hex}.part"


async def receive(stream: AsyncReadable, target: Path, *, max_bytes: int) -> tuple[str, int]:
    """`stream`을 `target`에 쓰고 (SHA-256, 크기)를 돌려준다. 너무 크면 `FileTooLarge`."""
    h = hashlib.sha256()
    size = 0
    with target.open("wb") as f:
        while block := await stream.read(_READ_CHUNK):
            size += len(block)
            if size > max_bytes:
                raise FileTooLarge(str(size))
            h.update(block)
            f.write(block)
    return h.hexdigest(), size


def discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
