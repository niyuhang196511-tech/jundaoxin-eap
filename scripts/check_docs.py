#!/usr/bin/env python3
"""文档/契约一致性检查（EAP M32 · 任务组 P5 `ci-`）。

用法（仓库根执行）：
    uv run --no-project python scripts/check_docs.py

检查项：
  ① README.md + docs/*.md 中的相对链接 [text](path) 与裸 `docs/...` 引用 →
     目标文件存在（锚点/外链忽略；代码块内不检查）；
  ② 所有 ```mermaid 代码块（含 diagrams/*.mmd 源文件）→ 轻量语法校验：
     声明行存在、边语法行两侧无孤立空段、块级括号平衡（够报警，不求全解析）；
  ③ docs/progress-plan.md 快照头「代码 commit」值存在于仓库历史
     （git rev-parse 解析 + git cat-file -e 验证）。

仅依赖 Python 3.12 标准库；发现问题以非零退出并列出问题清单。
路径一律相对仓库根解析（取本脚本父目录的上一级），与 CWD 无关。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 已知的前向引用：文档明示「待创建/待收版审计」，暂无实体文件（docs/16 = v0.8 收版审计）。
FORWARD_REF_ALLOWLIST = {"docs/16"}

# 裸引用只对「像是文件路径」的 token 生效：以已知扩展名结尾，或 docs/ 下单段编号引用。
BARE_REF_RE = re.compile(r"\bdocs/[0-9A-Za-z._/\u4e00-\u9fff-]+")
KNOWN_EXTENSIONS = (".md", ".py", ".yaml", ".yml", ".json", ".toml", ".html", ".txt", ".mmd")

# markdown 链接：[text](target "title")；target 允许 <> 包裹与锚点。
LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")

# mermaid 声明关键字（首条非注释行）。
MERMAID_DECL_RE = re.compile(
    r"^\s*(graph|flowchart|sequenceDiagram|stateDiagram(?:-v2)?|classDiagram(?:-v2)?"
    r"|erDiagram|journey|gantt|pie|mindmap|timeline|quadrantChart|gitGraph|sankey-beta"
    r"|architecture-beta|requirementDiagram|C4(?:Context|Container|Dynamic|Deployment))\b"
)

# 边/消息语法（长 token 在前，避免 -- 误吞 -->）。
MERMAID_ARROW_RE = re.compile(r"-->>|-->|-\.(?:->|x|-)|==>|->>|--x|--o|-\)|->|---|-x")
# 这几类图的行语法自成体系（关系线 ||--o{ / 任务行 / 数据行），不做边两侧校验。
MERMAID_SKIP_EDGE_DECLS = ("erDiagram", "gantt", "pie", "journey", "mindmap", "quadrantChart", "gitGraph")

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def strip_code_spans(line: str) -> str:
    """去掉行内代码 span，避免示例代码被当作真实引用。"""
    return re.sub(r"`[^`]*`", "`", line)


def iter_effective_lines(text: str) -> list[tuple[int, str]]:
    """逐行返回 (行号, 去掉围栏代码块后的行)；围栏内内容整段跳过。"""
    out: list[tuple[int, str]] = []
    in_fence = False
    for no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append((no, strip_code_spans(raw)))
    return out


def extract_mermaid_blocks(text: str) -> list[str]:
    """提取 ```mermaid 围栏块原文（保留行内原始内容）。"""
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```mermaid"):
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            blocks.append("\n".join(body))
        i += 1
    return blocks


def resolve_link(source_file: Path, target: str) -> Path | None:
    """解析链接目标（去掉锚点）；返回绝对路径，外链返回 None。"""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
        return None  # 外部链接 / 纯锚点
    target = target.split("#", 1)[0]
    if not target:
        return None
    base = source_file.parent
    return (base / target).resolve()


def resolve_bare_ref(token: str) -> Path | None:
    """裸 docs/ 引用的候选路径（相对仓库根）。"""
    return ROOT / token


