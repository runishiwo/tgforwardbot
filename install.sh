#!/usr/bin/env bash
# =============================================================================
#  TG 转发机器人 —— 一键安装脚本
#
#  用法：
#     sudo bash install.sh
#
#  可选环境变量（不设置则交互式询问）：
#     INSTALL_DIR=/opt/tgforwardbot   安装目录
#     TG_API_ID=123456                你的 api_id
#     TG_API_HASH=xxxxxxxx            你的 api_hash
#     TG_WEB_PORT=8080                管理台端口
#     TG_WEB_PASSWORD=xxxx            管理台访问密码
#     TG_TARGET_ENTITY=-1001234567890 默认转发目标（可留空）
#     SERVICE_NAME=tgforwardbot       systemd 服务名
#     NO_SYSTEMD=1                    只安装不注册 systemd（调试用）
# =============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/tgforwardbot}"
SERVICE_NAME="${SERVICE_NAME:-tgforwardbot}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c()  { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
info(){ c '1;36' "==> $*"; }
ok()  { c '1;32' "✓  $*"; }
warn(){ c '1;33' "!  $*"; }
die() { c '1;31' "✗  $*"; exit 1; }

# ------------------------------ 0. 前置检查 ------------------------------
if [ "$(id -u)" -ne 0 ]; then
  die "请用 root 运行：sudo bash install.sh"
fi

info "安装目录：$INSTALL_DIR"
info "项目来源：$SRC_DIR"

# ------------------------------ 1. 安装系统依赖 ------------------------------
need_pkg=0
command -v python3 >/dev/null 2>&1 || need_pkg=1
python3 -c 'import venv'  >/dev/null 2>&1 || need_pkg=1
python3 -c 'import ensurepip' >/dev/null 2>&1 || need_pkg=1

if [ "$need_pkg" -eq 1 ]; then
  info "安装 Python3 / venv / pip ..."
  if   command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 py3-pip py3-virtualenv
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm python python-pip
  else
    die "未识别的包管理器，请手动安装 python3 + venv + pip 后重试。"
  fi
else
  ok "系统已具备 python3 / venv / pip"
fi
PYBIN="$(command -v python3)"
ok "Python：$($PYBIN --version 2>&1)"

# ------------------------------ 2. 复制项目 ------------------------------
if [ "$SRC_DIR" = "$INSTALL_DIR" ]; then
  ok "已在安装目录，跳过复制"
else
  info "复制项目文件到 $INSTALL_DIR ..."
  mkdir -p "$INSTALL_DIR"
  # 排除运行期产物与敏感文件
  tar -C "$SRC_DIR" \
      --exclude='./venv' --exclude='./.git' --exclude='./data' \
      --exclude='./log' --exclude='./media_tmp' --exclude='./backup_*' \
      --exclude='./__pycache__' --exclude='*.session' --exclude='./config.yml' \
      -cf - . | tar -C "$INSTALL_DIR" -xf -
fi
cd "$INSTALL_DIR"

# ------------------------------ 3. Python 虚拟环境 ------------------------------
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  info "创建虚拟环境 venv ..."
  "$PYBIN" -m venv "$INSTALL_DIR/venv"
fi
info "安装 Python 依赖 ..."
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
ok "依赖安装完成"

# ------------------------------ 4. 生成 config.yml ------------------------------
ask() {  # ask <提示> <变量名> [默认值]
  local prompt="$1" var="$2" def="${3:-}" ans=""
  if [ -n "$def" ]; then prompt="$prompt [$def]"; fi
  if [ -t 0 ]; then
    read -r -p "$prompt: " ans || true
  fi
  ans="${ans:-$def}"
  eval "$var=\$ans"
}

if [ -f "$INSTALL_DIR/config.yml" ]; then
  ok "已存在 config.yml，保留未覆盖"
