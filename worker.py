"""
worker.py — RQ-воркер, обрабатывает очередь ai_tasks.
"""

import os
import sys
import logging
from redis import Redis
from rq import Worker, Queue
from dotenv import load_dotenv

load_dotenv()

LOGS_PATH = os.getenv("LOGS_PATH", "logs")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

os.makedirs(LOGS_PATH, exist_ok=True)

fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
root = logging.getLogger()
root.setLevel(logging.INFO)

fh = logging.FileHandler(os.path.join(LOGS_PATH, "worker.log"), encoding="utf-8")
fh.setFormatter(fmt)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(fmt)

root.addHandler(fh)
root.addHandler(sh)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    try:
        redis_conn = Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        q = Queue("ai_tasks", connection=redis_conn)

        logger.info(f"Воркер запущен. Redis: {REDIS_HOST}:{REDIS_PORT}")

        worker = Worker([q], connection=redis_conn)
        worker.work(burst=False)
    except Exception as e:
        logger.error(f"Критическая ошибка в worker: {e}", exc_info=True)
        sys.exit(1)