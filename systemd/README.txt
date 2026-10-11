systemd 部署说明
================

推荐直接用仓库根目录的一键安装脚本：  sudo bash install.sh
（它会自动创建 venv、写入 config.yml、安装并启动本服务）

如果你想手动部署：

  1. 把整个项目目录放到 /opt/tgforwardbot
  2. 准备 Python 环境：
       python3 -m venv /opt/tgforwardbot/venv
       /opt/tgforwardbot/venv/bin/pip install -r /opt/tgforwardbot/requirements.txt
  3. 复制并填写配置：
       cp /opt/tgforwardbot/config.example.yml /opt/tgforwardbot/config.yml
       vi /opt/tgforwardbot/config.yml     # 填写 api_id / api_hash / web_password
  4. 安装服务：
       cp systemd/tgforwardbot.service /etc/systemd/system/
       systemctl daemon-reload
       systemctl enable --now tgforwardbot
  5. 首次登录 Telegram（前台跑一次完成手机号/验证码/二步密码）：
       cd /opt/tgforwardbot && ./venv/bin/python main.py
       # 或在网页管理台右上角「Telegram 未登录 · 点此连接」完成登录
  6. 查看状态 / 日志：
       systemctl status tgforwardbot
       journalctl -u tgforwardbot -f

注意：本 unit 里的 User / WorkingDirectory / ExecStart 路径需与实际部署路径一致，
install.sh 会按 INSTALL_DIR 自动生成一份正确的 unit。
