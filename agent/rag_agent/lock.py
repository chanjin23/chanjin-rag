"""단일 인스턴스 (TRD 6.1 Agent 쪽 규칙).

Windows에서는 이름 있는 뮤텍스(`Local\\chanjin-rag-agent`)로 한 사용자 세션에 하나만 실행한다.
이미 있으면(`ERROR_ALREADY_EXISTS`) 바로 종료한다. 프로세스가 죽으면 OS가 핸들을 닫아 풀린다.
테스트는 `InstanceLock` 프로토콜을 따르는 가짜를 넘긴다.
"""

import sys
from typing import Protocol

MUTEX_NAME = "Local\\chanjin-rag-agent"
_ERROR_ALREADY_EXISTS = 183


class InstanceLock(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class NamedMutexLock:
    """Windows 이름 있는 뮤텍스. Windows가 아니면 파일 잠금(fcntl)으로 대신한다."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None
        self._fd: int | None = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                return False
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = int(handle)
            return True
        import fcntl
        import os
        import tempfile

        path = os.path.join(tempfile.gettempdir(), self.name.replace("\\", "_") + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if sys.platform == "win32":
            if self._handle is not None:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
                self._handle = None
            return
        if self._fd is not None:
            import os

            os.close(self._fd)
            self._fd = None
