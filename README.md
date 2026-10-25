# 北邮 ucloud 消息通知 → 飞书

使用 Python、Playwright、httpx、SQLite，单个 Docker 容器常驻。CAS 登录完成后关闭浏览器，正常轮询直接调用通知接口。运行时不发布端口，不接入其他项目的容器网络。

## 已确认的接口（2026-10-25 实测）

- 登录：Playwright 浏览器访问北邮 CAS。学校页面自身的 `init.js` 支持 `dev=1` 原生登录表单；默认内嵌登录页在自动化会话中曾返回空白 400。独立浏览器会话使用兼容启动参数，仍执行正常账号密码认证，遇到验证码停止提交。
- 通知列表：`POST https://apiucloud.bupt.edu.cn/ykt-basics/api/inform/news/list`。
- URL 查询参数：`newsCopyPersonId`、`current`（从 1 开始）、`size`。已验证 `size=100`，自动遍历所有分页。
- 列表在 `data.records`，分页字段为 `total`、`pages`。通知唯一 ID 为 `id`；课程/标题 `newsTitle`，内容 `newsInfo`，时间 `newsCopyTime`，类型 `type`。接口未提供稳定的发送人姓名字段，因此推送展示课程名称。
- 请求使用 `blade-auth` 中的原始 JWT，不能自行加 `Bearer` 前缀；还保留浏览器实际发送的 `authorization`、`tenant-id`、`identity` 等必要请求头。凭证只存在权限 600 的私有缓存中。
- 凭证有效期实测约 7200 秒。优先调用 `POST /ykt-basics/oauth/token` 以表单刷新令牌，提前 120 秒刷新；仅刷新令牌明确失效时才重新打开浏览器登录。浏览器优先复用 CAS SSO，等待门户取得令牌后再进入通知应用初始化角色，避免低配置服务器上过早跳转。刷新令牌的最长寿命尚未长期验证。
- 列表查询不调用“标记已读”“删除”等接口，不改变网页通知的阅读状态。

## 配置

将 `.env.example` 复制为 `.env`，填入 `BUPT_USERNAME`、`BUPT_PASSWORD`、`FEISHU_WEBHOOK`。启用机器人签名校验时填写 `FEISHU_SECRET`。`POLL_INTERVAL` 默认 60 秒，最小 30 秒，每轮另加 0～10 秒随机抖动。

`.env` 使用 python-dotenv 读取，关闭变量插值，密码中的 `$` 不会被当成 shell 变量。密码建议使用引号，文件不要通过 `source` 加载，也不要提交到 Git。私钥、`.env`、`data/`、`debug/` 均已排除 Git；凭据和运行数据也排除 Docker 构建上下文。

## Docker 安装

服务器需已有 Docker Engine 和 Compose v2。安装脚本不会安装或重启 Docker，不升级系统软件。

先克隆项目并准备配置：

```bash
git clone https://github.com/buwuzhengyeccc/bupt-ucloud-feishu-notifier.git ucloud-notify
cd ucloud-notify
cp .env.example .env
chmod 600 .env
```

用编辑器填写 `.env` 后，在项目目录安装：

```bash
sudo bash install.sh
```

Python 和 Chromium 及其系统依赖均位于固定版本的 Playwright 容器镜像中，不在宿主机安装浏览器依赖。`wheelhouse/` 是 Linux 离线依赖包；不存在时安装脚本会在临时容器中下载。若更新了 requirements.txt，也需要更新该目录。

容器使用 UID 10001，配置权限为 600，数据目录为 700；内存上限 768 MiB、CPU 上限 0.75 核、进程数上限 256。根文件系统只读、移除额外 capabilities，日志最多保留 3 个 5 MiB 文件。

## 命令行

本地 Python 3.10+：

```bash
python -m venv .venv
# Linux: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python main.py --test-push
python main.py --debug-login
python main.py --once
python main.py
```

本机已经安装 Chrome 时，探查也可以执行 `python main.py --debug-login --chrome --headed`，使用独立的临时会话，不读写日常 Chrome 配置。

服务器容器中对应命令：

```bash
docker compose run --rm --no-deps -T --interactive=false notify python main.py --test-push
docker compose run --rm --no-deps -T --interactive=false notify python main.py --once
```

人工重新探查登录前先停止本项目服务，避免并行登录：

```bash
sudo systemctl stop bupt-ucloud-notify
docker compose run --rm --no-deps -T --interactive=false notify python main.py --debug-login
sudo systemctl start bupt-ucloud-notify
```

`--debug-login` 保存脱敏请求结构、字段名、输入框遮罩截图到 `debug/`，不保存 HAR 或浏览器 trace。最小会话缓存保存在 `data/api-session.json`；该文件和浏览器状态缓存包含凭证，不能分享。

