"""루프 실행기: loop/tasks.json의 작업을 순서대로 구현 → 규칙 검사 → 기능 검증 → AI 채점 → 커밋한다.

실행은 `bash scripts/loop.sh`(worktree 준비 후 이 파일을 실행)로 한다. 흐름과 옵션은 loop/README.md.
표준 라이브러리만 쓴다(Windows 기본 Python으로도 실행 가능).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOOP_DIR = ROOT / "loop"
STATE_DIR = ROOT / ".loop"
STATE_FILE = STATE_DIR / "state.json"
IS_WINDOWS = os.name == "nt"

# 구현 세션이 바꾸면 안 되는 경로. loop/memory/는 구현 세션이 갱신하는 기억 파일이라 예외다.
PROTECTED = (
    "loop/tasks.json",
    "loop/tasks/",
    "loop/prompts/",
    "loop/rubric.md",
    "loop/config.json",
    "loop/grade.schema.json",
    "loop/README.md",
    "loop/MANUAL_CHECKS.md",
    "loop/memory/_template.md",
    "scripts/loop.sh",
    "scripts/loop/",
    "docs/PRD.md",
    "CLAUDE.md",
)
# verify.sh에서 빠지면 안 되는 검사 명령
VERIFY_REQUIRED = (
    "uv run pytest",
    "ruff check",
    "ruff format --check",
    "mypy",
    "pnpm build",
    "pnpm e2e",
)
SKIP_RE = re.compile(
    r"\.(skip|only|fixme)\(|pytest\.mark\.(skip|skipif|xfail)|pytest\.skip\(|unittest\.skip"
)
TEST_PATH_RE = re.compile(r"(^|/)(tests|e2e)/|(^|/)test_[^/]*\.py$|\.(spec|test)\.tsx?$")
DOC_EXTS = {".pdf", ".hwp", ".hwpx", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}

LIMIT_RE = re.compile(
    r"usage limit|limit reached|hit your (usage )?limit|out of (extra )?usage|"
    r"(session|weekly) limit|rate[_ ]limit",
    re.IGNORECASE,
)
TRANSIENT_RE = re.compile(r"overloaded|\b529\b|api error: 5\d\d|ECONNRESET|ETIMEDOUT", re.I)
NO_SESSION_RE = re.compile(r"no conversation found|session .* not found", re.IGNORECASE)


# --------------------------------------------------------------------------- 공용


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def stamp() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def say(msg: str) -> None:
    print(f"[loop {now().strftime('%H:%M:%S')}] {msg}", flush=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def which(name: str) -> str:
    """PATH 순서대로 찾은 전체 경로. Windows의 CreateProcess는 System32를 PATH보다 먼저 찾아서
    `bash`가 WSL의 bash.exe로 잡힐 수 있으므로, 항상 전체 경로로 실행한다."""
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} 명령을 찾지 못했습니다. 설치하고 PATH에 넣으세요.")
    return found


def git(*args: str) -> str:
    res = subprocess.run(
        [which("git"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {res.stderr.strip()}")
    return res.stdout


def kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, 9)
        except ProcessLookupError:
            pass


def cleanup_ports(ports: list[int]) -> None:
    """E2E 포트에 남은 서버를 정리한다(구현 세션이 띄우고 안 끈 경우)."""
    if not IS_WINDOWS:
        for port in ports:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        return
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING":
            if any(parts[1].endswith(f":{p}") for p in ports):
                pids.add(int(parts[4]))
    for pid in pids:
        if pid > 0:
            kill_tree(pid)


def run_logged(
    cmd: list[str],
    *,
    log_path: Path,
    timeout_s: float,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, bool]:
    """명령을 실행해 출력을 로그 파일에 쓰고 (종료 코드, 출력, 타임아웃 여부)를 돌려준다."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=flags,
        start_new_session=not IS_WINDOWS,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(stdin, timeout=max(timeout_s, 1))
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        out, _ = proc.communicate()
    log_path.write_text(out or "", encoding="utf-8")
    return proc.returncode, out or "", timed_out


def tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])


# --------------------------------------------------------------------------- 설정과 상태


