"""DOCX·PPTX 공용: 파일 열기 실패를 사유 코드로 바꾼다.

암호가 걸린 Office 문서는 ZIP(OOXML)이 아니라 OLE 복합 파일 안에 `EncryptedPackage` 스트림으로
저장된다. 이 경우는 `encrypted`, 그 밖에 ZIP·XML로 읽지 못하면 `corrupted`.
"""

import zipfile
from pathlib import Path

from app.db.enums import FailureCode
from app.ingest.parsers.base import ParseFailure

OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")


def check_ooxml_container(path: Path) -> None:
    """ZIP이 아니면 암호 파일인지 손상 파일인지 가려 `ParseFailure`를 낸다."""
    with path.open("rb") as f:
        head = f.read(8)
    if head == OLE_SIGNATURE:
        # 파일 크기는 업로드 상한(10MB) 이하라 통째로 읽어도 된다.
        if ENCRYPTED_PACKAGE in path.read_bytes():
            raise ParseFailure(FailureCode.ENCRYPTED)
        raise ParseFailure(FailureCode.CORRUPTED, "OLE 형식(이전 Office 형식이거나 확장자 불일치)")
    if not zipfile.is_zipfile(path):
        raise ParseFailure(FailureCode.CORRUPTED, "ZIP(OOXML) 형식이 아님")