def bare_ref_resolves(token: str) -> bool:
    """裸引用解析规则：
    - 以已知扩展名结尾 → 目标文件必须存在；
    - 单段（无内部 /，如 docs/01、docs/03-AgentRuntime）→ 精确文件、+.md、
      或 docs/ 下文件名前缀匹配（编号/名称 + `-` 或 CJK 边界）任一命中即可；
    - 其余（含内部 / 的无扩展名 token，如行文简写 docs/12/13 = docs/12、docs/13）
      → 语义不明确，跳过不判（返回 True）。
    """
    candidates = resolve_bare_ref(token)
    if candidates.is_file():
        return True
    if candidates.with_name(candidates.name + ".md").is_file():
        return True
    if "/" in token[len("docs/"):]:
        return True
    if "/" not in token[len("docs/"):]:
        seg = token[len("docs/"):]
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for f in docs_dir.iterdir():
                stem = f.stem
                if stem == seg or (stem.startswith(seg) and len(seg) > 0 and
                                   (stem[len(seg)] == "-" or CJK_RE.match(stem[len(seg)] or " "))):
                    return True
    return False


def check_mermaid_block(body: str, label: str, issues: list[str]) -> None:
    """轻量 mermaid 校验：声明行、边两侧非空、块级括号平衡。"""
    lines = body.splitlines()

    # 1) 声明行存在（跳过 %% 注释行；块首 --- frontmatter 跳过）
    decl = ""
    idx = 0
    stripped = [l.strip() for l in lines]
    while idx < len(stripped) and (not stripped[idx] or stripped[idx].startswith("%%")):
        idx += 1
    if idx < len(stripped) and stripped[idx] == "---":  # frontmatter
        idx += 1
        while idx < len(stripped) and stripped[idx] != "---":
            idx += 1
        idx += 1
    while idx < len(stripped) and (not stripped[idx] or stripped[idx].startswith("%%")):
        idx += 1
    if idx < len(stripped) and MERMAID_DECL_RE.match(stripped[idx]):
        decl = MERMAID_DECL_RE.match(stripped[idx]).group(1)  # type: ignore[union-attr]
    else:
        issues.append(f"{label}: 未找到 mermaid 声明行（graph/flowchart/sequenceDiagram 等）")
    if not body.strip():
        issues.append(f"{label}: mermaid 块为空")
        return

    skip_edge = any(decl.startswith(d) for d in MERMAID_SKIP_EDGE_DECLS)

    # 2) 边语法行：拆箭头，两侧不得为空（标签 |x| / : text 留在右侧，不影响非空判断）
    if not skip_edge:
        for no, line in enumerate(lines, start=1):
            s = line.strip()
            if not s or s.startswith("%%"):
                continue
            m = MERMAID_ARROW_RE.search(s)
            if not m:
                continue
            left, right = s[: m.start()], s[m.end():]
            if not left.strip():
                issues.append(f"{label}:{no}: 边语法缺少左侧节点 → {s!r}")
            if not right.strip().strip("|"):
                issues.append(f"{label}:{no}: 边语法缺少右侧节点 → {s!r}")

    # 3) 块级括号平衡（剥离双引号串与注释行；全角括号不计）。
    #    erDiagram 关系线（含 --）上的 { } 是鸦爪记号（||--o{ / }o--||）而非括号，
    #    只统计属性块所在的非关系行。
    counts = {"[": 0, "]": 0, "(": 0, ")": 0, "{": 0, "}": 0}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("%%"):
            continue
        if decl.startswith("erDiagram") and "--" in s:
            continue
        s = re.sub(r'"[^"]*"', "", s)
        for ch in s:
            if ch in counts:
                counts[ch] += 1
    for pair in (("(", ")"), ("[", "]"), ("{", "}")):
        if counts[pair[0]] != counts[pair[1]]:
            issues.append(
                f"{label}: 括号不平衡 {pair[0]}={counts[pair[0]]} {pair[1]}={counts[pair[1]]}"
            )