@dataclass
class Task:
    id: str
    title: str
    depends: list[str]
    pytest: list[str]
    e2e: list[str]
    log_events: list[str]
    extra_checks: list[str]

    @property
    def spec_path(self) -> Path:
        return LOOP_DIR / "tasks" / f"{self.id}.md"

    @property
    def memory_path(self) -> Path:
        return LOOP_DIR / "memory" / f"{self.id}.md"

    def spec_text(self) -> str:
        """사람이 쓴 명세 + tasks.json에서 만든 필수 검증 목록(한 곳에서만 관리)."""
        lines = [
            self.spec_path.read_text(encoding="utf-8").rstrip(),
            "",
            "## 필수 검증 (자동 생성)",
        ]
        lines.append("")
        lines.append("- `bash scripts/verify.sh` 전체 통과(이전 작업의 테스트 포함)")
        if self.pytest:
            lines.append("- pytest(파일명::함수명, 이름 그대로 만들 것):")
            lines += [f"  - `{t}`" for t in self.pytest]
        if self.e2e:
            lines.append("- E2E(파일 > 테스트 제목, 제목 그대로 만들 것):")
            lines += [f"  - `{t}`" for t in self.e2e]
        if self.log_events:
            lines.append("- E2E 백엔드 로그에 나타나야 할 이벤트:")
            lines += [f"  - `{e}`" for e in self.log_events]
        if self.extra_checks:
            lines.append("- 추가 검사 명령(0으로 끝나야 함):")
            lines += [f"  - `{c}`" for c in self.extra_checks]
        return "\n".join(lines)


def load_tasks() -> list[Task]:
    data = read_json(LOOP_DIR / "tasks.json")
    return [Task(**t) for t in data["tasks"]]


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        state: dict[str, Any] = read_json(STATE_FILE)
        return state
    return {"tasks": {}}


def task_state(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    default = {
        "status": "pending",
        "phase": "idle",
        "attempts": 0,
        "active_s": 0.0,
        "session_id": None,
        "base": None,
        "last_score": None,
        "history": [],
    }
    ts: dict[str, Any] = state["tasks"].setdefault(task_id, default)
    return ts


def ensure_memory(task: Task) -> None:
    if task.memory_path.exists():
        return
    template = (LOOP_DIR / "memory" / "_template.md").read_text(encoding="utf-8")
    task.memory_path.write_text(
        render(template, {"TASK_ID": task.id, "TASK_TITLE": task.title}), encoding="utf-8"
    )


def memory_set_status(task: Task, status: str) -> None:
    text = task.memory_path.read_text(encoding="utf-8")
    text = re.sub(r"^- 상태:.*$", f"- 상태: {status}", text, count=1, flags=re.M)
    text = re.sub(r"^- 마지막 갱신:.*$", f"- 마지막 갱신: {stamp()}", text, count=1, flags=re.M)
    task.memory_path.write_text(text, encoding="utf-8")


def memory_append(task: Task, entry: str) -> None:
    with task.memory_path.open("a", encoding="utf-8") as f:
        f.write("\n" + entry.rstrip() + "\n")


# --------------------------------------------------------------------------- Claude 호출


@dataclass
class ClaudeResult:
    exit_code: int | None
    output: str
    data: dict[str, Any] | None
    timed_out: bool
    usage_limited: bool
    transient: bool
    no_session: bool
    reset_at: dt.datetime | None
    duration_s: float

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and self.data is not None
            and not self.data.get("is_error", False)
        )

    @property
    def text(self) -> str:
        if self.data and isinstance(self.data.get("result"), str):
            return str(self.data["result"])
        return self.output


