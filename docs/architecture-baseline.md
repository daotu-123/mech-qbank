# mech-qbank 架构基线（2026-10-19 审计快照，2026-10-20 补 §0 与 §8-i）

> 本文只记录**审计时点的客观事实**与证据位置（`文件:行号`），不含重构建议、不含任何题目内容。
> 结论若与本文冲突，以代码为准；发现本文过时请连同证据一起改本文。
> 数据口径：`data/qbank.sqlite` 为本机私有全量库（不入库），本文出现的题数统计取自审计当日的库。
> 两处后补章节：**§0 形态与部署决策**（2026-10-20，Gate 0 缺项）、**§8-i 测试数据污染**（2026-10-20，审计后追加）。

---

## 0. Gate 0：产品形态与部署形态（决策前提）

> 本节回答「这个项目**是什么形态的东西**」，因为它决定了后面所有重构的可行边界。
> 结论若与代码冲突，以代码为准。

### 0.1 三种产品形态

| 形态 | 定义 | 与当前代码的距离 |
|---|---|---|
| **A. 本机单用户工具** | exe / APK 双击即用，数据只在本机，无账号、无网络服务 | ✅ **当前所处形态** |
| **B. 局域网多设备** | 电脑跑服务，手机/平板经 IP 访问同一份数据 | 差 3 件事：监听地址、`ALLOWED_HOSTS` 白名单、鉴权 |
| **C. 公网多用户 SaaS** | 有账号体系、云端数据库、按用户隔离数据 | 差一整个数据层（用户表、按 user 过滤、并发写隔离） |

### 0.2 三种部署形态

| 部署 | 监听 | 鉴权 | 数据隔离 |
|---|---|---|---|
| **本机** | `127.0.0.1` | 无（靠白名单挡 DNS rebinding） | 单文件即天然隔离 |
| 公网单用户 | `0.0.0.0` + TLS | 必须有 | 单文件仍可，但暴露在公网 |
| 多用户 SaaS | `0.0.0.0` + TLS | 必须有，且要按用户分库/分表 | 必须重构 |

### 0.3 结论：采用默认「形态 A + 本机部署」

**形态 A + 本机部署**。这是代码现状的如实描述，也是本次重构的授权范围。

代码证据（逐条可验证）：

| 事实 | 证据 |
|---|---|
| 只监听回环地址 | `api.py:316` `host='127.0.0.1'`；`server.py` 同；`core.py:20` 注释自述 |
| Host/Origin 白名单只有三个 loopback 字面量 | `core.py:99` `ALLOWED_HOSTS = {'127.0.0.1','localhost','[::1]'}` |
| 全项目**零账号、零 token、零 session** | `web/src/**` 与 `app/**` 检索 `login|token|auth|userId|sync` 无业务命中（仅 `api.js` 的函数名与 `async` 关键字） |
| 数据是单文件 sqlite + 单把 `RLock` | `core.py:245` `sqlite3.connect(DB, check_same_thread=False)`、`core.py:240` `self.lock` |
| 7 个视图无登录页/多用户切换 | `web/src/views/`：`TodayView / QuizView / ReviewView / WrongView / CardsView / StatsView / ExcerptView` |
| 打包目标是桌面双击即用 | `start.bat` 拉起 `--windowed` exe；APK 走 Capacitor |

### 0.4 这条决策对重构的约束（可直接当验收标准）

**在形态 A 下，以下改动一律不做**（做了就等于偷偷引入形态 B/C，属于越权）：

1. 不引入账号 / 登录 / token / session 中间件
2. 不把 `127.0.0.1` 改成 `0.0.0.0`，不改 `ALLOWED_HOSTS` 白名单
3. 不引入云端数据库、不做跨设备同步、不引入按用户过滤数据的字段
4. 不做并发多写的隔离改造（单 `RLock` + 单文件 sqlite 在形态 A 下够用）

**在形态 A 下，以下是可做且应当做的**：

1. 消除双端重复（判分/排期/组卷口径两份实现 → 单一真源 + 生成）
2. 拆分超大文件（`core.py` 948 行、`ExcerptView.vue`、`QuestionCard.vue`、`static-store.js` 726 行）
3. 补测试安全网与真实起进程的冒烟（现有 `smoke_test.py` 不调 `main()`，见 §8-c）
4. 把测试从真实数据里隔离出来（见 §8-i）
5. 收拢魔法数字与 CSS 变量

### 0.5 若将来要升到形态 B / C

不在本次范围，但记下代价，避免将来误以为"顺手就能改"：

- **形态 B（局域网）**：改 `api.py:316` 监听地址 + 放宽 `ALLOWED_HOSTS` + 加一层共享口令（或只读模式）。
  额外风险：当前 `check_same_thread=False` + 单 `RLock` 在多设备并发写下仍成立，但 `mark()` 的 1 秒防抖落盘（`core.py:629-666`）在多客户端下会互相覆盖。
- **形态 C（SaaS）**：数据层要重写成按用户分库，`progress` 主键从 `id` 变成 `(user_id, qid)`，
  `checkin()` 的 `progress.last_seen` 聚合（`core.py:710`）也要带用户维度。**这不是重构，是重写。**

