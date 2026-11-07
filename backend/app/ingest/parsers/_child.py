"""파서 자식 프로세스 진입점.

`python -m app.ingest.parsers._child [--memory-mb N] [--parser 모듈:이름] 경로`

`isolation.parse_in_subprocess`만 실행한다. 결과(`ParseResult`)는 JSON 한 줄로, 원래 stdout에
`RESULT_PREFIX`를 붙여 쓴다. 파서 라이브러리가 stdout에 무언가 출력해도 섞이지 않게, 시작하자마자
stdout(fd 1)을 stderr로 돌리고 결과 전용으로 복제한 fd에만 쓴다.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from app.ingest.parsers.base import RESULT_PREFIX


def main(argv: list[str] | None = None) -> int:
    result_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    ap = argparse.ArgumentParser()
    ap.add_argument("--memory-mb", type=int, default=0)
    ap.add_argument("--parser", default=None)
    ap.add_argument("path")
    args = ap.parse_args(argv)

    from app.ingest.parsers.memlimit import apply_memory_limit
    from app.ingest.parsers.registry import load_ref, parse_document, run_parser

    limit_error = apply_memory_limit(args.memory_mb)
    path = Path(args.path)
    if args.parser:
        result = run_parser(load_ref(args.parser), path)
    else:
        result = parse_document(path)
    if limit_error:
        result.meta["memory_limit_error"] = limit_error

    payload = RESULT_PREFIX + json.dumps(result.to_dict(), ensure_ascii=False).encode("utf-8")
    with os.fdopen(result_fd, "wb") as out:
        out.write(payload + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
