"""Реестр модулей. Новый модуль — новый файл в этой папке + строчка в MODULES."""
from .breaking import BreakingModule
from .learning import LearningModule
from .media import MediaModule
from .queue import QueueModule

MODULES = [MediaModule, QueueModule, LearningModule, BreakingModule]


def load(app) -> dict:
    return {cls.id: cls(app) for cls in MODULES}