---

## 1. 架构图：职责与依赖方向

```
                        ┌──────────────────────────────────────────┐
   浏览器 (http://) ───▶│  app/api.py    FastAPI + RESTful 传输层   │──┐
   浏览器 (file://) ───▶│  web/src/api.js  → staticRoute() 分流     │  │
                        └──────────────────────────────────────────┘  │
                        ┌──────────────────────────────────────────┐  │
                        │  app/core.py   数据层（零第三方依赖）      │◀─┘
                        │  · Store：sqlite / public JSON 自动降级  │
                        │  · grade / mark / schedule / pick         │
                        │  · checkin / notes / custom 题 CRUD       │
                        │  · FSRS 排期（py-fsrs）、_log 日志         │
                        └───────────────┬──────────────────────────┘
                                        │ from core import ...
                        ┌───────────────▼──────────────────────────┐
                        │  app/server.py  标准库 http.server 版     │  ← 遗留回退路径（见 §8-c）
                        │  同一份 core.py，传输层是 if 路由链        │
                        └──────────────────────────────────────────┘

   web/（Vue 3 + Element Plus + Vite）
     ├─ api.js ─────────┬─ 在线：fetch /api/v1/*        → app/api.py
     │                  └─ file://：staticRoute() → static-store.js（纯 JS，无网络）
     ├─ store/static-store.js ── 手抄 core.py 的判分/排期/组卷口径
     ├─ store/{session,notes,meta,bus}.js ── 视图共享状态
     ├─ views/ (7) + components/ (7)
     └─ util.js / styles.css
   tools/（构建与测试，不参与运行时）
     qbank_build.py（源库→sqlite+公开子集）、make_webdata.py（公开子集→qbank-data.js）、
     smoke_test.py / smoke_api.py / smoke_static.js / smoke_parity.py / audit_types.py /
     audit_choice_answers.py、answer_fix.tsv（人工核定答案表）
```

**依赖方向（单向，无循环）**

| 层 | 依赖 | 反向依赖 |
|---|---|---|
| `app/api.py` | `app/core.py`、fastapi/uvicorn | 无 |
| `app/server.py` | `app/core.py`（`import core` + `from core import *`） | 无 |
| `app/core.py` | 仅标准库（可选 `py-fsrs`） | 不知道 HTTP 存在 |
| `web/src/**` | `api.js` → `static-store.js` | 无 |
| `tools/**` | `app/core.py`（差分/审查）、`web/src/store/static-store.js`（Node 侧） | 无 |

**双端手抄关系（唯一的跨语言耦合）**：`app/core.py` 与 `web/src/store/static-store.js` 各自实现一遍判分、排期、组卷、备注、打卡口径，靠 `tools/smoke_parity.py` 差分守护。详见 §6。

---

## 2. 全部 HTTP 端点清单（两套服务端）

### 2.1 `app/api.py` — FastAPI 主路径（23 个端点 + 1 个静态挂载）

| 方法 | 路径 | 行号 | 说明 |
|---|---|---|---|
| GET | `/api/v1/questions` | `api.py:106` | 组卷/复习/搜索统一集合端点（`review=1` 取到期，`q=` 搜索，`limit` 夹 1–100） |
| POST | `/api/v1/questions/{qid}/grade` | `api.py:117` | 判分，返回 `{id, correct, answer, explanation, self, fill?}` |
| GET | `/api/v1/questions/{qid}` | `api.py:126` | 单题（走 `_pub_row`，不下发 answer） |
| GET | `/api/v1/questions/{qid}/note` | `api.py:134` | 读单题备注 |
| PUT | `/api/v1/questions/{qid}/note` | `api.py:144` | 写备注（空串=删除） |
| DELETE | `/api/v1/questions/{qid}/note` | `api.py:154` | 删备注 |
| GET | `/api/v1/notes` | `api.py:164` | 备注列表（默认 500，上限 2000） |
| POST | `/api/v1/import_questions` | `api.py:170` | 批量导入自定义题（PDF 摘录编辑器） |
| POST | `/api/v1/custom` | `api.py:177` | 新建单道自定义题 |
| GET | `/api/v1/custom` | `api.py:185` | 自定义题列表（不含答案） |
| DELETE | `/api/v1/custom/{qid}` | `api.py:190` | 删自定义题（级联清 progress） |
| GET | `/api/v1/progress` | `api.py:199` | 导出进度 |
| POST | `/api/v1/progress` | `api.py:204` | 导入进度（合并） |
| PUT | `/api/v1/progress/{qid}` | `api.py:209` | 记录结果（mark） |
| GET | `/api/v1/wrong` | `api.py:216` | 错题本 |
| GET | `/api/v1/stats` | `api.py:222` | 总览统计 |
| GET | `/api/v1/banks` | `api.py:227` | 题库分布 |
| GET | `/api/v1/sections` | `api.py:232` | 章节来源分布 |
| GET | `/api/v1/chapters` | `api.py:237` | 知识点章节 |
| GET | `/api/v1/cards` | `api.py:242` | 知识点卡片搜索 |
| GET | `/api/v1/checkins` | `api.py:248` | 打卡日历（`?month=YYYY-MM`） |
| POST | `/api/v1/checkins` | `api.py:253` | 手动补签 |
| POST | `/api/v1/system/shutdown` | `api.py:259` | 退出服务（置 `SRV.should_exit`，见 `api.py:263-266`） |
| — | `mount('/', StaticFiles(web/dist))` | `api.py:323-325` | `web/dist` 存在才挂；否则 `GET /` 返回 503 |

