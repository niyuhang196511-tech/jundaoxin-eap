// M51-E 性能度量三层之「采集」（P3 #27 销项）：performance.now 轻量计时包装 +
// 本地 audit 表写入（Rust record_perf 命令：action=perf.<类别>，detail=紧凑 JSON）。
//
// 纪律：采集失败绝不影响主流程——recordPerf 内部 try/catch + Promise.catch 双静默，
// 调用点一律 `recordPerf(...)`（fire-and-forget，不 await、不检查返回）。
// detail ≤512 字符：前端预截断（省上报流量），Rust 写入侧与平台 audit.py 各有兜底截断。
//
// perf 类别与 detail JSON 形状（MonitorView「本机性能」聚合展示依赖这些键名）：
// - platform_stream  {"ms","ttfb_ms","bytes","agent"}   平台流式调用（TTFB+总时长）
// - local_model      {"ms","ttfb_ms","bytes","model"}   本地模型流式调用
// - skill_pull       {"ms","bytes","name"}             技能包拉取（包大小可得时）
// - agent_list       {"ms","count"}                    Agent 目录拉取
// - audit_report     {"ms","count","accepted"}         审计上报批次（条数+接受数）
// - update_download  {"ms","bytes","kb_per_sec"}       更新下载（Rust 侧 updater.rs 直写，
//                    Windows NSIS 安装即退进程，前端 invoke 有竞态丢行风险——单写者在 Rust）
import { invoke } from "@tauri-apps/api/core";

/** detail 上限（对齐平台 audit.py detail[:512]；JS slice 按 UTF-16 码元 ≤ Python 按字符数，只会更短） */
export const DETAIL_MAX = 512;

export function truncate512(s: string): string {
  return s.length > DETAIL_MAX ? s.slice(0, DETAIL_MAX) : s;
}

/** 计时起点：performance.now 单调时钟（不受系统时间调整影响） */
export function mark(): number {
  return performance.now();
}

/** 自起点经过的毫秒数（四舍五入整数；负值防御归 0） */
export function elapsedMs(t0: number): number {
  return Math.max(0, Math.round(performance.now() - t0));
}

export type PerfDetail = Record<string, number | string | null | undefined>;

/** 写 perf 审计行（静默、fire-and-forget）：undefined 值剔除，JSON 紧凑序列化 + 512 预截断 */
export function recordPerf(category: string, detail: PerfDetail): void {
  try {
    const clean: Record<string, number | string | null> = {};
    for (const [k, v] of Object.entries(detail)) {
      if (v !== undefined) clean[k] = v;
    }
    void invoke("record_perf", { category, detail: truncate512(JSON.stringify(clean)) }).catch(
      () => {
        /* 静默：本地库写入失败不打扰任何主流程 */
      },
    );
  } catch {
    /* 静默：JSON.stringify 异常（病理值）等一律吞掉 */
  }
}
