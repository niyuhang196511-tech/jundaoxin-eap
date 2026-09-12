"""python -m eap 启动入口。"""

import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run("eap.main:app", host=s.host, port=s.port)


if __name__ == "__main__":
    main()
