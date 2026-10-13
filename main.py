"""Точка входа: веб-панель + бот + расписание в одном процессе."""
import asyncio
import logging
import secrets

from aiohttp import web

import config
from core import App
from db import DB
from web import build_web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("newsbot")


async def main():
    db = DB(config.DB_PATH)

    password = config.PANEL_PASSWORD
    if not password:
        password = db.get("panel_password_generated")
        if not password:
            password = secrets.token_urlsafe(12)
            db.set("panel_password_generated", password)
        log.warning("PANEL_PASSWORD не задан в .env. Пароль от панели: %s", password)

    core = App(db)
    runner = web.AppRunner(build_web(core, password))
    await runner.setup()
    await web.TCPSite(runner, config.PANEL_HOST, config.PANEL_PORT).start()
    log.info("Панель запущена на %s:%s %s", config.PANEL_HOST, config.PANEL_PORT,
             f"({config.PANEL_URL})" if config.PANEL_URL else "")

    await core.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
