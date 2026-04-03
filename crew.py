"""
crew.py — Агенты CrewAI с полной fallback-цепочкой провайдеров.

Распределение ролей:
  manager  → лучшие reasoning модели (планирование, декомпозиция)
  coder    → лучшие coding модели (qwen3-coder, mimo, gpt-4o)
  tester   → анализирующие модели (minimax, nemotron)
  deployer → быстрые модели (llama, qwen)

Приоритет провайдеров для OpenRouter:
  1. Cerebras (BYOK) — самый быстрый инференс
  2. OpenRouter free models — бесплатные
  3. GitHub Models — GPT-4.1 бесплатно через PAT
  4. AIHubMix — резерв
  5. Groq — резерв

Fallback: при 429 или любой ошибке — следующий провайдер в цепочке.
"""

import os
import time
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM

from .tools import SecureFileWriteTool, SecureDockerTool, GitCommitTool, GitHubTool

load_dotenv()
logger = logging.getLogger(__name__)

MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", 10))

# =============================================================================
# Цепочки провайдер+модель для каждой роли
# Формат: (provider_key, model_id, base_url_or_None)
#
# provider_key используется для получения API ключа из .env
# =============================================================================

# OpenRouter base URL
OR_URL = "https://openrouter.ai/api/v1"

# GitHub Models base URL
GH_URL = "https://models.github.ai/inference"

# AIHubMix base URL
AH_URL = "https://aihubmix.com/v1"

# Цепочки: (env_key_для_апи_ключа, model_id, base_url)
# base_url=None означает нативный провайдер через litellm (Groq, Gemini)

CHAINS = {
    "manager": [
        # Cerebras через OpenRouter BYOK — быстро, мощно
        ("OPENROUTER_API_KEY", "openai/openai/gpt-oss-120b:free", OR_URL),
        # GitHub Models — GPT-4.1 бесплатно
        ("GITHUB_TOKEN",       "openai/gpt-4.1",                  GH_URL),
        # OpenRouter бесплатные
        ("OPENROUTER_API_KEY", "openai/qwen/qwen3-coder:free",     OR_URL),
        ("OPENROUTER_API_KEY", "openai/nvidia/nemotron-3-super-120b-a12b:free", OR_URL),
        # AIHubMix резерв
        ("AIHUBMIX_API_KEY",   "openai/gpt-4.1-free",             AH_URL),
        # Groq резерв
        ("GROQ_API_KEY",       "groq/llama-3.3-70b-versatile",    None),
    ],
    "coder": [
        # qwen3-coder — лучший бесплатный coding model
        ("OPENROUTER_API_KEY", "openai/qwen/qwen3-coder:free",     OR_URL),
        # Cerebras через OR BYOK — быстрый инференс для кода
        ("OPENROUTER_API_KEY", "openai/openai/gpt-oss-120b:free",  OR_URL),
        # GitHub Models — GPT-4.1 хорошо кодирует
        ("GITHUB_TOKEN",       "openai/gpt-4.1",                   GH_URL),
        # AIHubMix mimo — специализирован на коде
        ("AIHUBMIX_API_KEY",   "openai/mimo-v2-flash-free",        AH_URL),
        # AIHubMix GPT-4.1
        ("AIHUBMIX_API_KEY",   "openai/gpt-4.1-free",              AH_URL),
        # Groq Qwen резерв
        ("GROQ_API_KEY",       "groq/qwen/qwen3-32b",              None),
    ],
    "tester": [
        # minimax — SWE-Bench 80.2%, хорошо анализирует
        ("OPENROUTER_API_KEY", "openai/minimax/minimax-m2.5:free", OR_URL),
        # nemotron — большой контекст, анализ кода
        ("OPENROUTER_API_KEY", "openai/nvidia/nemotron-3-super-120b-a12b:free", OR_URL),
        # GitHub Models
        ("GITHUB_TOKEN",       "openai/gpt-4.1",                   GH_URL),
        # AIHubMix mini
        ("AIHUBMIX_API_KEY",   "openai/gpt-4.1-mini-free",         AH_URL),
        # Groq быстрый резерв
        ("GROQ_API_KEY",       "groq/llama-3.3-70b-versatile",     None),
    ],
    "deployer": [
        # qwen3-coder понимает команды деплоя
        ("OPENROUTER_API_KEY", "openai/qwen/qwen3-coder:free",     OR_URL),
        # llama быстрый для простых задач
        ("OPENROUTER_API_KEY", "openai/meta-llama/llama-3.3-70b-instruct:free", OR_URL),
        # GitHub Models
        ("GITHUB_TOKEN",       "openai/gpt-4.1",                   GH_URL),
        # Groq самый быстрый резерв
        ("GROQ_API_KEY",       "groq/llama-3.3-70b-versatile",     None),
        # AIHubMix резерв
        ("AIHUBMIX_API_KEY",   "openai/gpt-4.1-mini-free",         AH_URL),
    ],
}


