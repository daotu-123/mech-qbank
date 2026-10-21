# mech-qbank · Linux 服务器部署（10 步从零到可访问）

> 目标：一台 VPS + 一个域名 + TLS 终止，浏览器打开 `https://你的域名` 能登录、能刷题、能上传 PDF。
>
> ⚠️ **本文档里的每一步都别跳。** 跳错一步的后果不是"用不了"，
> 而是**你的学习数据全泄露**或者**限流形同虚设**。
>
> 配套：`docs/public-deploy.md`（每个配置项的含义）、`docs/deploy-checklist.md`（要准备什么）。

---

## 0. 先看这三条（能省你两小时）

### ① `dist\mech-qbank.exe` 在 Linux 上**跑不了**

那个 exe 是 **PyInstaller 在 Windows 上打的**，是 Windows PE 二进制格式。
Linux 内核无法执行它 —— 报 `cannot execute binary file: Exec format error`。

**所以 VPS 部署只能走源码运行**：

```
Windows 桌面：双击 mech-qbank.exe          （frozen 分支）
Linux 服务器：python app/api.py + venv      （非 frozen 分支）← 你在这里
```

这不只是"换个启动方式"，**PDF worker 的 spawn 形态也不同**：

| | frozen（Windows exe） | 非 frozen（Linux 源码） |
|---|---|---|
| worker 命令 | `[exe, '--pdf-worker', IN, OUT]` | `[python, app/pdf_worker.py, IN, OUT]` |
| 分发在 | `app/api.py` 顶部（`import core` 之前） | `app/pdf_worker.py` 自己解析 argv |
| 验证覆盖 | `tests/test_frozen_dispatch.py`（含真 exe） | `tests/test_source_deploy.py`（2026-10-21 新增） |

`tests/test_source_deploy.py` 就是为这条路径补的：**真起子进程、真解析真 PDF**，
验 worker 能跑、结果正确、不开端口、MuPDF 只在 worker 侧、缺 pymupdf 时能诊断。

### ② `requirements.txt` 里**没有** pymupdf，但服务器上**必须装**

`requirements.txt` 刻意不含 pymupdf，因为 **Web 进程不 import 它**。

但 PDF **worker 子进程必须有** —— `app/pdf_extract.py:78` 是全项目唯一
`import pymupdf` 的地方。worker 与 Web 进程用**同一个解释器**，所以要装进同一个 venv。

```bash
.venv/bin/pip install -r requirements-server.txt
```

**这不会让 Web 进程背上 MuPDF 的内存**：装在环境里 ≠ 被 import。
实测（`tests/test_source_deploy.py` 的 d）：`import pdf_client` 之后
`sys.modules` 里**没有** pymupdf，只有 worker 子进程里才有（约 117 MB）。

⚠️ **忘了装的症状**：Web 一切正常，但**上传 PDF 永远失败**。
客户端报 `worker_crashed`，日志里有 `import_error`（不是超时）——
所以能自检出来，不用干等。

### ③ 服务器上没有桌面，`webbrowser` 必须关掉

`app/api.py` 顶层 `import webbrowser`（标准库，Linux 上 import 没问题），
但**只有** `QBANK_NO_BROWSER != '1'` 时才会调 `webbrowser.open()`。

- systemd unit 里已设 `QBANK_NO_BROWSER=1` → **完全不走那条路**，无需处理 DISPLAY。
- 万一忘了设：Linux 上 `webbrowser.open()` 在没有 DISPLAY 时通常**返回 False 而不抛异常**
  （它会尝试 `xdg-open`/`www-browser` 等，找不到就放弃），但：
  - 它在 `threading.Timer(0.8, ...)` 里跑 → **不影响服务启动**；
  - 但会在 journal 里留一条 `xdg-open: not found` 之类的噪音。
  - 极端情况：环境变量 `BROWSER` 指向坏命令时 `subprocess` 会抛，
    但那也只发生在那个 Timer 线程里 → **只打一条 traceback，不影响服务**。
- **结论**：设 `QBANK_NO_BROWSER=1` 就够了，不需要额外处理 DISPLAY。

---

## 1. 你需要改哪几行（占位符表）

⚠️ **所有 `${...}` 都是占位符，必须手工替换**（Caddyfile 与 systemd 都不做变量展开）。

