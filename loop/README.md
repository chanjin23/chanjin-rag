# 루프 엔지니어링

PRD·TRD의 MVP를 작업 18개(T00~T17)로 나눠(미뤄 둔 T18 별도), 사람 확인 없이 **구현 → 규칙 검사 → 기능 검증 → AI 채점 → 커밋**을 끝까지 반복합니다.

## 실행

```bash
bash scripts/loop.sh --dry-run          # 계획과 첫 프롬프트 확인(아무것도 바꾸지 않음)
bash scripts/loop.sh                    # 처음부터 끝까지 실행
bash scripts/loop.sh --status           # 진행 상태 보고서
bash scripts/loop.sh --only T05         # 한 작업만
bash scripts/loop.sh --from T05         # 이 작업부터
bash scripts/loop.sh --retry-blocked    # 막힌 작업 다시 시도(시도 횟수·시간 초기화)
bash scripts/loop.sh --only T18         # 미뤄 둔 작업 실행(HWP·HWPX·XLSX 파서)
```

**미뤄 둔 작업:** `tasks.json`에서 `"deferred": true`인 작업(현재 T18: HWP·HWPX·XLSX 파서)은 기본 실행에서 건너뜁니다. 그동안 이 형식의 파일은 `unsupported_format`으로 기록되고 파일명으로만 검색됩니다. 실제 샘플이 생기면 `config.json`의 `sample_docs_dirs`에 폴더를 추가하고 `--only T18`로 실행하세요.

- `scripts/loop.sh`는 `../chanjin-rag-loop` 폴더에 git worktree(`loop/mvp` 브랜치)를 만들고 그 안에서 실행합니다. 원래 저장소 폴더는 건드리지 않습니다.
- 루프 설정(`loop/`, `scripts/loop*`)은 worktree에서도 보여야 하므로 커밋된 상태여야 합니다. 설정을 바꿨으면 `loop/mvp` 브랜치에도 반영(merge 또는 cherry-pick)하세요.
- 구현 세션은 `--dangerously-skip-permissions`로 실행됩니다. 루프가 도는 동안 이 PC에서 명령이 확인 없이 실행됩니다.

## 한 작업의 흐름

```
① 구현     claude -p (새 세션 또는 이어가기) — 명세 + 기억 파일 + 이전 피드백
② 규칙 검사  보호 파일 수정, 테스트 삭제, skip/only/xfail 추가, 문서 바이너리·.env 추가,
            verify.sh 필수 단계 삭제, 구현 세션의 직접 커밋 → 하나라도 있으면 실패
③ 기능 검증  bash scripts/verify.sh 전체 + 필수 pytest·E2E가 "이름 그대로 존재하고 통과"
            + 필수 로그 이벤트가 E2E 백엔드 로그에 있음 + 추가 검사 명령
④ AI 채점   별도 claude -p 세션(읽기 전용 도구만), loop/rubric.md 기준 JSON 채점
⑤ 판정     ②③ 통과 + 85점 이상 + 모든 항목 배점의 50% 이상 + 치명 결함 0개
            → 기억 파일 갱신 후 `loop(Txx): 제목`으로 커밋하고 다음 작업
            → 아니면 피드백을 만들어 ①로(최대 5회, 작업당 실사용 90분)
```

- 2~3번째 시도는 같은 세션을 이어가며 고치고, 4번째부터는 새 세션으로 시작합니다(같은 실수를 반복하지 않게).
- 5번 실패하거나 90분을 넘기면 그 작업은 "막힘"이 되고 루프가 멈춥니다. `.loop/report.md`에 이유가 남습니다.

## 세션이 끊겼을 때 (사용량 한도 대비)

세 겹으로 대비합니다.

1. **기억 파일 `loop/memory/Txx.md`**: 구현 세션이 단계마다 진행 체크리스트, 결정, 바꾼 파일, 다음 할 일을 기록합니다. 루프는 매 시도의 결과를 "시도 이력"에 덧붙입니다. 통과하면 이 파일도 함께 커밋되므로, 다음 작업이 이전 작업의 결정을 읽을 수 있습니다.
2. **세션 이어가기**: 구현 세션은 루프가 정한 세션 ID(`--session-id`)로 시작합니다. 사용량 한도나 일시 오류로 끊기면 메시지에서 초기화 시각을 읽어(못 읽으면 30분) 기다린 뒤 같은 세션을 `--resume`으로 이어갑니다. 기다린 시간은 90분 예산에 넣지 않습니다. 세션 기록이 없으면 새 세션을 시작하고, 기억 파일로 이어서 합니다.
3. **루프 자체가 끊겼을 때**(창 닫힘, 재부팅): `.loop/state.json`에 단계가 저장돼 있어, `bash scripts/loop.sh`를 다시 실행하면 그 작업의 그 시도를 같은 세션으로 이어갑니다.

## 파일

| 경로 | 내용 | 구현 세션 수정 |
| --- | --- | --- |
| `loop/tasks.json` | 작업 목록, 의존 관계, 필수 테스트·E2E·로그 이벤트·추가 검사 | 불가 |
| `loop/tasks/Txx.md` | 작업 명세(목표, 근거, 구현 항목, 범위 밖) | 불가 |
| `loop/memory/Txx.md` | 작업별 기억 파일 | 자기 작업 것만 가능 |
| `loop/prompts/` | 구현(implement), 재시도(fix), 재개(resume), 채점(grade) 프롬프트 | 불가 |
| `loop/rubric.md`, `loop/grade.schema.json` | 채점표와 채점 결과 형식 | 불가 |
| `loop/config.json` | 모델, 통과 점수, 시도 횟수, 시간 예산, 샘플 폴더 | 불가 |
| `loop/MANUAL_CHECKS.md` | 루프로 검증할 수 없는 수동 확인 항목 | 불가 |
| `.loop/` (git 제외) | 상태, 시도별 로그·리포트·채점 결과·피드백, 보고서 | — |

## 샘플 문서

`config.json`의 `sample_docs_dirs`를 `SAMPLE_DOCS_DIRS`(`;`로 구분)로 넘깁니다.

| 폴더 | 들어 있는 형식 |
| --- | --- |
| `C:\Chanjin\dev\obsidian` | MD 191, PDF 7, TXT 2 (개인·업무 메모) |
| `C:\Chanjin\dev\디딤` | DOCX 1, PPTX 1 (고객사 문서) |

개인 메모와 고객사 문서이므로 **읽기만 하고 저장소에 복사하지 않습니다.** 내용과 파일명도 테스트·로그·기억 파일에 옮겨 적지 않습니다(개수와 비율만). 규칙 검사가 문서 바이너리 추가를 막고, 채점표가 샘플 내용 복사를 치명 결함으로 봅니다. 기대값이 필요한 테스트는 라이브러리로 만든 합성 문서를 씁니다. HWP·HWPX·XLSX는 샘플이 없어 T18로 미뤘습니다.