def _build_llm_with_fallback(role: str, temperature: float) -> LLM:
    """
    Перебирает цепочку провайдеров для роли до первого рабочего.
    При 429 (rate limit) сразу переходит к следующему.
    При других ошибках тоже пробует следующий.
    """
    chain  = CHAINS.get(role, CHAINS["manager"])
    errors = []

    for env_key, model_id, base_url in chain:
        api_key = os.getenv(env_key, "").strip()

        # Пропускаем незаполненные ключи
        if not api_key or "ЗАМЕНИТЕ" in api_key or api_key.startswith("ghp_ВАШ"):
            errors.append(f"{env_key}: ключ не задан")
            continue

        try:
            kwargs = dict(
                model=model_id,
                api_key=api_key,
                temperature=temperature,
                max_tokens=4096,
            )

            if base_url:
                kwargs["base_url"] = base_url

            # Заголовки для OpenRouter (помогает с rate limit идентификацией)
            if base_url == OR_URL:
                kwargs["extra_headers"] = {
                    "HTTP-Referer": "https://ai-factory.local",
                    "X-Title": "AI Factory",
                }

            llm = LLM(**kwargs)
            logger.info(f"[{role}] LLM: {env_key} / {model_id}")
            return llm

        except Exception as e:
            err = str(e)
            if "429" in err or "rate limit" in err.lower():
                logger.warning(f"[{role}] Rate limit: {env_key}/{model_id}, следующий...")
            else:
                logger.warning(f"[{role}] Ошибка {env_key}/{model_id}: {err[:80]}")
            errors.append(f"{env_key}/{model_id}: {err[:60]}")
            continue

    raise RuntimeError(
        f"[{role}] Все провайдеры недоступны.\n"
        f"Ошибки: {'; '.join(errors[-4:])}\n"
        f"Проверьте ключи в .env: OPENROUTER_API_KEY, GITHUB_TOKEN, AIHUBMIX_API_KEY, GROQ_API_KEY"
    )


# =============================================================================
# Создание агентов и Crew
# =============================================================================

