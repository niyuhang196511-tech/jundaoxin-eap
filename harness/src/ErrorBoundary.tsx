// M51-E 崩溃最小准备：React ErrorBoundary 包住主视图防白屏。
// React 渲染/生命周期异常不会走 window.onerror 的全部场景（被 boundary 捕获后
// 不再向 window 重抛），故 componentDidCatch 内也 recordCrash（kind=react_render）；
// crash.ts 的 3s 同错去重防双路重复记录。
// 样式对齐现有内联风格（无 UI 库）。
import { Component, type ErrorInfo, type ReactNode } from "react";
import { recordCrash } from "./crash";

type Props = { children: ReactNode };
type State = { failed: boolean; message: string };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, message: "" };

  static getDerivedStateFromError(error: unknown): State {
    return { failed: true, message: error instanceof Error ? error.message : String(error ?? "") };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 栈摘要 = JS 栈 + 组件栈（各截半，crash.ts/Rust 侧再兜底 512）
    const stack = `${error?.stack ?? ""}\n--- componentStack ---\n${info.componentStack ?? ""}`;
    recordCrash("react_render", error?.message ?? "React 渲染异常", stack.slice(0, 512));
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div style={{ fontFamily: "system-ui, sans-serif", padding: 24, display: "flex", flexDirection: "column", gap: 12 }}>
        <h2 style={{ margin: 0, color: "#c0392b" }}>界面异常</h2>
        <p style={{ margin: 0, color: "#555" }}>
          界面遇到未捕获异常，已被隔离防止白屏——本机数据（会话/记忆/审计/技能）不受影响。
        </p>
        {this.state.message && (
          <p style={{ margin: 0, fontSize: 13, color: "#888" }}>异常摘要：{this.state.message.slice(0, 200)}</p>
        )}
        <p style={{ margin: 0, color: "#555" }}>
          异常已记录到本机崩溃诊断——重载后可在「隐私清单 → 崩溃诊断」查看、导出或清除崩溃记录。
        </p>
        <div>
          <button onClick={() => window.location.reload()}>重新加载界面</button>
        </div>
      </div>
    );
  }
}
