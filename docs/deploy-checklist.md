# 部署到 VPS 前，主人需要准备什么

> 面向 owner 的**准备清单**。代码侧已经就绪（`docs/public-deploy.md` 讲的是怎么配），
> 本文只讲**你得先搞定什么**——域名、TLS、反代、备份，以及每一样东西的坑。
>
> 目标形态：**公网单用户 + 单账号**（`docs/architecture-baseline.md` §0.2 / §0.3）。
> 一台 VPS + 一个域名 + TLS 终止，你自己用，手机和电脑登录后访问同一份数据。
>
> ⚠️ **本文里的每一样都不要跳过。** 跳错的代价不是"用不了"，
> 而是**你的学习数据全泄露**或者**限流形同虚设**。

> 📦 **部署工件已经写好了**（2026-10-21），不用从零写配置：
>
> | 文件 | 干什么 |
> |---|---|
> | [`deploy/README.md`](../deploy/README.md) | **从零到可访问的 10 步**，每步带验证命令 + 「你要改哪几行」表 |
> | [`deploy/Caddyfile`](../deploy/Caddyfile) | 反代 + 自动 TLS + 大文件放行 + XFF 传递 |
> | [`deploy/mech-qbank.service`](../deploy/mech-qbank.service) | systemd unit（环境变量、重启策略、非 root、加固） |
> | [`deploy/backup.sh`](../deploy/backup.sh) | 备份 / 恢复 / 校验（已本地实跑验证） |
> | `requirements-server.txt` | 服务器依赖（含 pymupdf，见 §1.2） |
>
> **本文是「要准备什么」，`deploy/README.md` 是「怎么做」。** 两份配合看。

---

## 0. 一句话总览

| # | 要准备的 | 谁提供 | 缺了会怎样 |
|---|---|---|---|
| 1 | 一台 VPS（1 核 1 GB 就够） | 云厂商 | 没服务器就谈不上部署 |
| 2 | 一个域名 + 一条 A 记录 | 域名注册商 | 反代拿不到 Host 头 → 全部 403 |
| 3 | TLS 证书 | Let's Encrypt（Caddy 自动签） | 浏览器标"不安全"；微信内置浏览器可能直接拒 |
| 4 | 反向代理 | Caddy 或 Nginx | 公网流量打不到应用 |
| 5 | 全量题库 `qbank.sqlite` | 本机 `data/qbank.sqlite` | 只能用 266 道公开子集 |
| 6 | **数据备份**方案 | 脚本 + 定时任务 | 一次磁盘事故 = 全部进度/备注/打卡归零 |
| 7 | 一个够长够复杂的管理口令 | 你自己想 | 弱口令 = 数据裸奔 |
| 8 | 会话密钥 | `secrets.token_hex(32)` | 重启即所有旧会话失效（可接受，但要知情） |

---

## 1. VPS

- **规格**：1 核 1 GB 足够。这是自用单用户工具，不是服务大众。
  内存主要被 PDF 解析吃掉（500 页 PDF 峰值约 200 MB + PyMuPDF 常驻约 117 MB），
  但解析是**子进程**、**串行**的，1 GB 扛得住。
- **系统**：Ubuntu 22.04 / Debian 12 即可。
- **必须做的两件事**：
  1. **别把 8640 端口直接开到公网。** 应用只监听 `127.0.0.1`（或 `0.0.0.0` 但被防火墙挡住），
     流量必须经反代进来。安全组只放行 `22` / `80` / `443`。
  2. **应用绑 `127.0.0.1`，让反代去连它。** 很多人第一反应是"绑 0.0.0.0 对外"，
     那就等于把 FastAPI 直接暴露在公网上——绕过反代的 TLS、限流配置全部作废。

