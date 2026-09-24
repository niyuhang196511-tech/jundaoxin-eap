// M51-E 崩溃最小准备（选型无关）之 JS 侧捕获：window.onerror + unhandledrejection
// → Rust crash_record 命令（双写 crashes/ 日志文件 + crash.<kind> 审计行）。
//
// 诚实局限：覆盖面 = JS 运行时错误 + Rust panic（crashes.rs）；**不覆盖**进程级
// abort/栈溢出与 WebView2 渲染进程崩溃（Windows SEH/独立进程层面）——Sentry/
// breakpad 级全覆盖属上报选型确定后事项（P3 #26 选型半区）。
//
// 隐私边界：崩溃记录默认仅存本机；出端只在用户开启隐私页「审计上报」开关后随
// crash.* 审计行走既有 POST /api/v1/audit/harness-report（消息摘要 ≤512 字符，
// 不含对话内容；crashes/*.log 文件本体永不出端）。
import { invoke } from "@tauri-apps/api/core";

/** message/stack 摘要上限（对齐审计 detail 512 语义；Rust 侧再兜底截断） */
const MAX_SUMMARY = 512;

// 同一错误去重：onerror 与 React ErrorBoundary（componentDidCatch 内 recordCrash）
// 可能对同一次异常双路触发——3s 窗口内同 kind+message 只记一次，防日志刷屏
let lastKey = "";
let lastAt = 0;

/** 记录一次崩溃（fire-and-forget、全静默——捕获路径自身绝不再抛） */
export function recordCrash(kind: string, message: string, stack?: string | null): void {
  try {
    const key = `${kind}:${message}`;
    const nowMs = Date.now();
    if (key === lastKey && nowMs - lastAt < 3000) return;
    lastKey = key;
    lastAt = nowMs;
    void invoke("crash_record", {
      kind,
      message: message.slice(0, MAX_SUMMARY),
      stack: (stack ?? "").slice(0, MAX_SUMMARY),
    }).catch(() => {
      /* 静默：Rust 侧不可达（如进程正在退出）不影响页面 */
    });
  } catch {
    /* 静默 */
  }
}

/** 全局错误钩子（main.tsx 模块顶层安装一次）：addEventListener 而非 onerror= 赋值，
 *  不覆盖宿主/未来其他监听；error 事件不阻止默认行为（控制台照常打印）。 */
export function installGlobalCrashHandlers(): void {
  window.addEventListener("error", ev => {
    const err = ev.error as { message?: string; stack?: string } | null;
    recordCrash("js_error", String(err?.message ?? ev.message ?? "unknown error"), err?.stack ?? null);
  });
  window.addEventListener("unhandledrejection", ev => {
    const reason = (ev as PromiseRejectionEvent).reason;
    const message =
      reason instanceof Error ? reason.message : String(reason ?? "unknown rejection");
    recordCrash("unhandled_rejection", message, reason instanceof Error ? reason.stack : null);
  });
}
