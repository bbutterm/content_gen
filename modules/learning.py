"""Модуль «Обучение на правках»: показывает нейросети одобренные посты и отклонённые темы."""
import time

from settings import UserError

from .base import Field, Module


class LearningModule(Module):
    id = "learning"
    name = "Обучение на правках"
    description = ("Бот запоминает, что ты опубликовал и как поправил текст, и показывает нейросети "
                   "лучшие примеры — черновики постепенно приближаются к твоему стилю. "
                   "Пропущенные новости учитываются как неинтересные темы.")
    fields = [
        Field("examples", "Сколько примеров показывать нейросети", "int", "3", min=0, max=8,
              help="Сначала берутся посты, которые ты правил руками. Больше примеров — точнее стиль, но дороже запрос."),
        Field("use_skipped", "Учитывать пропущенные темы", "bool", "1"),
        Field("skipped", "Сколько последних пропущенных тем учитывать", "int", "15", min=0, max=50),
    ]

    def since(self) -> int:
        return self.geti("since")

    def examples(self):
        n = self.geti("examples")
        return self.db.published_examples(self.since(), n) if n else []

    def prompt_extra(self, kind, p=None):
        parts = []
        examples = self.examples()
        if examples:
            body = "\n\n---\n\n".join(e["text"][:900] for e in examples)
            parts.append("Примеры постов, которые редактор одобрил и опубликовал (с его правками). "
                         "Пиши в таком же стиле, тоне и формате, но не копируй их содержание:\n\n" + body)
        if kind == "propose" and self.get("use_skipped") == "1" and self.geti("skipped"):
            titles = self.db.skipped_titles(self.since(), self.geti("skipped"))
            if titles:
                parts.append("Новости на такие темы редактор отклонял — похожие не предлагай, "
                             "если событие не действительно важное:\n" + "\n".join(f"- {t}" for t in titles))
        return "\n\n".join(parts)

    async def on_action(self, action, pid=None, data=None):
        if action == "reset":
            self.set("since", int(time.time()))
            self.app.event("info", "Обучение сброшено: старые примеры больше не используются")
            return "Обучение сброшено"
        raise UserError("Неизвестное действие")

    def panel_info(self):
        counts = self.db.learning_counts(self.since())
        return {
            **counts,
            "since": self.since() or None,
            "examples": [{"id": e["id"], "title": e["text"].split("\n", 1)[0][:90],
                          "edited": (e.get("original_text") or e["text"]) != e["text"]}
                         for e in self.examples()],
        }
