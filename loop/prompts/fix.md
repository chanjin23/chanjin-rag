작업 **{{TASK_ID}} · {{TASK_TITLE}}** 의 이전 시도가 통과하지 못했습니다. (시도 {{ATTEMPT}}/{{MAX_ATTEMPTS}})

`{{MEMORY_PATH}}`를 다시 읽고, 아래 피드백을 모두 해결하세요. 처음 받은 규칙(CLAUDE.md, 보호 파일, skip 금지, 커밋 금지, 기억 파일 갱신)은 그대로 적용됩니다.

## 피드백

{{FEEDBACK}}

## 끝내는 방법

1. 피드백의 각 항목을 고친 뒤 `bash scripts/verify.sh`가 통과하는지 확인합니다.
2. 기억 파일의 "알려진 문제·막힌 점"과 "다음에 할 일"을 갱신합니다.
3. 마지막 응답 첫 줄에 `TASK_DONE` 또는 `TASK_BLOCKED: <이유>`를 적습니다.