传输层规则：Host/Origin 白名单中间件 `api.py:47-56`；CORS 只放行 localhost 任意端口 `api.py:61-63`。

### 2.2 `app/server.py` — 零依赖回退路径（14 个 API + 4 个静态）

| 方法 | 路径 | 行号 | 对应的 FastAPI 端点 |
|---|---|---|---|
| GET | `/api/stats` | `server.py:123` | `/api/v1/stats` |
| GET | `/api/quiz` | `server.py:125` | `/api/v1/questions`（默认 10） |
| GET | `/api/review` | `server.py:129` | `/api/v1/questions?review=1` |
| GET | `/api/search` | `server.py:133` | `/api/v1/questions?q=` |
| GET | `/api/banks` | `server.py:135` | `/api/v1/banks` |
| GET | `/api/sections` | `server.py:137` | `/api/v1/sections` |
| GET | `/api/cards` | `server.py:139` | `/api/v1/cards` |
| GET | `/api/chapters` | `server.py:142` | `/api/v1/chapters` |
| GET | `/api/wrong` | `server.py:144` | `/api/v1/wrong` |
| GET | `/api/progress` | `server.py:146` | `/api/v1/progress` |
| GET | `/api/checkin` | `server.py:149` | `/api/v1/checkins` |
| GET | `/api/notes` | `server.py:151` | `/api/v1/notes` |
| GET | `/api/question?id=` | `server.py:153` | `/api/v1/questions/{qid}` |
| GET | `/api/shutdown` | `server.py:156` | `/api/v1/system/shutdown`（方法不同：GET vs POST） |
| POST | `/api/grade` | `server.py:182` | `/api/v1/questions/{qid}/grade`（参数在 body 里带 id） |
| POST | `/api/mark` | `server.py:184` | `/api/v1/progress/{qid}` |
| POST | `/api/progress` | `server.py:188` | `/api/v1/progress` |
| POST | `/api/checkin` | `server.py:190` | `/api/v1/checkins` |
| POST | `/api/notes` | `server.py:192` | `/api/v1/questions/{qid}/note` |
| GET | `/`、`/index.html` | `server.py:113` | 由 `mount('/')` 提供 |
| GET | `/favicon.ico` | `server.py:115` | 空 204 |
| GET | `/static/*` | `server.py:117` | 由 StaticFiles 提供 |

**缺失**：`/api/v1/import_questions` 与 `/api/v1/custom*` 一族在 `server.py` 中**完全不存在**，即 PDF 摘录编辑器依赖的自定义题链路只支持在线（FastAPI）模式。

---

## 3. 数据来源与落盘位置

| 数据 | 路径 | 谁写 | 是否入库 |
|---|---|---|---|
| 全量题库 | `data/qbank.sqlite` | `tools/qbank_build.py`（从源库重建；重建时迁移 `source_class=自定义` 的行） | ❌ `.gitignore:2` |
| 公开子集 | `data/public/qbank.json` | `qbank_build.py:646-650` | ✅ `git ls-files data` |
| 原创自研题 | `data/selfauthored/questions.json` | 手工/独立流程维护，`schema:1`、`count:190`、CC-BY-SA-4.0；由 `qbank_build.py:511-533` 合入公开子集 | ✅ `git ls-files data` |
| 前端内联数据 | `web/public/qbank-data.js` → 构建后 `web/dist/qbank-data.js` | `tools/make_webdata.py`（写 `window.QBANK_DATA`） | ✅ |
| 前端产物 | `web/dist/{index.html, assets/index-*.js, qbank-data.js}` | `vite build` | ✅（`.gitignore:25` 忽略根 `dist/`，`:34` 用 `!web/dist/` 放行） |
| 进度（sqlite 模式） | `progress` 表 | `core.py:662-664` | ❌ |
| 进度（public 模式） | `data/progress.json` | `core.py:373-384`（1 秒防抖 + atexit） | ❌ `.gitignore:5` |
| 手动补签 | `data/checkin.json` | `core.py:698`、`core.py:573-575` | ❌ `.gitignore:6` |
| 题目备注 | `data/notes.json` | `core.py:812` 附近 | ❌ `.gitignore:7` |
| FSRS 卡片状态 | `fsrs_state` 表（sqlite 内） | `core.py:319-321` | ❌ 随 sqlite |
| 前端进度 | `localStorage['mech-qbank-progress']` | `static-store.js:68` | 浏览器本地 |
| 前端自定义题 | `localStorage['mech-qbank-custom']` | `static-store.js:69` | 浏览器本地 |
| 前端补签 | `localStorage['mech-qbank-checkin']` | `static-store.js:70` | 浏览器本地 |
| 前端备注 | `localStorage['mech-qbank-notes']` | `static-store.js:71` | 浏览器本地 |
| 私有产物/对照表 | `data/private/` | 人工 | ❌ `.gitignore:8` |