def check_snapshot_commit(issues: list[str]) -> None:
    """progress-plan.md 快照头「代码 commit」必须指向仓库历史中的真实提交。"""
    plan = ROOT / "docs" / "progress-plan.md"
    if not plan.is_file():
        issues.append("docs/progress-plan.md: 文件不存在，无法校验快照头")
        return
    row_re = re.compile(r"^\|\s*代码 commit\s*\|\s*([^|]+)\|", re.M)
    m = row_re.search(plan.read_text(encoding="utf-8"))
    if not m:
        issues.append("docs/progress-plan.md: 快照头缺少「代码 commit」行")
        return
    value = m.group(1).strip()
    hex_m = re.search(r"\b([0-9a-f]{7,40})\b", value)
    if not hex_m:
        issues.append(f"docs/progress-plan.md: 快照头「代码 commit」无法解析出提交号 → {value!r}")
        return
    ref = hex_m.group(1)
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if rev.returncode != 0:
            issues.append(f"docs/progress-plan.md: 快照头 commit {ref} 不存在于仓库历史")
            return
        full = rev.stdout.strip()
        cat = subprocess.run(
            ["git", "cat-file", "-e", f"{full}^{{commit}}"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if cat.returncode != 0:
            issues.append(f"docs/progress-plan.md: commit {ref} 对象不可读（cat-file -e 失败）")
    except OSError as exc:
        issues.append(f"docs/progress-plan.md: git 校验执行失败 → {exc}")


def main() -> int:
    issues: list[str] = []
    md_files = sorted([ROOT / "README.md", *(ROOT / "docs").glob("*.md")])
    stats = {"links": 0, "bare": 0, "mermaid": 0}

    # ① 链接 + 裸引用
    for f in md_files:
        if not f.is_file():
            issues.append(f"{f.relative_to(ROOT)}: 文件不存在")
            continue
        rel = f.relative_to(ROOT)
        text = f.read_text(encoding="utf-8")
        for no, line in iter_effective_lines(text):
            for m in LINK_RE.finditer(line):
                target = m.group(2)
                resolved = resolve_link(f, target)
                if resolved is None:
                    continue
                stats["links"] += 1
                if not resolved.exists():
                    issues.append(f"{rel}:{no}: 链接目标不存在 → [{m.group(1)}]({target})")
            # 裸引用：先移除 markdown 链接本体，避免与链接目标重复计数
            bare_line = LINK_RE.sub(" ", line)
            for m in BARE_REF_RE.finditer(bare_line):
                token = m.group(0).rstrip(".")
                if token in FORWARD_REF_ALLOWLIST:
                    continue
                stats["bare"] += 1
                if not bare_ref_resolves(token):
                    issues.append(f"{rel}:{no}: 裸引用目标不存在 → {token}")

    # ② mermaid 块（文档内嵌 + diagrams/*.mmd 源文件）
    for f in md_files:
        rel = f.relative_to(ROOT)
        text = f.read_text(encoding="utf-8")
        for i, body in enumerate(extract_mermaid_blocks(text)):
            stats["mermaid"] += 1
            check_mermaid_block(body, f"{rel}#mermaid[{i}]", issues)
    for mmd in sorted((ROOT / "diagrams").glob("*.mmd")):
        stats["mermaid"] += 1
        check_mermaid_block(mmd.read_text(encoding="utf-8"), str(mmd.relative_to(ROOT)), issues)

    # ③ 快照头 commit 校验
    check_snapshot_commit(issues)

    if issues:
        print(f"check_docs: 发现 {len(issues)} 个问题：")
        for item in issues:
            print(f"  - {item}")
        return 1
    print(
        "check_docs: OK — "
        f"文档 {len(md_files)} 个，链接 {stats['links']} 条，"
        f"裸引用 {stats['bare']} 条，mermaid 块 {stats['mermaid']} 个，快照 commit 校验通过"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
