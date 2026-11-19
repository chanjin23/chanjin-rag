"""URL 프로토콜 `chanjin-sync://` (TRD 10.2). 실행기와 동기화는 가짜로 바꿔 기록만 한다."""

import os
from pathlib import Path

import pytest
from fakes import write

from rag_agent.keys import reveal_key
from rag_agent.protocol import ProtocolError, ProtocolHandler, explorer_path, parse_url
from rag_agent.rules import SyncRules
from rag_agent.scan import Scanner
from rag_agent.state import StateDB


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    def __init__(self) -> None:
        self.runs: list[str] = []
        self.launches: list[list[str]] = []

    def run_sync(self, source: str) -> None:
        self.runs.append(source)

    def launch(self, args: list[str]) -> None:
        self.launches.append(args)


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def handler(state: StateDB, rec: Recorder, clock: Clock) -> ProtocolHandler:
    return ProtocolHandler(state, run_sync=rec.run_sync, launcher=rec.launch, clock=clock)


def _index(rules: SyncRules, state: StateDB) -> None:
    """동기화가 하는 것처럼 스캔 결과와 설정을 state.db에 남긴다."""
    state.set("config", rules.to_config())
    result = Scanner(rules, state).scan()
    state.put_files([f.cached() for f in result.files], seen_at=1.0)


def test_run_accepts_only_source(handler: ProtocolHandler, rec: Recorder, clock: Clock) -> None:
    # 정해진 매개변수(source) 외에는 무시한다. 서버 주소·경로·명령을 URL로 받지 않는다.
    req = parse_url(
        "chanjin-sync://run?source=web&server=https://evil.example&path=C:%5CWindows&cmd=calc"
    )
    assert (req.action, req.source, req.key) == ("run", "web", None)
    assert parse_url("chanjin-sync://run/").source == "web"  # 없으면 web
    assert parse_url("CHANJIN-SYNC://RUN?source=cli").source == "cli"

    for bad, reason in [
        ("chanjin-sync://run?source=evil", "invalid_source"),
        ("chanjin-sync://run?source=web&source=cli", "duplicate_source"),
        ("chanjin-sync://run/C:/Windows/calc.exe", "unexpected_path"),
        ("chanjin-sync://user@run?source=web", "malformed"),
        ("chanjin-sync://open?file=C:%5Ca.exe", "unknown_action"),
        ("https://run?source=web", "scheme"),
    ]:
        with pytest.raises(ProtocolError) as exc:
            parse_url(bad)
        assert exc.value.reason == reason, bad

    result = handler.handle("chanjin-sync://run?source=web&server=https://evil.example")
    assert result.status == "ran"
    assert rec.runs == ["web"]
    assert rec.launches == []

    clock.t += 120
    assert handler.handle("chanjin-sync://run?source=evil").status == "rejected"
    assert rec.runs == ["web"]


def test_reveal_requires_64_hex(
    handler: ProtocolHandler,
    rec: Recorder,
    clock: Clock,
    root: Path,
    rules: SyncRules,
    state: StateDB,
) -> None:
    f = write(root / "a.md")
    _index(rules, state)
    key = reveal_key(str(f))

    for bad in [
        "chanjin-sync://reveal",
        "chanjin-sync://reveal?k=",
        f"chanjin-sync://reveal?k={key[:63]}",
        f"chanjin-sync://reveal?k={key}0",
        "chanjin-sync://reveal?k=" + "g" * 64,
        f"chanjin-sync://reveal?path={f}",
        f"chanjin-sync://reveal?k={key}&k={key}",
        "chanjin-sync://reveal?k=..%2F..%2F" + "a" * 58,
    ]:
        with pytest.raises(ProtocolError):
            parse_url(bad)
        clock.t += 11
        assert handler.handle(bad).status == "rejected", bad
    assert rec.launches == []

    # 대문자 16진수도 같은 값으로 받고, 다른 매개변수는 무시한다
    assert parse_url(f"chanjin-sync://reveal?k={key.upper()}&path=C:%5Cx").key == key
    assert handler.handle(f"chanjin-sync://reveal?k={key}").status == "revealed"
    assert len(rec.launches) == 1


