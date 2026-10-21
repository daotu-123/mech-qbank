# CHANGELOG

本文件记录对外可见的版本变化。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **日期说明**：本项目的版本号表示「可交付的一组能力」，不是「一次提交」。
> 下面每条都注明了对应的提交，便于回溯。

---

## [3.0.0] — 2026-10-21

本轮四件大事：**后端分层**、**统一错误格式**、**PDF 全链路**、**公网暴露安全**。
四件都是破坏性变更，故主版本号从 2 跳到 3。

### 破坏性变更

- **后端分层落地**（`2d19923` / `8c2d9ca` / `9ba61d7`）
  `app/` 的依赖方向固定为 `storage → repository → service → core → router`。
  新增 `app/storage.py`（文件 I/O 唯一出口）、`app/repository.py`（SQL 唯一出口）、
  `app/service.py`（业务用例，不做 HTTP 翻译）。两套服务端共用同一份数据层。
  **`storage`/`repository`/`service` 三个模块只允许标准库** —— `app/server.py`
  是零依赖回退服务端（`start.bat` 在没有 fastapi 时启动它），新层一旦 import
  第三方包，这条回退路径直接失效。
- **统一错误格式**（`8c944c6`）
  唯一真源是 `app/errors.py`（纯标准库），响应体固定为
  `{"error":{"code":...,"message":...,"request_id":...}}`。
  `request_id` 在响应体、`X-Request-Id` 响应头、日志三处保持一致。
- **健康检查端点**（`8c944c6`）
  新增 `/health/live`（不读数据层）与 `/health/ready`（真读，不可用时 503）。
  两者都放在 `/api/v1` **之外**，供反代探针使用。
- **许可从 MIT 改为 AGPL-3.0**（`021a7d5`）
  引入 PyMuPDF 的前置条件。AGPL 是强 copyleft，与 MIT 不兼容 ——
  "代码保留 MIT、只在内部用 PyMuPDF"在法律上不成立。
  ⚠️ 后果：**闭源分发 exe 不允许**。公网部署则需向使用者提供源码
  （AGPL §13；本仓库已公开即满足）。
- **`X-Forwarded-For` 不再被 uvicorn 自动采信**（`90c349e`）
  `proxy_headers` 显式关闭，改由 `app/security.py` 的 `client_ip()` 按
  fail-closed 规则自己判断（对端不在受信列表 → 完全忽略 XFF，用真实对端地址）。
  原因：uvicorn 默认 `proxy_headers=True` 会拿 XFF 覆盖 `request.client`，
  攻击者加个 XFF 头就能让限流按它声明的 IP 计数，**限流形同虚设**。

### 新增

- **PDF 文档域全链路**（`8555f43` / `fdc8288` / `8ada58b` / `6bb83f8`）
  - `app/pdf_extract.py`：纯函数抽取器（`extract(blob) -> ExtractResult`），
    统一走 words、不做分栏分层。
  - `app/pdf_worker.py` + `app/pdf_client.py`：**进程隔离**。Web 进程永远不持有
    MuPDF；解析在子进程里做，崩了不影响服务，判定只靠退出码 + 输出文件。
  - `app/docstore.py`：文档落盘 + job 表 + 后台解析线程。
    上传立即返回 202，解析异步进行。
  - `app/multipart.py`：纯标准库 multipart 解析（两端共用）。
  - 前端「PDF 文档」页：上传 → 轮询状态 → 页码锚点 → 送去摘录。
  - ⚠️ 表格结构化**首版不做**：`find_tables()` 在 500 页样本上耗时 +100.48s、
    内存 +368 MB。
