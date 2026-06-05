"""analyze drift 系列核心纯函数的回归测试 (word-run 指标 / 趋势 / 统计检验).

固化文档 docs/language-drift-analysis.md 声称的核心指标行为, 防合并/重构回归。
不依赖外部对话文件 (用内联文本/数列)。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import analyze_drift_runs as dr  # noqa: E402
import analyze_conversation_lang as cl  # noqa: E402


# ---------- word-run 指标 (文档核心声称的回归保护) ----------

def test_word_runs_chinese_with_english_terms():
    # 文档方法论例句: 中文句夹英文术语 → 中文段保持长度, 英文段都是单词
    cjk, asc = dr.compute_word_runs("需要 refactor 一下这个 module 的 interface")
    assert cjk == [2, 4, 1]
    assert asc == [1, 1, 1]


def test_word_runs_english_with_chinese_glue():
    # 文档例句: 漂移后英文句夹中文胶水词 → 中文退化单字, 英文成连续段
    cjk, asc = dr.compute_word_runs("Dispatch implementer inline config 进 adapter 达 zero legacy")
    assert cjk == [1, 1]
    assert asc == [4, 1, 2]


def test_tokenize_discards_non_letter_boundaries():
    assert dr.tokenize("中 文") == ["cjk", "cjk"]
    assert dr.tokenize("a, b") == ["ascii", "ascii"]


def test_tokenize_contiguous_ascii_is_one_token():
    assert dr.tokenize("hello") == ["ascii"]


# ---------- 趋势斜率 ----------

def test_linear_slope_monotonic():
    assert dr.linear_slope([0, 1, 2, 3]) == 1.0
    assert dr.linear_slope([3, 2, 1, 0]) == -1.0
    assert dr.linear_slope([5, 5, 5]) == 0.0


def test_linear_regression_perfect_fit():
    a, b, r2 = cl.linear_regression([0, 1, 2], [0, 2, 4])
    assert abs(a - 0) < 1e-9
    assert abs(b - 2) < 1e-9
    assert abs(r2 - 1.0) < 1e-9


# ---------- 统计检验 ----------

def test_normal_cdf_known_values():
    assert abs(dr.normal_cdf(0) - 0.5) < 1e-9
    assert abs(dr.normal_cdf(1.96) - 0.975) < 1e-3


def test_wilcoxon_all_positive():
    # 10 个全正值: w_plus=55, mu=27.5, sigma≈9.81, z≈2.803 (无连续性修正, 见函数注释)
    w, z, p = dr.wilcoxon_signed_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert w == 55
    assert abs(z - 2.803) < 0.01
    assert p < 0.005


def test_wilcoxon_too_few_returns_nonsignificant():
    assert dr.wilcoxon_signed_rank([1, 2, 3]) == (0, 0, 1.0)


def test_binomial_p_below_expected_is_one():
    assert dr.binomial_p(5, 10) == 1.0  # k <= n*p0


def test_binomial_p_all_success_is_small():
    assert dr.binomial_p(10, 10) < 0.01


# ---------- 字符分类 ----------

def test_is_cjk():
    assert dr.is_cjk("中") is True
    assert dr.is_cjk("a") is False


def test_count_chars_separates_cjk_and_ascii():
    assert cl.count_chars("中a文b") == (2, 2)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n[OK] {passed}/{len(fns)} 通过")


if __name__ == "__main__":
    _run_all()
