#!/usr/bin/env bash
# 在 VPS 上拉取 GitHub 最新代码并重启（本地 push 后在服务器执行，或由本地 `make deploy` 远程调用）
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch --quiet origin
git merge --ff-only origin/main
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m reprohub init >/dev/null
sudo systemctl restart reprohub
sleep 2
systemctl is-active reprohub
curl -fsS -o /dev/null -w "local http %{http_code}\n" http://127.0.0.1:8008/api/v1/status