> 说明：审计时 `data/selfauthored/questions.json` **存在于工作区且被 git 跟踪**（`git ls-files data` 列出，HEAD 内 150 342 字节）。若在其他机器上缺失，构建会少 190 道原创题，应视为环境缺失而非本仓库状态。

---

## 4. 离线（file://）/ 在线（http://）两种模式的行为差异

分流点只有一个：`web/src/api.js:28` `STATIC = location.protocol === 'file:'`。

| 维度 | 在线（http://） | 离线（file://，APK 与本机双击） |
|---|---|---|
| 数据源 | `data/qbank.sqlite`（本机全量 2528 题） | `window.QBANK_DATA`（`qbank-data.js` 内联公开子集 266 题） |
| 通道 | `fetch /api/v1/*`（`api.js:44-55`） | `staticRoute()`（`api.js:59-103`） |
| 判分/排期 | 服务端 `core.Store.grade/mark` | 浏览器内 `static-store.js:292/353`（手抄实现） |
| 持久化 | sqlite `progress` 表 / `progress.json` | `localStorage['mech-qbank-progress']` |
| 备注/补签 | `data/notes.json` / `data/checkin.json` | `localStorage['mech-qbank-notes']` / `['mech-qbank-checkin']` |
| 自定义题（PDF 摘录） | ✅ `POST /api/v1/import_questions` 写入 sqlite，`source_class=自定义` | ✅ 走 `merge_questions()` 存 localStorage |
| 退出服务 | ✅ `POST /api/v1/system/shutdown`（`api.js:106-110`，静态模式下直接 return） | ❌ 无此能力 |
| 题库导入卡（`ImportCard.vue`） | 置灰（在线题库不可从浏览器改） | ✅ 可用 |
| 接口覆盖 | api.py 全量 23 端点 | `staticRoute()` 覆盖其中 **22 个**；缺 `/questions/{id}/note` 的 PUT 之外均已对齐，另有 JS 独有 `/template`（`api.js:90`） |
| 静态资源挂载 | `web/dist`（`api.py:325`） | 浏览器直接读 `index.html` + `qbank-data.js` + 单包 JS |

两端**必须保持一致**的规则（改动任何一边都要同步另一边，并由 `tools/smoke_parity.py` 差分）：判分四分支、填空空数口径、题型排序档位、组卷过滤与排序、进度导出导入、打卡月历与连续天数、备注截断与排序、自定义题规范化。

---

## 5. 前端构建的特殊约束（为什么必须是 IIFE 单包）

**约束来源**：APK 用 WebView 以 `file://` 加载页面，桌面也支持双击 `web/dist/index.html` 直接用。

`web/vite.config.js`：

| 行号 | 配置 | 原因 |
|---|---|---|
| `vite.config.js:33` | `rollupOptions.output = { format: 'iife', inlineDynamicImports: true }` | `file://` 下 ES module 的 `<script type="module">` 被浏览器按跨源处理并 CORS 拒绝，与包格式无关。IIFE 是经典脚本，单文件无请求，故障点从"能否加载"降级为"能否解析"。`inlineDynamicImports` 强制代码不分割——任何动态 import 都会产出第二个 chunk，在 `file://` 下必然失败。 |
| `vite.config.js:16-18` | `transformIndexHtml`（`order:'post'`）把 `<script type="module" crossorigin src=…>` 改写为 `<script defer src=…>` | 入口标签的 `type="module"` 属性本身才是被拦的根因。**必须补 `defer`**：经典脚本默认在 head 里同步执行，而 `#app` 挂载点在 body 末尾，不 defer 会 `mount(null)` 静默失败。`order:'post'` 保证在 Vite 注入 module 标签之后再改写。 |
| `vite.config.js:24` | `base: './'` | 产物用相对路径引用，才能同时被 `file://` 与 `/`（服务端挂载）加载。 |
| `web/index.html:8-13` | 第一道闸：`file:` 协议时 `document.write('<script src="qbank-data.js">')` | `file://` 下 `fetch` 读本地 JSON 同样被 CORS 拦，只能用 `<script>` 注入全局变量绕过。 |
| `vite.config.js:34` | `chunkSizeWarningLimit: 4096` | 单包约 1.5 MB，屏蔽体积告警（体积是 IIFE 约束的必然结果，不是问题）。 |
| 无 `sourcemap` 配置 | 生产产物无 `.map` | 与 `.gitignore`/产物清单一致，`web/dist/assets/` 只有一个 JS。 |

**副作用**：整站无法做按需加载与代码分割，任何"拆包优化"都会直接破坏 `file://` 可用性。

---

## 6. 判分与排期逻辑有几处实现

### 6.1 判分：2 处（必须同源）

