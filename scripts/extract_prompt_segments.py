"""
静态分析 cli.js, 提取所有"可能进入 system prompt"的字符串字面量.

设计理念:
  替代运行时 capture - 容器 print mode 看不到 interactive 入口 / template literal
  / kK()?null:"..." 类条件分支. 静态扫描遍历所有字面量, 用启发式过滤 prompt 候选.

用法:
  python3 extract_prompt_segments.py <binary-or-cli.js> [--out report.json]
                                     [--translations translations.json]
                                     [--min-score 2] [--show-context 40]

输入既可以是 CC binary (自动 extract cli.js), 也可以是已 dump 的 cli.js.

输出 JSON 报告:
  {
    "summary": { total: N, candidates: M, translated: X, untranslated: Y, ... },
    "candidates": [ {offset, type, score, evidence[], text, translated, ...}, ... ]
  }

启发式打分 (累加):
  + 4  含 markdown 标题 (行首 #)
  + 3  以 "You are " / "You should " / "You can " / "You have " 开头
  + 3  含 "IMPORTANT:" / "CRITICAL:" / "NEVER " / "ALWAYS "
  + 2  含 "${...}" template placeholder
  + 2  含 ">= 3 个英文 stop words" (the/a/an/is/are/of/to/and/in/for/with/that/this/your/you)
  + 2  含 "<example>" / "<commentary>" / "<tag>" XML-ish
  + 2  长度 >= 200 字节
  + 1  长度 >= 80 字节
  + 1  包含中文 (说明已被翻译, 仍计为 prompt 候选, 用于覆盖率确认)
  +/- penalize: 纯标识符 / 纯路径 / 纯命令 / 纯 URL / 纯 JSON

score >= --min-score (默认 2) 进入 candidates 输出.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 允许作为 script 直接跑 (无需 PYTHONPATH)
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bun_format import extract_cli_js
from js_codec import decode_literal
from js_string_scanner import scan_string_literals
from log_setup import setup_logging

log = setup_logging(__name__)

CN_RE = re.compile(r"[一-鿿　-〿＀-￯]")
MD_HEADING_RE = re.compile(r"(?m)^#{1,6} \S")
STOP_WORDS = {
    "the", "a", "an", "is", "are", "be", "of", "to", "and", "in", "for", "with",
    "that", "this", "your", "you", "it", "on", "at", "as", "by", "or", "if",
    "but", "not", "no", "do", "does", "should", "would", "can", "may", "must",
    "from", "into", "when", "where", "how", "what", "why", "which", "who",
    "they", "them", "their", "we", "us", "our", "his", "her", "its",
}
WORD_RE = re.compile(r"[A-Za-z']{2,}")
SENTINEL_RE = re.compile(r"\$\{[\w.]+\}")
# 句子标点 (句号/逗号/分号/冒号 后接空白或右括号): 散文有句子结构, 语言关键字表/
# SYSRES 常量表/base64/正则 pattern 是无标点 token 流 -> 句子标点密度是强区分特征。
SENT_PUNCT_RE = re.compile(r"[.,;:][ \n)]")
URL_RE = re.compile(r"^https?://[\S]+$")
PATH_RE = re.compile(r"^/?[A-Za-z0-9_\-./]+\.(?:ts|tsx|js|jsx|py|md|json|sh|toml)$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CMD_RE = re.compile(r"^[a-z][a-z0-9\-]*(?: [a-z0-9\-./]+)*$")

# minified-JS 残片 token (零误伤版, 见 scripts/diagnostic/test_minijs_penalty.py):
# 真 prompt 不可能含 >=3 个这类 runtime token, 也不会以续接标点开头。
MINI_JS_TOKENS = (
    "N(`", "})}", "}),", ":!0", ":!1", "void 0", ".then(", ".push(", ".has(",
    "catch(", "try{", "finally{", ",content:`", ",error:`", ",note:`",
    ",reason:`", ",request_id:", ":return`", "Date.now()", "Array.isArray",
    ".uuid", "){let ", "){if(", "];if(", ");if(", ")}if(", "})),", "=>{", "=>(",
    ",identity:", ",properties:", ",profiles:", ",subtitle:", ",tool_use_id:",
    ",source:", ",worktree:", ",attempts:", ",dryRun:", ",maxResultSizeChars",
    "QBH()", "].push(", ",{cwd:", ".rm(", ".unlink(", ".dispose()",
    ",instructions:[", ",command:", ",errorDescription:", ".at(-1)",
    ".number()", ".optional()", ".min(", ".format()", "}:{error:", ",K6=",
    "=ZH(", ",_)(", "?`${", ".apiBaseUrl", ".pluginId", ",changed_files:",
    "=O.", ".whats_work", "this.version", "this.format",
)
MINI_JS_LEAD = set("&),;?:]}")


def score_text(text: str) -> tuple[int, list[str]]:
    """给一段文本打 prompt-likelihood 分数, 返回 (score, evidence_tags)."""
    evidence: list[str] = []
    score = 0
    n = len(text)

    if MD_HEADING_RE.search(text):
        score += 4
        evidence.append("markdown_heading")

    if re.match(r"^You (?:are|should|can|have|must|may)\b", text):
        score += 3
        evidence.append("starts_with_You")

    if re.search(r"\b(?:IMPORTANT|CRITICAL|NEVER|ALWAYS|MUST|NOTE):\s", text):
        score += 3
        evidence.append("imperative_marker")

    if SENTINEL_RE.search(text):
        score += 2
        evidence.append("has_placeholder")

    words = WORD_RE.findall(text.lower())
    stop_count = sum(1 for w in words if w in STOP_WORDS)
    if stop_count >= 3:
        score += 2
        evidence.append(f"stop_words={stop_count}")

    if re.search(r"<(?:example|commentary|when_to_save|how_to_use|types?|user|assistant)\b", text):
        score += 2
        evidence.append("xml_tags")

    if n >= 200:
        score += 2
        evidence.append("long")
    elif n >= 80:
        score += 1
        evidence.append("medium")

    if CN_RE.search(text):
        score += 1
        evidence.append("has_chinese")

    # description-like 启发式: tool description / setting description / 配置项说明
    # 这类字面量"看起来像 prompt 但启发式分数低" — 用动词起首 + 长度 + stop_words 触发强信号
    # 让它们不再 silent skip
    DESC_VERBS = (
        "Reads", "Read", "Writes", "Write", "Edits", "Edit",
        "Executes", "Execute", "Lists", "List", "Searches", "Search",
        "Creates", "Create", "Deletes", "Delete", "Updates", "Update",
        "Sends", "Send", "Returns", "Return", "Runs", "Run",
        "Performs", "Perform", "Manages", "Manage", "Uploads", "Upload",
        "Takes", "Take", "Gets", "Get", "Finds", "Find",
        "Resizes", "Resize", "Clicks", "Click", "Types", "Type",
        "Presses", "Press", "Opens", "Open", "Closes", "Close",
        "Provides", "Provide", "Accepts", "Accept", "Generates", "Generate",
        "Captures", "Capture", "Extracts", "Extract", "Filters", "Filter",
        "Saves", "Save", "Loads", "Load", "Reports", "Report",
        "Inspects", "Inspect", "Analyzes", "Analyze", "Validates", "Validate",
        "Verifies", "Verify", "Schedules", "Schedule", "Cancels", "Cancel",
        "Stops", "Stop", "Starts", "Start", "Continues", "Continue",
        "Resumes", "Resume", "Pauses", "Pause", "Initializes", "Initialize",
        "Configures", "Configure", "Sets", "Set", "Toggles", "Toggle",
        "Enables", "Enable", "Disables", "Disable", "Displays", "Display",
        "Renders", "Render", "Outputs", "Output", "Fetches", "Fetch",
        "Calls", "Call", "Invokes", "Invoke",
        "Allow", "Allows", "Exit", "Exits",
        "Use", "Uses", "Used",
    )
    first_word = re.match(r"[A-Za-z]+", text.lstrip())
    if (n >= 60 and stop_count >= 3
            and first_word
            and first_word.group(0) in DESC_VERBS
            and not MD_HEADING_RE.search(text)):
        score += 3
        evidence.append("desc_verb_lead")

    # 散文结构启发式: 长文本 + stop_words 密集 + 句子标点密集 = 自然语言散文 prompt。
    # 主 system prompt 行为指令 / 各场景 prompt / tool·setting·agent 描述这类散文常
    # 缺 markdown 标题 / "You are" / IMPORTANT: 等强信号, 仅靠 stop_words+长度 停在 4 分
    # (< prompt 阈值 5), 导致 class=weak 静默漏译。句子标点密度把它们与语言关键字表 /
    # SYSRES 常量 / base64 / 正则等无标点 token 流区分开 (后者密度 ~0)。
    sentence_punct = len(SENT_PUNCT_RE.findall(text))
    prose_density = sentence_punct / max(len(words), 1)
    if (n >= 160 and stop_count >= 8
            and sentence_punct >= 6 and prose_density >= 0.045):
        score += 2
        evidence.append("prose_structure")

    # 排除明显不是 prompt 的形态
    stripped = text.strip()
    if URL_RE.match(stripped):
        evidence.append("url")
        score -= 5
    if PATH_RE.match(stripped):
        evidence.append("path")
        score -= 5
    if IDENT_RE.match(stripped):
        evidence.append("identifier")
        score -= 5
    if len(words) == 0 and stop_count == 0:
        score -= 3
        evidence.append("no_words")

    # minified JS code 启发式 penalty: 字面量内含 JS 语法 token, 一定不是 prompt
    js_markers = sum(1 for pat in [
        "throw Error(", "throw new Error(", ".diag.", "Object.assign",
        "function(", "function ", "=>{", "=>(", "var H=", "var _=", "var q=",
        "let H=", "let _=", "let q=", "let O=", "let K=", "let z=", "let $=", "let D=",
        "const H=", "const _=", "const q=",
        ";if(", ";if($){", ";if(_){", ";if(H){", ";if(q){",
        ";for(", ";for(let ", ";return ", ";await ",
        "if($){let ", "if(_){let ", "if(H){let ", "if(q){let ",
        "Array.isArray", "this._", "Buffer.from", "new Date(", "new Promise(",
        "JSON.stringify", "JSON.parse", "Promise.all", "Promise.resolve",
        "Object.keys", "Object.entries", "typeof H", "typeof _", "typeof q",
        ".replace(", ".indexOf(", ".lastIndexOf(", ".slice(", ".split(",
        ".match(", ".exec(", ".test(", ".filter(", ".reduce(",
        ".toLowerCase()", ".toUpperCase()", ".trim()", ".length", ".concat(",
        "{value:!0}", "{value:!1}", "return Error", "instanceof ",
        "elementType!==", "decisionReason:", "behavior===",
        ")q.push(", ")H.push(", ")_.push(",
        "delete K[", "delete H[", "delete _[",
        ".write(M,", ".write(_", ".write(O,",
        ".httpHeader", ".raw}return", ".name}",
        "PK3/1000", "FuH===",
    ] if pat in text)
    if js_markers >= 2:
        evidence.append(f"js_code={js_markers}")
        score -= 10  # 强降权
    # 字面量首字符是 JS 拼接 punctuation, 极可能是 minified code
    if stripped and stripped[0] in "}){,;])`&|":
        if re.match(r"^[}\)\],;`\\&|]{1,3}\s*(?:if|for|let|const|var|return|await|throw|else|q\.|_\.|H\.|message:|P=|\${|[a-zA-Z_$]+\()",
                    stripped):
            evidence.append("js_punct_lead")
            score -= 5

    # minified-JS 残片 penalty (零误伤版, 见 scripts/diagnostic/test_minijs_penalty.py):
    # mj>=3 (prompt 不可能含 3 个 runtime token) 或 (续接标点开头 且 mj>=1 且非 imperative
    # 指令)。补 js_markers/js_punct_lead 之外的细碎 minified token (`})}` / `,reason:` /
    # `.then(` 等), 收掉残存 score>=5 的 JS 假候选。对真散文 prompt 结构安全 (不以 &),;?:]}
    # 开头, 不含 3 个 runtime token)。
    mj = sum(1 for p in MINI_JS_TOKENS if p in text)
    mj_lead = bool(stripped) and stripped[0] in MINI_JS_LEAD
    if mj >= 3 or (mj_lead and mj >= 1 and "imperative_marker" not in evidence):
        evidence.append(f"minified_js={mj}")
        score -= 8

    # 第三方 SDK runtime 字符串 (Azure / AWS / OAuth 错误等)
    sdk_markers = sum(1 for pat in [
        "@aws-sdk/", "@azure/identity", "@anthropic-ai/sdk", "@azure/msal",
        "NodeDeprecationWarning", "DefaultAzureCredential", "ManagedIdentityCredential",
        "useIdentityPlugin", "AWS SDK", "AWS_REGION", "AWS_PROFILE",
        "PublicClientApplication", "ConfidentialClientApplication",
        "azd auth login", "Service Fabric", "CloudShell",
        "tenantId", "clientId", "federatedTokenFilePath", "DefaultAzureCredentialClientIdOptions",
    ] if pat in text)
    if sdk_markers >= 1:
        evidence.append(f"sdk_string={sdk_markers}")
        score -= 5

    # Setting field description / tool input schema description: 短句以特定动词起首
    if re.match(r"^(?:Default false|Default true|When true|When false|Set this to|Path to|Allow |Disable |Enable |Override |Pass |Provide |Sets |Specifying |Use for |Use to |Optional |Required |Reserved |Background |Inline |Policy-list|Regex pattern|Directories|Additional |Arguments |Background |Sets the )",
                stripped):
        evidence.append("config_desc")
        score -= 3

    # Anthropic SDK / API error / deprecation 字符串
    if any(p in text for p in [
        " is deprecated. Use ", " is deprecated and will be removed",
        "is not supported by the", "Could not resolve authentication method",
        "Could not retrieve the token", "Cannot return token from cache",
        "Cannot find Windows browser", "Max Age was requested",
        "Authority URIs must use", "Authority mismatch error",
        "It looks like you're running in a browser-like environment",
        "Failed to import '@",
    ]):
        evidence.append("sdk_error_msg")
        score -= 3

    # Browser automation / Chrome MCP tool description
    if any(p in text for p in [
        "tabs_context_mcp", "claude-in-chrome", "MCP tab group",
        "Chrome extension", "browser automation", "page context",
        "DOM, win", "right_click", "left_click", "screenshot",
        "switch_browser", "key sequence",
    ]):
        if not re.search(r"[一-鿿]", text):
            evidence.append("chrome_tool_desc")
            score -= 3

    return score, evidence


def collect_array_groups(cli: bytes, ranges: list[tuple[int, int, str]]) -> dict[int, str]:
    """
    识别 array literal of string literals: `[ "...", "...", ... ]`.
    返回 dict: literal_offset -> group_id (同一 array 内的字面量共享 group_id, 便于聚合).

    检测逻辑: 对每对相邻字面量 (e1, e2), 中间字符若为 "," (允许空白) 则视为同 group.
    再向左/右扩展至首尾 ["..."] 或非 "," 字符. 若 group 边界字符是 "[" "]" 则确认是 array literal.

    简化实现: 只看相邻 literal 之间是不是 `,`. 准确率够用.
    """
    n = len(ranges)
    group: list[int] = list(range(n))  # union-find parent

    def find(i: int) -> int:
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            group[ri] = rj

    for i in range(n - 1):
        s1, e1, _ = ranges[i]
        s2, e2, _ = ranges[i + 1]
        # 在 cli[e1+1 .. s2-1] 之间 (跳过引号), 看是否纯 "," + 空白
        between_start = e1 + 1  # 跳过 closing quote
        between_end = s2 - 1    # 不含 opening quote
        if between_start >= between_end:
            continue
        seg = cli[between_start:between_end]
        # 允许 \s, , 但不允许其他字符
        if seg and all(c in b" \t\n\r,/" for c in seg):
            # 还要确认确实有 ","
            if b"," in seg:
                union(i, i + 1)

    # 输出: literal_offset -> root_id
    out: dict[int, str] = {}
    for i, (s, _, _) in enumerate(ranges):
        out[s] = f"g{find(i)}"
    return out


def classify_literal(text: str, offset: int, group_size: int,
                     min_score: int) -> tuple[str, int, list[str]]:
    """
    返回 (classification, score, evidence). classification ∈ {prompt, weak, skip}.
    在 array group >= 2 时, 即使个体分数低也保留 (整组 prompt 拼接).
    """
    score, ev = score_text(text)
    if group_size >= 3:
        score += 1
        ev.append(f"array_group={group_size}")
    # prompt class 阈值固定 >= 5 (强候选, strict 时 fail-condition)
    # weak class >= min_score (弱候选, 仅警告)
    if score >= 5:
        cls = "prompt"
    elif score >= min_score:
        cls = "weak"
    else:
        cls = "skip"
    return cls, score, ev


def load_translations_keys(path: Path, platform: str | None = None) -> set[str]:
    """加载 translations 的所有 src key (含 sentinel 形态). 支持单文件或目录.

    platform: 额外加载 <path>/<platform>/*.json 平台专属 key (仅对该平台 binary 计入覆盖)。
    """
    from bun_format import translation_files
    files = translation_files(path, platform)
    keys: set[str] = set()
    for fp in files:
        raw = json.loads(fp.read_text())
        for k in raw.keys():
            if not k.startswith("_"):
                keys.add(k)
    return keys


def load_whitelist(path: Path) -> dict:
    """返回 dict: {exact, regex, literal_exact}.

    literal_exact 用 set + frozenset 加速完整字面量匹配.

    契约: literal_exact 条目必须已 strip 存储. is_whitelisted 用
    ``text.strip() in literal_exact`` 比较, 带外层空白 (前/后导 \\n / 空格)
    的条目永远无法命中 = 静默死条目. 此处强制 raise 让缺陷可见, 不静默纠正
    (auto-strip 会掩盖数据缺陷).
    """
    raw = json.loads(path.read_text())
    literal_raw = [
        s for s in raw.get("literal_exact", [])
        if isinstance(s, str) and not s.startswith("_comment")
    ]
    padded = [s for s in literal_raw if s != s.strip()]
    if padded:
        sample = padded[0]
        raise ValueError(
            f"whitelist literal_exact 含 {len(padded)} 条带外层空白的死条目 "
            f"(is_whitelisted 用 strip 比较, 永不命中): 首条 repr_head={sample[:40]!r}. "
            f"请在 {path} 中 strip 这些条目后重试."
        )
    return {
        "exact": [s for s in raw.get("exact", []) if isinstance(s, str) and not s.startswith("_comment")],
        "regex": [re.compile(p) for p in raw.get("regex", []) if not p.startswith("_comment")],
        "literal_exact": frozenset(literal_raw),
    }


def is_whitelisted(text: str, wl: dict) -> bool:
    """
    判断字面量整体是否落在 whitelist 覆盖范围.
    优先级:
      1. text.strip() 整体在 literal_exact 集合中 (完全相等, 最严格)
      2. exact + regex 段落减法: 减掉所有匹配后, 剩余无英文词
    """
    if not text.strip():
        return True
    stripped = text.strip()
    if stripped in wl.get("literal_exact", ()):
        return True
    s = stripped
    for item in sorted(wl.get("exact", []), key=len, reverse=True):
        s = s.replace(item, "")
    for pat in wl.get("regex", []):
        s = pat.sub("", s)
    return not re.search(r"[A-Za-z]{3,}", s)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="CC binary 或 cli.js 路径")
    ap.add_argument("--out", default=None, help="JSON 报告输出路径")
    ap.add_argument("--translations", default=None,
                    help="translations.json 路径 (对比覆盖率)")
    ap.add_argument("--whitelist", default=None,
                    help="whitelist.json 路径 (分类 untranslated 是否被白名单覆盖)")
    ap.add_argument("--min-score", type=int, default=2,
                    help="进入 candidates 的最小分数 (默认 2)")
    ap.add_argument("--show", type=int, default=20,
                    help="stderr 打印前 N 条 untranslated 候选 (默认 20)")
    ap.add_argument("--show-context", type=int, default=60,
                    help="打印时上下文字符数 (默认 60)")
    args = ap.parse_args()

    src_path = Path(args.source)
    log.info(f"读取 {src_path} ...")
    cli = extract_cli_js(src_path)
    log.info(f"cli.js: {len(cli):,} 字节")
    from bun_format import platform_from_binary
    platform = platform_from_binary(src_path)

    log.info(f"扫描字符串字面量 ...")
    ranges = list(scan_string_literals(cli))
    log.info(f"  共 {len(ranges):,} 个字面量")

    log.info(f"识别 array literal 分组 ...")
    groups = collect_array_groups(cli, ranges)
    group_sizes: dict[str, int] = {}
    for gid in groups.values():
        group_sizes[gid] = group_sizes.get(gid, 0) + 1

    trans_keys: set[str] = set()
    if args.translations:
        trans_keys = load_translations_keys(Path(args.translations), platform)
        log.info(f"加载 translations: {len(trans_keys)} 条 (platform={platform})")

    wl: dict = {"exact": [], "regex": [], "literal_exact": frozenset()}
    if args.whitelist:
        wl = load_whitelist(Path(args.whitelist))
        log.info(f"加载 whitelist: {len(wl['exact'])} exact + {len(wl['regex'])} regex "
                 f"+ {len(wl['literal_exact'])} literal_exact")

    # 处理每条字面量
    candidates = []
    stats = {
        "total_literals": len(ranges),
        "decoded_ok": 0,
        "scored_prompt": 0,
        "scored_weak": 0,
        "scored_skip": 0,
        "translated": 0,
        "whitelisted": 0,
        "untranslated_prompt": 0,
        "untranslated_weak": 0,
    }

    # 用于覆盖率: 把 trans_keys 转成 bytes 集合, 还要考虑 sentinel 形态
    # trans_keys 是原始 JSON key (含 ${__SENTINEL__}), 需要拿 cli 中真实变量名来 materialize
    # 简化处理: 直接用文本相等比较 (sentinel 在原始 key 中, decoded text 在 cli 中是真变量名,
    # 所以我们也把 trans_keys 中的 sentinel 替换成 cli 真实变量名)
    # sentinel_real_names: name -> list of "${var}" strings (每实例一个)
    sentinel_real_names: dict[str, list[str]] = {}
    if trans_keys:
        from repack_universal import resolve_sentinels
        sentinel_map = resolve_sentinels(cli)
        for name, vars_ in sentinel_map.items():
            sentinel_real_names[f"${{{name}}}"] = [v.decode("utf-8") for v in vars_]

    def materialize_key(k: str) -> str:
        """单实例 materialize: 每 sentinel 取首实例 (用于子串包含检查的代表性 key)."""
        for s, vars_ in sentinel_real_names.items():
            if vars_:
                k = k.replace(s, vars_[0])
        return k

    def materialize_all_keys(k: str) -> set[str]:
        """全部实例展开: 含 N 实例 sentinel 的 k 展开为 N 条 (笛卡尔积逐项替换;
        多个 sentinel 假设按 index 对齐, 与 expand_translations 一致)."""
        names_in_k = [s for s in sentinel_real_names if s in k]
        if not names_in_k:
            return {k}
        # 取最大实例数作 N (对齐); 若长度不同, 按 zip-shortest 处理 (实际所有
        # sentinel 应在同一 N, 否则 expand_translations 会抛错)
        ns = [len(sentinel_real_names[s]) for s in names_in_k]
        N = min(ns) if ns else 0
        out = set()
        for i in range(N):
            v = k
            for s in names_in_k:
                v = v.replace(s, sentinel_real_names[s][i])
            out.add(v)
        return out

    trans_keys_real: set[str] = set()
    for k in trans_keys:
        trans_keys_real.update(materialize_all_keys(k))

    # 模板字面量片段覆盖: trans key 中含 ${...} placeholder 时, 整段在 cli.js 中是
    # 跨多个 tmpl_frag (静态片段) 的, 单个 tmpl_frag 在 trans_keys 中找不到精确匹配.
    # 把 sentinel/materialized 形态按 ${...} 切分的所有静态片段额外加入覆盖集.
    import re as _re
    _ph_split_re = _re.compile(r"\$\{[^{}]*(?:\{[^}]*\}[^{}]*)*\}")
    for k in list(trans_keys) + list(trans_keys_real):
        # 按 ${...} 切分, 保留两侧静态片段
        parts = _ph_split_re.split(k)
        for p in parts:
            if len(p) >= 8:  # 太短易误伤代码区, 限制 8+ 字符
                trans_keys_real.add(p)

    for start, end, lit_type in ranges:
        text = decode_literal(cli, start, end, lit_type)
        if text is None:
            continue
        stats["decoded_ok"] += 1
        gid = groups.get(start, f"g_solo_{start}")
        gsize = group_sizes.get(gid, 1)
        cls, score, ev = classify_literal(text, start, gsize, args.min_score)
        if cls == "skip":
            stats["scored_skip"] += 1
            continue
        if cls == "prompt":
            stats["scored_prompt"] += 1
        else:
            stats["scored_weak"] += 1

        # 覆盖状态 (单一 decode-match): text 是 canonical 完全解码形态, trans key 同源,
        # 直接 decoded == decoded 比对。repack 用同一 decode_literal 枚举字面量, verify 与之一致。
        # trans_keys_real 含 sentinel materialized 形态 + 按 ${...} 切分的静态片段 (覆盖跨插值 key)。
        in_trans = text in trans_keys or text in trans_keys_real
        # 部分匹配: 长 trans key 作为字面量子串 (decoded 形态)
        partial_trans = False
        if not in_trans and trans_keys:
            for k in trans_keys:
                if len(k) >= 20 and (k in text or materialize_key(k) in text):
                    partial_trans = True
                    break

        has_chinese = bool(CN_RE.search(text))
        whitelisted = False
        if not in_trans and not partial_trans and not has_chinese and any(wl.values()):
            whitelisted = is_whitelisted(text, wl)

        if in_trans:
            stats["translated"] += 1
        elif partial_trans:
            stats["translated"] += 1
            ev.append("partial_trans")
        elif has_chinese:
            # patched binary 的字面量已是中文 -> 视为 translated
            stats["translated"] += 1
            ev.append("contains_chinese")
        elif whitelisted:
            stats["whitelisted"] += 1
        else:
            # 未翻 + 无中文 + 不在白名单 = 真候选
            if cls == "prompt":
                stats["untranslated_prompt"] += 1
            else:
                stats["untranslated_weak"] += 1

        entry = {
            "offset": start,
            "end": end,
            "len": end - start,
            "type": lit_type,
            "group": gid,
            "group_size": gsize,
            "class": cls,
            "score": score,
            "evidence": ev,
            "text": text,
            "translated": in_trans or partial_trans or has_chinese,
            "whitelisted": whitelisted,
        }
        candidates.append(entry)

    candidates.sort(key=lambda e: (-e["score"], e["offset"]))

    report = {
        "source": str(src_path),
        "cli_js_size": len(cli),
        "summary": stats,
        "candidates": candidates,
    }

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        log.info(f"\n报告已写入 {args.out}")

    log.info(f"\n=== 摘要 ===")
    for k, v in stats.items():
        log.info(f"  {k:<28} {v:>6}")

    # 打印未翻译的前 N 条
    untrans = [c for c in candidates
               if not c["translated"] and not c["whitelisted"]]
    if untrans:
        log.info(f"\n=== 未翻译 (前 {min(args.show, len(untrans))} / {len(untrans)}) ===")
        for c in untrans[:args.show]:
            t = c["text"]
            preview = t[:args.show_context].replace("\n", "\\n")
            if len(t) > args.show_context:
                preview += "..."
            log.info(f"  @{c['offset']:>8} score={c['score']} [{c['class']}] "
                     f"{','.join(c['evidence'])}")
            log.info(f"           {preview!r}")

    # 退出码: 有未翻译 prompt 时返回 1
    if stats["untranslated_prompt"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
