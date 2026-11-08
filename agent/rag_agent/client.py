"""Sync API 클라이언트 (TRD 8.2). Bearer 토큰, httpx 동기 Client.

테스트는 `transport`에 `httpx.MockTransport`(가짜 서버)를 넘긴다.
토큰은 헤더에만 넣고 로그·예외 메시지에 남기지 않는다.
"""

from typing import Any

import httpx

from rag_agent.scan import ScannedFile

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ApiError(Exception):
    """Sync API가 오류 응답을 줬다. `code`는 서버의 `error.code`(없으면 `http_<상태>`)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


def _raise_for(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    code = f"http_{resp.status_code}"
    try:
        body = resp.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = str(body["error"].get("code") or code)
    except ValueError:
        pass
    raise ApiError(resp.status_code, code)


class SyncClient:
    def __init__(
        self,
        server: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=server.rstrip("/") + "/api/sync",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "rag-agent/0.1"},
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SyncClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        _raise_for(resp)
        data = resp.json()
        if not isinstance(data, dict):
            raise ApiError(resp.status_code, "invalid_response")
        return data

    def get_config(self) -> dict[str, Any]:
        return self._json(self._http.get("/config"))

    def start_run(self, source: str) -> int:
        return int(self._json(self._http.post("/runs", json={"source": source}))["run_id"])

    def heartbeat(self, run_id: int) -> None:
        _raise_for(self._http.post(f"/runs/{run_id}/heartbeat"))

    def manifest(
        self, run_id: int, files: list[dict[str, Any]], scan_errors: list[str]
    ) -> dict[str, Any]:
        return self._json(
            self._http.post(
                f"/runs/{run_id}/manifest", json={"files": files, "scan_errors": scan_errors}
            )
        )

    def upload(self, run_id: int, item: ScannedFile) -> dict[str, Any]:
        name = item.path.replace("\\", "/").rsplit("/", 1)[-1]
        with open(item.fs_path, "rb") as f:
            resp = self._http.put(
                "/files",
                data={
                    "path": item.path,
                    "sha256": item.sha256,
                    "mtime": item.mtime_iso,
                    "run_id": str(run_id),
                },
                files={"file": (name, f, "application/octet-stream")},
                timeout=UPLOAD_TIMEOUT,
            )
        return self._json(resp)

    def register_meta(self, run_id: int, files: list[dict[str, Any]]) -> dict[str, Any]:
        return self._json(self._http.post("/files/meta", json={"files": files, "run_id": run_id}))

    def complete(
        self, run_id: int, status: str, stats: dict[str, Any], error: str | None = None
    ) -> None:
        body: dict[str, Any] = {"status": status, "stats": stats}
        if error:
            body["error"] = error
        _raise_for(self._http.post(f"/runs/{run_id}/complete", json=body))
