# 部署到 VPS（Ubuntu + Caddy + systemd，HTTPS 由 Caddy 自动签发）

当前线上：<https://reprohub.ninthflux.com>，服务器 SSH 别名 `oracle`，代码目录 `~/ReproHub`。

## 同步模型

```
本地  --git push-->  GitHub (origin/main)  --git pull (ff-only)-->  VPS
```

- 只在本地改代码、提交、推送；VPS 上不改被跟踪的文件，只用 `deploy/update.sh` 快进拉取。
- 机密与数据只在 VPS：`.env`（口令、API key、`SITE_BASE_URL`）与 `data/`（SQLite、`.secret_key`、备份），均被 git 忽略。

本地推送后更新线上：

```bash
git push && ssh oracle '~/ReproHub/deploy/update.sh'
```

## 首次部署

```bash
sudo apt-get install -y python3-venv git   # 服务器已有 Caddy 占用 80/443
git clone https://github.com/kongkk233/ReproHub.git ~/ReproHub && cd ~/ReproHub
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m reprohub init          # 生成 .env、建库
```

编辑 `.env`，至少设置：

```
ADMIN_PASSWORD=<强口令>          # 公网必须设置
SITE_BASE_URL=https://reprohub.ninthflux.com
DEEPSEEK_API_KEY=sk-...          # 不填为模拟模式
INGEST_TOKEN=<随机串>            # 需要推送入库时
```

```bash
sudo cp deploy/reprohub.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now reprohub
sudo cp deploy/reprohub.caddy /etc/caddy/sites/reprohub.caddy
grep -q reprohub.caddy /etc/caddy/Caddyfile || echo 'import /etc/caddy/sites/reprohub.caddy' | sudo tee -a /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy
sudo cp deploy/reprohub-backup.service deploy/reprohub-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now reprohub-backup.timer   # 每日 03:15 备份到 data/backups/
```

DNS：Cloudflare 上 `reprohub` A 记录指向 VPS IP（仅 DNS，不走代理，便于 Caddy 签证书）。

## 安全要点

- 应用只监听 `127.0.0.1:8008`，公网只经 Caddy。
- 未设 `ADMIN_PASSWORD` 时后台只认「本机直连」；带 `X-Forwarded-For` 等代理头的请求一律不算本机，所以经 Caddy 的访客不会获得管理权限。
- 登录 cookie 为 HMAC 签名的有效期令牌（30 天，`secure` 随 HTTPS），密钥为 `SECRET_KEY` 或 `data/.secret_key`；改口令即作废旧会话。
- 网站与调度同进程（`serve --scheduler`），只能单 worker 运行。

## 运维

```bash
systemctl status reprohub
journalctl -u reprohub -f
~/ReproHub/.venv/bin/python -m reprohub status
```
