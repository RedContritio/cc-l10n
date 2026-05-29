"""
把 cli.js 中所有 'You are' 开头的 prompt 字面量 dump 到指定目录.
每个文件 NN_short_name.en.txt (中文翻译手动添加为 NN_short_name.zh.txt)
"""
import argparse
import json
import pathlib
import re
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts"))
from bun_format import extract_cli_js
from js_string_scanner import scan_string_literals


def short_name(content: bytes) -> str:
    """从 prompt 内容提取一个简短英文标签作为文件名"""
    head = content[:200].decode('utf-8', errors='replace')
    head = head.split('.')[0]  # 取第一句
    # 提取关键词
    patterns = [
        (r"security monitor", "security_monitor"),
        (r"status line setup", "status_line_setup"),
        (r"onboarding guide", "onboarding_guide"),
        (r"agent architect", "agent_architect"),
        (r"stop-condition", "stop_condition_hook"),
        (r"title and git branch", "branch_name"),
        (r"reviewer of auto mode classifier", "automode_classifier_reviewer"),
        (r"selecting memories", "memory_selector"),
        (r"architect and planning", "plan_agent"),
        (r"file search specialist", "explore_agent"),
        (r"Claude guide agent", "claude_code_guide"),
        (r"searching for past Claude Code conversation", "session_searcher"),
        (r"agent for Claude Code", "agent_basic"),
        (r"evaluating a hook condition", "hook_condition_eval"),
        (r"highly capable", "capable_agent"),
        (r"currently using your overages", "overages_notice"),
        (r"not in plan mode", "not_plan_mode"),
        (r"interactive agent that helps users", "interactive_agent"),
        (r"highest Max subscription", "max_plan"),
        (r"verifying a stop condition", "stop_condition_verify"),
    ]
    for pat, name in patterns:
        if re.search(pat, head, re.IGNORECASE):
            return name
    # fallback: 用 head 头几个 word
    words = re.findall(r"[a-zA-Z]+", head)[:4]
    return "_".join(words).lower()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="CC binary 路径 (会 extract cli.js) 或已 dump 的 cli.js")
    ap.add_argument("--out", default="subagent_prompts",
                    help="输出目录 (默认: ./subagent_prompts)")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cli = extract_cli_js(pathlib.Path(args.source))

    ranges = scan_string_literals(cli)
    agent_prompts = []
    for s, e, t in ranges:
        content = cli[s:e]
        if content.startswith(b"You are ") and (e - s) >= 100:
            agent_prompts.append((s, e, t, content))
    agent_prompts.sort(key=lambda x: -(x[1] - x[0]))

    total = 0
    manifest = []
    for i, (s, e, t, content) in enumerate(agent_prompts):
        name = short_name(content)
        fname = f"{i:02d}_{name}.en.txt"
        (out / fname).write_bytes(content)
        size = len(content)
        total += size
        has_placeholder = b"${" in content
        manifest.append((i, name, size, has_placeholder, fname))
        print(f"  [{i:02d}] {name:<35} {size:>6,}B  {'(has ${...})' if has_placeholder else ''}")

    print(f"\n总计 {len(agent_prompts)} prompts, {total:,} bytes")
    print(f"输出: {out}")

    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps([
        {"id": m[0], "name": m[1], "size": m[2], "has_placeholder": m[3], "file": m[4]}
        for m in manifest
    ], indent=2, ensure_ascii=False))
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()