| 文件 | 占位符 | 换成 | 说明 |
|---|---|---|---|
| `deploy/Caddyfile` | `${DOMAIN}` | `qbank.example.com` | 你的域名。**必须与 systemd 里的 `QBANK_ALLOWED_HOSTS` 完全一致** |
| `deploy/Caddyfile` | `${ACME_EMAIL}` | `you@example.com` | 只用于收证书到期通知 |
| `deploy/Caddyfile` | `${APP_PORT}` | `8640` | 应用端口。**必须与 systemd 的 `QBANK_PORT` 一致** |
| `deploy/mech-qbank.service` | `${SERVICE_USER}` | `mechqbank` | 专用系统用户（**不要用 root**） |
| `deploy/mech-qbank.service` | `${SERVICE_GROUP}` | `mechqbank` | 同上 |
| `deploy/mech-qbank.service` | `${APP_DIR}` | `/opt/mech-qbank` | 代码目录 |
| `deploy/mech-qbank.service` | `${DATA_DIR}` | `/opt/mech-qbank/data` | 数据目录。**必须与 `APP_DIR` 下的 `data/` 一致**（core.py 的 `DATA`） |
| `deploy/mech-qbank.service` | `${DOMAIN}` | `qbank.example.com` | 同 Caddyfile |
| `deploy/mech-qbank.service` | `${PASSWORD_HASH}` | `scrypt$32768$8$1$...` | `tools/hash_password.py` 生成 |
| `deploy/mech-qbank.service` | `${TOKEN_SECRET}` | 64 位 hex | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `deploy/mech-qbank.service` | `${APP_PORT}` | `8640` | 同 Caddyfile |
| `deploy/backup.sh` | `${APP_DIR}` / `${DATA_DIR}` / `${BACKUP_DIR}` | 同上 | 可用环境变量覆盖，不必改文件 |

> ⚠️ **端口和域名必须两边一致** —— 不一致的表现是 403（Host 白名单不匹配）
> 或 502（反代连不上）。这是部署时最常见的错。

---

## 2. 十步部署

### 第 1 步：装系统包

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip sqlite3 git
```

> `sqlite3` **可选但强烈建议**：装上之后 `deploy/backup.sh` 会用
> `sqlite3 .backup` 做**在线热备份**（WAL 模式安全，不用停服务）。
> 没装的话脚本会退回到「停服务 + cp」，备份期间服务短暂不可用。

### 第 2 步：建专用用户与目录

```bash
sudo useradd --system --home /opt/mech-qbank --shell /usr/sbin/nologin mechqbank
sudo mkdir -p /opt/mech-qbank
sudo chown -R mechqbank:mechqbank /opt/mech-qbank
```

> ⚠️ **不要用 root 跑服务。** 理由见 `deploy/mech-qbank.service` 文件末尾。

### 第 3 步：拉代码

```bash
sudo -u mechqbank git clone https://github.com/daotu-123/mech-qbank.git /opt/mech-qbank
cd /opt/mech-qbank
```

**验证**：
```bash
ls app/api.py app/security.py web/dist/index.html
# 应能看到 api.py、security.py 与 web/dist/index.html
```

### 第 4 步：建 venv 并装依赖（含 pymupdf）

```bash
cd /opt/mech-qbank
sudo -u mechqbank python3 -m venv .venv
sudo -u mechqbank .venv/bin/pip install -r requirements-server.txt
```

**验证**（两条都要过）：
```bash
# a) Web 依赖
.venv/bin/python -c "import fastapi, uvicorn, fsrs; print('web deps ok')"
# b) worker 依赖 —— 这一条最容易被漏
.venv/bin/python -c "import pymupdf; print('pymupdf', pymupdf.__doc__.split(':')[0])"
```

> ⚠️ **b 条失败 = 上传 PDF 一定失败。** 先把它验掉。

### 第 5 步：放题库与前端

```bash
cd /opt/mech-qbank
# 题库（可选）：不放就用公开子集 266 题
# scp data/qbank.sqlite mechqbank@你的VPS:/opt/mech-qbank/data/qbank.sqlite
# 前端产物：仓库里已带 web/dist/（随库提交），不用 npm run build
ls web/dist/index.html
```

**验证**：
```bash
ls data/                    # 应该有 qbank.sqlite 或 public/
ls web/dist/assets/         # 应该有 index-*.js
```

### 第 6 步：生成口令哈希与令牌密钥

```bash
cd /opt/mech-qbank
sudo -u mechqbank .venv/bin/python tools/hash_password.py
# 交互输入口令（不回显），输出：
#   QBANK_PASSWORD_HASH=scrypt$32768$8$1$<salt>$<hash>