- ⚠️ **一定要开 `QBANK_PUBLIC=1`。** 这不是"公网模式才有鉴权"，而是
  **只要白名单里出现真实域名，就必须有鉴权**（2026-10-21 打包前自查发现的漏洞，
  已由 `start_errors()` 拦住并有测试守护）。

  半开配置长这样：

  ```bash
  QBANK_ALLOWED_HOSTS=qbank.example.com   # ← 有外部域名
  # 但没设 QBANK_PUBLIC，也没设 QBANK_PASSWORD_HASH
  ```

  它**看起来**是安全的（绑 127.0.0.1），但白名单里放着外部域名 =
  存在一条能把外部请求带进来的路径（反代 / 端口转发 / SSH 隧道）。
  本机直连会被 Host 白名单拦掉（403），你自测时看到"打不开"，
  很可能以为配置生效了；可反代把 `Host: qbank.example.com` 转发过来时
  请求全部放行——**任何人都能读走全部进度、备注、打卡记录**。

  现在这种配置会**直接拒绝启动**并打印原因。正确写法见 §8。

- **别装宝塔面板之类的可视化面板**：多一层攻击面，且容易把防火墙规则改乱。

### ⚠️ 1.1 部署形态是**源码运行**，不是跑那个 exe

**`dist\mech-qbank.exe` 在 Linux 上跑不了。** 它是 PyInstaller 在 **Windows** 上打的
（Windows PE 二进制），Linux 内核执行会报 `cannot execute binary file: Exec format error`。

所以 VPS 上是**源码运行**：

```bash
cd /opt/mech-qbank
python3 -m venv .venv
.venv/bin/pip install -r requirements-server.txt
.venv/bin/python app/api.py          # ← 这才是服务器上的启动方式
```

**这不只是"换个启动方式"，PDF worker 的 spawn 形态也不同**：

| | frozen（Windows exe，双击） | 非 frozen（Linux 源码） |
|---|---|---|
| worker 命令 | `[exe, '--pdf-worker', IN, OUT]` | `[python, app/pdf_worker.py, IN, OUT]` |
| argv 分发在 | `app/api.py` 顶部（`import core` 之前） | `app/pdf_worker.py` 自己解析 |
| 验证覆盖 | `tests/test_frozen_dispatch.py`（含真 exe） | `tests/test_source_deploy.py`（2026-10-21 新增） |

`tests/test_source_deploy.py` 就是为这条路径补的：**真起子进程、真解析真 PDF**，
验 worker 能跑、结果正确、不开端口、MuPDF 只在 worker 侧、缺 pymupdf 时能诊断。

> ⚠️ 桌面 exe 用户**不需要** `requirements-server.txt` ——
> 打包脚本自己会装 pymupdf（`build_exe.bat` 里的 `pip install pymupdf==2.0`
> + `--collect-all pymupdf`）。

### ⚠️ 1.2 服务器上必须装 pymupdf（但 Web 进程不 import 它）

`requirements.txt` 刻意**不含** pymupdf —— 因为 **Web 进程（`app/api.py`）不 import 它**，
保持零重依赖（`app/server.py` 是零第三方依赖的回退服务端，新层 import 第三方包会让那条路径失效）。

但 **PDF worker 子进程必须有它**：`app/pdf_extract.py:78` 是全项目唯一
`import pymupdf` 的地方，而它只在 worker 子进程里被加载。worker 与 Web 进程
**用同一个解释器**（`sys.executable`），所以要装进**同一个 venv**。

```bash
# 做法（推荐）：用专门的服务器依赖文件
.venv/bin/pip install -r requirements-server.txt
# 里面是 requirements.txt 的内容 + pymupdf==2.0

# 或等价的一行：
.venv/bin/pip install "pymupdf==2.0"
```

**这不会让 Web 进程背上 MuPDF 的内存**：装在环境里 ≠ 被 import。
实测（`tests/test_source_deploy.py` 的 d，用**干净子进程**验）：
`import pdf_client` 之后 `sys.modules` 里**没有** pymupdf，只有 worker 子进程里才有
（约 117 MB 的 MuPDF 运行时）。

⚠️ **忘了装的症状**：Web 一切正常，但**上传 PDF 永远失败**。
客户端报 `worker_crashed`，日志里有 `import_error`（**不是超时**）——
所以能自检出来，不用干等 60 秒。

```bash
# 部署后立刻自检：
.venv/bin/python -c "import pymupdf; print('pymupdf ok')"
```

