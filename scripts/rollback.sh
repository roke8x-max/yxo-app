#!/bin/bash
# -*- coding: utf-8 -*-
"""
rollback.sh - 回滚脚本
"""
set -euo pipefail

APP_DIR="/opt/yxo/mailbots"
BACKUP_DIR="/opt/yxo/backups"

log_info() { echo -e "\033[0;32m[INFO]\033[0m $*"; }
log_error() { echo -e "\033[0;31m[ERROR]\033[0m $*"; }

list_backups() {
    ls -lt "$BACKUP_DIR" | head -10
}

rollback() {
    local target="${1:-}"
    if [[ -z "$target" ]]; then
        echo "可用备份："
        ls -lt "$BACKUP_DIR" | head -10
        read -p "请输入要回滚的备份目录名: " target
    fi
    
    local backup_path="$BACKUP_DIR/$target"
    if [[ ! -d "$backup_path" ]]; then
        echo "备份不存在: $backup_path"
        exit 1
    fi
    
    log_info "回滚到 $target ..."
    systemctl stop yxo-mailbot
    rm -rf /opt/yxo/mailbots/*
    cp -r "$backup_path"/* /opt/yxo/mailbots/
    chown -R yxo:yxo /opt/yxo/mailbots
    systemctl restart yxo-mailbot
    log_info "回滚完成"
}

case "${1:-}" in
    list) list_backups ;;
    rollback) rollback "${2:-}" ;;
    *) echo "用法: $0 {list|rollback [backup_name]}" ;;
esac