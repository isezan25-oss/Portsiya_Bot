"""
Рассылка объявлений: один текст всем, кто пользуется ботом.

Отправка идёт по журналу, а не просто циклом по пользователям. Причина
конкретная: объявления отправляются сразу после обновления бота, а обновление
на Railway — это перезапуск процесса. Рассылка, прерванная на середине, без
журнала продолжения не имеет: половина получила, половина нет, и кто именно —
неизвестно. С журналом остаток досылается командой.

Заблокировавшие бота — не ошибка рассылки, а нормальный её исход, поэтому они
считаются отдельно и при досылке не перебираются заново.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest,
                                TelegramForbiddenError, TelegramRetryAfter)

# Телеграм разрешает около 30 сообщений в секунду на разные чаты. Берём вдвое
# меньше: рассылка не срочная, а 429 стоит дороже, чем лишние секунды.
PAUSE = 0.08
RETRY_LIMIT = 3

Send = Callable[[int, str], Awaitable[None]]
Mark = Callable[[int, str, str], None]


@dataclass
class Report:
    sent: int = 0
    blocked: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.blocked + self.failed

    def line(self) -> str:
        parts = [f"доставлено {self.sent}"]
        if self.blocked:
            parts.append(f"заблокировали бота {self.blocked}")
        if self.failed:
            parts.append(f"ошибок {self.failed}")
        return ", ".join(parts)


async def _send_one(send: Send, tg_id: int, text: str) -> tuple[str, str]:
    """Отправляет одному человеку. Возвращает статус и подробность к нему."""
    for _ in range(RETRY_LIMIT):
        try:
            await send(tg_id, text)
            return "sent", ""
        except TelegramRetryAfter as e:
            # Телеграм сам говорит, сколько ждать. Ждём и пробуем снова.
            logging.warning("Рассылка: 429, пауза %s с", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            # Заблокировал бота, удалил чат или аккаунт.
            return "blocked", str(e)[:200]
        except TelegramAPIError as e:
            return "failed", str(e)[:200]
    return "failed", "подряд несколько ответов «слишком часто»"


async def deliver(send: Send, mark: Mark, targets: Iterable[int], text: str,
                  pause: float = PAUSE) -> Report:
    """Рассылает текст и отмечает каждый исход через mark, а не в конце.

    Отметка сразу — чтобы перезапуск процесса на середине терял одно
    сообщение, а не весь остаток списка."""
    rep = Report()
    for tg_id in targets:
        status, detail = await _send_one(send, tg_id, text)
        mark(tg_id, status, detail)
        setattr(rep, status, getattr(rep, status) + 1)
        await asyncio.sleep(pause)
    logging.info("Рассылка завершена: %s", rep.line())
    return rep
