#!/usr/bin/env bash
# =============================================================================
#  TG 转发机器人 —— 卸载脚本（谨慎使用）
#  用法：sudo bash uninstall.sh [--purge]
#    不加 --purge ：仅停止并移除 systemd 服务，保留程序与数据
#    加   --purge ：删除 systemd 服务 + 安装目录（含数据/日志/session，不可恢复）
# =============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/tgforwardbot}"
SERVICE_NAME="${SERVICE_NAME:-tgforwardbot}"

c()  { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
info(){ c '1;36' "==> $*"; }
ok()  { c '1;32' "✓  $*"; }
warn(){ c '1;33' "!  $*"; }

[ "$(id -u)" -eq 0 ] || { c '1;31' "请用 root 运行：sudo bash uninstall.sh"; exit 1; }

if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
  info "停止并移除服务 $SERVICE_NAME ..."
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
  ok "服务已移除"
else
  warn "未发现 systemd 服务，跳过"
fi

if [ "${1:-}" = "--purge" ]; then
  warn "即将删除目录：$INSTALL_DIR （含数据与 session，不可恢复）"
  if [ -t 0 ]; then
    read -r -p "确认删除请输入 yes: " a
    [ "$a" = "yes" ] || { warn "已取消删除"; exit 0; }
  fi
  rm -rf "$INSTALL_DIR"
  ok "已删除 $INSTALL_DIR"
else
  ok "完成（程序与数据仍保留在 $INSTALL_DIR，如需彻底删除请加 --purge）"
fi
