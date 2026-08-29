#!/usr/bin/env bash
# -*- coding: utf-8 -*-
"""
deploy.sh - YXO MailBots 生产环境部署脚本 (Linux 开发环境用)
用法: ./deploy.sh [install|update|status]
注意: 生产环境为 Windows，请使用 scripts/deploy/Deploy-YXO-MailBots.ps1
"""

set -euo pipefail

# ============================================================
# 配置变量 (开发/测试环境)
# ============================================================
APP_NAME="yxo-mailbots"
APP_USER="yxo"
APP_GROUP="yxo"
APP_DIR="/opt/yxo/mailbots"
VENV_DIR="/opt/yxo/venv"
CONFIG_DIR="/etc/yxo"
LOG_DIR="/var/log/yxo"
DATA_DIR="/var/lib/yxo"
SERVICE_NAME="yxo-mailbot"
TIMER_NAME="yxo-mailbot.timer"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ============================================================
# 工具函数
# ============================================================
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "请使用 sudo 运行此脚本"
        exit 1
    fi
}

check_dependencies() {
    log_info "检查依赖..."
    command -v python3 >/dev/null 2>&1 || { log_error "需要 python3"; exit 1; }
    python3 -c "import sqlite3" 2>/dev/null || { log_error "需要 sqlite3"; exit 1; }
    command -v systemctl >/dev/null 2>&1 || { log_error "需要 systemd"; exit 1; }
    log_info "依赖检查通过"
}

create_user() {
    if ! id "$APP_USER" &>/dev/null; then
        log_info "创建用户 $APP_USER"
        useradd -r -s /bin/bash -d "$APP_DIR" "$APP_USER"
    else
        log_info "用户 $APP_USER 已存在"
    fi
}

setup_directories() {
    log_info "创建目录结构..."
    mkdir -p "$APP_DIR" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
    chown -R "$APP_USER:$APP_GROUP" "$APP_DIR" "$LOG_DIR" "$DATA_DIR"
    chmod 750 "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
}

create_venv() {
    if [[ ! -d "$VENV_DIR" ]]; then
        log_info "创建虚拟环境..."
        python3 -m venv "$VENV_DIR"
        chown -R "$APP_USER:$APP_GROUP" "$VENV_DIR"
    fi
}

install_python_deps() {
    log_info "安装 Python 依赖..."
    cd /opt/yxo/mailbots
    sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip
    sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r requirements.txt
}

deploy_code() {
    log_info "部署代码..."
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.pytest_cache' --exclude='.venv' --exclude='tmp' \
        --exclude='*.db' --exclude='*.log' \
        ./ /opt/yxo/mailbots/
    chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
}

create_config() {
    log_info "创建配置文件..."
    cat > "$CONFIG_DIR/config.py" <<'EOF'
# -*- coding: utf-8 -*-
"""YXO MailBots 生产环境配置 (Linux 测试环境模板)"""
import os

# 基础路径
YXO_ROOT = os.environ.get("YXO_ROOT", "/opt/yxo")
DATA_DIR = os.environ.get("YXO_DATA_DIR", "/var/lib/yxo")
CONFIG_DIR = os.environ.get("YXO_CONFIG_DIR", "/etc/yxo")
LOG_DIR = os.environ.get("YXO_LOG_DIR", "/var/log/yxo")

# 数据库路径
YXO_DB_PATH = os.path.join(DATA_DIR, "yxo.db")
EVENTS_DB_PATH = os.path.join(DATA_DIR, "events.db")
EML_REPO_DIR = os.path.join(DATA_DIR, "eml_repo")

# IMAP/SMTP 配置
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.qiye.aliyun.com")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.qiye.aliyun.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

# 账号配置（从环境变量或配置文件加载）
def load_accounts():
    accounts = {}
    for i in range(1, 10):
        email = os.environ.get(f"YXO_MAIL_ACCOUNT_{i}")
        pwd = os.environ.get(f"YXO_MAIL_PASSWORD_{i}")
        if email and pwd:
            accounts[email] = pwd
    config_file = os.path.join(CONFIG_DIR, "accounts.json")
    if os.path.exists(config_file):
        import json
        with open(config_file) as f:
            accounts.update(json.load(f))
    return accounts

ACCOUNTS = load_accounts()

# SMTP 端点
def smtp_endpoint():
    return SMTP_SERVER, SMTP_PORT

# 企微配置
WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID")
WECOM_AGENT_ID = os.environ.get("WECOM_AGENT_ID")
WECOM_SECRET = os.environ.get("WECOM_SECRET")

# 邮戳 API
STAMP_API = "http://127.0.0.1:5011/api/stamp"
STAMP_TOKEN = os.environ.get("STAMP_TOKEN")

EOF

    # 创建账号配置模板
    cat > "$CONFIG_DIR/accounts.json.example" <<'EOF'
{
  "maoxiaoyang@cqtransit.com": "your_password_here",
  "yangyawen@cqtransit.com": "your_password_here",
  "fengqian@cqtransit.com": "your_password_here",
  "hanwenhao@cqtransit.com": "your_password_here"
}
EOF
    chmod 600 "$CONFIG_DIR/accounts.json.example"
}

