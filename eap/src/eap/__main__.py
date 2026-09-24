"""python -m eap 启动入口。"""

import uvicorn

from .config import Settings, get_settings

# 生产必改项：与 config.py 字段默认值同源比较（命中即打警告，不阻断，开发体验优先）
_INSECURE_DEFAULT_ATTRS = ("dev_api_key", "session_secret")


def insecure_config_problems(s: Settings) -> list[str]:
    """收集不安全配置项（M47-C）：警告与 fail-fast（EAP_STRICT_CONFIG=1）共用一份判定。"""
    problems: list[str] = []
    for attr in _INSECURE_DEFAULT_ATTRS:
        if getattr(s, attr, None) == Settings.model_fields[attr].default:
            problems.append(f"EAP_{attr.upper()} 仍为开发默认值")
    if s.cors_origins.strip() == "*":
        problems.append("EAP_CORS_ORIGINS='*' 全放行")
    if s.skill_signing_key is None:
        problems.append("EAP_SKILL_SIGNING_KEY 未配置（技能包签名信任根）")
    if s.secret_key is None:
        problems.append("EAP_SECRET_KEY 未配置（模型/连接器密钥将明文落库）")
    return problems


def _warn_insecure_defaults() -> None:
    import logging

    s = get_settings()
    log = logging.getLogger("eap.boot")
    for attr in _INSECURE_DEFAULT_ATTRS:
        if getattr(s, attr, None) == Settings.model_fields[attr].default:
            log.warning("EAP_%s 仍为开发默认值——生产部署必须更换！", attr.upper())
    if s.cors_origins.strip() == "*":
        log.warning("EAP_CORS_ORIGINS='*' 全放行——仅限本地开发！")
    if s.skill_signing_key is None:
        log.warning("EAP_SKILL_SIGNING_KEY 未配置——技能包签名使用开发默认密钥，生产必须更换！")
    if s.secret_key is None:
        log.warning("EAP_SECRET_KEY 未配置——秘密加密关闭（仅开发）！")
    if s.db_url.startswith("sqlite"):
        log.info("使用 SQLite（单机开发形态）；生产请配置 EAP_DB_URL=postgresql+psycopg://…")


def main() -> None:
    s = get_settings()
    _warn_insecure_defaults()
    # M47-C 启动 fail-fast（deploy/docker-compose.prod.yml 默认开启）：
    # 与警告共用同一份判定，命中即拒绝启动——防止带着开发密钥上生产
    if s.strict_config:
        problems = insecure_config_problems(s)
        if problems:
            import logging
            import sys

            log = logging.getLogger("eap.boot")
            for p in problems:
                log.error("严格配置检查未通过：%s", p)
            log.error("EAP_STRICT_CONFIG=1：请修复以上配置后重启（开发环境请勿开启此开关）")
            sys.exit(78)  # EX_CONFIG：配置错误约定退出码
    uvicorn.run("eap.main:app", host=s.host, port=s.port)


if __name__ == "__main__":
    main()