## 去重、失败与告警

SQLite 数据库为 `data/state.db`。首次成功抓取全部通知后，只发送一条“监控已启动，当前共 N 条通知”，然后建立基线。之后新 ID 才推送，富文本包含课程、类型、时间、正文摘要和通知页链接。正文过长会截断，多个通知按大小合并；请求体控制在 20 KB 以下，发送间隔至少 0.7 秒。

仅飞书 HTTP 和业务状态都成功后才记录已推送。部分批次失败时，已成功的批次保留记录；只要学校接口仍返回其余通知，下轮会重新抓取并重试。当前未单独保存未投递通知正文的本地队列，因此不能保证捕获两次轮询之间出现又撤回的通知。普通重启不会重复推送；若飞书已接收但响应丢失，或进程在接收成功、SQLite 提交之前崩溃，仍可能重复一批，飞书自定义 webhook 没有可用于消除此窗口的幂等确认接口。

富文本使用[飞书自定义机器人文档](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)中的 `text`、`a` 节点；不附加机器人接口不支持的文本样式字段。

登录失败指数退避，连续失败 3 次后每次至少等待 30 分钟，出现验证码也等待 30 分钟。退避状态落盘，重启不会清除。相同登录告警成功发送后 1 小时内不再发送。网络错误和接口 5xx 仅记录安全摘要、稍后重试，不发送群告警。

## 日志、状态与更新

```bash
systemctl status bupt-ucloud-notify --no-pager
journalctl -u bupt-ucloud-notify -f
docker compose ps
docker compose logs --tail 50 notify
docker stats --no-stream
```

健康探针超过 10 分钟未成功时开始失败，连续 3 次探针失败后标记 unhealthy，实际通常约 12 分钟；该状态本身不会触发密码重试。

安全日志现在记录：

- `poll_ok mode=api`：分页数、总通知数、本轮投递数、耗时、凭证剩余秒数、下一轮等待秒数。
- `auth_session_loaded`：启动时从本地缓存恢复凭证，只记录过期时间，不记录凭证值。
- `auth_refresh_start/ok/rejected/failed`：刷新原因、耗时、有效期、结果；`password_submitted=false` 明确表示该操作未提交密码。
- `LOGIN_STAGE=browser_start/reuse_cas_session/submit_once`：区分打开浏览器、复用单点登录与真正提交密码。`LOGIN_COOLDOWN`、`LOGIN_RETRY_SCHEDULED` 记录失败次数和退避时间。
- `notifications_detected`、`feishu_request_ok/retry/rejected`、`notifications_delivered`：区分发现新增、请求飞书、成功后提交去重记录。成功投递日志包含通知业务 ID，便于与列表核对。

密码、学号、token、cookie 值、webhook 地址、通知正文均不写入运行日志；刷新令牌和 CAS 会话仅存在权限 600 的私有缓存。Docker 日志轮转为 5 MiB × 3；journald 仍按服务器原有全局策略保留，不能把两者的总占用理解为只有 15 MiB。

只看认证相关日志：

```bash
journalctl -u bupt-ucloud-notify --since today -o cat | grep -E 'auth_|LOGIN_STAGE|LOGIN_COOLDOWN|LOGIN_RETRY'
```

代码按职责划分：`login.py` 管理浏览器登录和退避，`fetcher.py` 管理接口分页与凭证刷新，`storage.py` 管理持久去重，`feishu.py` 管理消息格式和发送，`service.py` 串联轮询流程，`main.py` 提供命令行入口。个人部署验收记录与调试资料保留在本地，不提交到仓库。

更新源码后，在本项目目录执行：

```bash
docker compose build
sudo systemctl restart bupt-ucloud-notify
```

更新不删除 `.env` 或 `data/`。修改配置后也需要重启本项目服务。

## 卸载

```bash
sudo systemctl disable --now bupt-ucloud-notify
docker compose down
sudo rm /etc/systemd/system/bupt-ucloud-notify.service
sudo systemctl daemon-reload
```

以上保留配置、SQLite、调试文件，便于恢复。不要对整台服务器执行 Docker 全局 prune 或删除其他项目的数据卷。

## 验证

`python -m unittest -q test_core.py` 运行九项检查，覆盖持久去重、失败推送不误记成功、诊断与认证日志脱敏、刷新后的凭证格式、失效刷新只触发一次浏览器恢复、普通轮询复用凭证、服务端 5xx 不触发密码登录、浏览器缓存仅保留 CAS 会话、富文本节点格式与批次字节限制。测试使用虚构数据，不需要真实账号或 webhook。部署验收另需检查服务器实连、首次基线、历史通知重放和重启去重；真实新通知的长期到达、验证码流程、签名机器人实连需要对应场景验证。