| 实现 | 位置 | 分支 |
|---|---|---|
| Python | `app/core.py:572-626` `Store.grade()` | ① 答案待补→自评 ② 填空（**分支 0，最优先**，`core.py:588`）③ 判断题 `core.py:602` ④ 选择题 `core.py:616` ⑤ 其余自评 `core.py:626` |
| JS | `web/src/store/static-store.js:292-350` `grade()` | 同 5 个分支，顺序一致 |

配套口径函数也各写两份：填空空数（`core.py:218-233` / `static-store.js:112-118`）、判断归一（`core.py:210-215` / `:98-103`）、填空答案归一（`core.py:203-207` / `:105-110`）、题型排序（`core.py:107-120` / `:121-132`）、公开行脱敏 `_pub_row`/`pubRow`（`core.py:485-494` / `:283-289`，两者都**不下发 answer**，只发 `fill_n`）。

### 6.2 排期（FSRS-6）：2 处 + 1 处回退

| 实现 | 位置 | 说明 |
|---|---|---|
| Python | `app/core.py:69-79` 引入 `py-fsrs`（`Scheduler`/`Card`/`Rating`），`core.py:323-358` `schedule()`，`core.py:288-308` `schedule_chain()`（纯内存，供差分） | 卡片状态存 `fsrs_state` 表（`core.py:282-285`） |
| JS | `web/src/store/static-store.js:22-29` 引入 `ts-fsrs`，`static-store.js:353-390` `mark()`，`static-store.js:393-408` `scheduleChain()` | 卡片状态存 `localStorage['mech-qbank-progress']` 内 |
| 回退路径 | `core.py:650-660`（FSRS 不可用时用 `INTERVALS[streak-1]`）、`static-store.js` 对应分支 | 仅当 `py-fsrs` 缺失或 `QBANK_FSRS=0` |

> 旧固定间隔 `INTERVALS`（`core.py:45-48`、`static-store.js:12-14`）现在**只是回退**，不再是主路径；`intervals` 仍会写进 `qbank-data.js`（`make_webdata.py:75-77`）供静态端回退使用。

### 6.3 其它双份实现

`pick()`（`core.py:443-483` / `static-store.js:411-450`，含"先随机抽 n 再按题型标签排序"的语义）、`stats()`（`core.py:418-441` / `static-store.js:513-536`）、`checkin()`（`core.py:704-739` / `static-store.js:592-634`，周一起始月历与连续天数）、`notes_list()/setNote()`（`core.py:781/812` / `static-store.js:673/691`）、自定义题规范化与导入（`core.py:836/913` / `static-store.js:177/200`）。

---

## 7. 现有测试脚本各自覆盖什么

| 脚本 | 运行方式 | 覆盖对象 | 当前数字（审计日实测） |
|---|---|---|---|
| `tools/smoke_test.py` | `.venv-build/Scripts/python.exe` | 进程内起 `ThreadingHTTPServer` 打**数据层 + `server.py` 老路由**全链路：判分、组卷过滤、搜索、卡片、错题、进度 round-trip、HTTP 状态码、目录穿越、白名单、请求体夹紧；公开模式 `--public` 换公开子集与 json 落盘路径。**收尾自动调 `smoke_parity.py`**（`smoke_test.py:580-583`）。**不调用 `server.main()`**，因此服务端"启动路径"无覆盖 | 103 / 118 |
| `tools/smoke_api.py` | 同上 | 起真实 uvicorn 打 FastAPI REST 契约：方法与状态码、404/422、Host/Origin 白名单、CORS 预检、`qrank` 顺序与"不下发 answer"、静态首页与 assets、shutdown 后进程真退出 | 55 |
| `tools/smoke_static.js` | `node` | 直接 `require` ESM 的 `static-store.js`：判分/排期/组卷/搜索/统计/打卡/备注/导入/自定义题/公开子集体检/localStorage 持久化；另含**源码静态回归**（读 `.vue`/`router.js` 做正则断言，防跳转失效、错题本读 `answer`） | 232 |
| `tools/smoke_parity.py` | Python（内含 Node 子进程） | 前后端差分：判分三元组（`correct/self/answer/fill`）、FSRS 排期 `due` 链、题型排序标签 `qrank`；两侧喂同一份题库（含 5 道合成哨兵题） | 65858 判分点 + 2533 排期点 + 2533 标签点，不一致 0 |
| `tools/audit_types.py` | Python | 按结构证据核对每道题题型是否自洽（选择题有选项、判断题答案是对错、单选答案单字母、多选答案多字母……），有问题退出码 1 | exit 0 |
| `tools/audit_choice_answers.py` | Python | 选择题答案体检：答案文本 vs 选项文本比对，产出"强匹配/词面匹配/待人工"分档清单（分析用，不改库） | — |
| `tools/qbank_build.py` | Python | 构建器：源库 md/json → `qbank.sqlite` + 公开子集 + `BUILD_REPORT.md`；含 `tools/answer_fix.tsv` 人工核定答案表（`qbank_build.py` 第 ⑩ 步） | — |
| `tools/make_webdata.py` | Python | 公开子集 → `web/public/qbank-data.js`，写前跑三道闸 | — |

**已知盲区**（审计确认，供后续补测试时对照）

