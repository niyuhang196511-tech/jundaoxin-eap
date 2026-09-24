"""审计动作目录防漂移静态防线（M50-B2，对齐 test_rls_coverage 的防线风格）。

两层防线，防「新增留痕动作漏登记目录」与「目录条目腐化（幽灵动作）」：

1. 正向覆盖——扫描 src/eap 源码树全部审计留痕调用点（audit.record / _audit.record），
   提取字符串字面量首参，断言 ⊆ AUDIT_ACTIONS；首参非字面量（变量/拼接）的调用点
   属动态站点，必须登记在 DYNAMIC_RECORD_SITES 白名单（每项带注释 + 解析值清单）。
2. 反向防幽灵——AUDIT_ACTIONS 每个条目都必须能对应到某个源码调用点（静态字面量
   或白名单登记的动态解析值），防止改名/删除调用点后目录腐化。

目录本身是文档/提示性质（过滤端点仍接受任意字符串），但目录一旦失真，
审计页 datalist 提示与治理盘点都会误导——故以静态扫描保持双向同步。
"""

from __future__ import annotations

import re
from pathlib import Path

_EAP_DIR = Path(__file__).resolve().parents[1]  # eap/（src/ 所在）
_SRC = _EAP_DIR / "src" / "eap"

# 审计留痕调用点：模块别名 audit / _audit（全仓仅这两种导入别名，见 observability.audit 导入点）
_CALL = re.compile(r"\b(?:audit|_audit)\.record\(")
# 首参为纯字符串字面量（无 f 前缀；留痕动作名历史上均为纯字面量）
_STR_FIRST = re.compile(r"""(['"])((?:[^\\'"]|\\.)*)\1""")

# 动态首参调用点白名单（键 = 相对 src/eap 的 POSIX 路径；值 = 该站点可发出的动作名全集）。
# 每项必须带注释说明动态原因与解析值来源——新增动态站点须在此登记，否则防线 1 即红。
DYNAMIC_RECORD_SITES: dict[str, set[str]] = {
    # runtime/context.py 的 _audit_compress(db, action, ...) 把首参原样转发给留痕函数；
    # action 由同文件两处调用方以静态字面量传入（会话摘要压缩留痕），非运行时拼接，
    # 解析值可穷举如下（若新增调用方，须同步本清单与 AUDIT_ACTIONS 目录）。
    "runtime/context.py": {
        "memory.summary_compress",
        "memory.summary_compress_fallback",
    },
}


def _scan_record_calls() -> tuple[set[str], dict[str, list[int]], int]:
    """扫描 src/eap 全部留痕调用点。

    返回 (静态字面量动作集, 动态站点 {相对路径: [行号]}, 调用点总数)。
    """
    static: set[str] = set()
    dynamic: dict[str, list[int]] = {}
    total = 0
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(_SRC).as_posix()
        for m in _CALL.finditer(text):
            total += 1
            line = text[: m.start()].count("\n") + 1
            rest = text[m.end():]
            probe = _STR_FIRST.match(rest.lstrip())
            # f 前缀字面量（f"..."）视为动态：拼接名不可静态穷举
            is_fstring = re.match(r"\s*[fF][bB]?['\"]", rest) is not None
            if probe is not None and not is_fstring:
                static.add(probe.group(2))
            else:
                dynamic.setdefault(rel, []).append(line)
    return static, dynamic, total


def test_catalog_shape():
    """目录基本形状：非空、无重复、命名规范（小写点分段）、分组视图覆盖全集。"""
    from eap.observability.audit_actions import AUDIT_ACTIONS, group_by_domain

    assert len(AUDIT_ACTIONS) >= 80, "目录规模应≈全仓留痕动作数（防空转/误删）"
    assert len(set(AUDIT_ACTIONS)) == len(AUDIT_ACTIONS), "目录不得含重复动作"
    for action in AUDIT_ACTIONS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", action), \
            f"动作名不合规范（小写字母/数字/下划线 + 点分段）: {action!r}"
    groups = group_by_domain()
    assert sorted(sum(groups.values(), [])) == sorted(AUDIT_ACTIONS)


def test_static_record_literals_subset_of_catalog():
    """防线 1（正向）：源码里全部静态字面量动作 ⊆ AUDIT_ACTIONS（防新动作漏登记）。"""
    from eap.observability.audit_actions import AUDIT_ACTIONS

    static, dynamic, total = _scan_record_calls()
    assert total >= 90, f"扫描到的留痕调用点过少（{total}），扫描器可能失效（防空转）"

    unregistered = sorted(static - set(AUDIT_ACTIONS))
    assert not unregistered, (
        f"源码存在未登记目录的审计动作字面量: {unregistered}——"
        "请在 src/eap/observability/audit_actions.py 的 AUDIT_ACTIONS 按域登记")

    unlisted_sites = sorted(set(dynamic) - set(DYNAMIC_RECORD_SITES))
    assert not unlisted_sites, (
        f"动态首参留痕调用点未登记白名单: {unlisted_sites}（行号见 {dynamic}）——"
        "请在 tests/test_audit_actions.py 的 DYNAMIC_RECORD_SITES 登记（附注释 + 解析值清单）")

    # 白名单不得腐化：登记的文件必须确有动态站点（防调用点改回字面量后白名单遗留）
    stale = sorted(set(DYNAMIC_RECORD_SITES) - set(dynamic))
    assert not stale, f"DYNAMIC_RECORD_SITES 中 {stale} 已无动态留痕调用点，请移除白名单条目"


def test_catalog_has_no_ghost_actions():
    """防线 2（反向）：目录条目必须对应真实调用点（静态字面量 ∪ 白名单解析值），防腐化。"""
    from eap.observability.audit_actions import AUDIT_ACTIONS

    static, _dynamic, _total = _scan_record_calls()
    referenced = static | set().union(*DYNAMIC_RECORD_SITES.values())
    ghosts = sorted(set(AUDIT_ACTIONS) - referenced)
    assert not ghosts, (
        f"AUDIT_ACTIONS 中 {ghosts} 在源码中已无对应留痕调用点——"
        "动作被改名/删除时请同步移除目录条目（目录 = 调用点的文档镜像）")
