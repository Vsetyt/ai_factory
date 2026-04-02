"""
bot.py — Telegram-бот управления AI Factory.

ИСПРАВЛЕНИЕ: добавлен _watch_tasks() — без него бот никогда не уведомлял
о завершении задач. Пользователь запускал задачу и просто не знал когда готово.
"""

import asyncio
import logging
import sys
import os
import uuid

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from redis import Redis
from rq import Queue
from rq.job import Job
from rq.exceptions import NoSuchJobError
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID       = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
REDIS_HOST     = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT     = int(os.getenv("REDIS_PORT", 6379))
MAX_EXEC_TIME  = int(os.getenv("MAX_EXECUTION_TIME", 900))
LOGS_PATH      = os.getenv("LOGS_PATH", "logs")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

os.makedirs(LOGS_PATH, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_PATH, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

bot        = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp         = Dispatcher()
redis_conn = Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
queue      = Queue("ai_tasks", connection=redis_conn)

ACTIVE_TASKS_KEY = "ai_factory:active_tasks"


def _active_job_count() -> int:
    return len(queue.get_job_ids()) + len(queue.started_job_registry.get_job_ids())


def _remove_task(msg_id: str = None, task_id: str = None) -> None:
    if msg_id:
        redis_conn.hdel(ACTIVE_TASKS_KEY, msg_id)
        return
    if task_id:
        for mid, tid in redis_conn.hgetall(ACTIVE_TASKS_KEY).items():
            if tid == task_id:
                redis_conn.hdel(ACTIVE_TASKS_KEY, mid)
                return


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_result(job: Job) -> dict:
    try:
        r = job.result
        if isinstance(r, dict):
            return r
        return {"status": "unknown", "result": str(r) if r else "Нет данных"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _format_result(task_id: str, result: dict) -> str:
    ok      = result.get("status") == "success"
    icon    = "✅" if ok else "❌"
    elapsed = result.get("elapsed", "")
    elapsed_str = f" ({elapsed}с)" if elapsed else ""

    content = _esc(result.get("result" if ok else "error", "N/A")[:600])

    files_part = ""
    if ok and result.get("files"):
        items = "\n".join(f"  • {_esc(f)}" for f in result["files"][:15])
        files_part = f"\n\n<b>Файлы:</b>\n{items}"

    return (
        f"{icon} <b>Задача завершена</b>{elapsed_str}\n\n"
        f"ID: <code>{task_id[:8]}</code>\n"
        f"Статус: <code>{result.get('status', 'unknown')}</code>\n"
        f"Результат:\n<pre>{content}</pre>"
        f"{files_part}"
    )


# =============================================================================
# Команды
# =============================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 <b>AI Factory Bot</b>\n\n"
        "Доступные команды:\n"
        "/status — список активных задач\n"
        "/stop <code>task_id</code> — остановить задачу\n"
        "/logs <code>task_id</code> — последние логи задачи\n\n"
        "Отправьте описание проекта — фабрика запустится автоматически."
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    active = redis_conn.hgetall(ACTIVE_TASKS_KEY)
    if not active:
        await message.answer("ℹ️ Нет активных задач")
        return
    lines = ["📋 <b>Активные задачи:</b>"]
    for _, task_id in active.items():
        try:
            job = Job.fetch(task_id, connection=redis_conn)
            if job.is_queued:   icon = "🟡 В очереди"
            elif job.is_started: icon = "🟢 Выполняется"
            elif job.is_failed:  icon = "🔴 Ошибка"
            else:                icon = "⚪ Завершена"
        except Exception:
            icon = "❓ Неизвестно"
        lines.append(f"<code>{task_id[:8]}</code> — {icon}")
    await message.answer("\n".join(lines))


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ Используйте: /stop <code>task_id</code>")
        return
    task_id = parts[1].strip()
    try:
        job = Job.fetch(task_id, connection=redis_conn)
        job.cancel()
        _remove_task(task_id=task_id)
        await message.answer(f"✅ Задача <code>{task_id[:8]}</code> остановлена")
    except NoSuchJobError:
        await message.answer("❌ Задача не найдена")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {_esc(str(e))}")


@dp.message(Command("logs"))
async def cmd_logs(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ Используйте: /logs <code>task_id</code>")
        return
    task_id  = parts[1].strip()
    log_file = os.path.join(LOGS_PATH, "worker.log")
    try:
        if not os.path.exists(log_file):
            await message.answer("❌ Файл логов не найден")
            return
        with open(log_file, encoding="utf-8") as f:
            lines = f.readlines()
        relevant = [l.strip() for l in lines if task_id[:8] in l][-20:]
        if not relevant:
            await message.answer(f"ℹ️ Логов для <code>{task_id[:8]}</code> не найдено")
            return
        log_text = _esc("\n".join(relevant))
        await message.answer(f"📋 <b>Логи {task_id[:8]}:</b>\n<pre>{log_text[:3000]}</pre>")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {_esc(str(e))}")


@dp.message()
async def handle_task(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён")
        return

    if _active_job_count() >= MAX_CONCURRENT:
        await message.answer(
            f"⏳ Достигнут лимит задач ({MAX_CONCURRENT}).\n"
            f"Дождитесь завершения или используйте /stop."
        )
        return

    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("📝 Опишите задачу текстом.")
        return

    task_id = str(uuid.uuid4())
    msg_id  = str(message.message_id)

    try:
        from agents.crew import run_factory
        queue.enqueue(
            run_factory, prompt, task_id,
            job_id=task_id,
            job_timeout=MAX_EXEC_TIME,
            result_ttl=86400,
            failure_ttl=86400,
        )
        redis_conn.hset(ACTIVE_TASKS_KEY, msg_id, task_id)
        redis_conn.expire(ACTIVE_TASKS_KEY, 86400 * 7)

        logger.info(f"[{task_id[:8]}] Задача создана: {prompt[:60]}")
        await message.answer(
            f"🚀 <b>Задача принята!</b>\n\n"
            f"ID: <code>{task_id[:8]}</code>\n"
            f"Ожидаемое время: ~{MAX_EXEC_TIME // 60} мин\n"
            f"/status для проверки прогресса"
        )
    except Exception as e:
        logger.error(f"[{task_id[:8]}] Ошибка создания: {e}")
        await message.answer(f"❌ Не удалось создать задачу: {_esc(str(e))}")


# =============================================================================
# Фоновая проверка завершённых задач — уведомляет пользователя
# ИСПРАВЛЕНИЕ: этой функции не было в оригинале — пользователь никогда
# не получал уведомление о результате задачи
# =============================================================================

async def _watch_tasks() -> None:
    while True:
        try:
            for msg_id, task_id in list(redis_conn.hgetall(ACTIVE_TASKS_KEY).items()):
                try:
                    job = Job.fetch(task_id, connection=redis_conn)

                    if job.is_finished:
                        result = _safe_result(job)
                        await bot.send_message(ADMIN_ID, _format_result(task_id, result))
                        _remove_task(msg_id=msg_id)

                    elif job.is_failed:
                        exc = _esc(str(job.exc_info or "нет деталей")[:400])
                        await bot.send_message(
                            ADMIN_ID,
                            f"❌ <b>Задача упала</b>\n\n"
                            f"ID: <code>{task_id[:8]}</code>\n"
                            f"<pre>{exc}</pre>",
                        )
                        _remove_task(msg_id=msg_id)

                except NoSuchJobError:
                    _remove_task(msg_id=msg_id)
                except Exception as e:
                    logger.warning(f"Ошибка проверки {task_id[:8]}: {e}")

        except Exception as e:
            logger.error(f"_watch_tasks error: {e}")

        await asyncio.sleep(30)


# =============================================================================
# Запуск
# =============================================================================

async def main():
    logger.info("AI Factory Bot запускается...")
    asyncio.create_task(_watch_tasks())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    logger.info("Bot остановлен")


if __name__ == "__main__":
    asyncio.run(main())