1. 前端**零组件测试、零单元测试**：`web/package.json` 无 `test` 脚本、无 vitest/jest；`util.js`、`api.js` 的 22 条 `staticRoute`、各 store 均无自动化覆盖。
2. `app/server.py` 的 `main()`（启动、端口扫描、开浏览器、日志）**无任何测试覆盖**——见 §8-c 的实测后果。
3. `app/api.py` 的 `main()`（端口扫描、`_alert` 弹窗、`SRV.should_exit`）只在 `smoke_api.py` 间接覆盖了 shutdown 一条路径。
4. 源码级回归（`smoke_static.js` 末段）是目前唯一拦住"视图层只在浏览器里暴露"的问题的手段。

---

## 8. 八条核对结论

### a. `app/core.py` 是否已接入 FSRS？—— ✅ 确认

- `core.py:69-70` `from fsrs import Scheduler as _FsrsScheduler, Card as _FsrsCard, Rating as _FsrsRating`（PyPI 包名 **fsrs**，即 py-fsrs；`requirements.txt:4` `fsrs>=6.3`）
- `core.py:75` 引擎单例 `_FSRS_ENGINE = _FsrsScheduler(enable_fuzzing=False)`
- `core.py:323-358` `Store.schedule()` 是实际调用点；`core.py:282-285` 建 `fsrs_state` 表
- `core.py:64-66,86-95` 四档评分（again/hard/good/easy）与旧语义 `correct/wrong/skip` 的映射
- 前端对偶实现用 **ts-fsrs**：`static-store.js:22`、`:29`

### b. 排期逻辑是否前后端双实现？—— ✅ 确认（Python 与 JS 各一份）

- Python：`core.py:323-358` `schedule()`、`core.py:288-308` `schedule_chain()`
- JS：`static-store.js:353-390` `mark()`、`static-store.js:393-408` `scheduleChain()`
- 两侧都由 `tools/smoke_parity.py` 的"排期比对点"差分（审计日 2533 题 × 同序列，不一致 0）
- 两份实现还共享一份"手抄的评分映射"（`core.py:86-95` ↔ `static-store.js:31-38`）

### c. `app/server.py` 是否仍在工作？—— ✅ 确认（**审计时曾崩溃，2026-10-20 已修并补上守护**）

- 仍在仓库、仍被 `tools/smoke_test.py:47-50` 以 spec 方式加载并全量打测（14 个 API 路由，见 §2.2）
- `start.bat:22-27`：当 `import fastapi` 失败时**回退启动它**——也就是说它是设计上的兜底路径
- **审计时实测崩溃**：

  ```
  $ QBANK_NO_BROWSER=1 python app/server.py
  Traceback (most recent call last):
    File "E:\agent\mech-qbank\app\server.py", line 244, in <module>
      sys.exit(main())
    File "E:\agent\mech-qbank\app\server.py", line 209, in main
      _log('mech-qbank 数据模式：…')
  NameError: name '_log' is not defined
  ```

  根因：`server.py:49` 用 `from core import *` 导入 core，而 Python 的 `import *` **跳过下划线开头的名字**；`_log` 定义在 `core.py:134`（同理 `_log_exc`、`_alert`），因此未被导入。日志改为 `_log()` 是 2026-10-18 为 windowed 打包做的改造（`core.py:122-177`），改造时漏了这个回退文件。
  之所以当时测试全绿仍无发现：`smoke_test.py:42-50` 只 `exec_module` 加载模块并直接用 `H` handler 与 `Store`，**从不调用 `main()`**。

- **2026-10-20 修复**（提交 `2139501`）：`server.py:55` 补 `from core import _alert, _log, _log_exc`，
  三个全导（`_alert` 只出现在「Store 初始化失败」与「端口全被占用」两条路径，漏了照样崩）；
  顺带把 `server.py:13` 那句已被 FSRS-6 取代的文档串改对。
- **同时补上守护**（提交 `4045cd1`）：`smoke_test.py` 新增 `subprocess_boot_smoke()` ——
  沙箱根目录里起真子进程 → 轮询端口 → 断言 200 → 杀进程，
  覆盖 `server.py`（回退路径）、`api.py`（主路径）与端口冲突退出码 1 三条。
  已用回退法验证：移除修复即 2 条断言变红并打出 NameError 与行号。

### d. 公开子集的过滤规则写在哪？当前放行多少题？—— ✅ 确认

- 三道闸：`tools/qbank_build.py:77` `PUBLIC_CLASSES = {'自出','通用知识点'}`（来源分级白名单）→ `:82-84` `TAINT` 正则（网址/来源名等硬痕迹）→ `:87-90` `clean()`（题干/答案/题解/来源任一命中即不公开）；`:648-649` 组合使用
- `make_webdata.py` 复用同一套模块级常量（写 `qbank-data.js` 前再过闸）
- 原创题单独放行路径：`qbank_build.py:511-533`（把 `data/selfauthored/questions.json` 的 190 道原创选择题以 `source_class='自出'` 合入）
- **当前放行**：`data/public/qbank.json` = **266 题 + 387 张知识点卡**（`git ls-files` 确认入库）；本次审计实测 `len(questions)=266`
- 审计当日本地全量库分级：`自出 423 / 公司真题 2069 / 网络搜集 36`，即仅本地 **2105 题**