### ⚠️ 1.3 服务器上没有桌面，`webbrowser` 必须关掉

`app/api.py` 顶层 `import webbrowser`（标准库，Linux 上 import 没问题），
但**只有** `QBANK_NO_BROWSER != '1'` 时才会调 `webbrowser.open()`。

- systemd unit 里已设 `QBANK_NO_BROWSER=1` → **完全不走那条路**。
- 万一忘了设：Linux 上 `webbrowser.open()` 在没有 DISPLAY 时通常**返回 False 而不抛异常**
  （它会试 `xdg-open` / `www-browser` 等，找不到就放弃），但会在 journal 里留噪音。
  极端情况（环境变量 `BROWSER` 指向坏命令）会在那个 Timer 线程里抛 →
  **只打一条 traceback，不影响服务**（它在 `threading.Timer(0.8, ...)` 里，不在启动路径上）。
- **结论**：设 `QBANK_NO_BROWSER=1` 就够了，**不需要额外处理 DISPLAY**。

---

## 2. 域名与 DNS

- 一个 A 记录：`qbank.example.com` → VPS 公网 IP。
  **只开 A 记录，不要开泛解析**（`*`）——泛解析会让任意子域都能指向这台机器。
- **TTL 设短一点**（300 秒），方便日后换 IP。
- 域名不需要备案（中国大陆机房需要），但**用大陆 IP 的话 80/443 端口需要备案**，
  否则会被拦截。用香港/海外 IP 无此问题。

---

## 3. TLS 证书

- **推荐 Caddy**：自动申请 + 自动续期，零配置。
  ```caddyfile
  qbank.example.com {
      reverse_proxy 127.0.0.1:8640
  }
  ```
  Caddy 默认：
  - 自动 HTTPS + 自动续期证书 ✅
  - 自动带 `X-Forwarded-For` ✅（但服务端只在反代被登记为受信时才采信它）
  - 保留原始 `Host` 头 ✅（与白名单对齐的前提）
- **用 Nginx 的话**：证书要自己申请（certbot）+ 自己写续期定时任务。
  另外必须手动加 `client_max_body_size 12m;`，默认 1 MB 会让 PDF 上传 413。

> **为什么必须 HTTPS**：口令与令牌走明文 HTTP 的话，任何公共 WiFi 都能截获。
> 不要为了"省事"用 `http://` 直连。

---

## 4. 反向代理：两个必做项

| # | 做什么 | 不做的后果 |
|---|---|---|
| 1 | **登记反代为受信代理** `QBANK_TRUSTED_PROXIES=127.0.0.1` | 服务端忽略 XFF，一律用真实对端地址 = 反代 IP。**所有请求的限流计数都算在反代头上** → 一个人就能把全站打限流 |
| 2 | **放行 ≥10 MB 请求体** | PDF 上传返回 413 |

⚠️ 反代**不要**把 `Host` 改写成 `127.0.0.1:8640`。改了的话白名单里就得加 `127.0.0.1`，
而那样公网访问也会被当成本机访问 —— 白名单就形同虚设。

---

## 5. 迁移你的全量题库

公开子集只有 266 道题；你的全量库在本机 `data/qbank.sqlite`（2330 道，含公司真题等本地数据）。

```bash
# 在本机
scp data/qbank.sqlite user@vps:/srv/mech-qbank/data/qbank.sqlite
```

- `data/qbank.sqlite` **不入库**（`.gitignore` 已排除），只能手动传。
- 传到 VPS 后程序自动切全量模式（`data/qbank.sqlite` 存在即切换）。
- ⚠️ **这个文件里有你本地的全部题目内容**——如果里面有公司真题，
  通过任何渠道传都要考虑保密要求（scp 走 SSH 是加密的，别用网盘/微信传）。

---

## 6. 数据备份（**最容易被忽略、代价最大的一项**）

线上会落盘的数据（**都在 `data/` 目录**）：

