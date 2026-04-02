"""
tools.py — Инструменты агентов: запись файлов, Docker sandbox, Git, GitHub.

ИСПРАВЛЕНИЕ GitHubTool:
  Оригинал смешивал PyGithub (API) и gitpython (локальный git) неправильно —
  вызывал repo.git.add() на объекте PyGithub, который не имеет такого метода.
  Исправлено: PyGithub используется только для создания репо через API,
  gitpython — для локального коммита и push через HTTPS с токеном.
"""

import os
import logging
from pathlib import Path
from uuid import UUID

import docker
import git
from crewai.tools import BaseTool
from github import Github, GithubException

from .validators import validate_code, validate_requirements

logger = logging.getLogger(__name__)

BASE_WORKSPACE = Path(os.getenv("WORKSPACE_PATH", "/tmp/workspace")).resolve()
MEMORY_LIMIT   = os.getenv("MEMORY_LIMIT", "512m")
CPU_QUOTA      = int(os.getenv("CPU_QUOTA", 50000))

DOCKER_IMAGES = {
    "python": "python:3.11-slim",
    "node":   "node:20-alpine",
    "go":     "golang:1.21-alpine",
}


def _validate_task_id(task_id: str) -> bool:
    try:
        UUID(task_id, version=4)
        return True
    except (ValueError, AttributeError):
        return False