### e. 全库多少题？多少题被标为仅本地？—— ✅ 确认

- 全量库 `data/qbank.sqlite`：**2528 题 + 387 张知识点卡**（`core.stats()` 实测，`mode=sqlite`）
- 仅本地（不公开）：`公司真题 2069 + 网络搜集 36 = 2105 题`
- 可公开（`自出`）：423 题 → 经三道闸后实际放出 266 题
- 参考：`tools/smoke_parity.py` 报"2533 题"= 2528 + 5 道合成哨兵题（`SYNTH`，只进临时副本库，不写回真实库）

### f. `web/dist` 是否被 git 跟踪？—— ✅ 确认（被跟踪）

```
$ git ls-files web/dist
web/dist/assets/index-DRPM8TjL.js
web/dist/index.html
web/dist/qbank-data.js
```

`.gitignore:25` 忽略仓库根的 `dist/`（第 26 行忽略 `build/`），而 `.gitignore:34` 用 `!web/dist/` 放行 `web/dist`——因为 exe/APK 打包依赖它，仓库需要自带构建产物。

### g. 前端是否有 `@media` 响应式断点？—— ✅ 确认（3 个）

`web/src/styles.css`：

| 行号 | 断点 |
|---|---|
| `:213` | `@media (max-width: 768px)` |
| `:220` | `@media (max-width: 480px)` |
| `:232` | `@media (max-width: 375px)` |

`styles.css:210` 的注释自述"此前全文 0 个 @media，375px 屏必溢出"，可与提交 `15862de`（全站响应式）对上。

### h. `docs/CODE_REVIEW.md` 的结论是否与当前代码一致？—— ⚠️ 部分过时，逐条如下

| 报告条目 | 现状 | 证据 |
|---|---|---|
| 3.1 P0 无 Host/Origin 校验（DNS rebinding） | ✅ **已修** | `core.py:99` `ALLOWED_HOSTS`；`api.py:47-56` 中间件；`server.py:79-92` `_guard()` |
| 3.2 P1 500 响应回显异常原文 | ✅ **已修** | `server.py:166-168` 只 `_log_exc()`、回 `{'error':'internal error'}`；`api.py` 无自定义异常回显，FastAPI 默认不回显 traceback |
| 3.3 P2 `esc()` 少转义单引号 / 二次注入 | ⛔ **已不适用** | 主渲染已换 Vue 模板（自动转义）；仅 `ImportCard.vue:67` 还保留一个 `esc`，且该处**已包含 `'` 转义**。`innerHTML` 全项目仅剩 `App.vue:73`（退出服务后替换整页） |
| 2.2 P1 `mark()` 锁内磁盘 I/O | ✅ **已修** | `core.py:629-666`：锁内只做 sqlite 写，落盘挪到锁外 `_schedule_progress_save()`（1 秒防抖 + atexit 兜底） |
| 2.3 P1 `notes_list()` N+1 | ✅ **已修** | `core.py:781-800` 改分片 `IN` 查询，注释里保留 6.74 ms → 2.09 ms 的实测对比 |
| 2.4 P2 `sections()` 全表 Python 聚合 | ❌ **未落地**（但已有缓存，优先级低） | `core.py:515-533` 仍是两条 SELECT + Python 聚合，`_sections` 缓存冷启动只跑一次 |
| 4.1 P0 判分两份实现 | ⚠️ **仍成立且已扩大** | `core.py:572` ↔ `static-store.js:292`，现在还要同步 `mark`/FSRS、打卡、备注、自定义题等约 15 组函数；已由 `smoke_parity.py` 自动化守护（比报告时期好） |
| 4.2 P0 `QBANK_INTERVALS` 两端分叉 | ✅ **已修（且被 FSRS 取代）** | `make_webdata.py:75-77` 把间隔写进 `qbank-data.js`，`static-store.js:150-151` 读取；主路径已改 FSRS-6 |
| 4.3 P2 定题型逻辑散落 | ⚠️ **仍成立** | `qbank_build.py` 的 ①②③…⑩ 步顺序敏感，仍是一段线性流程 |
| 4.4 P2 路由 if 链 + 字段表两份 | ✅ **主路径已改**（遗留文件仍留） | `api.py` 为 RESTful 表驱动；`server.py:112-194` 仍是 if 链 |
| 6 章"改一行有 379 条断言兜底" | ⚠️ **数字已过时** | 审计日实测合计：103 + 118 + 55 + 232 + parity 65858/2533/2533 比对点；断言总数口径已变 |
| 报告基准"真实 2334 题 sqlite" | ⚠️ **数字已过时** | 现为 2528 题（报告成文后题库重建过多次） |

**报告未覆盖、审计新增的发现**：`app/server.py` 因 `from core import *` 跳过下划线名字而在启动时 `NameError`（§8-c），且 `smoke_test.py` 从不调用 `main()`，所以六项测试全绿也发现不了。**两条均已于 2026-10-20 修复并补上守护**（`2139501` / `4045cd1`）。