.venv/bin/python -c "import secrets; print(secrets.token_hex(32))"
# 输出 64 位 hex —— 这是会话密钥
```

> ⚠️ **口令哈希可以进配置**（它是哈希不是明文）；**明文口令绝对不行**。
> 交互式输入比命令行参数安全 —— 命令行参数会留在 shell 历史里。

**验证**：
```bash
echo "scrypt$" && echo "  格式对：scrypt$n$r$p$salt$hash"
```

### 第 7 步：装 systemd unit

```bash
sudo cp /opt/mech-qbank/deploy/mech-qbank.service /etc/systemd/system/
sudo nano /etc/systemd/system/mech-qbank.service   # ← 替换所有 ${...}
sudo systemctl daemon-reload
```

**验证**（语法与关键项）：
```bash
sudo systemd-analyze verify /etc/systemd/system/mech-qbank.service
# 无输出 = 语法没问题
grep -n '${' /etc/systemd/system/mech-qbank.service
# 应该**没有输出** —— 还有 ${} 就是没替换干净
```

> ⚠️ `grep '${'` 必须为空。漏掉一个占位符 → systemd 会把它当**字面量路径**，
> 表现为「服务起不来，报文件不存在」。

### 第 8 步：装 Caddyfile

```bash
sudo cp /opt/mech-qbank/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile      # ← 替换所有 ${...}
sudo caddy validate --config /etc/caddy/Caddyfile
```

**验证**：
```bash
sudo caddy validate --config /etc/caddy/Caddyfile
# 期望：Valid configuration
grep -n '${' /etc/caddy/Caddyfile     # 应该没有输出
```

### 第 9 步：起服务

```bash
sudo systemctl enable --now mech-qbank
sudo systemctl status mech-qbank
sudo journalctl -u mech-qbank -n 50 --no-pager
```

**验证（三条都要过）**：
```bash
# a) 健康检查（走回环，不经过反代）
curl -s http://127.0.0.1:8640/health/live     # 期望 {"status":"ok"}
curl -s http://127.0.0.1:8640/health/ready    # 期望 {"status":"ready"}
# b) 鉴权生效（公网模式下必须 401）
curl -i http://127.0.0.1:8640/api/v1/stats | head -1   # 期望 HTTP/1.1 401
#    ⚠️ 注意：Host 头要用 127.0.0.1 会被白名单拦成 403 —— 那也说明白名单在生效。
#    要单独验 401 请用域名 Host，见下面第 10 步。
# c) 服务在跑
systemctl is-active mech-qbank                # 期望 active
```

> ⚠️ **服务起不来时先看 journal**，99% 是配置问题：
> - `配置错误：公网模式必须设置 QBANK_PASSWORD_HASH` → 第 6 步没做
> - `配置错误：公网模式必须设置 QBANK_ALLOWED_HOSTS` → 第 7 步没替换
> - `QBANK_ALLOWED_HOSTS 里有非本机名但没有开启鉴权` → 半开配置（见 §0 与部署指南）
> - `找不到 web/dist` → 第 5 步没放前端

### 第 10 步：重载 Caddy 并做端到端自检

```bash
sudo systemctl reload caddy
```

**验证（照着做，10 分钟）**：

```bash
# 1) 域名能打开
curl -i https://你的域名/health/live
# 期望 HTTP/2 200

# 2) 陌生 Host 必须被拒（403）—— 这条要过，说明白名单真的在生效
curl -i -H 'Host: evil.example.com' https://你的域名/api/v1/stats
# 期望 403

# 3) 无令牌必须被拒（401）—— 这次要用域名 Host
curl -i -H 'Host: 你的域名' https://你的域名/api/v1/stats
# 期望 401

# 4) 登录拿令牌
curl -s -X POST https://你的域名/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"password":"你的口令"}'
# 期望 {"auth_required":true,"token":"tok_...","expires_at":...}

# 5) 带令牌能用
TOKEN=把上一步的 token 粘这里
curl -s -H "Authorization: Bearer $TOKEN" https://你的域名/api/v1/stats
# 期望 JSON 数据
```

浏览器里再手测一遍：
- [ ] 打开 `https://你的域名` → **弹出登录框**
- [ ] 输**错**口令 → 被拒；连打 6 次 → 开始变慢/429
- [ ] 输**对**口令 → 登录成功，页面能刷题
- [ ] 答一题 → **刷新页面，进度还在**（说明真的落盘了）
- [ ] 上传一个 PDF → 能解析、能送去做摘录
- [ ] 手机浏览器打开同一域名 → 同样能用

