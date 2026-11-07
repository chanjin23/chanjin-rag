"""구조 기반 청킹 (TRD 6.3). 합성 문서(`tmp_path`)와 합성 섹션만 쓴다."""

import hashlib
from pathlib import Path

import docx
import pptx

from app.ingest.chunking import (
    MAX_TOKENS,
    MIN_TOKENS,
    TABLE_MAX_TOKENS,
    Chunk,
    build_chunks,
    count_tokens,
    merge_locations,
)
from app.ingest.parsers import parse_document
from app.ingest.parsers.base import Section
from app.ingest.parsers.pptx_parser import NOTES_LABEL
from app.ingest.tokenize import bm25_text

SUBJECTS = ["계약", "납품", "검수", "정산", "보증", "유지보수", "교육", "보안", "일정", "인력"]


def paragraph(i: int, sentences: int = 4) -> str:
    """번호가 들어간 서로 다른 문단(문장 4개면 약 100토큰)."""
    subject = SUBJECTS[i % len(SUBJECTS)]
    return " ".join(
        f"{i}번 문단의 {j}번째 문장은 {subject} 조건과 담당자의 확인 절차를 설명하며, "
        f"기한 안에 처리되지 않으면 다음 단계로 넘기지 않는다."
        for j in range(1, sentences + 1)
    )


def common_overlap(prev: str, nxt: str) -> str:
    """`nxt` 앞부분 중 `prev` 끝과 같은 가장 긴 부분(겹친 부분)."""
    for k in range(min(len(prev), len(nxt)), 0, -1):
        if prev.endswith(nxt[:k]):
            return nxt[:k]
    return ""


def body_paragraphs(chunk: Chunk, overlap: str) -> list[str]:
    """겹친 부분을 뺀 본문을 문단 구분자로 나눈다."""
    body = chunk.content[len(overlap) :]
    return [p.strip() for p in body.split("\n\n") if p.strip()]


def test_chunk_token_range_and_overlap() -> None:
    paragraphs = [paragraph(i) for i in range(40)]
    sections = [
        Section({"paragraph": i + 1}, p, "text", ["계약서", "일반 조건"])
        for i, p in enumerate(paragraphs)
    ]
    chunks = build_chunks(sections, filename="계약서.docx")

    assert len(chunks) >= 4
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.token_count == count_tokens(c.content)
        assert c.token_count <= MAX_TOKENS
        assert c.heading_path == ["계약서", "일반 조건"]
    # 마지막 청크만 남은 분량이라 500토큰보다 작을 수 있다
    for c in chunks[:-1]:
        assert MIN_TOKENS <= c.token_count <= MAX_TOKENS, c.token_count

    covered: list[str] = []
    for i, c in enumerate(chunks):
        overlap = ""
        if i > 0:
            prev = chunks[i - 1]
            overlap = common_overlap(prev.content, c.content)
            assert overlap, "앞 청크 끝이 다음 청크 앞에 겹쳐 있어야 한다"
            ratio = count_tokens(overlap) / prev.token_count
            assert 0.10 <= ratio <= 0.15, ratio
        body = body_paragraphs(c, overlap)
        # 본문은 원래 문단 그대로(중간에서 자르지 않음)
        assert all(p in paragraphs for p in body)
        covered.extend(body)
        # 위치는 원래 섹션(문단 번호) 범위
        first, last = paragraphs.index(body[0]) + 1, paragraphs.index(body[-1]) + 1
        expected = {"paragraph": first}
        if first != last:
            expected["paragraph_end"] = last
        assert c.location == expected
    assert covered == paragraphs  # 빠지거나 겹쳐 중복된 문단 없음


def test_paragraph_not_split_unless_too_long() -> None:
    # 약 300토큰짜리 문단들: 800토큰에 딱 맞지 않아도 문단 중간에서 자르지 않는다
    medium = [paragraph(i, sentences=12) for i in range(6)]
    assert all(300 <= count_tokens(p) <= MAX_TOKENS for p in medium)
    # 800토큰을 넘는 문단 하나: 이것만 문장 단위로 자른다
    long_sentences = [
        f"긴 문단의 {j}번째 문장은 보안 점검 결과와 후속 조치 계획을 자세히 적은 것이다."
        for j in range(1, 121)
    ]
    long_para = " ".join(long_sentences)
    assert count_tokens(long_para) > MAX_TOKENS * 2

    sections = [Section({"lines": [i * 10 + 1, i * 10 + 5]}, p) for i, p in enumerate(medium)]
    sections.append(Section({"lines": [100, 160]}, long_para))
    chunks = build_chunks(sections, filename="메모.md")

    seen_medium: list[str] = []
    long_parts: list[str] = []
    for i, c in enumerate(chunks):
        assert c.token_count <= MAX_TOKENS
        overlap = common_overlap(chunks[i - 1].content, c.content) if i else ""
        for p in body_paragraphs(c, overlap):
            if p in medium:
                seen_medium.append(p)
            else:
                long_parts.append(p)
    # 보통 문단은 모두 통째로 한 청크 안에 있다
    assert seen_medium == medium
    # 긴 문단은 여러 청크로 나뉘고, 조각은 문장 경계에서 끝난다
    assert len(long_parts) >= 2
    for part in long_parts:
        assert part.endswith(".")
        assert part.split(" ")[0] == "긴"  # 문장 시작에서 시작
    assert " ".join(long_parts) == long_para
    # 긴 문단 조각의 위치는 원래 섹션 위치를 그대로 쓴다
    assert any(c.location == {"lines": [100, 160]} for c in chunks)


