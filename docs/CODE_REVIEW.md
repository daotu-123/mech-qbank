# mech-qbank 代码审查报告

- 审查日期：2026-10-17
- 审查范围：`app/server.py`（650 行）、`web/qbank-static.js`（579 行）、`web/index.html`（1179 行）、`tools/qbank_build.py`（572 行）、`tools/audit_types.py`（159 行）、冒烟测试（460 + 460 行）
- 触发背景：机械工程师反馈「Q1514 明显是填空题却判成判断题」以及在线模式进度页 404
- 审查方法：静态走读 + 实测基准（真实 `data/qbank.sqlite`，2334 题）+ 前后端判分差分比对（44346 个比对点）

---

## 一、先给结论

**这个项目的工程质量高于同规模个人项目的平均水平**：SQL 全部参数化、LIKE 通配符做了转义、静态目录做了穿越兜底、前后端双模式有契约测试、改一行有 379 条断言兜底。你反馈的两个 bug 都属于**解析器边界条件**问题，不是架构问题。

本次审查发现的真问题排序：

| 级别 | 问题 | 类别 |
|---|---|---|
| P0 | 判分/间隔逻辑存在两份实现，靠人工同步；`QBANK_INTERVALS` 已实测分叉 | 可维护性 |
| P0 | 无 Host/Origin 校验，DNS rebinding 可读本地进度与备注 | 安全 |
| P1 | `mark()` 在锁内做全量 `progress.json` 重写（实测 3.16 ms / 279 KB） | 性能 |
| P1 | `notes_list()` 是 N+1 查询，且每次查询都抢全局锁 | 性能 |
| P1 | 500 响应把异常原文回显给客户端 | 安全 |
| P2 | `esc()` 不转义单引号；反馈区缓存 HTML 二次注入 | 安全 |
| P2 | 定题型逻辑散落在 `infer_type()` + `parse_bank()` 后处理 8 步里 | 可维护性 |
| P2 | 路由是 20+ 分支 if 链；`_pub_row` / `pubRow` 字段表也是两份 | 可维护性 |

**性能当前不是瓶颈**——最慢的一次 `mark()` 实测 7.44 ms。下面的性能项都属于「会随数据量线性放大」的架构债，不是现在的故障。不要为了 3 ms 去做缓存和异步框架。

---

## 二、性能

### 2.1 实测基准（真实 2334 题 sqlite，本机，50 次取均值）

```
stats(): 6 次聚合 SQL                            3.07 ms
pick(): ORDER BY RANDOM() LIMIT 20               3.35 ms
sections(): 全表扫 2334 行 + Python 聚合          4.65 ms
sections() 改 SQL GROUP BY 后                     3.42 ms
notes_list(): 300 条备注 → 300 次单条查询          6.74 ms
notes_list() 改成 1 次 IN 查询                    2.09 ms
save_progress_json(): 2000 条全量重写             3.16 ms   （产物 279.3 KB）
mark(): sqlite 写 + progress.json 全量重写        7.44 ms
```

结论：**单机单人场景下没有性能问题**。下面三条是放大风险。

### 2.2 P1 · `mark()` 在锁内做磁盘 I/O

`app/server.py:343-362`：

```python
def mark(self, qid: str, result: str) -> dict:
    with self.lock:                      # ← 整段持锁
        ...
        self.con.commit()
        self.save_progress_json()        # ← 全表 SELECT + 279 KB JSON 序列化 + fsync 写盘
    return {...}
```

问题不在 3.16 ms，在于**持锁期间做 I/O**。`ThreadingHTTPServer` 下所有 `/api/*` 请求共用这一把 `RLock`，一次落盘 = 全体请求排队。公开模式（APK / 无 sqlite）下每答一题触发一次。

### 2.3 P1 · `notes_list()` 的 N+1

`app/server.py:471-486`，每条备注一次 `SELECT ... WHERE id=?`。300 条备注 = 300 次查询 + **300 次加锁/解锁**，与答题的 `mark()` 直接抢锁。实测 6.74 ms → 改成 `IN` 查询后 2.09 ms（3.2×）。

### 2.4 P2 · `sections()` 全表拉进 Python 聚合

`app/server.py:254-262`。有 `_sections` 缓存，冷启动一次 4.65 ms，改 SQL 后 3.42 ms。**收益有限，优先级低**，别为它引入缓存层。

---

## 三、安全

### 3.1 P0 · 无 Host / Origin 校验（DNS rebinding）

`app/server.py` 全文没有一处读 `Host` 或 `Origin` 头。服务绑在 `127.0.0.1` 上能挡住局域网，但**挡不住 DNS rebinding**：