def parse_reset(text: str) -> dt.datetime | None:
    """한도 메시지에서 초기화 시각을 찾는다. 못 찾으면 None."""
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1))).astimezone()
    m = re.search(
        r"resets?\s+(?:at\s+)?(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    minute = int(m.group(2) or 0)
    target = now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now():
        target += dt.timedelta(days=1)
    return target


def parse_json_output(out: str) -> dict[str, Any] | None:
    for candidate in (out.strip(), *reversed(out.strip().splitlines())):
        if candidate.startswith("{"):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def run_claude(args: list[str], prompt: str, timeout_s: float, log_path: Path) -> ClaudeResult:
    start = time.monotonic()
    cmd = [which("claude"), "-p", "--output-format", "json", *args]
    code, out, timed_out = run_logged(cmd, log_path=log_path, timeout_s=timeout_s, stdin=prompt)
    data = parse_json_output(out)
    failed = code != 0 or (data is not None and bool(data.get("is_error")))
    haystack = out + ("\n" + str(data.get("result", "")) if data else "")
    return ClaudeResult(
        exit_code=code,
        output=out,
        data=data,
        timed_out=timed_out,
        usage_limited=failed and bool(LIMIT_RE.search(haystack)),
        transient=failed and bool(TRANSIENT_RE.search(haystack)),
        no_session=failed and bool(NO_SESSION_RE.search(haystack)),
        reset_at=parse_reset(haystack) if failed else None,
        duration_s=time.monotonic() - start,
    )


def wait_until(reset_at: dt.datetime | None, cfg: dict[str, Any], reason: str) -> None:
    fallback = dt.timedelta(minutes=cfg["usage_limit_fallback_wait_min"])
    cap = dt.timedelta(minutes=cfg["usage_limit_max_wait_min"])
    target = (reset_at + dt.timedelta(minutes=2)) if reset_at else now() + fallback
    target = min(target, now() + cap)
    say(f"{reason}: {target.strftime('%m-%d %H:%M')}까지 기다린 뒤 이어서 진행합니다.")
    while now() < target:
        time.sleep(min(60.0, max((target - now()).total_seconds(), 1.0)))


# --------------------------------------------------------------------------- 단계


@dataclass
class Gate:
    ok: bool
    summary: str
    details: str


def load_prompt(name: str, values: dict[str, str]) -> str:
    return render((LOOP_DIR / "prompts" / name).read_text(encoding="utf-8"), values)


def prompt_values(task: Task, cfg: dict[str, Any], attempt: int, feedback: str) -> dict[str, str]:
    return {
        "TASK_ID": task.id,
        "TASK_TITLE": task.title,
        "ATTEMPT": str(attempt),
        "MAX_ATTEMPTS": str(cfg["max_attempts"]),
        "MEMORY_PATH": task.memory_path.relative_to(ROOT).as_posix(),
        "SPEC_PATH": task.spec_path.relative_to(ROOT).as_posix(),
        "TASK_SPEC": task.spec_text(),
        "FEEDBACK": feedback or "(피드백 없음. 첫 시도이거나 이전 시도가 검증 전에 끊겼습니다)",
        "SAMPLE_DOCS_DIR": cfg["sample_docs_dir"],
    }


def step_implement(
    task: Task,
    ts: dict[str, Any],
    cfg: dict[str, Any],
    attempt: int,
    feedback: str,
    run_dir: Path,
    budget_s: float,
    resume: bool,
) -> tuple[str, float]:
    """구현 세션을 돌린다. (결과 요약, 실제 사용 시간)을 돌려준다. 한도 대기 시간은 제외.

    - 첫 시도와 `fresh_session_from_attempt` 이후: 새 세션 + implement.md
    - 그 사이 재시도: 같은 세션 이어가기 + fix.md(피드백)
    - 루프 자체가 중간에 끊겼다가 다시 시작(resume=True): 같은 세션 + resume.md
    """
    values = prompt_values(task, cfg, attempt, feedback)
    common = ["--dangerously-skip-permissions", "--model", cfg["implement_model"]]
    if resume and ts["session_id"]:
        args = [*common, "--resume", ts["session_id"]]
        prompt = load_prompt("resume.md", values)
    elif attempt == 1 or attempt >= cfg["fresh_session_from_attempt"] or not ts["session_id"]:
        ts["session_id"] = str(uuid.uuid4())
        args = [*common, "--session-id", ts["session_id"]]
        prompt = load_prompt("implement.md", values)
    else:
        args = [*common, "--resume", ts["session_id"]]
        prompt = load_prompt("fix.md", values)

    used = 0.0
    part = 0
    while True:
        part += 1
        remaining = budget_s - used
        if remaining <= 60:
            return "시간 예산 초과로 구현 세션을 멈췄습니다.", used
        res = run_claude(args, prompt, remaining, run_dir / f"implement-{part}.log")
        if res.usage_limited or res.transient:
            used += res.duration_s
            reason = "사용량 한도" if res.usage_limited else "일시 오류"
            reset = res.reset_at
            if res.transient and not res.usage_limited:
                reset = now() + dt.timedelta(minutes=cfg["transient_error_wait_min"])
            wait_until(reset, cfg, f"{task.id} 구현 세션 중단({reason})")
            args = [*common, "--resume", ts["session_id"]]
            prompt = load_prompt("resume.md", values)
            continue
        if res.no_session:
            # 세션 기록이 없으면 새 세션으로 시작한다. 진행 상황은 기억 파일에 남아 있다.
            ts["session_id"] = str(uuid.uuid4())
            args = [*common, "--session-id", ts["session_id"]]
            prompt = load_prompt("implement.md", values)
            continue
        used += res.duration_s
        if res.timed_out:
            return "구현 세션이 시간 예산 안에 끝나지 않았습니다.", used
        first_line = res.text.strip().splitlines()[0] if res.text.strip() else "(응답 없음)"
        return first_line, used


def step_guard(task: Task, base: str) -> Gate:
    problems: list[str] = []
    # 구현 세션이 커밋했으면 변경을 스테이징 상태로 되돌린다(커밋은 루프만 한다).
    if git("rev-parse", "HEAD").strip() != base:
        git("reset", "--soft", base)
        problems.append(
            "구현 세션이 직접 커밋했습니다. 루프가 되돌렸습니다. 다음부터 커밋하지 마세요."
        )
    git("add", "-A")
    changed = [
        line.split("\t") for line in git("diff", "--cached", "--name-status", base).splitlines()
    ]
    own_memory = task.memory_path.relative_to(ROOT).as_posix()
    for parts in changed:
        status, path = parts[0], parts[-1]
        if path != own_memory and any(path.startswith(p) for p in PROTECTED):
            problems.append(f"보호 파일을 바꿨습니다: {path}")
        if status.startswith("D") and TEST_PATH_RE.search(path):
            problems.append(f"테스트 파일을 지웠습니다: {path}")
        if status[0] in "AMR" and Path(path).suffix.lower() in DOC_EXTS:
            problems.append(
                f"문서 바이너리를 저장소에 추가했습니다(테스트 안에서 생성할 것): {path}"
            )
        if Path(path).name == ".env" or path.endswith("/.env"):
            problems.append(f".env를 커밋하려 했습니다: {path}")
    diff = git("diff", "--cached", "-U0", base)
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and TEST_PATH_RE.search(current):
            if SKIP_RE.search(line):
                problems.append(f"skip/only/xfail을 추가했습니다: {current}: {line[1:].strip()}")
    verify = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
    for needed in VERIFY_REQUIRED:
        if needed not in verify:
            problems.append(f"verify.sh에서 필수 검사가 빠졌습니다: {needed}")
    ok = not problems
    return Gate(ok, "규칙 검사 통과" if ok else f"규칙 위반 {len(problems)}건", "\n".join(problems))


def merge_status(prev: str | None, new: str) -> str:
    """같은 이름의 결과가 여러 개면(매개변수화) 하나라도 실패면 failed, 하나라도 통과면 passed."""
    if "failed" in (prev, new):
        return "failed"
    if "passed" in (prev, new):
        return "passed"
    return new


def junit_results(report_dir: Path) -> dict[str, str]:
    """'파일명::함수명' → passed|failed|skipped."""
    results: dict[str, str] = {}
    for xml_path in sorted(report_dir.glob("pytest*.xml")):
        for case in ET.parse(xml_path).getroot().iter("testcase"):
            # classname은 'backend.tests.test_auth' 또는 'backend.tests.test_auth.TestX' 형태
            module = case.get("classname", "").split(".")
            file_part = next((m for m in reversed(module) if m.startswith("test_")), module[-1])
            key = f"{file_part}.py::{case.get('name', '').split('[')[0]}"
            if case.find("failure") is not None or case.find("error") is not None:
                status = "failed"
            elif case.find("skipped") is not None:
                status = "skipped"
            else:
                status = "passed"
            results[key] = merge_status(results.get(key), status)
    return results


def e2e_results(report_path: Path) -> dict[str, str]:
    """'파일명 > 제목' → passed|failed|skipped (Playwright JSON 리포트)."""
    results: dict[str, str] = {}
    if not report_path.exists():
        return results

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            file_name = Path(spec.get("file", suite.get("file", ""))).name
            statuses = [t.get("status") for t in spec.get("tests", [])]
            if statuses and all(s == "skipped" for s in statuses):
                status = "skipped"
            elif spec.get("ok"):
                status = "passed"
            else:
                status = "failed"
            key = f"{file_name} > {spec.get('title', '')}"
            results[key] = merge_status(results.get(key), status)
        for child in suite.get("suites", []):
            walk(child)

    for suite in read_json(report_path).get("suites", []):
        walk(suite)
    return results


def backend_log_events() -> set[str]:
    log = ROOT / "web" / "e2e" / ".logs" / "backend.log"
    events: set[str] = set()
    if not log.exists():
        return events
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("{"):
            try:
                events.add(str(json.loads(line).get("event", "")))
            except json.JSONDecodeError:
                continue
    return events


def step_verify(task: Task, cfg: dict[str, Any], run_dir: Path) -> tuple[Gate, float]:
    start = time.monotonic()
    report_dir = run_dir / "reports"
    shutil.rmtree(report_dir, ignore_errors=True)
    report_dir.mkdir(parents=True)
    env = {
        **os.environ,
        "LOOP_REPORT_DIR": str(report_dir),
        "SAMPLE_DOCS_DIR": cfg["sample_docs_dir"],
    }
    cleanup_ports(cfg["e2e_ports"])
    code, out, timed_out = run_logged(
        [which("bash"), "scripts/verify.sh"],
        log_path=run_dir / "verify.log",
        timeout_s=cfg["verify_timeout_min"] * 60,
        env=env,
    )
    cleanup_ports(cfg["e2e_ports"])
    problems: list[str] = []
    if timed_out:
        problems.append(f"verify.sh가 {cfg['verify_timeout_min']}분 안에 끝나지 않았습니다.")
    elif code != 0:
        problems.append(
            f"verify.sh 실패(종료 코드 {code}). 마지막 출력:\n```\n{tail(out, 120)}\n```"
        )

    for i, check in enumerate(task.extra_checks, 1):
        c_code, c_out, c_timeout = run_logged(
            [which("bash"), "-c", check],
            log_path=run_dir / f"extra-{i}.log",
            timeout_s=1800,
            env=env,
        )
        if c_timeout or c_code != 0:
            problems.append(f"추가 검사 실패: `{check}`\n```\n{tail(c_out, 60)}\n```")

    py = junit_results(report_dir)
    for name in task.pytest:
        status = py.get(name)
        if status != "passed":
            problems.append(f"필수 pytest `{name}`: {status or '없음(이름이 정확한지 확인)'}")
    e2e = e2e_results(report_dir / "e2e.json")
    for name in task.e2e:
        status = e2e.get(name)
        if status != "passed":
            problems.append(f"필수 E2E `{name}`: {status or '없음(파일명과 제목이 정확한지 확인)'}")
    events = backend_log_events()
    for event in task.log_events:
        if event not in events:
            problems.append(f"E2E 백엔드 로그에 `{event}` 이벤트가 없습니다.")

    ok = not problems
    passed_py = sum(1 for s in py.values() if s == "passed")
    passed_e2e = sum(1 for s in e2e.values() if s == "passed")
    summary = (
        f"verify.sh {'통과' if code == 0 and not timed_out else '실패'}, "
        f"pytest {passed_py}/{len(py)} passed, E2E {passed_e2e}/{len(e2e)} passed, "
        f"필수 검증 {'모두 충족' if ok else f'{len(problems)}건 미충족'}"
    )
    return Gate(ok, summary, "\n".join(problems)), time.monotonic() - start


def step_grade(
    task: Task, cfg: dict[str, Any], base: str, verify_summary: str, run_dir: Path
) -> tuple[dict[str, Any] | None, str, float]:
    """채점 세션을 돌린다. (채점 결과, 오류 메시지, 사용 시간)."""
    values = {
        "TASK_ID": task.id,
        "TASK_TITLE": task.title,
        "BASE": base,
        "DIFF_STAT": git("diff", "--cached", "--stat", base).strip()[-6000:],
        "VERIFY_SUMMARY": verify_summary,
        "TASK_SPEC": task.spec_text(),
        "RUBRIC": (LOOP_DIR / "rubric.md").read_text(encoding="utf-8"),
    }
    prompt = render((LOOP_DIR / "prompts" / "grade.md").read_text(encoding="utf-8"), values)
    schema = json.dumps(read_json(LOOP_DIR / "grade.schema.json"), ensure_ascii=False)
    args = [
        "--model",
        cfg["grader_model"],
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)",
        "--json-schema",
        schema,
        "--no-session-persistence",
    ]
    used = 0.0
    for part in range(1, 4):
        res = run_claude(args, prompt, cfg["grade_timeout_min"] * 60, run_dir / f"grade-{part}.log")
        if res.usage_limited or res.transient:
            reason = "사용량 한도" if res.usage_limited else "일시 오류"
            reset = res.reset_at
            if res.transient and not res.usage_limited:
                reset = now() + dt.timedelta(minutes=cfg["transient_error_wait_min"])
            wait_until(reset, cfg, f"{task.id} 채점 중단({reason})")
            continue
        used += res.duration_s
        grade = extract_grade(res, cfg)
        if grade is not None:
            return grade, "", used
    return None, "채점 결과를 3번 연속 얻지 못했습니다(형식 오류 또는 실패).", used


