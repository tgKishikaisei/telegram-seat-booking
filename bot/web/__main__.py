"""Запуск веб-сервера мини-приложения: python -m bot.web

Слушает 0.0.0.0:8000 внутри контейнера; снаружи доступен только через Caddy (HTTPS).
Заголовкам X-Forwarded-* доверяем, потому что порт веб-приложения не опубликован наружу —
к нему может обратиться только Caddy из внутренней сети web.
"""

import uvicorn

from bot.web.app import create_web_app

if __name__ == "__main__":
    uvicorn.run(create_web_app(), host="0.0.0.0", port=8000, proxy_headers=True,  # nosec B104 — внутри контейнера
                forwarded_allow_ips="*", server_header=False, log_config=None)