1. 用户开着 App 时访问了恶意页面；
2. 攻击者把 `evil.com` 的 A 记录改成 `127.0.0.1`（TTL=0，或重绑后刷新）；
3. 该页面对 `http://evil.com:8640/api/progress` 发 `fetch` —— 此时与目标**同源**，浏览器同源策略放行；
4. 全部 2334 题的答题进度、备注、打卡记录被读走。

POST 同理：`/api/mark`、`/api/notes`、`/api/progress` 可被跨站改写。

**修复**：见 §5.1。

### 3.2 P1 · 500 响应回显异常原文

`app/server.py:585-586` 与 `611-612`：

```python
except Exception as e:
    return self.j({'error': str(e)}, 500)
```

sqlite 的异常文案会带库文件路径、SQL 片段。本机应用严重度有限，但排查信息不该出进程边界。

### 3.3 P2 · `esc()` 少转义单引号；反馈区缓存 HTML 二次注入

`web/index.html:366`：

```js
const esc = s => String(s??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
```

- 没转义 `'`。当前所有插值都用双引号属性（`"${esc(...)}"`），**暂无可利用点**；但这是定时炸弹——谁哪天写了 `title='${x}'` 就破了。
- `web/index.html:741` 把 `fb.innerHTML` 存进 `S.fb[qid].html`，`659` 行再原样回填。内容是首次渲染时转义过的，但**二次注入已转义 HTML 本身是坏味道**，一旦将来有未转义字段进来就是存储型自 XSS。

### 3.4 已检查通过

| 项 | 结论 |
|---|---|
| SQL 注入 | 全部参数化，无字符串拼接的值（拼接的只有 `IN` 占位符和固定片段） |
| LIKE 通配符注入 | `like_escape()` 处理了 `\ % _`，`ESCAPE '\'` 成对出现 |
| 目录穿越 | `/static/` 有 `WEB.resolve() in f.parents` 兜底，逻辑正确 |
| XSS（题干/选项/备注/自定义题） | 22 处 `innerHTML` 全部经 `esc()`，无内联 `on*=` 插值 |
| 请求体大小 | `Content-Length` 已 `clamp_int` 到 10 MB |

---

## 四、可维护性

### 4.1 P0 · 判分逻辑两份实现（最大技术债）

`app/server.py:301-340` 的 `Store.grade()` 与 `web/qbank-static.js:203-245` 的 `grade()` 是手抄的两份，注释里写着「改动必须两边同步」——**靠人记住的约束等于没有约束**。

我做了全库差分比对（2334 题 × 19 种作答取值 = **44346 个比对点**）：

```
比对题数: 2334 ｜ 每组 19 个作答取值 ｜ 比对点 44346
真实不一致点: 0
```

**当前是一致的**，值得肯定。但这个结论只在我跑的那一刻成立：
- 没有任何自动化测试守护它，改任何一边的正则都会静默分叉；
- 已经有一处真实分叉了 —— 见 4.2。

### 4.2 P0（已实测）· `QBANK_INTERVALS` 两端分叉

`app/server.py:58` 从环境变量读，`web/qbank-static.js:26` 硬编码 `[1,3,7,15,30,60]`。实测：

```
$ QBANK_INTERVALS="2,5,10" python -c "import server; print(server.INTERVALS)"
server INTERVALS = [2, 5, 10]
js     INTERVALS = [1, 3, 7, 15, 30, 60]
→ 同一道题答对后，服务端 due=+2 天，离线端 due=+1 天
```

更糟的是 `web/index.html:1120` 会把服务端返回的 `intervals` 显示在界面上——**界面显示 2/5/10，实际按 1/3/7 排期**，用户完全无从察觉。

### 4.3 P2 · 定题型逻辑散落

`tools/qbank_build.py` 里定题型分散在 `infer_type()`（107-115 行）和 `parse_bank()` 后处理的 8 个步骤（338-420 行）：选项恢复 → 字母反查 → 结构优先定题型 → 判断题选项搬家 → 答案归一 → 元信息回填 → 兜底抽取 → 重塑。

这正是 Q1514 bug 的温床：判定规则分散在 8 个 mutating 步骤里，任何一步的顺序调整都会改变结果，而**没有一条单测覆盖 `infer_type` 本身**。

### 4.4 P2 · 路由 if 链 + 字段表两份

- `do_GET` 是 20+ 个 `if p == '/api/xxx'` 分支；加接口要同时改路由、前端、`smoke_test.py` 的契约断言。契约测试已经兜住了「前端调的接口服务端没有」，但**反向（服务端有、前端没用）没覆盖**。
- `_pub_row()`（server.py:229）和 `pubRow()`（qbank-static.js:196）返回字段也是两份，加字段要改两处。

### 4.5 做得好的地方（保留）

- 契约测试（`smoke_test.py` 里「前后端接口契约」断言）是这次 404 bug 之后补的，直接堵死了同类问题；
- `clamp_int()` 对所有外部数值参数做夹紧；
- 所有 JSON 读取都有 `try/except` 降级，文件损坏不阻塞启动；
- 379 条断言（91 + 104 + 184）覆盖双模式。

