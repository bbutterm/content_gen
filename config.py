"""Минимальные настройки запуска из .env. Всё остальное настраивается в веб-панели."""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _path(value: str) -> str:
    return value if os.path.isabs(value) else os.path.join(BASE_DIR, value)


# Пароль от веб-панели. Если не задан — сгенерируется и будет напечатан в лог.
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "").strip()
PANEL_HOST = os.getenv("PANEL_HOST", "0.0.0.0").strip()
PANEL_PORT = int(os.getenv("PANEL_PORT", "8080"))
# Адрес, по которому панель открывается снаружи (бот присылает его по команде /panel)
PANEL_URL = os.getenv("PANEL_URL", "").strip().rstrip("/")

DB_PATH = _path(os.getenv("DB_PATH", "data/newsbot.db"))
TZ = os.getenv("TZ_NAME", "Europe/Moscow").strip()  # для «тихих часов»

MIN_POST_LEN = 40          # посты короче пропускаются
FIRST_FETCH_LIMIT = 20     # при первом сборе нового канала
FIRST_FETCH_HOURS = 24
