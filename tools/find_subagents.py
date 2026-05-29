"""
找 cli.js 中所有 subagent / 大型 prompt 的字面量位置.
依据: 字面量开头匹配 "You are " (含双引号 / 单引号 / 反引号 / tmpl_frag).
"""
import argparse
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts"))
from bun_format import extract_cli_js
from js_string_scanner import scan_string_literals


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="CC binary 路径 (会 extract cli.js) 或已 dump 的 cli.js")
    args = ap.parse_args()
    cli = extract_cli_js(pathlib.Path(args.source))

    ranges = scan_string_literals(cli)
    print(f"扫描到 {len(ranges):,} 个字面量")

    agent_prompts = []
    for s, e, t in ranges:
        content = cli[s:e]
        if content.startswith(b"You are ") and (e - s) >= 100:
            agent_prompts.append((s, e, t, content))
    agent_prompts.sort(key=lambda x: -(x[1] - x[0]))

    print(f"\n找到 {len(agent_prompts)} 个 'You are' 开头 (>=100B) 的字面量:\n")
    for s, e, t, content in agent_prompts:
        size = e - s
        head = content[:120].decode('utf-8', errors='replace').replace('\n', ' ')
        print(f"  [{t:9s}] @{s:>10,} len={size:>6,}  | {head}...")


if __name__ == "__main__":
    main()