- **公网暴露安全 W1–W4**（`90c349e`）
  - **W1** 绑定地址配置化（默认仍 `127.0.0.1`）。
  - **W2** `ALLOWED_HOSTS` 配置化（**精确匹配**，不支持通配/后缀 ——
    放宽会让 DNS rebinding 防线形同虚设）。
  - **W3** 单账号鉴权：`hashlib.scrypt` 口令哈希 + `hmac` + 服务端可撤销令牌表。
    **不引第三方库**（argon2-cffi 需编译扩展，JWT 撤不掉）。
  - **W4** 速率限制：登录失败指数退避（**有上限**，否则被打 1000 次等于永久封禁）
    + 上传滑动窗口限流。
  - 前端登录对话框（`web/src/auth.js` + `web/src/components/LoginDialog.vue`）：
    access token 只放**内存**，refresh token 放 **sessionStorage**，
    **localStorage 一个字节都不放**。
  - 部署步骤见 [`docs/public-deploy.md`](docs/public-deploy.md)。
- **测试**：`tests/test_security.py`（30 项）、`tests/fixtures/pdf/`（六类 PDF 验收样本）、
  `tests/verify_fixtures.py`（24 项）、PDF 抽取 14 项 / worker 31 项 / HTTP 32 项 / 前端 6 项。

### 修复

- **frozen worker 的 argv 分发**（`bd20653`）
  `app/api.py` 必须在 `import core` **之前**分发 `--pdf-worker`。
  否则 exe 会无条件去启动 Web 服务，worker 永不执行 → **所有 PDF 解析必然超时**。
- **`build_exe.bat` 漏打 pymupdf**（`916e83b`）
  需要 `pip install pymupdf==2.0` + `--collect-all pymupdf`
  （PyMuPDF 带约 105 MB 二进制资源，静态分析发现不了）。
- **PDF 抽取的分栏分层缺陷**（`e2d4938`）
  交错双栏的栏序会错。取消分层，统一走 words。
- **FSRS 事务脆弱点**（`48586c6`）：`_fsrs_save()` 不 commit 是**有意为之**
  （与 progress 同事务原子落盘），不是遗漏。
- **`smoke_test.py` 的 103 基线不可靠**（`4045cd1`）
  它把真实库的 progress 表一起复制进临时目录，打卡断言从 `last_seen` 聚合 →
  该数字随日期漂移。已修（清空 progress/fsrs_state），稳定线是 108 → 后端分层后 109。
- **auth 中间件挂载顺序**（`48586c6`）：Starlette 的中间件**后注册先执行**，
  auth 装饰器必须写在 guard 之前。
- **健康检查在公网模式下被 Host 白名单误拦**（`90c349e`）
  反代探针的 Host 是上游地址（不是用户域名）→ 探针全 503 → 反代把服务踢下线。
  已豁免**且仅豁免** `/health/live` 与 `/health/ready`。

### 已知边界

- PDF 表格**未**结构化（性能原因，见上）。
- PDF 解析的**硬内存上限未实现**（Job Objects），兜底是超时 + 串行 + 进程隔离。
- 批量上传、多账号**不做**。身份模型就是单账号。
- 令牌**不持久化到 localStorage**（理由见 `docs/architecture-baseline.md` §0.4）。
- 截断损坏的**百分比不可复现**，禁止当规格。

---

## [2.0.0] — 2026-10-17/18

- **架构迁移**：前端 Vue 3 + Element Plus（Vite，`web/`），后端 FastAPI
  RESTful（`app/api.py`），数据层抽成 `app/core.py` 两端共用；
  file:// 双模式保留（IIFE 单包 + 入口标签改写），判分/排期由 `smoke_parity.py` 差分守护。
- **exe 改 windowed 打包**，双击不再弹终端（配套安全输出、日志文件、页面退出按钮）。
- **排期换成 FSRS-6**（`c421172`）。
- **组卷按题型排序**（`a2e6da6`）：单选→多选→判断→…→计算垫底。
- **UI 改为「今日学习台」**（`15862de`）+ 响应式补全。
- 公开子集新增 190 道原创选择题（76 → 266 题）。
- 选择题答案归一 + 人工核定表（134 → 33 道自评）。

---

## [1.0.0] — 2026-10-17

- 首个可交付版本：题库浏览、随机刷题、艾宾浩斯复习、错题本、知识点卡片、学习打卡。
