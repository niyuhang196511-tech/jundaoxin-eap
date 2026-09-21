"""技能包（docs/04 §3，M2-M3 技能市场地基）：SKILL.md 解析 + Ed25519 签名/验签。

- 打包：技能记录 → SKILL.md（YAML frontmatter + 正文）→ 签名 bundle（分发格式）
- 导入：验签（平台公钥或外部私钥对）→ 结构校验 → 入库（默认停用，人工审查后启用）
- 签名密钥：EAP_SKILL_SIGNING_KEY（32 字节 hex）；未配置用开发默认密钥（生产必换），
  公钥经 /api/v1/skills/public-key 分发给 Harness 做本地校验。
- 附件（M34，L5 工程部分）：bundle 在 SKILL.md 之外支持 scripts/（仅 .py，执行属
  M33 沙箱 script_tool 范畴——本模块只分发不执行）与 assets/（白名单扩展）子目录。
  逐文件 sha256 清单写入 skill["assets"] 随签名一起覆盖（附件不入签即为漏洞），
  文件本体以 base64 存 bundle["files"]；导入端按清单逐一复核 sha256，并执行
  M28 bundles 同款安全面：拒绝绝对路径/`..`、扩展白名单、单文件 ≤1MB、总数 ≤10、
  总量 ≤5MB、落盘路径 resolve 锁定包内（见 runtime/skill_files.py）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import datetime, timezone

FORMAT = "eap-skill/1"
_REQUIRED_FIELDS = ("name", "version", "instructions")

# ---------- 附件（M34）常量：安全上限镜像 M28 bundles，尺寸收紧（附件非代码） ----------
SCRIPTS_DIR = "scripts"
ASSETS_DIR = "assets"
SCRIPT_EXTENSIONS = (".py",)  # scripts/ 仅 .py；执行一律经 M33 沙箱 script_tool
ASSET_EXTENSIONS = frozenset({".png", ".jpg", ".svg", ".csv", ".json", ".md", ".txt"})
MAX_ASSET_FILE_BYTES = 1 * 1024 * 1024   # 单文件 ≤1MB
MAX_ASSETS_TOTAL_BYTES = 5 * 1024 * 1024  # 总量 ≤5MB
MAX_ASSET_FILES = 10                      # 总数 ≤10
_SCRIPT_NAME_RE = re.compile(r"[A-Za-z0-9_]+\.py")


# ---------- SKILL.md 解析 / 生成 ----------

def render_skill_md(skill: dict) -> str:
    """技能 → SKILL.md（YAML frontmatter + 正文；字符串值统一加引号防注入）。"""
    meta = {
        "name": skill.get("name", ""),
        "version": skill.get("version", "1.0.0"),
        "description": skill.get("description", ""),
        "permissions": list(skill.get("permissions") or []),
    }
    lines = ["---"]
    for key, value in meta.items():
        if key == "permissions":
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f'{key}: "{str(value).replace(chr(34), chr(39))}"')
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (skill.get("instructions") or "")


def parse_skill_md(text: str) -> dict:
    """SKILL.md → 技能 dict；缺必备字段或 frontmatter 格式错误抛 ValueError（EAP-8102）。"""
    if not text.startswith("---"):
        raise ValueError("EAP-8102 SKILL.md 缺少 YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("EAP-8102 SKILL.md frontmatter 未闭合")
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            meta[key] = [v.strip() for v in raw[1:-1].split(",") if v.strip()]
        else:
            meta[key] = raw.strip('"').strip("'")
    for field in _REQUIRED_FIELDS:
        if field != "instructions" and not meta.get(field):
            raise ValueError(f"EAP-8102 SKILL.md 缺少必备字段 {field}")
    instructions = parts[2].lstrip("\n")
    if not instructions.strip():
        raise ValueError("EAP-8102 SKILL.md 缺少必备字段 instructions（正文为空）")
    return {"name": str(meta["name"]), "version": str(meta.get("version") or "1.0.0"),
            "description": str(meta.get("description") or ""),
            "permissions": list(meta.get("permissions") or []),
            "instructions": instructions}


# ---------- 附件（M34）：路径安全面 + sha256 清单 ----------

def validate_asset_path(path: str) -> str:
    """附件相对路径安全校验（镜像 M28 bundles.read_bundle 安全面），通过返回规范化路径。

    - 反斜杠归一为 `/`；拒绝绝对路径（`/` 开头或盘符）与含 `..`/空段的路径
    - 必须位于 scripts/（仅 .py，文件名与 M28 python 白名单同法）或 assets/（扩展白名单）
    """
    normalized = (path or "").replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized.split("/")[0]:
        raise ValueError(f"EAP-8104 非法附件路径: {path!r}")
    segments = normalized.split("/")
    if ".." in segments or "" in segments or "." in segments:
        raise ValueError(f"EAP-8104 非法附件路径: {path!r}")
    if normalized.startswith(f"{SCRIPTS_DIR}/"):
        base = segments[-1]
        if not _SCRIPT_NAME_RE.fullmatch(base):
            raise ValueError(f"EAP-8104 scripts/ 仅允许 .py 文件（执行属 M33 沙箱 script_tool）: {base!r}")
    elif normalized.startswith(f"{ASSETS_DIR}/"):
        ext = os.path.splitext(segments[-1])[1].lower()
        if ext not in ASSET_EXTENSIONS:
            raise ValueError(f"EAP-8104 assets/ 扩展名不在白名单 {sorted(ASSET_EXTENSIONS)}: {ext!r}")
    else:
        raise ValueError(f"EAP-8104 附件必须位于 {SCRIPTS_DIR}/ 或 {ASSETS_DIR}/ 子目录: {path!r}")
    return normalized


def build_asset_manifest(files) -> list[dict]:
    """附件 [(相对路径, 字节)] → 签名清单 [{path, size, sha256}]；安全面全检（EAP-8104）。

    清单随 skill["assets"] 进入签名 payload（附件不入签即为漏洞）；导入端以本函数
    重建清单并与签名清单比对，实现「清单 ↔ 实际文件」双向绑定。
    """
    files = list(files)
    if len(files) > MAX_ASSET_FILES:
        raise ValueError(f"EAP-8104 附件数量超过上限 {MAX_ASSET_FILES}")
    manifest: list[dict] = []
    seen: set[str] = set()
    total = 0
    for path, content in files:
        normalized = validate_asset_path(path)
        if normalized in seen:
            raise ValueError(f"EAP-8104 附件路径重复: {normalized}")
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError(f"EAP-8104 附件内容须为字节: {normalized}")
        if len(content) > MAX_ASSET_FILE_BYTES:
            raise ValueError(f"EAP-8104 单个附件超过 {MAX_ASSET_FILE_BYTES // 1024 // 1024}MB 上限: {normalized}")
        total += len(content)
        if total > MAX_ASSETS_TOTAL_BYTES:
            raise ValueError(f"EAP-8104 附件总量超过 {MAX_ASSETS_TOTAL_BYTES // 1024 // 1024}MB 上限")
        seen.add(normalized)
        manifest.append({"path": normalized, "size": len(content),
                         "sha256": hashlib.sha256(content).hexdigest()})
    return manifest


def decode_bundle_files(bundle: dict) -> list[tuple[str, bytes]]:
    """bundle["files"]（[{path, content_b64}]）→ [(路径, 字节)]；无 files 返回 []。"""
    files = bundle.get("files") if isinstance(bundle, dict) else None
    if files is None:
        return []
    if not isinstance(files, list):
        raise ValueError("EAP-8104 附件 files 字段格式非法")
    decoded: list[tuple[str, bytes]] = []
    for item in files:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("content_b64"), str)):
            raise ValueError("EAP-8104 附件 files 条目须为 {path, content_b64}")
        try:
            content = base64.b64decode(item["content_b64"], validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"EAP-8104 附件 {item['path']} 内容不是合法 base64") from e
        decoded.append((item["path"], content))
    return decoded


def _verify_assets_integrity(skill: dict, bundle: dict) -> None:
    """附件完整性（M34）：清单在签名覆盖内（验签之后才走到这里），文件按清单逐一复核。"""
    files = bundle.get("files")
    manifest = skill.get("assets")
    if not manifest and not files:
        return
    if files and not manifest:
        raise ValueError("EAP-8104 技能包含附件文件但缺少签名清单（清单必须入签）")
    if manifest and not files:
        raise ValueError("EAP-8104 技能包含附件清单但缺少附件文件")
    if build_asset_manifest(decode_bundle_files(bundle)) != manifest:
        raise ValueError("EAP-8104 附件清单与实际文件不符（path/size/sha256 不一致）")


# ---------- 签名 / 验签（Ed25519） ----------

def _signing_key(seed_hex: str | None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if seed_hex:
        try:
            seed = bytes.fromhex(seed_hex)
        except ValueError as e:
            raise ValueError("EAP-8103 EAP_SKILL_SIGNING_KEY 须为 32 字节 hex") from e
        if len(seed) != 32:
            raise ValueError("EAP-8103 EAP_SKILL_SIGNING_KEY 须为 32 字节 hex")
    else:
        seed = b"eap-dev-skill-signing-key-0123456789"[:32]  # 开发默认密钥（生产必换）
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_hex(seed_hex: str | None = None) -> str:
    key = _signing_key(seed_hex).public_key()
    return key.public_bytes_raw().hex()


def _payload_bytes(skill: dict) -> bytes:
    canonical = json.dumps({"format": FORMAT, "skill": skill},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return canonical.encode()


def sign_bundle(skill: dict, seed_hex: str | None = None,
                files: list[tuple[str, bytes]] | None = None) -> dict:
    """技能 → 签名分发 bundle（SKILL.md 供人读，signature 供机器验）。

    files（M34）：scripts/ 与 assets/ 附件 [(相对路径, 字节)]——先过安全面
    （validate_asset_path + 大小/数量上限），逐文件 sha256 清单写入 skill["assets"]
    随签名一起覆盖，文件本体以 base64 存 bundle["files"]。scripts/ 仅收集分发，
    执行属 M33 沙箱 script_tool 范畴，本模块不自动执行。
    """
    key = _signing_key(seed_hex)
    signed = dict(skill)
    bundle_files: list[dict] = []
    if files:
        manifest = build_asset_manifest(files)
        signed["assets"] = manifest
        # build_asset_manifest 保序，与 files 一一对应
        bundle_files = [{"path": item["path"], "content_b64": base64.b64encode(content).decode()}
                        for item, (_, content) in zip(manifest, files)]
    payload = _payload_bytes(signed)
    bundle = {
        "format": FORMAT,
        "skill": signed,
        "skill_md": render_skill_md(skill),
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signature": base64.b64encode(key.sign(payload)).decode(),
    }
    if bundle_files:
        bundle["files"] = bundle_files
    return bundle


def verify_bundle(bundle: dict, seed_hex: str | None = None) -> dict:
    """验签 + 结构校验 + 附件完整性（M34）；通过返回技能 dict，否则抛
    PermissionError（EAP-8101，签名层）或 ValueError（EAP-8104，附件安全面/清单一致）。"""
    from cryptography.exceptions import InvalidSignature

    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise PermissionError(f"EAP-8101 不支持的技能包格式：{bundle.get('format')!r}")
    skill = bundle.get("skill")
    signature = bundle.get("signature")
    if not isinstance(skill, dict) or not isinstance(signature, str):
        raise PermissionError("EAP-8101 技能包缺少 skill 或 signature 字段")
    for field in _REQUIRED_FIELDS:
        if not skill.get(field):
            raise PermissionError(f"EAP-8101 技能包缺少必备字段 {field}")
    try:
        _signing_key(seed_hex).public_key().verify(
            base64.b64decode(signature), _payload_bytes(skill))
    except (InvalidSignature, binascii.Error) as e:
        raise PermissionError("EAP-8101 技能包签名校验失败：内容被篡改或来源不受信") from e
    _verify_assets_integrity(skill, bundle)  # 清单已入签：篡改清单在上一步即 401
    return skill
