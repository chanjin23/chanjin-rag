"""평가셋 형식 `eval/datasets/*.yaml` (PRD 8장, TRD 5.3, 14장).

```yaml
version: 1                 # 평가셋 버전(문항을 바꾸면 올린다)
items:
  - id: single-001         # 문항 ID(평가셋 안에서 유일)
    type: single           # single | multi | table | not_found | hwp
    question: 위약금은 계약 금액의 몇 %인가?
    answer: 계약 금액의 10%   # 정답(not_found는 비움)
    evidence:              # 정답 근거 (경로, 위치) 목록(not_found는 비움)
      - path: C:\\docs\\계약서.md
        location: {lines: [1, 3]}   # 비우면({}) 파일 어디든 인정
```

- 유형: 단일 문서 사실(`single`), 여러 문서 종합(`multi`), 표·슬라이드(`table`),
  답 없음(`not_found`, "찾지 못함" 기대), HWP(`hwp`).
- 정답 근거는 청크 ID가 아니라 `(경로, 위치)`로 적는다(청킹이 바뀌어도 라벨 유지).
  위치 키는 청크 위치와 같다: `page`, `slide`, `paragraph`, `table`, `row`, `sheet`,
  `lines`([처음, 끝]).
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, get_args

import yaml

ItemType = Literal["single", "multi", "table", "not_found", "hwp"]
ITEM_TYPES: tuple[str, ...] = get_args(ItemType)


class DatasetError(ValueError):
    """평가셋 YAML 형식 오류."""


@dataclass(frozen=True)
class Evidence:
    path: str
    location: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalItem:
    id: str
    question: str
    type: ItemType
    answer: str | None = None
    evidence: tuple[Evidence, ...] = ()

    @property
    def expects_not_found(self) -> bool:
        return self.type == "not_found"


@dataclass(frozen=True)
class Dataset:
    name: str
    version: str
    items: tuple[EvalItem, ...]
    digest: str = ""

    @property
    def label(self) -> str:
        """`eval_runs.dataset_version`: 이름, 버전, 내용 해시 앞 8자리."""
        return f"{self.name}@{self.version}" + (f"+{self.digest[:8]}" if self.digest else "")


def _item(raw: Any, index: int) -> EvalItem:
    if not isinstance(raw, dict):
        raise DatasetError(f"{index}번째 문항이 객체가 아닙니다")
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        raise DatasetError(f"{index}번째 문항에 id가 없습니다")
    question = str(raw.get("question") or "").strip()
    if not question:
        raise DatasetError(f"문항 {item_id}: question이 없습니다")
    kind = raw.get("type")
    if kind not in ITEM_TYPES:
        raise DatasetError(f"문항 {item_id}: type은 {', '.join(ITEM_TYPES)} 중 하나여야 합니다")
    answer = raw.get("answer")
    answer = str(answer).strip() if answer is not None and str(answer).strip() else None
    evidence: list[Evidence] = []
    for ev in raw.get("evidence") or []:
        if not isinstance(ev, dict) or not str(ev.get("path") or "").strip():
            raise DatasetError(f"문항 {item_id}: evidence 항목에 path가 없습니다")
        location = ev.get("location") or {}
        if not isinstance(location, dict):
            raise DatasetError(f"문항 {item_id}: evidence location은 객체여야 합니다")
        evidence.append(Evidence(path=str(ev["path"]).strip(), location=dict(location)))
    if kind == "not_found":
        if evidence:
            raise DatasetError(f"문항 {item_id}: not_found 문항에는 evidence를 두지 않습니다")
    else:
        if answer is None:
            raise DatasetError(f"문항 {item_id}: answer가 없습니다")
        if not evidence:
            raise DatasetError(f"문항 {item_id}: evidence가 없습니다")
    return EvalItem(
        id=item_id,
        question=question,
        type=cast(ItemType, kind),
        answer=answer,
        evidence=tuple(evidence),
    )


def parse_dataset(text: str, name: str = "dataset") -> Dataset:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise DatasetError("평가셋 최상위는 객체(version, items)여야 합니다")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise DatasetError("items가 비어 있습니다")
    items = [_item(raw, i) for i, raw in enumerate(raw_items, start=1)]
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise DatasetError(f"문항 ID가 겹칩니다: {item.id}")
        seen.add(item.id)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Dataset(
        name=name, version=str(data.get("version", "1")), items=tuple(items), digest=digest
    )


def load_dataset(path: Path) -> Dataset:
    return parse_dataset(path.read_text(encoding="utf-8"), name=path.stem)
