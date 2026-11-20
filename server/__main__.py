"""Запуск: python -m server [--host 127.0.0.1] [--port 8000]"""
import argparse

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="server", description="API и панель оператора «Сигнал.kg»")
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0 — открыть панель и приложение для телефонов в локальной сети")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    # proxy_headers=False: IP клиента берётся из соединения, заголовкам не доверяем.
    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
