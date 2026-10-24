"""Логи Cloud Functions: одна строка JSON на запись в stdout (спец. 13, 17).

Поля: ``level``, ``logger``, ``message``, ``user_id``, ``error_kind`` (из ``extra``, иначе null)
и ``exc_info`` — текст трассировки, если он есть. Байты фото и ответы модели сюда не пишутся.
"""
from __future__ import annotations

import json
import logging
import sys

_installed = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "user_id": getattr(record, "user_id", None),
            "error_kind": getattr(record, "error_kind", None),
        }
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            data["exc_info"] = record.exc_text
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """Корневой логгер: уровень INFO и обработчик JSON в stdout. Повторный вызов ничего не делает."""
    global _installed
    root = logging.getLogger()
    if _installed and any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _installed = True
