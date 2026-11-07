"""한국어 BM25 토큰화 (Kiwi, TRD 6.4)."""

from pathlib import Path

import pytest

from app.ingest import tokenize
from app.ingest.chunking import build_chunks, chunker
from app.ingest.parsers.base import Section
from app.ingest.tokenize import DEFAULT_USER_DICT, KiwiTokenizer, bm25_text, bm25_tokens


def test_keeps_content_pos_drops_particles() -> None:
    tokens = bm25_tokens("위약금을 확인했습니다. 서버가 3개 있었다. 아름다운 漢字를 봤다.")
    # 명사·수사·의존명사·동사·형용사 어간·한자는 남는다
    for kept in ("위약금", "확인", "서버", "3", "개", "있", "아름답", "漢字", "보"):
        assert kept in tokens, kept
    # 조사·어미·선어말어미·기호는 버린다
    for dropped in ("을", "가", "를", "었", "습니다", "다", "은", ".", "했습니다"):
        assert dropped not in tokens, dropped
    # 영문은 소문자, 날짜·버전은 숫자·영문 조각으로 남는다
    assert bm25_tokens("FastAPI와 PostgreSQL 2024-01-02 v1.2") == [
        "fastapi",
        "postgresql",
        "2024",
        "01",
        "02",
        "v",
        "1.2",
    ]
    assert bm25_tokens("") == [] and bm25_tokens("  \n ") == []


def test_compound_noun_expansion() -> None:
    # 붙어 있는 복합명사: 원형 + 분해형
    assert bm25_text("위약금조항") == "위약금조항 위약금 조항"
    assert bm25_text("위약금조항을 검토했다") == "위약금조항 위약금 조항 검토"
    # 띄어 쓴 명사는 합치지 않는다
    assert bm25_text("위약금 조항") == "위약금 조항"
    # 사이에 다른 품사가 끼면 따로 본다
    assert bm25_text("위약금의 조항") == "위약금 조항"


def test_same_function_for_query_and_document() -> None:
    # 청커는 토큰화 모듈의 함수 하나를 그대로 쓴다(별도 구현 없음)
    assert chunker.bm25_text is tokenize.bm25_text
    assert tokenize.bm25_text("위약금조항") == " ".join(tokenize.bm25_tokens("위약금조항"))

    doc = "제12조 위약금조항: 계약을 해지하면 위약금은 계약금의 10%로 한다."
    chunks = build_chunks([Section({"paragraph": 1}, doc)], filename="계약.docx")
    assert chunks[0].bm25_text == bm25_text(doc)
    doc_tokens = set(chunks[0].bm25_text.split())

    # 질문을 같은 함수로 토큰화하면 문서 토큰과 맞물린다(조사·어미 차이와 복합명사 띄어쓰기를 흡수)
    questions = ("위약금조항은 어디에 있나요?", "위약금 조항 알려줘", "계약 해지하면 위약금은?")
    for question in questions:
        q_tokens = [t for t in bm25_tokens(question) if t not in {"있", "알리"}]
        assert q_tokens and set(q_tokens) <= doc_tokens, (question, q_tokens)


def test_user_dictionary_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "사과나무숲에서 회의를 했다"
    plain = KiwiTokenizer(None).tokens(text)
    assert "숲" in plain  # 사전 없이는 나뉜다

    user_dict = tmp_path / "kiwi_user_dict.txt"
    user_dict.write_text("# 고유명사\n사과나무숲\tNNP\n", encoding="utf-8")
    with_dict = KiwiTokenizer(user_dict)
    assert with_dict.user_words == 1
    assert with_dict.tokens(text) == ["사과나무숲", "회의", "하"]

    # 문서·질문 공용 함수는 설정(KIWI_USER_DICT_PATH)의 사전을 읽는다
    monkeypatch.setenv("KIWI_USER_DICT_PATH", str(user_dict))
    assert tokenize.resolve_user_dict() == user_dict
    assert bm25_text(text) == "사과나무숲 회의 하"
    monkeypatch.delenv("KIWI_USER_DICT_PATH")
    tokenize.get_settings.cache_clear()

    # 설정이 없으면 저장소의 deploy/kiwi_user_dict.txt(주석만 있는 기본 파일)
    assert tokenize.resolve_user_dict() == DEFAULT_USER_DICT
    assert DEFAULT_USER_DICT.is_file()
    assert KiwiTokenizer(DEFAULT_USER_DICT).tokens("위약금조항") == ["위약금조항", "위약금", "조항"]
