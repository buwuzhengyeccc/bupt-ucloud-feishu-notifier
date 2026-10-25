#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
project_dir=$(pwd -P)
if (( EUID != 0 )); then
  echo '请使用 sudo bash install.sh；安装只管理 bupt-ucloud-notify 项目。' >&2
  exit 1
fi
command -v docker >/dev/null
docker compose version >/dev/null
if [[ ! -f .env ]]; then
  install -m 600 .env.example .env
  echo '已创建 .env，请填写后重新运行安装。' >&2
  exit 1
fi
if [[ -L .env || -L data || -L debug ]]; then
  echo '配置和数据目录不能是符号链接。' >&2
  exit 1
fi
install -d -m 700 -o 10001 -g 10001 data debug
chown 10001:10001 .env
chmod 600 .env
if [[ ! -d wheelhouse ]] || [[ -z "$(find wheelhouse -maxdepth 1 -name '*.whl' -print -quit)" ]]; then
  install -d -m 755 wheelhouse
  base_image=$(awk '/^FROM / {print $2; exit}' Dockerfile)
  docker run --rm --entrypoint python \
    -v "$project_dir/requirements.txt:/requirements.txt:ro" \
    -v "$project_dir/wheelhouse:/wheels" \
    "$base_image" -m pip download --only-binary=:all: --dest /wheels -r /requirements.txt
fi
docker compose build
docker compose run --rm --no-deps -T --interactive=false notify \
  python -c 'from config import Config; Config.load(); print("CONFIG_OK")'
cat > /etc/systemd/system/bupt-ucloud-notify.service <<UNIT
[Unit]
Description=BUPT ucloud notifications to Feishu (Docker)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$project_dir
ExecStart=/usr/bin/docker compose up --no-build --no-color notify
ExecStop=/usr/bin/docker compose stop -t 45 notify
Restart=always
RestartSec=30
SuccessExitStatus=0 130 143
TimeoutStopSec=75

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now bupt-ucloud-notify.service
systemctl is-active bupt-ucloud-notify.service
