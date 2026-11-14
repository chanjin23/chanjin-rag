"""운영 배포 구성 검사 (TRD 3.1~3.3, 11장, 12.1, 12.3, 12.5).

Docker 없이 파일만 읽어 확인한다. 실제 해석·빌드는
`docker compose -f deploy/compose.yaml config -q`, `... build`로 따로 확인한다.
"""

import re
from pathlib import Path
from typing import Any

import yaml

from app.core.config import REPO_ROOT, Settings

DEPLOY = REPO_ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
NGINX = DEPLOY / "nginx.conf"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

CSP = "default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
BACKEND_SERVICES = ("migrate", "api", "worker")


def _compose() -> dict[str, Any]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))  # `<<` 병합 키도 풀린다
    assert isinstance(data, dict)
    return data


def _bytes(value: str | int) -> int:
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg]?)b?", value.strip().lower())
    assert m, value
    unit = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[m.group(2)]
    return int(float(m.group(1)) * unit)


def _dockerfile_bases(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.split()[1] for ln in lines if ln.upper().startswith("FROM ")]


def test_compose_services_limits_and_order() -> None:
    compose = _compose()
    services: dict[str, dict[str, Any]] = compose["services"]
    assert set(services) == {"db", "migrate", "api", "worker", "web"}

    # 메모리 상한(TRD 3.2)
    gib = 1024**3
    limits = {name: _bytes(services[name]["mem_limit"]) for name in ("db", "api", "worker", "web")}
    assert limits == {
        "db": 4 * gib,
        "api": int(1.5 * gib),
        "worker": 3 * gib,
        "web": 128 * 1024**2,
    }

    # db: ParadeDB(PG17), 건강 검사, 데이터 볼륨, 조정값(TRD 12.5)
    db = services["db"]
    assert db["image"].startswith("paradedb/paradedb:") and db["image"].endswith("-pg17")
    assert "healthcheck" in db
    assert "pgdata:/var/lib/postgresql/data" in db["volumes"]
    tuning_mounts = [v for v in db["volumes"] if "postgres/" in v]
    for mount in tuning_mounts:
        assert (DEPLOY / mount.split(":")[0]).is_file(), mount
    tuning = (DEPLOY / "postgres" / "tuning.conf").read_text(encoding="utf-8")
    for setting in ("shared_buffers = 1GB", "maintenance_work_mem = 1GB", "work_mem = 32MB"):
        assert re.search(rf"^{re.escape(setting)}\s*$", tuning, re.M), setting
    assert any("tuning.conf" in m for m in tuning_mounts)
    assert any("docker-entrypoint-initdb.d" in m for m in tuning_mounts)

    # 순서: db(healthy) → migrate(1회, 성공) → api·worker → web
    migrate = services["migrate"]
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    assert migrate.get("restart", "no") == "no"
    assert "migrate" in " ".join(migrate["command"])
    for name in ("api", "worker"):
        deps = services[name]["depends_on"]
        assert deps["migrate"]["condition"] == "service_completed_successfully", name
        assert deps["db"]["condition"] == "service_healthy", name
        assert services[name]["restart"] == "unless-stopped"
    assert "worker" in services["worker"]["command"]
    assert "healthcheck" in services["api"]
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"

    # api·worker·migrate는 같은 backend 이미지(빌드는 api 한 곳), .env는 없어도 됨(required: false)
    assert services["api"]["build"]["dockerfile"] == "backend/Dockerfile"
    for name in BACKEND_SERVICES:
        svc = services[name]
        assert svc["image"] == services["api"]["image"]
        if name != "api":
            assert "build" not in svc and svc["pull_policy"] == "never", name
        env_files = svc["env_file"]
        assert env_files == [{"path": "../.env", "required": False}], name
        env = svc["environment"]
        assert env["APP_ENV"] == "prod"
        assert "@db:5432/" in env["DATABASE_URL"]
        assert env["UPLOAD_TMP_DIR"] and env["BACKUP_DIR"]
    for name in ("api", "worker"):
        vols = services[name]["volumes"]
        env = services[name]["environment"]
        assert f"tmp:{env['UPLOAD_TMP_DIR']}" in vols
        assert f"backups:{env['BACKUP_DIR']}" in vols

    # 네트워크: web만 127.0.0.1:8080에 바인딩, 나머지는 포트를 열지 않음(TRD 3.3)
    assert services["web"]["ports"] == ["127.0.0.1:8080:8080"]
    for name, svc in services.items():
        if name != "web":
            assert "ports" not in svc, name

    # 볼륨, 시간대
    assert set(compose["volumes"]) == {"pgdata", "tmp", "backups"}
    for name, svc in services.items():
        assert svc["environment"]["TZ"] == "Asia/Seoul", name

    # 이미지: arm64도 있는 공식 멀티 아키텍처 베이스, PG17 클라이언트와 age(TRD 12.2)
    backend_df = REPO_ROOT / "backend" / "Dockerfile"
    web_df = REPO_ROOT / "web" / "Dockerfile"
    assert services["web"]["build"]["dockerfile"] == "web/Dockerfile"
    multi_arch = ("python:3.12-", "ghcr.io/astral-sh/uv:", "node:", "nginx:")
    for df in (backend_df, web_df):
        bases = _dockerfile_bases(df)
        assert bases, df
        for base in bases:
            assert base.startswith(multi_arch), (df, base)
            assert "--platform" not in base
    backend_text = backend_df.read_text(encoding="utf-8")
    assert "postgresql-client-17" in backend_text
    assert re.search(r"\bage\b", backend_text)
    assert "deploy/nginx.conf" in web_df.read_text(encoding="utf-8")

    # 배포 스크립트: git pull → build → up -d (TRD 12.3)
    deploy_sh = (DEPLOY / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    steps = [deploy_sh.index("git pull"), deploy_sh.index(" build"), deploy_sh.index("up -d")]
    assert steps == sorted(steps)


def _location_blocks(conf: str) -> dict[str, str]:
    """`location <패턴> { ... }` 블록(중첩 없음)을 패턴 → 본문으로."""
    return {
        m.group(1).strip(): m.group(2)
        for m in re.finditer(r"location\s+([^{]+?)\s*\{([^{}]*)\}", conf)
    }


def test_nginx_csp_sse_and_upload_limit() -> None:
    conf = NGINX.read_text(encoding="utf-8")
    assert re.search(r"^\s*listen\s+8080;", conf, re.M)

    # 업로드 상한(TRD 11장)
    assert re.search(r"^\s*client_max_body_size\s+12m;", conf, re.M)

    # CSP: 서버 수준에 always로 붙인다(location에 add_header가 있으면 상속되지 않으므로 금지)
    m = re.search(r'add_header\s+Content-Security-Policy\s+"([^"]+)"\s+always;', conf)
    assert m and m.group(1) == CSP
    blocks = _location_blocks(conf)
    for pattern, body in blocks.items():
        assert "add_header" not in body, pattern

    # /api 프록시
    api = blocks["/api/"]
    assert "proxy_pass http://api:8000;" in api
    assert "proxy_set_header Host $http_host;" in api

    # SSE 경로(TRD 7.3): 정규식 location이 채팅 메시지 경로를 잡고 버퍼링을 끈다
    chat_path = "/api/conversations/c1/messages"
    sse = [
        (p, body)
        for p, body in blocks.items()
        if p.startswith("~") and re.search(p.lstrip("~* ").strip(), chat_path)
    ]
    assert len(sse) == 1
    pattern, body = sse[0]
    assert not re.search(pattern.lstrip("~* ").strip(), "/api/conversations")
    assert "proxy_buffering off;" in body
    assert "proxy_pass http://api:8000;" in body
    assert re.search(r"proxy_read_timeout\s+\d+s;", body)

    # 정적 파일과 SPA 경로
    assert "/index.html" in blocks["/"]
    assert re.search(r"^\s*root\s+/usr/share/nginx/html;", conf, re.M)


def test_env_example_covers_settings() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # 주석 처리된 예시(`# NAME=값`)도 설명된 것으로 본다
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))
    missing = [name.upper() for name in Settings.model_fields if name.upper() not in documented]
    assert missing == []

    # 운영 Compose가 치환에 쓰는 DB 계정
    assert {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} <= documented

    # TRD 12.1 표의 변수가 모두 있다
    trd = (REPO_ROOT / "docs" / "TRD.md").read_text(encoding="utf-8")
    section = trd.split("### 12.1 설정값", 1)[1].split("### 12.2", 1)[0]
    first_cols = [row.split("|")[1] for row in section.splitlines() if row.startswith("| `")]
    trd_names = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", " ".join(first_cols)))
    assert trd_names, "TRD 12.1 표를 찾지 못했습니다"
    assert sorted(trd_names - documented) == []

    # 비밀값은 비워 둔다(TRD 11장)
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M))
    for key in (
        "NVIDIA_LLM_API_KEY",
        "NVIDIA_EMBED_API_KEY",
        "NVIDIA_RERANK_API_KEY",
        "NVIDIA_JUDGE_API_KEY",
        "BACKUP_AGE_RECIPIENT",
    ):
        assert values[key] == "", key