### 4.6 测试缺什么

| 缺口 | 说明 |
|---|---|
| 前后端判分一致性 | **0 条**（我这次是临时脚本跑的，没固化） |
| 并发 | 0 条。`ThreadingHTTPServer` + 共享 sqlite 连接，多线程 `mark()` 没有测过 |
| 解析器回归 | 0 条。Q1514 这种应该固化成 fixture，防止下次改解析器再退回去 |
| `infer_type` 单测 | 0 条 |

---

## 五、重构方案

按 P0 → P2 排。每步都能独立提交、独立回滚。

### 5.1 【P0】加 Host / Origin 校验

`app/server.py`，模块常量区加：

```python
ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '[::1]'}
```

`H` 类加守卫，`do_GET` / `do_POST` 开头各调一次：

```python
    def _guard(self) -> bool:
        """本机服务只认 127.0.0.1 / localhost 的 Host 与 Origin。
        挡 DNS rebinding（恶意页面把域名重绑到 127.0.0.1 后同源读进度/备注）。"""
        host = (self.headers.get('Host') or '').split(':')[0].strip()
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get('Origin')
        if origin:
            o = urlparse(origin)
            if (o.hostname or '') not in ALLOWED_HOSTS:
                return False
        return True
```

```python
    def do_GET(self):
        if not self._guard():
            return self.j({'error': 'forbidden'}, 403)
        ...
```

**风险**：将来若要局域网/手机访问，需显式把 IP 加进 `ALLOWED_HOSTS` 并同时改绑定地址——这是一次需要你确认的取舍，我不会默认放开。

**验证**：
```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8640/api/stats              # 200
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.com' http://127.0.0.1:8640/api/stats   # 403
```

### 5.2 【P0】异常不再回显

两处 `except Exception as e` 改成：

```python
        except Exception:
            traceback.print_exc()          # 只进控制台，方便排障
            return self.j({'error': 'internal error'}, 500)
```

顶部补 `import traceback`。

### 5.3 【P0】固化前后端判分一致性测试

新增 `tools/smoke_parity.py`，把我这次跑的差分测试固化（2334 题 × 19 种作答取值，比对 `correct` / `self` / `answer`）。接进 `smoke_test.py` 的收尾，或独立成冒烟第四连。

这是**投入产出比最高的一步**：不用重构任何生产代码，就能让「两边不同步」在 3 秒内炸出来。

预期输出：
```
比对题数: 2334 ｜ 每组 19 个作答取值 ｜ 比对点 44346
真实不一致点: 0
```

### 5.4 【P0】修 `QBANK_INTERVALS` 分叉

`web/qbank-static.js:26` 改成从数据读：

```js
// 与 server.py INTERVALS 同源：由构建脚本写入 qbank-data.js，避免环境变量覆盖后两端排期分叉
var INTERVALS = (DATA && Array.isArray(DATA.intervals) && DATA.intervals.length)
  ? DATA.intervals.slice() : [1, 3, 7, 15, 30, 60];
```

（注意 `INTERVALS` 目前定义在 IIFE 外层，`make(DATA)` 在内层——需要把默认值传进 `make`，或在 `make` 里覆盖局部副本。）

`tools/make_webdata.py` 生成 `qbank-data.js` 时写入：

```python
'intervals': [int(x) for x in os.environ.get('QBANK_INTERVALS', '1,3,7,15,30,60').split(',')]
```

并在 §5.3 的差分测试里加一条「同一道答对的题，两端 `due` 日期必须相同」。

### 5.5 【P1】`mark()` 落盘移出锁 + 防抖

```python
    def mark(self, qid: str, result: str) -> dict:
        with self.lock:                     # 锁内只做内存/DB 的读-改-写
            ...
            self.con.execute(...)
            self.con.commit()
        self._schedule_progress_save()      # 落盘移出锁，1s 防抖
        return {'ok': True, 'state': state, 'streak': streak, 'wrong': wrong, 'due': due}

    def _schedule_progress_save(self) -> None:
        if self.mode != 'public':
            return
        with self.lock:
            if getattr(self, '_save_timer', None) and self._save_timer.is_alive():
                return                      # 已有待写，本次跳过
            self._save_timer = threading.Timer(1.0, self._flush_progress)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _flush_progress(self) -> None:
        with self.lock:
            self.save_progress_json()
```

配套 `atexit` 兜底，防止最后 1 秒的进度因强杀丢失：

```python
import atexit
atexit.register(lambda: STORE and STORE._flush_progress())
```

