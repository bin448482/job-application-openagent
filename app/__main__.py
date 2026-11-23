"""提供 `python -m app` 的进程入口，并返回命令行退出码。"""

from app.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