---

## 3. 备份与恢复

```bash
# 备份（建议放进 cron）
sudo -u mechqbank /opt/mech-qbank/deploy/backup.sh backup

# 看有哪些备份
sudo -u mechqbank /opt/mech-qbank/deploy/backup.sh list

# 校验最近一份
sudo -u mechqbank /opt/mech-qbank/deploy/backup.sh verify

# 恢复（会自动先校验，校验不过拒绝恢复）
sudo -u mechqbank /opt/mech-qbank/deploy/backup.sh restore /var/backups/mech-qbank/mech-qbank-20261021-030000.tar.gz
```

**加定时任务**（crontab -e，root 的 crontab）：

```cron
# 每天 03:17 备份（避开整点，错开其它机器）
17 3 * * * APP_DIR=/opt/mech-qbank BACKUP_DIR=/var/backups/mech-qbank /opt/mech-qbank/deploy/backup.sh backup >> /var/log/mech-qbank/backup.log 2>&1
```

> ⚠️ **备份必须有一份在本机之外。** 磁盘坏了就一起没了。
> 至少再同步一份到别处（对象存储 / 另一台机器 / NAS）。
> ⚠️ **恢复要演练一次** —— 真出事时才发现备份脚本没跑，是最糟的结局。

**备份了什么**（`deploy/backup.sh` 头部有完整说明）：
- `qbank.sqlite`（**全量模式下进度就在这个文件的表里**）
- `progress.json`（公开模式的进度）
- `notes.json`（**题目备注 —— 手写的心血，最容易漏**）
- `checkin.json`（打卡）
- `private/documents/`（上传的 PDF 原文件）

> ⚠️ **只备份 sqlite 不够**（备注/打卡在 JSON 里），
> **只备份 JSON 也不够**（全量模式的进度在 sqlite 表里）。脚本两个都备。

---

## 4. 常见问题

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 服务起不来，journal 报「配置错误」 | 缺 `QBANK_PASSWORD_HASH` / `QBANK_ALLOWED_HOSTS` | 看 §0 与部署指南 §2 |
| 服务起不来，报「文件不存在」 | systemd unit 里还有 `${...}` 没替换 | `grep -n '${' /etc/systemd/system/mech-qbank.service` |
| 访问域名返回 403 | 域名与 `QBANK_ALLOWED_HOSTS` 不一致 | 两边必须**完全一样**（含大小写、含端口后缀） |
| 访问域名返回 502 | 反代连不上应用 | `QBANK_PORT` 与 Caddyfile 的 `${APP_PORT}` 不一致；或服务没起 |
| **上传 PDF 一直失败** | **venv 里没装 pymupdf** | §0 的第 ④ 条；日志里会有 `import_error` |
| 所有人共用一个限流额度 | 没设 `QBANK_TRUSTED_PROXIES` | 设成 `127.0.0.1`（本机 Caddy） |
| 登录框不出现 | 没设 `QBANK_PUBLIC=1` 或 `QBANK_PASSWORD_HASH` | 两者都要；缺一个都会走半开检查 |
| 证书申请失败 | 80 端口被占 / DNS 没生效 / 用了 CDN 代理 | `dig 你的域名` 看是否已解析到本机 IP |
| 备份脚本报 `sqlite3: not found` | 没装 sqlite3 | `sudo apt install sqlite3`（或接受停服务备份） |
| 备份后恢复出来是双层目录 | 备份包结构异常 | 脚本已加保险会中止并报错；查备份包内容 |

---

## 5. 一键回滚

```bash
# 服务有问题，先停掉
sudo systemctl stop mech-qbank
# 回滚代码到上一个版本
sudo -u mechqbank -H git -C /opt/mech-qbank checkout <上一个 commit>
sudo -u mechqbank -H git -C /opt/mech-qbank pull
# 重启
sudo systemctl start mech-qbank
```

**数据回滚**用 `deploy/backup.sh restore`（它会先校验、失败则拒绝恢复）。

---

## 6. 相关文档

- `docs/public-deploy.md` —— 每个配置项的含义与全部环境变量表
- `docs/deploy-checklist.md` —— **部署前要准备什么**（VPS / 域名 / TLS / 题库 / 备份）
- `docs/architecture-baseline.md` §0 —— 目标形态与授权边界
- `CHANGELOG.md` —— v3.0.0 的变更