def make_table_docx(path: Path, rows: int) -> Path:
    d = docx.Document()
    d.add_heading("거래처 목록", level=1)
    table = d.add_table(rows=rows + 1, cols=4)
    header = ["회사명", "담당자", "연락처", "비고"]
    for c, value in enumerate(header):
        table.cell(0, c).text = value
    for r in range(1, rows + 1):
        values = [f"회사{r}", f"담당자{r}", f"010-1234-{r:04d}", f"{r}번째 거래처 메모 내용"]
        for c, value in enumerate(values):
            table.cell(r, c).text = value
    d.save(str(path))
    return path


def test_table_chunks_repeat_header(tmp_path: Path) -> None:
    result = parse_document(make_table_docx(tmp_path / "거래처.docx", rows=80))
    assert result.status == "ok"
    chunks = [c for c in build_chunks(result.sections, filename="거래처.docx") if c.kind == "table"]

    assert len(chunks) >= 2
    rows_seen: list[int] = []
    for c in chunks:
        assert c.token_count <= TABLE_MAX_TOKENS
        assert c.heading_path == ["거래처 목록"]
        assert c.embed_text.startswith("[거래처.docx > 거래처 목록]\n")
        lines = c.content.split("\n")
        # 청크의 모든 행에 헤더가 붙어 있다(청크마다 헤더 반복)
        for line in lines:
            for name in ("회사명: ", "담당자: ", "연락처: ", "비고: "):
                assert name in line
        # 위치: 같은 표, 원래 행 번호 범위
        assert c.location["table"] == 1
        start, end = c.location["row"], c.location.get("row_end", c.location["row"])
        assert end - start + 1 == len(lines)
        rows_seen.extend(range(start, end + 1))
    assert rows_seen == list(range(2, 82))  # 헤더(1행) 다음부터 빠짐없이, 겹침 없이

    # 형식에 기대지 않는다: XLSX 같은 시트 위치의 표 섹션도 같은 경로로 묶이고, 표가 바뀌면 나뉜다
    sheet_rows = [
        Section({"sheet": "매출", "row": r}, f"월: {r}월 | 금액: {r * 100}", "table", [])
        for r in range(2, 6)
    ] + [Section({"sheet": "비용", "row": 2}, "월: 1월 | 금액: 50", "table", [])]
    sheet_chunks = build_chunks(sheet_rows, filename="장부.xlsx")
    assert [c.location for c in sheet_chunks] == [
        {"sheet": "매출", "row": 2, "row_end": 5},
        {"sheet": "비용", "row": 2},
    ]


def make_slides(path: Path) -> Path:
    prs = pptx.Presentation()
    for n, (title, body, notes) in enumerate(
        [
            ("도입 배경", "현재 수작업으로 문서를 찾는다", "검색 시간이 길다는 점을 강조"),
            ("목표", "질문하면 근거와 함께 답한다", "인용 표시를 시연"),
        ],
        start=1,
    ):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text_frame.text = body
        s.notes_slide.notes_text_frame.text = f"{notes} {n}"
    # 3번 슬라이드: 본문이 아주 길어 800토큰을 넘는다
    s3 = prs.slides.add_slide(prs.slide_layouts[1])
    s3.shapes.title.text = "상세 일정"
    frame = s3.placeholders[1].text_frame
    frame.text = "1주차: 요구사항을 정리하고 담당자와 일정을 확정한다"
    for w in range(2, 120):
        frame.add_paragraph().text = f"{w}주차: 요구사항을 정리하고 담당자와 일정을 확정한다"
    s3.notes_slide.notes_text_frame.text = "일정은 바뀔 수 있음"
    prs.save(str(path))
    return path


