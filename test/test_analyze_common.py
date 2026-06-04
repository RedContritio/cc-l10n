"""analyze 系列共享纯函数契约测试 (不触网/不读真对话)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from analyze_common import extract_project_name  # noqa: E402


# ---------- extract_project_name ----------

def test_projects_path_simple():
    assert extract_project_name("-Users-redcontritio-Projects-cc-zh") == "cc-zh"


def test_projects_path_hyphenated_username():
    # regression: 含连字符用户名 (john-doe), 贪婪 [^-]+ 会把 'doe' 留在结果里
    assert extract_project_name("-Users-john-doe-Projects-myproj") == "myproj"


def test_projects_path_hyphenated_projectname():
    # rsplit 取最后一个 -Projects- 分隔, 保留 proj 名内部的连字符
    assert extract_project_name("-Users-redcontritio-Projects-Research-douzero-icml2021") \
        == "Research-douzero-icml2021"


def test_non_projects_path_falls_back_to_tilde():
    # 非 Projects 路径 (如 test-sidecar): 去 -Users-<user>- 前缀为 ~, 与旧行为一致
    assert extract_project_name("-Users-redcontritio-test-sidecar") == "~test-sidecar"


def test_home_path_hyphenated_username():
    # Linux /home/<user>/Projects/<proj> 编码, 含连字符用户名
    assert extract_project_name("-home-jane-roe-Projects-proj") == "proj"


def test_full_jsonl_path_input():
    # 接受完整 jsonl 路径, 取其中 CC 编码的目录名 segment
    p = "/Users/redcontritio/.claude/projects/-Users-redcontritio-Projects-gicg-mono/abc.jsonl"
    assert extract_project_name(p) == "gicg-mono"


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
