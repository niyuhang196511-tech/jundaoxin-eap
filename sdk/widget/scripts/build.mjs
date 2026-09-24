#!/usr/bin/env node
/**
 * @eap/widget 构建脚本（M50-C 打包就绪态）：构建期取源，不复制入库。
 *
 * 单一事实源：
 *   - widget 源码  = eap/src/eap/static/eap-widget.js（后端 main.py 同时以
 *     GET /sdk/eap-widget.js 服务端分发——npm 产物与其内容等价，仅多版本横幅头）
 *   - 版本号      = eap/src/eap/__init__.py 的 __version__（与 eap-sdk Python 包同源）
 *
 * 用法：
 *   node scripts/build.mjs           # 构建 dist/eap-widget.js（注入版本横幅）并同步 package.json version
 *   node scripts/build.mjs --check   # CI 漂移守卫：只校验 package.json version == 平台 __version__，不写任何文件
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkgDir = resolve(here, "..");
const repoRoot = resolve(pkgDir, "..", "..");

const PLATFORM_INIT = join(repoRoot, "eap", "src", "eap", "__init__.py");
const WIDGET_SRC = join(repoRoot, "eap", "src", "eap", "static", "eap-widget.js");
const PKG_JSON = join(pkgDir, "package.json");
const OUT_DIR = join(pkgDir, "dist");
const OUT_FILE = join(OUT_DIR, "eap-widget.js");

function fail(msg) {
  console.error(`[widget-build] ${msg}`);
  process.exit(1);
}

function platformVersion() {
  if (!existsSync(PLATFORM_INIT)) fail(`平台版本源不存在：${PLATFORM_INIT}（请在仓库检出内构建）`);
  const m = readFileSync(PLATFORM_INIT, "utf8").match(/__version__\s*=\s*["']([^"']+)["']/);
  if (!m) fail(`无法从 ${PLATFORM_INIT} 解析 __version__`);
  return m[1];
}

const version = platformVersion();
const pkgRaw = readFileSync(PKG_JSON, "utf8");
const pkg = JSON.parse(pkgRaw);

if (process.argv.includes("--check")) {
  if (pkg.version !== version) {
    fail(`版本漂移：package.json=${pkg.version} 平台=${version}——运行 node scripts/build.mjs 同步后提交`);
  }
  console.log(`[widget-build] version check OK: ${version}`);
  process.exit(0);
}

// package.json version 同步（单一事实源=平台版本；构建即同步，CI --check 守卫漂移）
if (pkg.version !== version) {
  const synced = pkgRaw.replace(/("version"\s*:\s*)"[^"]*"/, `$1"${version}"`);
  writeFileSync(PKG_JSON, synced, "utf8");
  console.log(`[widget-build] package.json version 同步 ${pkg.version} → ${version}（请随平台版本变更一并提交）`);
}

// 构建期取源 + 版本横幅注入（dist/ 不入库，.gitignore 覆盖）
if (!existsSync(WIDGET_SRC)) fail(`widget 源不存在：${WIDGET_SRC}`);
const src = readFileSync(WIDGET_SRC, "utf8");
const banner = [
  `/*! @eap/widget v${version} · EAP 嵌入外链 Widget（<eap-chat> / EAP.widget.mount）`,
  " * 构建期取源（单一事实源，勿直接改本产物）：eap/src/eap/static/eap-widget.js",
  " * 服务端分发等价物：GET /sdk/eap-widget.js；用法见包内 README.md 与 docs/14 §四",
  " */",
  "",
].join("\n");
mkdirSync(OUT_DIR, { recursive: true });
writeFileSync(OUT_FILE, banner + src, "utf8");
console.log(`[widget-build] dist/eap-widget.js 生成（v${version}，源 ${WIDGET_SRC}）`);
