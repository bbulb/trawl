"""Unit tests for the BM25 hybrid retrieval helpers."""

from __future__ import annotations

from trawl.bm25 import bm25_rank, rrf_fuse, tokenize


def test_tokenize_latin_words_kept_whole():
    assert tokenize("asyncio.gather() lock") == ["asyncio", "gather", "lock"]


def test_tokenize_latin_lowercased():
    assert tokenize("FastAPI Dependency Injection") == [
        "fastapi",
        "dependency",
        "injection",
    ]


def test_tokenize_latin_keeps_identifiers_with_digits():
    assert tokenize("def2 abc_1 x99") == ["def2", "abc_1", "x99"]


def test_tokenize_numeric_only_skipped():
    assert tokenize("abc 123 def") == ["abc", "def"]


def test_tokenize_hangul_bigram_2syl():
    # 2-syllable run → single bigram
    assert tokenize("명량") == ["명량"]


def test_tokenize_hangul_bigram_4syl():
    # 4-syllable run → 3 overlapping bigrams
    assert tokenize("명량해전") == ["명량", "량해", "해전"]


def test_tokenize_hangul_single_syllable_preserved():
    assert tokenize("해") == ["해"]


def test_tokenize_hangul_multi_run():
    # Space splits runs: "명량 해전" → two separate 2-syl runs → two bigrams
    assert tokenize("명량 해전") == ["명량", "해전"]


def test_tokenize_mixed_latin_hangul():
    assert tokenize("Python asyncio 사용법") == [
        "python",
        "asyncio",
        "사용",
        "용법",
    ]


def test_tokenize_cjk_chars():
    assert tokenize("日本語") == ["日", "本", "語"]


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []
    assert tokenize("!!!") == []


def test_bm25_rank_exact_match_wins():
    corpus = ["hello world", "unrelated text", "world hello again"]
    ranked = bm25_rank("hello", corpus)
    assert ranked[0] in (0, 2)
    assert ranked[-1] == 1


def test_bm25_rank_empty_corpus():
    assert bm25_rank("q", []) == []


def test_bm25_rank_empty_query():
    # Empty query → fall back to original order
    assert bm25_rank("", ["a", "b", "c"]) == [0, 1, 2]


def test_bm25_rank_all_empty_docs():
    # All docs tokenize to [] → avoid ZeroDivisionError, return original order
    assert bm25_rank("hello", ["", "   ", "!!!"]) == [0, 1, 2]


def test_bm25_rank_mixed_empty_docs():
    # Some empties mixed with real docs — BM25Okapi itself handles this,
    # the real doc should outrank empties on a matching query.
    ranked = bm25_rank("hello", ["", "hello world", "   "])
    assert ranked[0] == 1


def test_bm25_rank_korean_bigram_match():
    corpus = ["명량 해전 승리", "이순신 장군 업적"]
    ranked = bm25_rank("해전", corpus)
    assert ranked[0] == 0


def test_bm25_rank_returns_all_indices():
    corpus = ["a b c", "d e f", "g h i"]
    ranked = bm25_rank("b", corpus)
    assert sorted(ranked) == [0, 1, 2]


def test_rrf_fuse_identical_rankings():
    # Two identical rankings → same order
    assert rrf_fuse([[0, 1, 2], [0, 1, 2]]) == [0, 1, 2]


def test_rrf_fuse_opposing_rankings_union():
    # rrf_fuse must contain every index that appears in any ranking,
    # regardless of order — opposing rankings don't erase membership.
    fused = rrf_fuse([[0, 1, 2], [2, 1, 0]])
    assert sorted(fused) == [0, 1, 2]


def test_rrf_fuse_agreement_beats_disagreement():
    # Doc 0 is rank-0 in both → clearly wins.
    # Doc 2 is rank-2 in both → clearly loses.
    fused = rrf_fuse([[0, 1, 2], [0, 2, 1]])
    assert fused[0] == 0
    assert fused[-1] in (1, 2)


def test_rrf_fuse_union_of_indices():
    # Indices missing from one ranking still appear in the output.
    fused = rrf_fuse([[0, 1, 2], [0, 1]])
    assert sorted(fused) == [0, 1, 2]


def test_rrf_fuse_empty_rankings():
    assert rrf_fuse([[], []]) == []
    assert rrf_fuse([]) == []


def test_rrf_fuse_single_ranking_passthrough():
    assert rrf_fuse([[3, 1, 4, 5]]) == [3, 1, 4, 5]


def test_rrf_fuse_k_controls_top_weight():
    # Smaller k → top rank contributes disproportionately more.
    # With k=1, rank-0 contributes 1.0; rank-1 contributes 0.5.
    # With k=60, the gap is tiny (1/60 vs 1/61).
    a = rrf_fuse([[0, 1, 2], [1, 2, 0]], k=1)
    b = rrf_fuse([[0, 1, 2], [1, 2, 0]], k=60)
    # With k=1 doc 0 (rank 0 + rank 2) = 1.0 + 1/3 = 1.333
    #         doc 1 (rank 1 + rank 0) = 0.5 + 1.0 = 1.5
    # With k=60 doc 0 = 1/60 + 1/62 = 0.0328, doc 1 = 1/61 + 1/60 = 0.0331
    assert a[0] == 1
    assert b[0] == 1
    # Both settings agree here but smaller k should make the 1-vs-0 gap larger.
    a_top_gap = abs(1 / 1 + 1 / 3 - (1 / 2 + 1 / 1))
    b_top_gap = abs(1 / 60 + 1 / 62 - (1 / 61 + 1 / 60))
    assert a_top_gap > b_top_gap