def get_task_workspace(task_id: str) -> Path:
    if not _validate_task_id(task_id):
        raise ValueError(f"Невалидный task_id: {task_id}")
    ws = BASE_WORKSPACE / task_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _is_path_safe(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _ensure_docker_network(client: docker.DockerClient) -> str:
    name = "ai_factory_sandbox"
    try:
        client.networks.get(name)
    except docker.errors.NotFound:
        try:
            client.networks.create(name, driver="bridge", internal=True,
                                   labels={"managed_by": "ai_factory"})
        except docker.errors.APIError as e:
            if "already exists" not in str(e):
                raise
    return name


# =============================================================================
# ИНСТРУМЕНТ 1: Запись файлов
# =============================================================================

class SecureFileWriteTool(BaseTool):
    name: str = "Secure Write File"
    description: str = (
        "Записывает файл в workspace задачи с валидацией. "
        "Аргументы: task_id (UUID str), filename (str), content (str)."
    )

    def _run(self, task_id: str, filename: str, content: str) -> str:
        if not _validate_task_id(task_id):
            return f"❌ Невалидный task_id: {task_id}"
        try:
            ws     = get_task_workspace(task_id)
            target = (ws / filename).resolve()

            if not _is_path_safe(ws, target):
                return f"❌ Отказано: path traversal ({filename})"

            if filename.endswith(".py"):
                ok, msg = validate_code(content, filename)
                if not ok:
                    return f"❌ Валидация кода: {msg}"

            if filename == "requirements.txt":
                ok, msg = validate_requirements(content)
                if not ok:
                    return f"❌ Валидация requirements: {msg}"

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            logger.info(f"[{task_id[:8]}] Записан: {filename}")
            return f"✅ {filename} записан в workspace/{task_id[:8]}/"

        except Exception as e:
            logger.error(f"[{task_id[:8]}] Ошибка записи {filename}: {e}")
            return f"❌ Ошибка: {e}"


# =============================================================================
# ИНСТРУМЕНТ 2: Docker sandbox
# =============================================================================

class SecureDockerTool(BaseTool):
    name: str = "Secure Docker Sandbox"
    description: str = (
        "Запускает команду в изолированном Docker-контейнере. "
        "Аргументы: task_id (UUID str), command (str), "
        "language ('python'|'node'|'go', default='python')."
    )

    def _run(self, task_id: str, command: str, language: str = "python") -> str:
        if not _validate_task_id(task_id):
            return f"❌ Невалидный task_id: {task_id}"
        try:
            ws     = get_task_workspace(task_id)
            image  = DOCKER_IMAGES.get(language, DOCKER_IMAGES["python"])
            client = docker.from_env()
            net    = _ensure_docker_network(client)

            result = client.containers.run(
                image, command=command,
                volumes={str(ws): {"bind": "/app", "mode": "rw"}},
                working_dir="/app", network=net,
                remove=True, stderr=True, stdout=True,
                mem_limit=MEMORY_LIMIT, cpu_quota=CPU_QUOTA,
                pids_limit=50, detach=False,
                tmpfs={"/tmp": "size=64m"},
            )

            if result is None:
                return "⚠️ Контейнер завершился без вывода"
            output = result.decode("utf-8", errors="replace") if isinstance(result, bytes) else str(result)
            logger.info(f"[{task_id[:8]}] Docker {language}: {command[:50]}")
            return output[:5000]

        except docker.errors.ContainerError as e:
            stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
            return f"❌ Код выхода {e.exit_status}:\n{stderr[:2000]}"
        except docker.errors.ImageNotFound:
            return f"❌ Образ не найден: {image}. Запустите: docker pull {image}"
        except Exception as e:
            logger.error(f"[{task_id[:8]}] Docker error: {e}")
            return f"❌ Docker Error: {e}"


# =============================================================================
# ИНСТРУМЕНТ 3: Git коммит (локальный)
# =============================================================================

class GitCommitTool(BaseTool):
    name: str = "Git Commit"
    description: str = (
        "Коммитит файлы задачи в локальный git-репозиторий workspace. "
        "Аргументы: task_id (UUID str), message (str)."
    )

    def _run(self, task_id: str, message: str) -> str:
        if not _validate_task_id(task_id):
            return f"❌ Невалидный task_id: {task_id}"
        try:
            ws = get_task_workspace(task_id)
            try:
                repo = git.Repo(BASE_WORKSPACE)
            except git.exc.InvalidGitRepositoryError:
                repo = git.Repo.init(BASE_WORKSPACE)
                with repo.config_writer() as cw:
                    cw.set_value("user", "name", "AI Factory")
                    cw.set_value("user", "email", "ai@factory.local")
                gitignore = BASE_WORKSPACE / ".gitignore"
                if not gitignore.exists():
                    gitignore.write_text("*.env\n.env*\n__pycache__/\n*.pyc\n*.key\n*.pem\n")
                logger.info(f"Git репо инициализировано в {BASE_WORKSPACE}")

            repo.git.add("--", str(ws))

            if not repo.is_dirty(index=True, untracked_files=True):
                return "ℹ️ Нет изменений для коммита"

            commit_msg = f"[{task_id[:8]}] {message}"
            commit = repo.index.commit(commit_msg)
            logger.info(f"[{task_id[:8]}] Коммит: {commit.hexsha[:8]}")
            return f"✅ Коммит: {commit.hexsha[:8]} — {commit_msg}"

        except Exception as e:
            logger.error(f"[{task_id[:8]}] Git error: {e}")
            return f"❌ Git Error: {e}"


# =============================================================================
# ИНСТРУМЕНТ 4: GitHub — создание репо и push
#
# ИСПРАВЛЕНИЕ от оригинала:
#   Оригинал вызывал repo.git.add() на объекте PyGithub — это неверно.
#   PyGithub (github.Github) — это REST API клиент для GitHub.com
#   gitpython (git.Repo) — это обёртка над локальным git.
#   Правильная схема:
#     1. PyGithub → создаём репозиторий через GitHub API
#     2. gitpython → инициализируем локальный git, добавляем remote, делаем push
#     Аутентификация при push через HTTPS с токеном в URL.
# =============================================================================

class GitHubTool(BaseTool):
    name: str = "GitHub Tool"
    description: str = (
        "Создаёт репозиторий на GitHub и пушит туда код задачи. "
        "Аргументы: task_id (UUID str), repo_name (str)."
    )

    def _run(self, task_id: str, repo_name: str) -> str:
        if not _validate_task_id(task_id):
            return f"❌ Невалидный task_id: {task_id}"

        github_token = os.getenv("GITHUB_TOKEN", "")
        if not github_token:
            return "❌ GITHUB_TOKEN не задан в .env"

        try:
            ws = get_task_workspace(task_id)

            # --- Шаг 1: Создаём репозиторий через PyGithub API ---
            g    = Github(github_token)
            user = g.get_user()

            try:
                gh_repo = user.create_repo(
                    repo_name,
                    private=False,
                    auto_init=False,   # не инициализируем на GitHub — push сделаем сам
                    description=f"AI Factory task {task_id[:8]}",
                )
                action = "Создан новый репозиторий"
                logger.info(f"[{task_id[:8]}] GitHub repo создан: {gh_repo.html_url}")
            except GithubException as e:
                if e.status == 422:  # уже существует
                    gh_repo = user.get_repo(repo_name)
                    action  = "Репозиторий уже существовал"
                    logger.info(f"[{task_id[:8]}] Используем существующий: {gh_repo.html_url}")
                else:
                    return f"❌ GitHub API ошибка: {e.data}"

            # --- Шаг 2: Инициализируем локальный git в папке задачи ---
            try:
                local_repo = git.Repo(ws)
            except git.exc.InvalidGitRepositoryError:
                local_repo = git.Repo.init(ws)

            with local_repo.config_writer() as cw:
                cw.set_value("user", "name", "AI Factory")
                cw.set_value("user", "email", "ai@factory.local")

            # --- Шаг 3: Добавляем все файлы задачи ---
            local_repo.git.add("--all")

            if local_repo.is_dirty(index=True) or local_repo.untracked_files:
                local_repo.index.commit(f"feat: AI Factory task {task_id[:8]}")
            else:
                logger.info(f"[{task_id[:8]}] Нет изменений для коммита перед push")

            # --- Шаг 4: Push через HTTPS с токеном в URL ---
            # Формат: https://TOKEN@github.com/USER/REPO.git
            remote_url = f"https://{github_token}@github.com/{user.login}/{repo_name}.git"

            # Удаляем старый remote если есть, добавляем новый
            try:
                local_repo.delete_remote("origin")
            except Exception:
                pass
            origin = local_repo.create_remote("origin", remote_url)
            origin.push(refspec="HEAD:refs/heads/main")

            logger.info(f"[{task_id[:8]}] Push выполнен: {gh_repo.html_url}")
            return f"✅ {action}: {gh_repo.html_url}"

        except git.exc.GitCommandError as e:
            logger.error(f"[{task_id[:8]}] Git push error: {e}")
            return f"❌ Git push ошибка: {str(e)[:300]}"
        except Exception as e:
            logger.error(f"[{task_id[:8]}] GitHub tool error: {e}")
            return f"❌ Ошибка: {str(e)[:300]}"
