"""Базовый класс модуля.

Модуль — отдельная функция бота, которую можно включить и настроить в панели
(вкладка «Модули»). Ядро вызывает хуки только у включённых модулей.
Чтобы добавить свой модуль: унаследуйся от Module, опиши поля и нужные хуки,
затем добавь класс в список MODULES в modules/__init__.py.
"""
import re

from settings import UserError

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_times(raw: str) -> list[tuple[int, int]]:
    """'9:00, 13:30 19:00' -> [(9, 0), (13, 30), (19, 0)]."""
    result = set()
    for part in re.split(r"[,;\s]+", raw or ""):
        if not part:
            continue
        m = TIME_RE.match(part)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise UserError(f"Неверное время «{part}». Пример: 09:00, 13:30")
        result.add((int(m.group(1)), int(m.group(2))))
    return sorted(result)


class Field:
    """Описание настройки модуля. По нему панель сама рисует поле ввода."""

    def __init__(self, key, label, type="text", default="", help="", options=None, min=None, max=None):
        self.key, self.label, self.type, self.default = key, label, type, default
        self.help, self.options, self.min, self.max = help, options or [], min, max

    def to_dict(self, value) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type, "value": value,
                "help": self.help, "options": self.options, "min": self.min, "max": self.max}

    def clean(self, value) -> str:
        v = "" if value is None else str(value).strip()
        if self.type == "bool":
            return "1" if v in ("1", "true", "True", "on") else "0"
        if self.type == "int":
            if not v.lstrip("-").isdigit() or not self.min <= int(v) <= self.max:
                raise UserError(f"«{self.label}»: число от {self.min} до {self.max}")
            return str(int(v))
        if self.type == "select":
            if v not in [o[0] for o in self.options]:
                raise UserError(f"«{self.label}»: недопустимое значение")
            return v
        if self.type == "times":
            times = parse_times(v)
            return ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
        return v


class Module:
    id = ""
    name = ""
    description = ""
    default_enabled = False
    fields: list[Field] = []

    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ---------- настройки ----------
    def _key(self, key) -> str:
        return f"mod.{self.id}.{key}"

    def field(self, key):
        return next((f for f in self.fields if f.key == key), None)

    @property
    def enabled(self) -> bool:
        return self.db.get(self._key("enabled"), "1" if self.default_enabled else "0") == "1"

    def get(self, key) -> str:
        f = self.field(key)
        return self.db.get(self._key(key), f.default if f else "")

    def geti(self, key) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return 0

    def set(self, key, value):
        self.db.set(self._key(key), value)

    def save(self, data: dict):
        changes = {}
        if "enabled" in data:
            changes["enabled"] = "1" if str(data["enabled"]) in ("1", "true", "True") else "0"
        for f in self.fields:
            if f.key in data:
                changes[f.key] = f.clean(data[f.key])
        self.validate(changes)
        was = self.enabled
        for key, value in changes.items():
            self.set(key, value)
        if self.enabled != was:
            self.app.event("info", f"Модуль «{self.name}» {'включён' if self.enabled else 'выключен'}")

    def validate(self, changes: dict):
        """Дополнительная проверка настроек (переопределить при необходимости)."""

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "enabled": self.enabled,
                "fields": [f.to_dict(self.get(f.key)) for f in self.fields],
                "info": self.panel_info() if self.enabled else None}

    # ---------- хуки (все необязательные) ----------
    async def on_tick(self):
        """Вызывается примерно раз в 30 секунд."""

    def prompt_extra(self, kind: str, p=None) -> str:
        """Добавка к системному промпту. kind: 'propose' или 'rewrite' (тогда p — предложение)."""
        return ""

    async def on_proposal_created(self, pid: int, posts):
        """Новое предложение создано. posts — посты-источники (строки из БД)."""

    def preview_lines(self, p) -> list[str]:
        """Строки, которые добавляются в карточку предложения у админа."""
        return []

    def approve_label(self, p):
        """Свой текст для главной кнопки («Опубликовать»), если модуль её перехватывает."""
        return None

    async def intercept_approve(self, p):
        """Перехват «Опубликовать». Вернуть строку-ответ, если модуль обработал сам, иначе None."""
        return None

    def buttons(self, p) -> list[list[tuple[str, str]]]:
        """Дополнительные кнопки под предложением: [[(текст, действие), ...], ...]."""
        return []

    async def on_action(self, action: str, pid=None, data=None):
        """Нажатие кнопки модуля (в Telegram или в панели). Вернуть текст ответа."""
        raise UserError("Неизвестное действие")

    async def send_post(self, target, html_text: str, p) -> bool:
        """Своя отправка поста в канал. True — модуль отправил сам."""
        return False

    def panel_info(self):
        """Данные для блока модуля в панели."""
        return None
