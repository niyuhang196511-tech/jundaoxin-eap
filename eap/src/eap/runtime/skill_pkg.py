"""技能包（docs/04 §3，M2-M3 技能市场地基）：SKILL.md 解析 + Ed25519 签名/验签。

- 打包：技能记录 → SKILL.md（YAML frontmatter + 正文）→ 签名 bundle（分发格式）
- 导入：验签（平台公钥或外部私钥对）→ 结构校验 → 入库（默认停用，人工审查后启用）
- 签名密钥：EAP_SKILL_SIGNING_KEY（32 字节 hex）；未配置用开发默认密钥（生产必换），
  公钥经 /api/v1/skills/public-key 分发给 Harness 做本地校验。
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone

FORMAT = "eap-skill/1"
_REQUIRED_FIELDS = ("name", "version", "instructions")


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


def sign_bundle(skill: dict, seed_hex: str | None = None) -> dict:
    """技能 → 签名分发 bundle（SKILL.md 供人读，signature 供机器验）。"""
    key = _signing_key(seed_hex)
    payload = _payload_bytes(skill)
    return {
        "format": FORMAT,
        "skill": skill,
        "skill_md": render_skill_md(skill),
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signature": base64.b64encode(key.sign(payload)).decode(),
    }


def verify_bundle(bundle: dict, seed_hex: str | None = None) -> dict:
    """验签 + 结构校验；通过返回技能 dict，否则抛 PermissionError（EAP-8101）。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

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
    return skill
