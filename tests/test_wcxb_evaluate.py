"""Unit tests for the vendored WCXB word-level F1 evaluator."""

from benchmarks.wcxb.evaluate import word_f1


def test_identical_strings_f1_is_one():
    p, r, f = word_f1("the quick brown fox", "the quick brown fox")
    assert f == 1.0
    assert p == 1.0
    assert r == 1.0


def test_disjoint_strings_f1_is_zero():
    p, r, f = word_f1("alpha beta gamma", "one two three")
    assert f == 0.0


def test_partial_overlap_matches_hand_calculation():
    # prediction = 5 words, reference = 4 words, overlap = 3 words
    # precision = 3/5 = 0.6, recall = 3/4 = 0.75
    # f1 = 2 * 0.6 * 0.75 / (0.6 + 0.75) = 0.6666...
    p, r, f = word_f1("a b c d e", "a b c x")
    assert abs(p - 0.6) < 1e-9
    assert abs(r - 0.75) < 1e-9
    assert abs(f - (2 * 0.6 * 0.75 / (0.6 + 0.75))) < 1e-9


def test_empty_prediction_f1_is_zero():
    _, _, f = word_f1("", "hello world")
    assert f == 0.0


def test_empty_reference_f1_is_zero_when_prediction_nonempty():
    # Upstream semantics: if ref is empty AND pred is empty -> (1,1,1).
    # If ref is empty but pred is not -> (0,0,0). Verify the latter.
    _, _, f = word_f1("hello world", "")
    assert f == 0.0