def test_pptx_one_slide_per_chunk(tmp_path: Path) -> None:
    result = parse_document(make_slides(tmp_path / "발표.pptx"))
    assert result.status == "ok"
    chunks = build_chunks(result.sections, filename="발표.pptx")

    by_slide: dict[int, list[Chunk]] = {}
    for c in chunks:
        assert c.kind == "slide"
        by_slide.setdefault(c.location["slide"], []).append(c)
        assert c.location == {"slide": c.location["slide"]}

    # 짧은 슬라이드는 1장 = 청크 1개, 발표자 노트 포함
    assert len(by_slide[1]) == 1 and len(by_slide[2]) == 1
    assert by_slide[1][0].content == result.sections[0].text
    assert f"{NOTES_LABEL}\n검색 시간이 길다는 점을 강조 1" in by_slide[1][0].content
    assert by_slide[2][0].heading_path == ["목표"]
    assert by_slide[2][0].embed_text.startswith("[발표.pptx > 목표]\n")

    # 800토큰을 넘는 슬라이드는 나누되, 모두 같은 슬라이드 위치를 유지한다
    long_chunks = by_slide[3]
    assert len(long_chunks) >= 2
    assert all(c.token_count <= MAX_TOKENS for c in long_chunks)
    assert long_chunks[0].content.startswith("상세 일정")
    assert long_chunks[-1].content.endswith("일정은 바뀔 수 있음")
    assert all(c.heading_path == ["상세 일정"] for c in long_chunks)
    # 두 슬라이드가 한 청크에 섞이지 않는다
    assert "도입 배경" not in by_slide[2][0].content


def test_heading_prefix_only_in_embed_text(tmp_path: Path) -> None:
    md = tmp_path / "노트.md"
    md.write_text(
        "# 프로젝트\n\n소개 문단이다.\n\n## 배포\n\n배포는 매주 금요일에 한다.\n",
        encoding="utf-8",
    )
    result = parse_document(md)
    chunks = build_chunks(result.sections, filename="노트.md")
    assert [c.heading_path for c in chunks] == [["프로젝트"], ["프로젝트", "배포"]]

    deploy = chunks[1]
    assert deploy.content == "배포는 매주 금요일에 한다."
    assert deploy.embed_text == "[노트.md > 프로젝트 > 배포]\n배포는 매주 금요일에 한다."
    for c in chunks:
        assert "[노트.md" not in c.content
        assert c.embed_text.startswith("[노트.md > 프로젝트")
        # 해시는 임베딩 입력(머리말 포함) 기준
        assert c.content_sha256 == hashlib.sha256(c.embed_text.encode("utf-8")).hexdigest()
        assert c.content_sha256 != hashlib.sha256(c.content.encode("utf-8")).hexdigest()
        assert c.bm25_text == bm25_text(c.content)
        assert c.embeddable

    # 같은 본문이라도 제목이 다르면 임베딩 입력이 달라 해시가 다르다(임베딩 재사용 안 함)
    a = build_chunks([Section({"lines": [1, 1]}, "같은 문단", "text", ["가"])], filename="x.md")
    b = build_chunks([Section({"lines": [1, 1]}, "같은 문단", "text", ["나"])], filename="x.md")
    assert a[0].content == b[0].content
    assert a[0].content_sha256 != b[0].content_sha256
    # 제목이 없으면 파일명만
    c = build_chunks([Section({"page": 1}, "본문")], filename="보고서.pdf")
    assert c[0].embed_text == "[보고서.pdf]\n본문"


def test_meta_only_single_filename_chunk(tmp_path: Path) -> None:
    sections = [Section({"page": 1}, "본문이 있어도 meta_only면 쓰지 않는다")]
    path = "업무/계약/위약금조항_검토.pdf"
    chunks = build_chunks(sections, filename="위약금조항_검토.pdf", path=path, meta_only=True)

    assert len(chunks) == 1
    c = chunks[0]
    assert c.seq == 0 and c.kind == "meta"
    assert c.content == f"위약금조항_검토.pdf\n{path}"
    assert "본문" not in c.content
    assert c.location == {} and c.heading_path == []
    assert not c.embeddable  # BM25로만 검색
    tokens = c.bm25_text.split()
    assert {"위약금조항", "위약금", "조항", "검토", "계약", "pdf"} <= set(tokens)

    # no_text(빈 문서)도 같은 파일명 청크 1개
    empty = tmp_path / "빈문서.docx"
    docx.Document().save(str(empty))
    result = parse_document(empty)
    assert result.status == "no_text"
    no_text = build_chunks(result.sections, filename="빈문서.docx", path="폴더/빈문서.docx")
    assert len(no_text) == 1
    assert no_text[0].content == "빈문서.docx\n폴더/빈문서.docx"
    assert not no_text[0].embeddable


def test_merge_locations_rules() -> None:
    assert merge_locations([{"page": 3}]) == {"page": 3}
    assert merge_locations([{"page": 3}, {"page": 3}, {"page": 4}]) == {"page": 3, "page_end": 4}
    assert merge_locations([{"lines": [1, 4]}, {"lines": [6, 9]}]) == {"lines": [1, 9]}
    assert merge_locations([{"table": 2, "row": 2}, {"table": 2, "row": 7}]) == {
        "table": 2,
        "row": 2,
        "row_end": 7,
    }
    assert merge_locations([]) == {}


def test_very_long_word_split_by_tokens() -> None:
    # 공백 없는 아주 긴 문자열도 800토큰 이하로 나뉜다(토큰 경계)
    blob = "가나다라마바사아자차카타파하" * 400
    chunks = build_chunks([Section({"page": 1}, blob)], filename="blob.txt")
    assert len(chunks) >= 2
    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    assert all(c.location == {"page": 1} for c in chunks)
