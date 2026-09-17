"""python -m eap 启动入口。"""

import uvicorn

from .config import get_settings

# 生产必改项：默认值只用于离线开发；命中即打警告（不阻断，开发体验优先）
_INSECURE_DEFAULTS = {
    "dev_api_key": "dev-key-1",
    "session_secret": "dev-session-secret-change-me",
}


def _warn_insecure_defaults() -> None:
    import logging

    s = get_settings()
    log = logging.getLogger("eap.boot")
    for attr, default in _INSECURE_DEFAULTS.items():
        if getattr(s, attr, None) == default:
            log.warning("EAP_%s 仍为开发默认值——生产部署必须更换！", attr.upper())
    if s.cors_origins.strip() == "*":
        log.warning("EAP_CORS_ORIGINS='*' 全放行——仅限本地开发！")
    if s.skill_signing_key is None:
        log.warning("EAP_SKILL_SIGNING_KEY 未配置——技能包签名使用开发默认密钥，生产必须更换！")
    if s.db_url.startswith("sqlite"):
        log.info("使用 SQLite（单机开发形态）；生产请配置 EAP_DB_URL=postgresql+psycopg://…")


def main() -> None:
    s = get_settings()
    _warn_insecure_defaults()
    uvicorn.run("eap.main:app", host=s.host, port=s.port)


if __name__ == "__main__":
    main()