def extract_grade(res: ClaudeResult, cfg: dict[str, Any]) -> dict[str, Any] | None:
    if not res.ok or res.data is None:
        return None
    raw = res.data.get("structured_output")
    if raw is None:
        raw = parse_json_output(res.text)
    if not isinstance(raw, dict):
        return None
    items: dict[str, int] = cfg["rubric_items"]
    breakdown = raw.get("breakdown", {})
    total = 0
    weak: list[str] = []
    for key, max_score in items.items():
        item = breakdown.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("score"), int):
            return None
        score = max(0, min(int(item["score"]), max_score))
        item["score"] = score
        total += score
        if score < max_score * cfg["min_item_ratio"]:
            weak.append(f"{key} {score}/{max_score}")
    raw["score"] = total
    raw["weak_items"] = weak
    raw["passed"] = total >= cfg["pass_score"] and not weak and not raw.get("blockers")
    return raw


def build_feedback(guard: Gate, verify: Gate | None, grade: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if not guard.ok:
        parts.append("### 규칙 위반 (반드시 해결)\n" + guard.details)
    if verify and not verify.ok:
        parts.append("### 기능 검증 실패\n" + verify.details)
    if grade:
        lines = [
            f"### AI 채점: {grade['score']}점 (통과 기준 85점, 항목별 50% 이상, 치명 결함 0개)"
        ]
        for key, item in grade["breakdown"].items():
            lines.append(f"- {key}: {item['score']}점 — {item['reason']}")
        if grade.get("blockers"):
            lines.append("\n치명 결함:")
            lines += [f"- {b['title']} ({b['evidence']})" for b in grade["blockers"]]
        if grade.get("feedback"):
            lines.append("\n고칠 점:")
            lines += [f"- {f}" for f in grade["feedback"]]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def commit(task: Task, grade: dict[str, Any], verify_summary: str, attempt: int) -> str:
    git("add", "-A")
    body = (
        f"AI 채점 {grade['score']}/100 (시도 {attempt}회)\n"
        f"{verify_summary}\n\n"
        f"{grade.get('summary', '').strip()}\n\n"
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    )
    git("commit", "-q", "-m", f"loop({task.id}): {task.title}", "-m", body)
    return git("rev-parse", "--short", "HEAD").strip()


# --------------------------------------------------------------------------- 작업 실행


def run_task(task: Task, state: dict[str, Any], cfg: dict[str, Any]) -> bool:
    ts = task_state(state, task.id)
    ensure_memory(task)
    if ts["status"] != "in_progress":
        ts.update(status="in_progress", base=git("rev-parse", "HEAD").strip())
        memory_set_status(task, "진행 중")
        write_json(STATE_FILE, state)
    base: str = ts["base"]
    budget = cfg["task_timeout_min"] * 60
    reserve = cfg["reserve_min"] * 60
    feedback = ts.get("pending_feedback", "")
    # 루프 프로세스가 시도 도중에 끊겼으면(창 닫힘, 재부팅) 그 시도를 같은 세션으로 이어서 한다.
    resume = ts.get("phase", "idle") != "idle"
    if resume:
        ts["attempts"] = max(ts["attempts"] - 1, 0)
        say(f"{task.id} 이전 실행이 '{ts['phase']}' 단계에서 끊겼습니다. 같은 세션으로 이어갑니다.")

    while ts["attempts"] < cfg["max_attempts"]:
        remaining = budget - ts["active_s"]
        if remaining <= reserve:
            return block(task, ts, state, f"시간 예산 {cfg['task_timeout_min']}분을 다 썼습니다.")
        ts["attempts"] += 1
        attempt = ts["attempts"]
        run_dir = STATE_DIR / "runs" / task.id / f"attempt-{attempt}"
        say(
            f"{task.id} 시도 {attempt}/{cfg['max_attempts']} 구현 시작 "
            f"(남은 예산 {remaining / 60:.0f}분)"
        )
        ts["phase"] = "implement"
        write_json(STATE_FILE, state)

        result, used = step_implement(
            task, ts, cfg, attempt, feedback, run_dir, remaining - reserve, resume
        )
        resume = False
        ts["active_s"] += used
        ts["phase"] = "verify"
        write_json(STATE_FILE, state)
        cleanup_ports(cfg["e2e_ports"])
        say(f"{task.id} 구현 세션 종료: {result}")

        guard = step_guard(task, base)
        verify, v_used = step_verify(task, cfg, run_dir)
        ts["active_s"] += v_used
        say(f"{task.id} {guard.summary} / {verify.summary}")

        grade: dict[str, Any] | None = None
        grade_error = ""
        if guard.ok and verify.ok:
            ts["phase"] = "grade"
            write_json(STATE_FILE, state)
            git("add", "-A")
            grade, grade_error, g_used = step_grade(task, cfg, base, verify.summary, run_dir)
            ts["active_s"] += g_used
            if grade:
                write_json(run_dir / "grade.json", grade)
                weak = f", 약한 항목 {grade['weak_items']}" if grade["weak_items"] else ""
                blockers = f", 치명 결함 {len(grade['blockers'])}건" if grade["blockers"] else ""
                say(f"{task.id} AI 채점 {grade['score']}점{weak}{blockers}")
                ts["last_score"] = grade["score"]

        passed = bool(guard.ok and verify.ok and grade and grade["passed"])
        ts["phase"] = "idle"
        record = {
            "attempt": attempt,
            "at": stamp(),
            "session_id": ts["session_id"],
            "implement": result,
            "guard": guard.summary,
            "verify": verify.summary,
            "score": grade["score"] if grade else None,
            "passed": passed,
        }
        ts["history"].append(record)
        memory_append(
            task,
            f"- {record['at']} 시도 {attempt}: {result} | {guard.summary} | {verify.summary} | "
            f"AI {record['score'] if grade else (grade_error or '채점 안 함')} | "
            f"{'통과' if passed else '불통과'} | 세션 {ts['session_id']}",
        )

        if passed and grade:
            memory_set_status(task, "통과")
            sha = commit(task, grade, verify.summary, attempt)
            ts.update(status="passed", commit=sha, pending_feedback="")
            write_json(STATE_FILE, state)
            say(f"{task.id} 통과 → 커밋 {sha}")
            return True

        feedback = build_feedback(guard, verify, grade)
        if grade_error:
            feedback += f"\n\n### 채점 실패\n{grade_error}"
        (run_dir / "feedback.md").write_text(feedback, encoding="utf-8")
        ts["pending_feedback"] = feedback
        write_json(STATE_FILE, state)

    return block(task, ts, state, f"{cfg['max_attempts']}번 시도했지만 통과하지 못했습니다.")


def block(task: Task, ts: dict[str, Any], state: dict[str, Any], reason: str) -> bool:
    ts.update(status="blocked", blocked_reason=reason)
    memory_set_status(task, f"막힘 — {reason}")
    memory_append(task, f"- {stamp()} 루프 중단: {reason}")
    write_json(STATE_FILE, state)
    say(f"{task.id} 막힘: {reason}")
    return False


def write_report(tasks: list[Task], state: dict[str, Any]) -> Path:
    lines = [
        "# 루프 실행 보고서",
        "",
        f"생성: {stamp()}",
        "",
        "| 작업 | 제목 | 상태 | 시도 | 점수 | 사용 시간(분) | 커밋 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for t in tasks:
        ts = state["tasks"].get(t.id, {})
        lines.append(
            f"| {t.id} | {t.title} | {ts.get('status', 'pending')} | {ts.get('attempts', 0)} | "
            f"{ts.get('last_score') if ts.get('last_score') is not None else '-'} | "
            f"{ts.get('active_s', 0) / 60:.0f} | {ts.get('commit', '-')} |"
        )
    blocked = [t for t in tasks if state["tasks"].get(t.id, {}).get("status") == "blocked"]
    for t in blocked:
        ts = state["tasks"][t.id]
        lines += [
            "",
            f"## {t.id} 막힌 이유",
            "",
            ts.get("blocked_reason", ""),
            "",
            f"- 마지막 피드백: `.loop/runs/{t.id}/attempt-{ts.get('attempts')}/feedback.md`",
            f"- 기억 파일: `loop/memory/{t.id}.md`",
            f"- 다시 시도: `bash scripts/loop.sh --retry-blocked --only {t.id}`",
        ]
    lines += ["", "실제 인프라가 필요한 확인 항목은 `loop/MANUAL_CHECKS.md`를 보세요."]
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- 진입점


def preflight(cfg: dict[str, Any], tasks: list[Task], strict: bool) -> list[str]:
    issues: list[str] = []
    for name, why in (("claude", "구현·채점"), ("docker", "테스트 DB"), ("bash", "verify.sh")):
        if not shutil.which(name):
            issues.append(f"{name} 명령이 없습니다({why}에 필요).")
    if not Path(cfg["sample_docs_dir"]).is_dir():
        issues.append(f"샘플 폴더가 없습니다: {cfg['sample_docs_dir']}")
    ids = [t.id for t in tasks]
    for t in tasks:
        if not t.spec_path.exists():
            issues.append(f"명세 파일이 없습니다: {t.spec_path}")
        for dep in t.depends:
            if dep not in ids or ids.index(dep) >= ids.index(t.id):
                issues.append(f"{t.id}의 의존 작업 {dep}이 앞에 없습니다.")
    if strict:
        branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch != cfg["branch"]:
            issues.append(
                f"현재 브랜치가 {branch}입니다. {cfg['branch']}에서 실행하세요(loop.sh 사용)."
            )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="chanjin-rag 루프 실행기")
    parser.add_argument("--only", help="이 작업만 실행(예: T05)")
    parser.add_argument("--from", dest="start", help="이 작업부터 실행")
    parser.add_argument(
        "--retry-blocked", action="store_true", help="막힌 작업의 시도 횟수를 초기화"
    )
    parser.add_argument("--dry-run", action="store_true", help="실행 계획과 첫 프롬프트만 출력")
    parser.add_argument("--status", action="store_true", help="진행 상태만 출력")
    parser.add_argument("--max-attempts", type=int, help="작업당 최대 시도 횟수 덮어쓰기")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = read_json(LOOP_DIR / "config.json")
    if args.max_attempts:
        cfg["max_attempts"] = args.max_attempts
    tasks = load_tasks()
    state = load_state()

    if args.status:
        print(write_report(tasks, state).read_text(encoding="utf-8"))
        return 0

    issues = preflight(cfg, tasks, strict=not args.dry_run)
    if issues:
        for issue in issues:
            say(f"사전 점검 실패: {issue}")
        if not args.dry_run:
            return 2

    selected = tasks
    if args.start:
        selected = tasks[[t.id for t in tasks].index(args.start) :]
    if args.only:
        selected = [t for t in tasks if t.id == args.only]

    if args.dry_run:
        for t in selected:
            ts = state["tasks"].get(t.id, {})
            print(
                f"{t.id} [{ts.get('status', 'pending')}] {t.title} "
                f"(의존: {', '.join(t.depends) or '없음'}, pytest {len(t.pytest)}, "
                f"E2E {len(t.e2e)}, 로그 {len(t.log_events)})"
            )
        first = next(
            (t for t in selected if state["tasks"].get(t.id, {}).get("status") != "passed"), None
        )
        if first:
            print("\n----- 첫 구현 프롬프트 미리보기 -----\n")
            print(load_prompt("implement.md", prompt_values(first, cfg, 1, "")))
        return 0

    in_progress = [tid for tid, ts in state["tasks"].items() if ts.get("status") == "in_progress"]
    dirty = bool(git("status", "--porcelain").strip())
    if dirty and not in_progress:
        say("커밋되지 않은 변경이 있는데 진행 중인 작업이 없습니다. 정리한 뒤 실행하세요.")
        return 2

    STATE_DIR.mkdir(exist_ok=True)
    for t in selected:
        ts = task_state(state, t.id)
        if ts["status"] == "passed":
            continue
        if ts["status"] == "blocked":
            if not args.retry_blocked:
                say(f"{t.id}가 막혀 있습니다. --retry-blocked로 다시 시도할 수 있습니다.")
                write_report(tasks, state)
                return 1
            ts.update(status="in_progress", attempts=0, active_s=0.0, phase="idle")
        missing = [d for d in t.depends if state["tasks"].get(d, {}).get("status") != "passed"]
        if missing:
            say(f"{t.id}의 의존 작업이 끝나지 않았습니다: {', '.join(missing)}")
            write_report(tasks, state)
            return 1
        if not run_task(t, state, cfg):
            path = write_report(tasks, state)
            say(f"루프를 멈췄습니다. 보고서: {path}")
            return 1
        write_report(tasks, state)

    path = write_report(tasks, state)
    say(f"선택한 작업을 모두 통과했습니다. 보고서: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
