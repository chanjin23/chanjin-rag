"""`rag-agent install`·`uninstall` (TRD 10.1 설치와 실행).

- 작업 스케줄러: 하루 4회(09:00, 13:00, 18:00, 22:00), 놓친 예약은 가능한 빨리 실행
  (`StartWhenAvailable`), 이미 실행 중이면 새 인스턴스 시작 안 함(`IgnoreNew`),
  사용자 로그온 상태에서만 실행(`InteractiveToken`).
  콘솔 창 없이 `pythonw.exe -m rag_agent sync --source schedule`로 실행한다.
- URL 프로토콜: `HKCU\\Software\\Classes\\chanjin-sync`에 `URL Protocol`을 등록하고 명령은
  `"pythonw.exe" -m rag_agent protocol "%1"`.
- `--dry-run`이면 등록할 작업 XML과 레지스트리 키·값을 출력만 하고 시스템을 바꾸지 않는다.
"""

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "chanjin-rag-sync"
SCHEDULE_TIMES: tuple[str, ...] = ("09:00", "13:00", "18:00", "22:00")
PROTOCOL_KEY = r"Software\Classes\chanjin-sync"
REGISTRY_ROOT = "HKCU"

Echo = Callable[[str], None]


@dataclass(frozen=True)
class RegistryValue:
    key: str  # HKCU 기준 하위 키
    name: str  # "" = 기본값
    value: str

    def display(self) -> str:
        name = self.name or "(기본값)"
        return f"{REGISTRY_ROOT}\\{self.key}  {name} = {self.value}"


@dataclass(frozen=True)
class InstallPlan:
    task_name: str
    task_xml: str
    registry: tuple[RegistryValue, ...]
    pythonw: str


def pythonw_path() -> str:
    """uv 가상환경의 `pythonw.exe`(콘솔 창 없음). 없으면 지금 쓰는 파이썬."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def task_xml(pythonw: str, times: tuple[str, ...] = SCHEDULE_TIMES) -> str:
    triggers = "\n".join(
        "    <CalendarTrigger>\n"
        f"      <StartBoundary>2026-01-01T{t}:00</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
        "    </CalendarTrigger>"
        for t in times
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>chanjin-rag Sync Agent: 하루 {len(times)}회 문서 동기화</Description>
  </RegistrationInfo>
  <Triggers>
{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(pythonw)}</Command>
      <Arguments>-m rag_agent sync --source schedule</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def registry_values(pythonw: str) -> tuple[RegistryValue, ...]:
    return (
        RegistryValue(PROTOCOL_KEY, "", "URL:chanjin-sync Protocol"),
        RegistryValue(PROTOCOL_KEY, "URL Protocol", ""),
        RegistryValue(
            PROTOCOL_KEY + r"\shell\open\command", "", f'"{pythonw}" -m rag_agent protocol "%1"'
        ),
    )


def build_plan(pythonw: str | None = None) -> InstallPlan:
    exe = pythonw or pythonw_path()
    return InstallPlan(
        task_name=TASK_NAME, task_xml=task_xml(exe), registry=registry_values(exe), pythonw=exe
    )


def print_plan(plan: InstallPlan, echo: Echo) -> None:
    echo(f"[작업 스케줄러] 작업 이름: {plan.task_name}")
    echo(f"  실행 시각: {', '.join(SCHEDULE_TIMES)} (매일)")
    echo("  명령: schtasks /Create /TN " + plan.task_name + " /XML <아래 XML> /F")
    echo(plan.task_xml)
    echo("[레지스트리] URL 프로토콜 chanjin-sync")
    for value in plan.registry:
        echo("  " + value.display())


# -- 실제 등록(Windows 전용). 테스트는 dry-run만 검증한다 -------------------------------


def apply_install(plan: InstallPlan) -> None:
    if sys.platform != "win32":
        raise RuntimeError("install은 Windows에서만 실행할 수 있습니다")
    import winreg

    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(plan.task_xml)
        subprocess.run(
            ["schtasks", "/Create", "/TN", plan.task_name, "/XML", xml_path, "/F"],
            check=True,
            capture_output=True,
        )
    finally:
        os.unlink(xml_path)
    for value in plan.registry:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, value.key) as key:
            winreg.SetValueEx(key, value.name, 0, winreg.REG_SZ, value.value)


def apply_uninstall(plan: InstallPlan) -> None:
    if sys.platform != "win32":
        raise RuntimeError("uninstall은 Windows에서만 실행할 수 있습니다")
    import winreg

    subprocess.run(["schtasks", "/Delete", "/TN", plan.task_name, "/F"], capture_output=True)
    # 하위 키부터 지운다(winreg.DeleteKey는 하위 키가 있으면 실패)
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY + sub)
        except FileNotFoundError:
            pass