def test_reveal_rejects_outside_root_or_missing(
    tmp_path: Path,
    handler: ProtocolHandler,
    rec: Recorder,
    clock: Clock,
    root: Path,
    state: StateDB,
) -> None:
    inside = write(root / "notes" / "in.md")
    gone = write(root / "notes" / "gone.md")
    excluded_later = write(root / "drafts" / "d.md")
    other_root = write(root / "old" / "o.md")
    rules = SyncRules(allowed_root=str(root), roots=[str(root)])
    _index(rules, state)

    # 이후 설정이 바뀜: 루트가 notes·drafts로 좁아지고 drafts/는 제외 패턴에 걸림
    narrowed = SyncRules(
        allowed_root=str(root),
        roots=[str(root / "notes"), str(root / "drafts")],
        excludes=["drafts/"],
    )
    state.set("config", narrowed.to_config())
    gone.unlink()

    def reveal(path: Path) -> str:
        clock.t += 11
        return handler.handle(f"chanjin-sync://reveal?k={reveal_key(str(path))}").status

    assert reveal(other_root) == "rejected"  # 수집 루트 밖
    assert reveal(excluded_later) == "rejected"  # 제외 패턴
    assert reveal(gone) == "not_found"  # 파일이 없음
    assert reveal(tmp_path / "never-indexed.md") == "not_found"  # state.db에 없는 해시
    assert rec.launches == []
    assert reveal(inside) == "revealed"
    assert len(rec.launches) == 1

    # 로컬 설정이 없으면 아무것도 하지 않는다
    state.set("config", None)
    assert reveal(inside) == "rejected"
    assert len(rec.launches) == 1


def test_reveal_invokes_explorer_with_arg_list(
    handler: ProtocolHandler,
    rec: Recorder,
    root: Path,
    rules: SyncRules,
    state: StateDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = write(root / "공백 있는 폴더" / "a & b; calc.md")
    _index(rules, state)

    result = handler.handle(f"chanjin-sync://reveal?k={reveal_key(str(f))}")

    assert result.status == "revealed"
    assert rec.launches == [[explorer_path(), f"/select,{os.path.normpath(str(f))}"]]
    assert rec.launches[0][0].lower().endswith("explorer.exe")
    assert rec.runs == []

    # 기본 실행기는 셸 없이 인자 배열로 Popen을 부른다
    import rag_agent.protocol as protocol

    calls: list[tuple[object, dict[str, object]]] = []

    def fake_popen(args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(protocol.subprocess, "Popen", fake_popen)
    protocol.launch_explorer(["explorer.exe", "/select,C:\\x.md"])
    assert calls == [(["explorer.exe", "/select,C:\\x.md"], {"shell": False, "close_fds": True})]


def test_protocol_rate_limit(
    handler: ProtocolHandler,
    rec: Recorder,
    clock: Clock,
    root: Path,
    rules: SyncRules,
    state: StateDB,
) -> None:
    # run: 1분 안의 두 번째 호출은 무시
    assert handler.handle("chanjin-sync://run?source=web").status == "ran"
    clock.t += 30
    assert handler.handle("chanjin-sync://run?source=web").status == "rate_limited"
    clock.t += 31
    assert handler.handle("chanjin-sync://run?source=web").status == "ran"
    assert rec.runs == ["web", "web"]

    # reveal: 10초 안의 세 번째 호출은 무시(동작별로 따로 센다)
    f = write(root / "a.md")
    _index(rules, state)
    url = f"chanjin-sync://reveal?k={reveal_key(str(f))}"
    assert handler.handle(url).status == "revealed"
    clock.t += 1
    assert handler.handle(url).status == "revealed"
    clock.t += 1
    assert handler.handle(url).status == "rate_limited"
    assert len(rec.launches) == 2
    clock.t += 9
    assert handler.handle(url).status == "revealed"

    # 기록은 state.db에 남아 새 프로세스(새 handler)에서도 적용된다
    other = ProtocolHandler(state, run_sync=rec.run_sync, launcher=rec.launch, clock=clock)
    assert other.handle("chanjin-sync://run?source=web").status == "rate_limited"