| 文件 | 内容 | 丢了会怎样 |
|---|---|---|
| `qbank.sqlite` | 题库 **+ 全量模式下的全部进度** | 可从本机重新传，但进度归零 |
| `progress.json` | **公开模式**下的答题进度 / 掌握度 / 错题 | 全部学习记录归零 |
| `notes.json` | 题目备注 | 全部笔记丢失（这往往是手写的心血） |
| `checkin.json` | 打卡记录 | 连续天数断裂 |
| `private/documents/` | 上传的 PDF 原文件 | 摘录素材丢失 |

> ⚠️ **进度存在哪 depends on 模式**（`core.py:11-19`）：
> - **全量模式**（`data/qbank.sqlite` 存在）→ 进度写进 sqlite 的 **progress 表**，
>   FSRS 状态写进 **fsrs_state 表**。此时 `progress.json` **根本不存在**。
> - **公开模式**（只有 `data/public/qbank.json`）→ 进度写进 `progress.json`。
>
> 所以 **两个都要备份** —— 只备 sqlite 会丢备注和打卡，只备 JSON 会丢全量模式的进度。

**用现成的脚本**（`deploy/backup.sh`，已本地实跑验证过备份+恢复+校验）：

```bash
# 备份（会立刻自校验：sha256 + tar 完整性 + JSON 合法 + sqlite integrity_check）
sudo -u mechqbank /opt/mech-qbank/deploy/backup.sh backup

# 加到 root 的 crontab（crontab -e）—— 每天 03:17，避开整点错开其它机器
17 3 * * * APP_DIR=/opt/mech-qbank BACKUP_DIR=/var/backups/mech-qbank /opt/mech-qbank/deploy/backup.sh backup >> /var/log/mech-qbank/backup.log 2>&1
```

脚本自带这些保护：
- **sqlite 用 `sqlite3 .backup` 做在线热备份**（WAL 模式安全，不用停服务）；
  没装 `sqlite3` 时退回到「停服务 + cp」。
- **备份完立刻校验**，校验不过直接报错（不产出"看起来能用"的备份）。
- **恢复前先校验**，校验不过**拒绝恢复**（不拿未经验证的数据覆盖现网）。
- 恢复时把现网数据**先挪到临时目录**（不删），出问题还能找回。
- 自动清理超过 14 天的旧备份。

**手动方案**（如果不想用脚本）：

```bash
# 1) 每日凌晨 3:17 备份（避开整点，错开其它机器）
17 3 * * * cd /srv/mech-qbank && tar czf backup-$(date +\%F).tar.gz data/
# 2) 保留 14 天
0 4 * * * find /srv/mech-qbank -name 'backup-*.tar.gz' -mtime +14 -delete
```

- **别只备份在 VPS 本机**：磁盘坏了就一起没了。至少再同步一份到别处
  （对象存储 / 另一台机器 / NAS）。
- **恢复要演练一次**：真出事时才发现备份脚本没跑，是最糟的结局。
  恢复步骤：停服务 → 解压覆盖 `data/` → 起服务。
- ⚠️ `progress.json` 等是**文件 I/O**（不是 sqlite），备份时别漏。
- ⚠️ **正在写的时候直接 tar sqlite 会拿到撕裂的快照** —— 脚本用
  `sqlite3 .backup` 避开这个坑；手动方案要先停服务。

---

## 7. 管理口令与会话密钥

```bash
# 口令哈希（交互输入，不回显、不留 shell 历史）
python tools/hash_password.py
# → QBANK_PASSWORD_HASH=scrypt$32768$8$1$<salt>$<hash>

# 会话密钥
python -c "import secrets; print(secrets.token_hex(32))"
```

- **口令至少 12 位、别用生日/手机号/题库里的词。**
  scrypt 是内存硬哈希（32 MB/次），但**弱口令在离线爆破面前依然无效**——
  哈希算法只能拖慢暴力破解，拖不赢弱密码本身。
- **口令哈希可以进配置仓库**（它是哈希不是明文）；**明文口令绝对不行**。
- 会话密钥丢了/换了 = 所有已登录设备被登出。改口令哈希后重启同理。

---

## 8. 环境变量清单（部署时的实际配置）

> 📌 **这些值已经写进 [`deploy/mech-qbank.service`](../deploy/mech-qbank.service) 了。**
> 部署时只需要把里面的 `${...}` 占位符替换掉，不用在这里再抄一遍。