else
  TG_API_ID="${TG_API_ID:-}"
  TG_API_HASH="${TG_API_HASH:-}"
  TG_WEB_PASSWORD="${TG_WEB_PASSWORD:-}"
  TG_WEB_PORT="${TG_WEB_PORT:-8080}"
  TG_TARGET_ENTITY="${TG_TARGET_ENTITY:-}"

  [ -z "${TG_API_ID:-}" ]       && ask "请输入 Telegram api_id（my.telegram.org 获取）" TG_API_ID
  [ -z "${TG_API_HASH:-}" ]     && ask "请输入 Telegram api_hash" TG_API_HASH
  [ -z "${TG_WEB_PASSWORD:-}" ] && ask "请设置管理台访问密码" TG_WEB_PASSWORD "change-me"
  ask "管理台端口" TG_WEB_PORT "$TG_WEB_PORT"

  if [ -z "${TG_API_ID:-}" ] || [ -z "${TG_API_HASH:-}" ]; then
    warn "未提供 api_id / api_hash，将写入占位值，请稍后手动编辑 $INSTALL_DIR/config.yml"
    TG_API_ID="${TG_API_ID:-123456}"
    TG_API_HASH="${TG_API_HASH:-your_api_hash_here}"
  fi

  cat > "$INSTALL_DIR/config.yml" <<YAML
api_id: ${TG_API_ID}
api_hash: "${TG_API_HASH}"
target_entity: ${TG_TARGET_ENTITY}
keyword_file: "keyword.txt"
blocked_file: "blocked.txt"
target_file: "targets.txt"
session_name: "session_session"
reload_interval: 60
forward_interval: 2
max_retries: 3
album_scan_limit: 50
log_cleanup_interval: 21600
log_dir: "log"
media_tmp_dir: "media_tmp"
web_host: "0.0.0.0"
web_port: ${TG_WEB_PORT}
web_password: "${TG_WEB_PASSWORD}"
YAML
  chmod 600 "$INSTALL_DIR/config.yml"
  ok "已写入 config.yml"
fi

# ------------------------------ 5. 注册 systemd 服务 ------------------------------
if [ "${NO_SYSTEMD:-0}" = "1" ] || ! command -v systemctl >/dev/null 2>&1; then
  warn "跳过 systemd 注册。手动启动：cd $INSTALL_DIR && ./venv/bin/python main.py"
else
  info "安装 systemd 服务 $SERVICE_NAME ..."
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=TG Forward Bot Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
  systemctl restart "${SERVICE_NAME}"
  sleep 2
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    ok "服务已启动"
  else
    warn "服务未处于 active，请查看：journalctl -u ${SERVICE_NAME} -n 50"
  fi
fi

# ------------------------------ 6. 完成提示 ------------------------------
PORT="${TG_WEB_PORT:-$(grep -E '^web_port:' "$INSTALL_DIR/config.yml" 2>/dev/null | awk '{print $2}')}"
PORT="${PORT:-8080}"
cat <<EOF

$(c '1;32' '======================================================================')
$(c '1;32' ' 安装完成')
$(c '1;32' '======================================================================')
  安装目录 : $INSTALL_DIR
  配置文件 : $INSTALL_DIR/config.yml   （含 api 凭据与访问密码，请勿外泄）
  管理台   : http://<服务器IP>:${PORT}    （密码见 config.yml 的 web_password）
  服务名   : ${SERVICE_NAME}

  常用命令：
    systemctl status  ${SERVICE_NAME}
    systemctl restart ${SERVICE_NAME}
    journalctl -u ${SERVICE_NAME} -f

  首次登录 Telegram（任选其一）：
    1) 打开管理台，右上角点「Telegram 未登录 · 点此连接」，按提示输入手机号/验证码；
    2) 或前台手动跑一次完成登录：
         cd $INSTALL_DIR && ./venv/bin/python main.py
       登录成功后会在目录内生成 session_session.session（切勿外泄/提交）。

$(c '1;33' ' 提示：请立即修改 config.yml 中的 web_password，并确保服务器防火墙按需放行端口。')
EOF
