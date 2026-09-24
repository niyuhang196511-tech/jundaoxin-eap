import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import ErrorBoundary from "./ErrorBoundary";
import { installGlobalCrashHandlers } from "./crash";

// M51-E 崩溃最小准备：全局错误钩子先于 React 挂载安装（window error/unhandledrejection
// → crash_record 双写日志+审计）；ErrorBoundary 包住主视图防白屏（fallback 引导隐私页崩溃诊断）
installGlobalCrashHandlers();

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