create_systemd_service() {
    log_info "创建 systemd 服务..."
    cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=YXO MailBot Service
After=network.target
Wants=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
Environment=PYTHONPATH=$APP_DIR
Environment=YXO_ROOT=$APP_DIR
Environment=YXO_DATA_DIR=$DATA_DIR
Environment=YXO_CONFIG_DIR=$CONFIG_DIR
Environment=YXO_LOG_DIR=$LOG_DIR
ExecStart=$VENV_DIR/bin/python -m mailbots.mailbot_serve --live
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

# 安全设置
NoNewPrivileges=yes
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR $LOG_DIR $CONFIG_DIR

[Install]
WantedBy=multi-user.target
EOF

    # 定时器 (15分钟轮询，对齐短进程周期)
    cat > "/etc/systemd/system/$TIMER_NAME" <<EOF
[Unit]
Description=MailBot 定时触发器
Requires=$SERVICE_NAME.service

[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
}

setup_logrotate() {
    log_info "配置日志轮转..."
    cat > "/etc/logrotate.d/yxo-mailbots" <<EOF
$LOG_DIR/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 640 $APP_USER $APP_GROUP
    sharedscripts
    postrotate
        systemctl reload $SERVICE_NAME > /dev/null 2>&1 || true
    endscript
}
EOF
}

setup_database() {
    log_info "初始化数据库..."
    sudo -u "$APP_USER" "$VENV_DIR/bin/python" -c "
import sys
sys.path.insert(0, '$APP_DIR')
from core import events_store
conn = events_store.connect(events_store.events_db_path())
events_store.ensure_schema(conn)
print('数据库初始化完成')
"
}

run_tests() {
    log_info "运行测试..."
    cd "$APP_DIR"
    sudo -u "$APP_USER" "$VENV_DIR/bin/python" -m pytest mailbots/tests/unit -q --tb=short
}

# ============================================================
# 主流程
# ============================================================
main() {
    check_root
    check_dependencies
    
    case "\${1:-install}" in
        install)
            log_info "开始安装 (Linux 测试环境)..."
            create_user
            setup_directories
            create_venv
            deploy_code
            install_python_deps
            create_config
            create_systemd_service
            setup_logrotate
            setup_database
            run_tests
            log_info "安装完成！使用 systemctl start \$SERVICE_NAME 启动服务"
            log_warn "注意: 生产环境为 Windows，请使用 scripts/deploy/Deploy-YXO-MailBots.ps1"
            ;;
        update)
            log_info "更新代码..."
            deploy_code
            install_python_deps
            run_tests
            systemctl restart "\$SERVICE_NAME"
            log_info "更新完成"
            ;;
        status)
            systemctl status "\$SERVICE_NAME"
            systemctl status "\$TIMER_NAME"
            ;;
        *)
            echo "用法: \$0 {install|update|status}"
            echo "注意: 此脚本仅用于 Linux 测试环境，生产环境请使用 PowerShell 脚本"
            exit 1
            ;;
    esac
}

main "\$@"
