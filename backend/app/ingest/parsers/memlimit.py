"""현재 프로세스에 메모리 상한을 건다 (TRD 11장 파서 격리). 파서 자식 프로세스가 시작할 때 부른다.

- Windows: Job Object의 `JOB_OBJECT_LIMIT_PROCESS_MEMORY`(커밋 메모리 상한)를 만들고 자기 자신을
  넣는다. 상한을 넘는 할당은 실패해 Python에서는 `MemoryError`가 된다. Windows 8부터 Job 중첩이
  되므로 이미 Job 안에서 실행 중이어도(터미널·IDE 등) 동작한다.
- Linux 등 POSIX: `RLIMIT_AS`(가상 메모리)로 제한한다. 가상 주소 공간 기준이라 실제 사용량보다
  빨리 닿을 수 있어 상한은 넉넉하게 둔다(기본 1024MB).
- 걸지 못하면 예외 대신 사유 문자열을 돌려준다. 이때도 타임아웃은 그대로 동작한다.
"""

import sys

MB = 1024 * 1024


def apply_memory_limit(limit_mb: int) -> str | None:
    """상한을 걸고 None을 돌려준다. 걸지 못하면 그 이유. `limit_mb <= 0`이면 하지 않는다."""
    if limit_mb <= 0:
        return None
    limit = limit_mb * MB
    if sys.platform == "win32":
        return _apply_windows(limit)
    return _apply_posix(limit)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _job_handle: int | None = None  # 프로세스가 끝날 때까지 닫지 않는다

    def _apply_windows(limit: int) -> str | None:
        global _job_handle
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return f"CreateJobObject 실패({ctypes.get_last_error()})"
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = limit
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            return f"SetInformationJobObject 실패({ctypes.get_last_error()})"
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            return f"AssignProcessToJobObject 실패({ctypes.get_last_error()})"
        _job_handle = int(job)
        return None

    def _apply_posix(limit: int) -> str | None:
        return "POSIX 전용 경로"

else:
    import resource

    def _apply_windows(limit: int) -> str | None:
        return "Windows 전용 경로"

    def _apply_posix(limit: int) -> str | None:
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            new = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(resource.RLIMIT_AS, (new, hard))
        except (ValueError, OSError) as e:
            return f"setrlimit 실패({e})"
        return None