```bash
# ✅ 推荐配置：应用层有鉴权 + 网络层只听回环 + 反代在前面挡
QBANK_PUBLIC=1                  # 强制开启鉴权（并强制要求下面的哈希与域名）
QBANK_PASSWORD_HASH='scrypt$32768$8$1$...'   # tools/hash_password.py 生成
QBANK_ALLOWED_HOSTS=qbank.example.com
QBANK_BIND=127.0.0.1            # 只听回环，由反代挡在前面
QBANK_TRUSTED_PROXIES=127.0.0.1 # 本机的 Caddy
QBANK_PORT=8640
QBANK_TOKEN_SECRET='<64 位 hex>' # 会话密钥
QBANK_NO_BROWSER=1              # 服务器没有桌面（见 §1.3）
```

- `QBANK_PUBLIC=1` 默认让应用绑 `0.0.0.0`。**显式设 `QBANK_BIND=127.0.0.1`** 让它只听回环。
  此时**端口绝不能**在安全组里对公网开放 —— 只留 `22/80/443`。
- **不要**配「白名单有域名但没哈希」这种半开组合：现在会**拒绝启动**并说明原因
  （见 §1 的说明）。
- 想改严/改松：见 `docs/public-deploy.md` §2 的完整变量表与 §6 的调整口径。

---

## 9. 部署后自检（照着做，10 分钟）

> 完整版在 [`deploy/README.md` 第 10 步](../deploy/README.md)。这里是速查版。

```bash
# 1) 健康检查（反代探针打的就是这个，必须 200）
curl -i http://127.0.0.1:8640/health/live

# 2) 走域名 + TLS（公网模式下必须是 401）
curl -i https://qbank.example.com/api/v1/stats

# 3) 陌生 Host 必须被拒（403）—— 这条要过，说明白名单真的在生效
curl -i -H 'Host: evil.example.com' https://qbank.example.com/api/v1/stats

# 4) 上传一个真 PDF，走通上传→解析→取结果
#    ⚠️ 这一条同时验了「服务器上装了 pymupdf」（见 §1.2）
curl -i -X POST https://qbank.example.com/api/v1/documents \
     -H "Authorization: Bearer $TOKEN" \
     -F 'file=@你的.pdf'

# 5) 备份任务真的在跑
crontab -l | grep backup.sh
```

另外手测一遍：

- [ ] 手机浏览器打开域名，能看到登录框并能登录
- [ ] 答一题、刷新页面，进度还在（说明真的落盘了，不是只在内存）
- [ ] 故意输错口令连打 6 次，第 6 次开始应该被限流
- [ ] 上传一个 PDF 能解析（验 §1.2 的 pymupdf 装对了）
- [ ] 断网重连后仍能访问（TLS 证书没问题）
- [ ] **跑一次备份 + 恢复演练**（`deploy/backup.sh backup` 然后 `restore`）

---

## 10. 明确不做的事（省得你到时候期待）

- **不做多账号 / 注册流程** —— 身份模型是单账号（`docs/architecture-baseline.md` §0.3）。
  要给别人用是**另立项**（那要把 `progress` 表主键从 `id` 改成 `(user_id, qid)`，是重写不是重构）。
- **不做批量上传** —— 一次一个 PDF。
- **不做 PDF 表格结构化** —— 性能原因，见 `CHANGELOG.md`。
- **不做令牌持久化到 localStorage** —— 理由见 `docs/architecture-baseline.md` §0.4。
- **闭源分发 exe 不允许** —— AGPL §13 + PyMuPDF 是强 copyleft。

---

## 11. 需要主人决策的两件事

1. **VPS 选哪家 / 哪个地区？** 影响备案与延迟。
2. **数据备份存哪？** 只在 VPS 本机，还是再同步一份到别处？
   （我的建议：必须有一份在本机之外。）

这两件定了之后，部署工件（systemd unit / Caddyfile / 备份脚本）已经写好了，
照着 [`deploy/README.md`](../deploy/README.md) 的 10 步走就行。