**权衡**：进程被 `kill -9` / 断电，最多丢 1 秒进度。我认为这个代价值得——换来的是答题请求不再被磁盘 I/O 阻塞。如果你不接受，改成「每 10 次 mark 强制 flush 一次」也可以。

### 5.6 【P1】`notes_list()` 改 `IN` 查询

```python
    def notes_list(self, limit=500) -> list:
        limit = clamp_int(limit, 500, 1, 2000)
        notes = self._notes()
        if not notes:
            return []
        ids = list(notes.keys())
        meta = {}
        for i in range(0, len(ids), 900):        # SQLite 变量上限 999，分片
            chunk = ids[i:i + 900]
            sql = ('SELECT id,stem,qtype,level,bank FROM questions WHERE id IN (%s)'
                   % ','.join('?' * len(chunk)))
            with self.lock:                      # 一次加锁，而不是 N 次
                for r in self.con.execute(sql, chunk).fetchall():
                    meta[r['id']] = r
        out = []
        for qid, v in notes.items():
            r = meta.get(qid)
            out.append({'id': qid, 'note': v['note'], 'updated_at': v['updated_at'],
                        'stem': r['stem'] if r else '', 'qtype': r['qtype'] if r else '',
                        'level': r['level'] if r else None, 'bank': r['bank'] if r else '',
                        'missing': r is None})
        out.sort(key=lambda x: (x['updated_at'] or '', x['id']), reverse=True)
        return out[:limit]
```

实测 6.74 ms → 2.09 ms，锁竞争从 N 次降到 1 次。

### 5.7 【P2】`esc()` 补单引号

```js
const esc = s => String(s??'').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
```

反馈区（§3.3）的二次注入建议改成 DOM 构造而不是缓存 HTML 字符串，但那是 `index.html` 里的一小块重构，可以单独排。

### 5.8 【P2】定题型抽成纯函数

在 `tools/qbank_build.py` 里把散落的 8 步收敛成一个可单测的纯函数：

```python
def decide_type(stem: str, options: list, answer: str, marked: str = '') -> tuple:
    """结构优先定题型（纯函数，不修改入参）。
    返回 (qtype, flags)；flags 记录降级原因，供 audit_types.py 复核。
      答案 ∈ {对,错,√,×}              → 判断
      ≥2 个带字母选项                  → 单选 / 多选（按答案字母数）
      题干含空且无选项                  → 填空
      其余长答案                        → 问答 / 计算 / 识记（沿用原标注）
    """
```

然后 `parse_bank()` 后处理只剩「恢复选项 → 调 `decide_type` → 写 flags」三步，并补一组 fixture 单测（至少把 Q1514、Q1539 钉住）。

### 5.9 【P2】路由改表驱动

```python
GET_ROUTES = {
    '/api/stats':    lambda q: STORE.stats(),
    '/api/quiz':     lambda q: {'items': STORE.pick(...)},
    ...
}
```

收益是加接口只需改一处、契约测试可以从「扫源码正则」升级成「直接比对 `GET_ROUTES` 的键」——比现在扫 `'/api/xxx'` 字符串可靠。这条优先级最低，别为了好看做。

---

## 六、回归网（每步之后必跑）

```bash
python tools/smoke_test.py          # 期望 91 条断言全绿
python tools/smoke_test.py --public # 期望 104 条断言全绿
node   tools/smoke_static.js        # 期望 184 条断言全绿
python tools/smoke_parity.py        # 期望 44346 个比对点，0 不一致   ← §5.3 新增
python tools/audit_types.py         # 期望 2334 题 / 结构问题 1
```

涉及数据的改动还要重建产物：
```bash
python tools/qbank_build.py && python tools/make_webdata.py
# 同步 assets → 打 APK；PyInstaller 打 exe
```

---

## 七、明确不做的事

避免为了「看起来专业」引入过度设计：

- **不上 Web 框架**（FastAPI / Flask）——项目立身之本是零第三方依赖、双击即跑、能打 exe；
- **不引入 SQLite → 别的东西**——2334 题的规模，SQLite 是最优解；
- **不做 SSR / 不拆 `index.html`**——单文件是 APK `file://` 打包的硬约束；
- **不做判分逻辑的跨语言共享**（WASM / DSL 生成）——成本远高于 §5.3 的差分测试；
- **不加缓存层**——当前最慢接口 7.44 ms，加缓存只会带来失效 bug。

---

## 八、待你决策的点

1. **§5.5 的 1 秒防抖**：接受「最多丢 1 秒进度」换「答题不被磁盘 I/O 阻塞」吗？还是改成每 N 次强制落盘？
2. **§5.1 的 Host 白名单**：将来有没有手机 / 局域网访问的需求？如果有，需要同时改绑定地址和白名单，我会一并设计。
3. 要不要我直接把 P0 三项（Host 校验、异常回显、判分差分测试）实施了？这三项不涉及数据迁移，风险最低。