def _create_crew(prompt: str, task_id: str) -> Crew:
    tid = task_id

    llm_manager  = _build_llm_with_fallback("manager",  0.2)
    llm_coder    = _build_llm_with_fallback("coder",    0.1)
    llm_tester   = _build_llm_with_fallback("tester",   0.2)
    llm_deployer = _build_llm_with_fallback("deployer", 0.1)

    file_tool   = SecureFileWriteTool()
    docker_tool = SecureDockerTool()
    git_tool    = GitCommitTool()
    github_tool = GitHubTool()

    manager = Agent(
        role="Project Manager",
        goal=(
            f"Декомпозировать запрос на подзадачи и координировать команду. "
            f"task_id={tid}. Убедись что каждый агент получил task_id."
        ),
        backstory="Опытный CTO, управляющий командой AI-разработчиков.",
        llm=llm_manager,
        allow_delegation=True,
        max_iter=MAX_ITERATIONS,
        max_rpm=5,
        verbose=True,
    )

    coder = Agent(
        role="Senior Developer",
        goal=(
            f"Писать production-ready код и сохранять через SecureFileWriteTool. "
            f"Всегда передавай task_id={tid}. "
            f"Запрещено: os.system, subprocess shell=True, eval, exec."
        ),
        backstory="Эксперт-разработчик. Пишет чистый типизированный код.",
        llm=llm_coder,
        tools=[file_tool],
        max_iter=MAX_ITERATIONS,
        max_rpm=5,
        verbose=True,
    )

    tester = Agent(
        role="QA Engineer",
        goal=(
            f"Тестировать код через SecureDockerTool. "
            f"task_id={tid}. Описывай результаты подробно."
        ),
        backstory="Запускает тесты в изолированном Docker sandbox.",
        llm=llm_tester,
        tools=[docker_tool],
        max_iter=MAX_ITERATIONS,
        max_rpm=8,
        verbose=True,
    )

    deployer = Agent(
        role="DevOps Engineer",
        goal=(
            f"Создать репозиторий на GitHub и запушить код через GitHubTool. "
            f"task_id={tid}. repo_name=ai-task-{tid[:8]}."
        ),
        backstory="Деплоит проекты на GitHub.",
        llm=llm_deployer,
        tools=[git_tool, github_tool],
        max_iter=MAX_ITERATIONS,
        max_rpm=5,
        verbose=True,
    )

    tasks = [
        Task(
            description=(
                f"Проанализируй запрос: {prompt}. "
                f"Создай технический план. task_id={tid}. "
                f"Укажи стек, список файлов, команды запуска."
            ),
            agent=manager,
            expected_output="Пошаговый план: файлы, стек, команды, критерии приёмки.",
        ),
        Task(
            description=(
                f"Напиши все файлы согласно плану. "
                f"Вызывай SecureFileWriteTool(task_id={tid}, filename=..., content=...) "
                f"для каждого файла."
            ),
            agent=coder,
            expected_output="Список созданных файлов в workspace.",
        ),
        Task(
            description=(
                f"Протестируй код через SecureDockerTool(task_id={tid}, command=...). "
                f"Опиши: что прошло, что упало, stdout/stderr."
            ),
            agent=tester,
            expected_output="Отчёт: тесты прошли/упали, полный вывод.",
        ),
        Task(
            description=(
                f"Создай GitHub репозиторий и запушь через GitHubTool. "
                f"task_id={tid}, repo_name=ai-task-{tid[:8]}."
            ),
            agent=deployer,
            expected_output="Ссылка на GitHub репозиторий.",
        ),
    ]

    return Crew(
        agents=[manager, coder, tester, deployer],
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
        memory=True,
        cache=True,
    )


# =============================================================================
# Точка входа
# =============================================================================

def run_factory(prompt: str, task_id: str) -> dict:
    logger.info(f"[{task_id[:8]}] Старт: {prompt[:80]}...")
    start = time.time()
    try:
        crew   = _create_crew(prompt, task_id)
        result = crew.kickoff()

        ws    = BASE_WORKSPACE / task_id
        files = [str(f.relative_to(ws)) for f in ws.rglob("*") if f.is_file()] if ws.exists() else []
        elapsed = round(time.time() - start, 1)

        if not files:
            logger.warning(f"[{task_id[:8]}] Агенты не создали ни одного файла!")

        logger.info(f"[{task_id[:8]}] Завершено за {elapsed}с, файлов: {len(files)}")
        return {
            "status":    "success",
            "task_id":   task_id,
            "result":    str(result),
            "files":     files,
            "elapsed":   elapsed,
            "prompt":    prompt[:200],
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        logger.error(f"[{task_id[:8]}] Ошибка: {e}", exc_info=True)
        return {
            "status":    "error",
            "task_id":   task_id,
            "error":     str(e),
            "elapsed":   elapsed,
            "prompt":    prompt[:200],
            "timestamp": datetime.now().isoformat(),
        }
