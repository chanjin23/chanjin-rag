"""Agent 테스트 도우미: 가짜 Sync API 서버(httpx.MockTransport), 가짜 잠금, 파일 쓰기.

가짜 서버는 백엔드 판정의 단순한 부분만 흉내 낸다: 경로별 해시가 다르면 `upload`(최대 크기 이하)
또는 `meta_only`(초과). 업로드는 multipart를 풀어 해시를 다시 계산하고 다르면 409 `hash_mismatch`.
"""

import hashlib
import json
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import default as default_policy
from pathlib import Path
from typing import Any

import httpx

from rag_agent.client import SyncClient
from rag_agent.keys import make_path_key

TOKEN = "test-token-0123456789"


def parse_multipart(request: httpx.Request) -> dict[str, bytes]:
    head = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
    msg = BytesParser(policy=default_policy).parsebytes(head + request.content)
    fields: dict[str, bytes] = {}
    for part in msg.iter_parts():  # type: ignore[attr-defined]
        name = part.get_param("name", header="content-disposition")
        fields[str(name)] = part.get_payload(decode=True) or b""
    return fields


class FakeSyncServer:
    def __init__(self, root: Path, max_file_mb: int = 1) -> None:
        self.config: dict[str, Any] = {
            "allowed_root": str(root),
            "roots": [str(root)],
            "excludes": [
                {"pattern": "/project/", "fixed": True},
                {"pattern": "/backup/", "fixed": True},
                {"pattern": "node_modules/", "fixed": False},
                {"pattern": "~$*", "fixed": False},
                {"pattern": "*.tmp", "fixed": False},
            ],
            "max_file_mb": max_file_mb,
            "extensions": [".docx", ".hwp", ".hwpx", ".md", ".pdf", ".pptx", ".txt", ".xlsx"],
        }
        self.docs: dict[str, str] = {}  # path_key → sha256
        self.calls: list[tuple[str, str]] = []
        self.uploads: list[dict[str, Any]] = []
        self.meta: list[dict[str, Any]] = []
        self.manifests: list[dict[str, Any]] = []
        self.completes: list[dict[str, Any]] = []
        self.heartbeats = 0
        self.next_run = 1
        self.fail_upload: Callable[[str], bool] = lambda path: False
        self.on_manifest: Callable[[dict[str, Any]], None] = lambda body: None

    # -- 요청 처리 ---------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        path = request.url.path.removeprefix("/api/sync")
        self.calls.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return self._error(401, "invalid_token")
        if request.method == "GET" and path == "/config":
            return httpx.Response(200, json=self.config)
        if request.method == "POST" and path == "/runs":
            run_id = self.next_run
            self.next_run += 1
            return httpx.Response(201, json={"run_id": run_id, "status": "running"})
        if request.method == "POST" and path.endswith("/heartbeat"):
            self.heartbeats += 1
            return httpx.Response(200, json={"status": "running"})
        if request.method == "POST" and path.endswith("/manifest"):
            return self._manifest(json.loads(request.content))
        if request.method == "PUT" and path == "/files":
            return self._upload(request)
        if request.method == "POST" and path == "/files/meta":
            body = json.loads(request.content)
            self.meta.append(body)
            for f in body["files"]:
                self.docs[make_path_key(f["path"])] = f["sha256"]
            accepted = [
                {"path": f["path"], "document_id": 1, "outcome": "meta_only"} for f in body["files"]
            ]
            return httpx.Response(200, json={"accepted": accepted, "rejected": []})
        if request.method == "POST" and path.endswith("/complete"):
            self.completes.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "succeeded"})
        return self._error(404, "not_found")

    @staticmethod
    def _error(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": code}})

    def _manifest(self, body: dict[str, Any]) -> httpx.Response:
        self.manifests.append(body)
        max_bytes = int(self.config["max_file_mb"]) * 1024 * 1024
        upload: list[str] = []
        meta_only: list[str] = []
        unchanged = 0
        for f in body["files"]:
            if self.docs.get(make_path_key(f["path"])) == f["sha256"]:
                unchanged += 1
            elif f["size"] > max_bytes:
                meta_only.append(f["path"])
            else:
                upload.append(f["path"])
        self.on_manifest(body)
        return httpx.Response(
            200,
            json={
                "run_id": 1,
                "upload": upload,
                "meta_only": meta_only,
                "deleted": [],
                "restored": [],
                "moved": [],
                "copied": [],
                "deletion_held": [],
                "scan_error_held": [],
                "rejected": [],
                "counts": {
                    "upload": len(upload),
                    "meta_only": len(meta_only),
                    "unchanged": unchanged,
                },
            },
        )

    def _upload(self, request: httpx.Request) -> httpx.Response:
        fields = parse_multipart(request)
        path = fields["path"].decode("utf-8")
        if self.fail_upload(path):
            return self._error(500, "internal_error")
        content = fields["file"]
        expected = fields["sha256"].decode()
        if hashlib.sha256(content).hexdigest() != expected:
            return self._error(409, "hash_mismatch")
        self.uploads.append(
            {
                "path": path,
                "sha256": expected,
                "run_id": fields["run_id"].decode(),
                "size": len(content),
            }
        )
        self.docs[make_path_key(path)] = expected
        return httpx.Response(200, json={"document_id": len(self.uploads), "outcome": "queued"})

    def client(self, token: str = TOKEN) -> SyncClient:
        return SyncClient("http://fake.test", token, transport=httpx.MockTransport(self))


class FakeLock:
    def __init__(self, held_by_other: bool = False) -> None:
        self.held_by_other = held_by_other
        self.acquired = 0
        self.released = 0

    def acquire(self) -> bool:
        if self.held_by_other:
            return False
        self.acquired += 1
        return True

    def release(self) -> None:
        self.released += 1


def write(path: Path, data: str | bytes = "내용") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path