### i. 审计后追加：`smoke_test.py` 的 103 基线并不可靠（数据污染 + 午夜炸弹）—— ✅ 2026-10-20 已修

这是 §8 核对之外、但结论为「不确认」的一条，按影响面单列。

**根因链**：
1. `smoke_test.py:63` 把真实 `data/qbank.sqlite` **整份复制**进临时目录，包含 `progress` 表（真实刷题记录）。
2. `core.py:710` 的 `checkin()` 从 `progress.last_seen` 聚合打卡天数——于是真实记录进了打卡断言。
3. `smoke_test.py:291-295` 断言的是**绝对日期推算值**（今天+昨天+前天 = streak 3、累计 4 天），断言前先 `import_progress` 灌入「今天/昨天/前天/4 天前」四条合成记录。

**为什么它随日期漂移**（审计日 2026-10-20 实测复现）：

| 跑测试的日期 | 合成记录集合 | 真实库带入 | 并集 | streak | 断言期望 | 结果 |
|---|---|---|---|---|---|---|
| 2026-10-19 | {10-19, 10-18, 10-17, 10-15} | {10-17, 10-18} | 4 天 | 3 | 3 | 蒙对 ✓ |
| 2026-10-20（当日） | {10-20, 10-19, 10-18, 10-16} | {10-17, 10-18} | 5 天 | 5 | 3 | ✗ 失败 |

真实库 `progress.last_seen` 最后一条是 `2026-10-18T08:43`。日期一往后推，真实那条 10-17 就永远填在合成数据的空隙里，**103 通过不再可能复现**。实测：

```
$ python tools/smoke_test.py
========== 结果：100 通过 / 3 失败 ==========
失败项：
  - 连续天数：今天+昨天+前天 = 3（隔天不算）
  - 累计天数 = 4 天（含隔天）
  - 手动打卡幂等（今天已答题）
```

**同类污染面**（同一 `copy2` 模式，需一并核查）：`tools/smoke_api.py:67`、`tools/smoke_parity.py:155`。
`audit_types.py` 与 `smoke_static.js` 不复制库，但**直读真实库**——只读不写，本轮不列入缺陷。

**结论**：`103 通过` 不是一个稳定基线，在**任何**日期跑都可能因真实刷题记录而不同。它必须在 Step 1 之前修掉（复制后清空 `progress` / `fsrs_state`），否则后续每一次重构都在跟一条会漂移的线比较。

**2026-10-20 修复**（提交 `4045cd1`）：新增 `scrub_user_state()`，三个测试文件在复制后
统一清空 `progress` 与 `fsrs_state`，`questions` / `cards` 一行不动
（大量断言依赖真实题目：`pick` / `search` / `sections` / 「找一道实题验判分」）。
子进程沙箱那份库同样清。修完由 `100/3` 回到全绿，且不再随日期变化。
真实数据核对：跑完测试后 `data/qbank.sqlite` 仍是 `progress` 6 行、`fsrs_state` 0 行，
mtime 保持 `Oct 18 22:34` 未变。

---

## 9. 测试基线与环境留痕

### 9.1 重构前审计时（2026-10-19，`5d207d4`）

```
$ python tools/smoke_test.py            → 103 通过 / 0 失败   ← 后证不可靠，见 §8-i
$ python tools/smoke_test.py --public   → 118 通过 / 0 失败
$ node tools/smoke_static.js            → 通过 232 ｜ 失败 0
$ python tools/smoke_api.py             → 通过 55 ｜ 失败 0
$ python tools/smoke_parity.py          → 判分 65858 / 排期 2533 / 题型标签 2533，不一致均为 0
$ python tools/audit_types.py           → exit 0
```

> 「103」这个数字后来被证明是**日期相关**的（10-19 蒙对、10-20 实测 100/3），
> 详见 §8-i。重构起跑请以 9.2 为准。

### 9.2 修复后（2026-10-20，`4045cd1`）—— **以此为重构基线**

```
$ python tools/smoke_test.py            → 108 通过 / 0 失败   （103+5 为新增子进程启动测试）
$ python tools/smoke_test.py --public   → 118 通过 / 0 失败
$ node tools/smoke_static.js            → 通过 232 ｜ 失败 0
$ python tools/smoke_api.py             → 通过 55 ｜ 失败 0
$ python tools/smoke_parity.py          → 判分 65858 / 排期 2533 / 题型标签 2533，不一致均为 0
$ python tools/audit_types.py           → exit 0（2528 题，题型问题 0）
```

Python 解释器为仓库内 `.venv-build\Scripts\python.exe`（含 fastapi / uvicorn / py-fsrs）。

> **跑测试时的环境坑**：本机若设了 `http_proxy` / `https_proxy`，子进程打向 `127.0.0.1`
> 的请求会被代理拦成 502，症状伪装成「服务起不来」。`smoke_test.py` 的子进程测试
> 已在 `spawn()` 里清掉这些变量；`curl` 打本地接口需手动加 `--noproxy '*'`。
