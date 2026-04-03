#!/usr/bin/env bash
# AI Factory — Полная автоустановка (финальная стабильная версия)

set -euo pipefail

FACTORY_DIR="$HOME/ai_factory"
VENV="$FACTORY_DIR/venv"
SERVICE_USER="$(whoami)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
info() { echo -e "${BLUE}  → $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }
fail() { echo -e "${RED}  ✗ $1${NC}"; exit 1; }
header() { echo -e "\n\( {BOLD} \){BLUE}══ $1 ══${NC}"; }

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║     AI Factory — Автоустановка       ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

header "1/8 Установка системных пакетов и Docker"
sudo apt-get update -qq
sudo apt-get install -y ca-certificates curl gnupg lsb-release python3 python3-pip python3-venv git wget redis-server

# Docker
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -qq
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

sudo systemctl enable --now docker redis-server
sudo usermod -aG docker "$SERVICE_USER" 2>/dev/null || true

ok "Системные пакеты и Docker установлены"

header "2/8 Переходим в проект"
cd "$FACTORY_DIR" || fail "Папка $FACTORY_DIR не найдена"

header "3/8 Проверка .env"
ENV_FILE="$FACTORY_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    fail ".env файл не найден! Создайте его рядом с setup.sh"
fi

sed -i "s|WORKSPACE_PATH=БУДЕТ_ПОДСТАВЛЕНО_АВТОМАТИЧЕСКИ|WORKSPACE_PATH=$FACTORY_DIR/workspace|" "$ENV_FILE" 2>/dev/null || true
sed -i "s|LOGS_PATH=БУДЕТ_ПОДСТАВЛЕНО_АВТОМАТИЧЕСКИ|LOGS_PATH=$FACTORY_DIR/logs|" "$ENV_FILE" 2>/dev/null || true

chmod 600 "$ENV_FILE"
ok ".env проверен"

header "4/8 Структура папок и права"
mkdir -p workspace logs systemd agents
touch agents/__init__.py 2>/dev/null || true

sudo rm -f logs/*.log logs/*.err 2>/dev/null || true
touch logs/bot.log logs/worker.log logs/bot.err logs/worker.err
chmod -R 755 logs workspace
chown -R "$SERVICE_USER:$SERVICE_USER" logs workspace

ok "Структура и права настроены"

header "5/8 Python окружение"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip setuptools wheel --no-cache-dir
pip install -r requirements.txt --no-cache-dir
ok "Зависимости установлены"

header "6/8 Docker сеть"
docker network inspect ai_factory_sandbox &>/dev/null || docker network create ai_factory_sandbox --driver bridge --internal
ok "Docker сеть готова"

header "7/8 Systemd службы"
cat > systemd/ai-factory-bot.service << EOF
[Unit]
Description=AI Factory Telegram Bot
After=network.target redis.service
Requires=redis.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$FACTORY_DIR
ExecStart=$VENV/bin/python -m bot
Restart=always
RestartSec=5
EnvironmentFile=$ENV_FILE
StandardOutput=append:$FACTORY_DIR/logs/bot.log
StandardError=append:$FACTORY_DIR/logs/bot.err
EOF

cat > systemd/ai-factory-worker.service << EOF
[Unit]
Description=AI Factory Worker
After=network.target redis.service docker.service
Requires=redis.service docker.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$FACTORY_DIR
ExecStart=$VENV/bin/python -m worker
Restart=always
RestartSec=5
EnvironmentFile=$ENV_FILE
StandardOutput=append:$FACTORY_DIR/logs/worker.log
StandardError=append:$FACTORY_DIR/logs/worker.err
EOF

sudo cp systemd/ai-factory-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-factory-bot ai-factory-worker

ok "Службы запущены"

header "8/8 Финальная проверка"
sleep 5
systemctl is-active --quiet ai-factory-bot && ok "Bot работает" || warn "Bot не запущен"
systemctl is-active --quiet ai-factory-worker && ok "Worker работает" || warn "Worker не запущен"

echo -e "\( {BOLD} \){GREEN}╔══════════════════════════════════════╗"
echo -e "║        Установка завершена!          ║"
echo -e "╚══════════════════════════════════════╝${NC}"
echo "Отправьте сообщение боту в Telegram для теста."