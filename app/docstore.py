#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""mech-qbank · docstore 层：PDF 文档的落盘、元数据与解析任务

## 定位
--------------------------------------------------------------------------
`app/` 的分层是 storage → repository → service → core → router。
本模块属于 **service 层之下、storage 层之上**的一层「文档域」：

    docstore（本文档域）→ pdf_client（进程隔离层）→ worker → pdf_extract

**只允许标准库**。原因与 service.py 相同：`app/server.py` 是零第三方依赖的
回退服务端（start.bat 在 fastapi 不可用时启动它），本模块一旦 import
fastapi/pymupdf，这条回退路径直接失效。

`pdf_client` 本身是纯标准库（只 import subprocess/tempfile/json/os/sys/
time/threading），所以 `server.py` **可以** import 本模块与 pdf_client。

## 为什么解析要异步
--------------------------------------------------------------------------
实测端到端 2.2–3.1s，其中 `import pymupdf` 冷启动就要 2.0s。
同步解析意味着上传请求会挂 2–3 秒——那不是「慢一点」，那是**看起来卡死**。

所以：**上传立刻 202 + job id，前端轮询 GET /parse-jobs/{id}**。
这不是「同步等待加个转圈」，是另一套交互模型。

## 任务状态
--------------------------------------------------------------------------
job 状态机：

    queued ──> running ──> succeeded
                    │   └──> failed        （解析失败：corrupt / encrypted / no_text）
                    │   └──> interrupted   （进程重启遗留，见下）
                    └──> timeout / worker_crashed

`failed` 的细分原因放在 job 的 `result_status` 字段里（沿用抽取器的
status 词表：ok / no_text / corrupt / encrypted / repaired_ok），
这样前端不用解析错误消息就能分支。

### 状态**不跨重启持久化**
--------------------------------------------------------------------------
job 表在内存里。进程重启后：

- 磁盘上的 PDF 与页文本**保留**（那是用户的数据，不能丢）；
- job 表为空，`GET /parse-jobs/{id}` 返回 404。

但「重启前有 job 正在 running」这件事不能装作没发生过——
所以启动时会把**遗留的 running/queued 标成 interrupted**（见 `reap_orphans`）。
不持久化 job 表 + 保留 orphaned 标记，是两件不同的事：
前者是「不假装还记得」，后者是「不假装成功」。

## 存储布局
--------------------------------------------------------------------------
    <DATA>/private/documents/
        <doc_id>.pdf        原始文件（**随机键命名**，绝不用原始文件名）
        <doc_id>.json       元数据（sha256 / 字节数 / 原始文件名 / 状态 / job id）
        <doc_id>.pages.json 页文本（解析完成后写）

> **为什么在 `private/` 下**：`data/private/` 已在 .gitignore 里（用户上传的
> PDF 是私有数据，绝不能进仓库）。放在 `data/documents/` 的话，用户一上传
> 就会污染 git status——而**改 .gitignore 属于「要停下问 owner」的范围**，
> 所以这里选一个已经安全的位置，而不是去动 .gitignore。

`doc_id` 用 `secrets.token_hex(8)` 生成。**原始文件名只作元数据**，
绝不参与路径拼接——否则 `../../evil` 这类名字能写到任意位置。

## 并发上限 1
--------------------------------------------------------------------------
解析是**串行**的：一个后台线程，一次只处理一个 job。
排队而非立刻拒绝，理由是「一个坏 PDF 不该把后续请求全拖住」，
但也不能并发 spawn（117MB × N 会顶爆）。

第二个上传请求会**排队**（job 状态 queued），不是被拒。
（`pdf_client` 内部还有一道非阻塞锁，返回 busy——那是最后一道防线，
正常路径下走不到，因为本层已经串行了。）
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

import pdf_client

#: 文档 ID 字节数（token_hex(8) → 16 个十六进制字符）。
DOC_ID_BYTES = 8

#: 页文本单独存一个文件的原因：元数据 JSON 要频繁读写（列表接口），
#: 页文本可能很大（500 页几十 KB～几百 KB），混在一起会让列表接口变慢。
PAGES_SUFFIX = '.pages.json'

#: 任务队列长度上限。超过就不再收新 job（返回 busy）——
#: 队列无界会导致「上传 1000 个 PDF」把内存吃光。
MAX_QUEUE = 32

#: `os.replace` 的重试次数与退避基数（秒）。
#: Windows 上 replace 目标文件时，若另一线程正持有该文件的读句柄，
#: 会抛 PermissionError（共享冲突）。实测并发写必现，所以必须重试。
REPLACE_RETRIES = 8
REPLACE_RETRY_SLEEP = 0.005

#: 文档域的日志出口。与 core._log 同源（frozen 下 sys.stdout 是 None，
#: 不能裸 print）。延迟导入避免与 core 形成循环依赖。
def _log(msg: str) -> None:
    try:
        import core
        core._log(msg)
    except Exception:
        pass

#: 解析超时（秒）。与 pdf_client 的默认值一致，这里显式传下去。
PARSE_TIMEOUT = 60.0

#: 等待 pdf_client 并发锁的秒数。
#:
#: 本层**已经**串行化了（一个后台线程，一次一个 job），所以正常路径下
#: 根本不会撞上 client 的锁。但两套服务端并排跑（测试里就是这样）时
#: 会有两个 DocStore 实例共享同一个 client 模块级锁——那时 fail-fast
#: 会把对方的 job 打成 status='busy'，那是**假失败**（PDF 本身没问题）。
#:
#: 所以这里给一个等待窗口：让排队语义在本层成立，而不是让 client 的
#: 防御性 fail-fast 变成用户的错误。
LOCK_WAIT = 120.0

#: 状态词表（与抽取器一致，前端按这些词分支）。
STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_SUCCEEDED = 'succeeded'
STATUS_FAILED = 'failed'
STATUS_INTERRUPTED = 'interrupted'

#: 解析结果状态（抽取器给出的那套）。
RESULT_OK = 'ok'
RESULT_NO_TEXT = 'no_text'
RESULT_CORRUPT = 'corrupt'
RESULT_ENCRYPTED = 'encrypted'
RESULT_REPAIRED = 'repaired_ok'
RESULT_TIMEOUT = 'timeout'
RESULT_CRASHED = 'worker_crashed'


def new_doc_id() -> str:
    """生成随机文档 ID。**不用原始文件名**——见模块 docstring 的路径安全说明。"""
    return secrets.token_hex(DOC_ID_BYTES)


def sha256_of(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class DocStore(object):
    """PDF 文档存储 + 解析任务管理。

    纯标准库，线程安全。两个服务端各自持有一个实例。
    """

    def __init__(self, base_dir, store=None):
        """参数
            base_dir  文档根目录（一般是 <DATA>/documents）
            store     core.Store 实例，用于级联删除「由它生成的题目草稿」。
                       可为 None（此时删除不级联到题目）。
        """
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        self.store = store

        self._lock = threading.RLock()
        self._jobs = {}                 # job_id -> job dict（内存，不持久化）
        self._queue = []                # 待处理的 job_id 队列
        self._queue_cv = threading.Condition(self._lock)
        self._worker = None
        self._stop = False
        # 同一文档的元数据「读-改-写」必须串行。
        # 请求线程（写 job_id）与后台线程（写 status）会碰同一个文档，
        # 不加锁就会出现**丢失更新**：后写的把先写的字段覆盖回旧值。
        self._meta_lock = threading.Lock()
        # 按文档粒度的锁表（见 update_meta 的说明）。
        self._meta_locks = {}

    # ------------------------------------------------------------ 路径
    def pdf_path(self, doc_id: str) -> Path:
        return self.base / (doc_id + '.pdf')

    def meta_path(self, doc_id: str) -> Path:
        return self.base / (doc_id + '.json')

    def pages_path(self, doc_id: str) -> Path:
        return self.base / (doc_id + PAGES_SUFFIX)

    def update_meta(self, doc_id: str, **fields) -> bool:
        """原子地「读-改-写」某个文档的元数据。

        **所有对元数据的修改都必须走这里**，不能自己 read_meta → 改 →
        write_meta：那三步之间另一个线程可能插进来，导致字段被覆盖回旧值
        （实测踩过：后台线程把请求线程刚写的 job_id 覆盖没了）。

        锁是**按文档**粒度（全局一把锁会把不同文档的写入也串行化，
        没必要）。用 doc_id 做 key 的锁表。
        """
        with self._meta_lock_for(doc_id):
            meta = self.read_meta(doc_id)
            if not isinstance(meta, dict):
                return False
            meta.update(fields)
            return self.write_meta(meta)

    def _meta_lock_for(self, doc_id: str):
        """取某个文档的锁。惰性创建，文档删除时顺手清掉。"""
        with self._lock:
            lk = self._meta_locks.get(doc_id)
            if lk is None:
                lk = threading.Lock()
                self._meta_locks[doc_id] = lk
            return lk

    # ------------------------------------------------------------ 元数据
    def read_meta(self, doc_id: str):
        p = self.meta_path(doc_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None

    def write_meta(self, meta: dict) -> bool:
        """原子写元数据：先写**唯一**临时文件再 replace。

        两个坑都在这里踩过：

        1. **临时文件名必须唯一。** 早期版本用固定的 `<id>.json.tmp`，
           结果请求线程（写 job_id）与后台线程（写 status）同时写同一个
           tmp 文件时互相 `os.replace` 掉对方的 tmp —— 目标文件变成半截
           JSON，`read_meta` 解析失败返回 None，**GET 文档直接 404**。
           这种 bug 只在并发下偶发，单线程测试永远抓不到。
        2. **replace 是原子的**，所以读到的永远是完整的一个版本。
        3. **`os.replace` 在 Windows 上会因共享冲突失败**：另一个线程正
           持有该文件的读句柄时（Python 的 `open()` 默认不允许删除共享），
           `os.replace` 抛 PermissionError。这是**真实存在**的并发失败，
           不是理论问题——实测 4 线程 × 30 次并发写会出现若干次失败。
           所以要**退避重试**，不能一次失败就放弃（放弃 = 元数据没写上
           = 文档状态永远停在旧值）。

        不原子的话，进程在写一半时被杀会留下半截 JSON，
        读回来就是损坏的——而元数据损坏会让文档「查不到」。
        """
        doc_id = meta.get('id', '')
        if not doc_id:
            return False
        target = self.meta_path(doc_id)
        tmp = target.with_name(target.name + '.%s.tmp' % secrets.token_hex(4))
        try:
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

        # replace 失败重试（Windows 共享冲突）。见上面第 3 点。
        last_error = None
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(str(tmp), str(target))
                return True
            except PermissionError as exc:
                # 目标正被打开读取 —— 等一下再试。
                last_error = exc
                time.sleep(REPLACE_RETRY_SLEEP * (attempt + 1))
            except OSError as exc:
                last_error = exc
                break
        try:
            tmp.unlink()
        except OSError:
            pass
        if last_error is not None:
            _log('  文档元数据写入失败: %s' % last_error)
        return False

    def list_meta(self) -> list:
        """列出全部文档元数据，按创建时间倒序。"""
        out = []
        try:
            names = sorted(os.listdir(self.base))
        except OSError:
            return out
        for name in names:
            # ⚠️ 页文本文件的后缀是 `.pages.json`，**也以 .json 结尾**。
            # 早期版本没排除它，read_meta 读回来是个 list，后面 `m.get(...)`
            # 直接抛 AttributeError → 整个列表接口 500。
            # 这种「后缀互相包含」的坑，靠测试才发现得了。
            if not name.endswith('.json') or name.endswith('.tmp'):
                continue
            if name.endswith(PAGES_SUFFIX):
                continue
            m = self.read_meta(name[:-5])
            if isinstance(m, dict):
                out.append(m)
        out.sort(key=lambda m: m.get('created_at', ''), reverse=True)
        return out

    # ------------------------------------------------------------ 落盘
    def save_blob(self, blob: bytes, filename: str) -> dict:
        """落盘一个 PDF，返回文档元数据（尚未解析）。

        filename 只作元数据（original_name），**不参与路径拼接**。
        """
        doc_id = new_doc_id()
        meta = {
            'id': doc_id,
            'original_name': filename or '',
            'sha256': sha256_of(blob),
            'bytes': len(blob),
            'created_at': now_iso(),
            'status': STATUS_QUEUED,
            'result_status': '',
            'job_id': '',
            'page_count': 0,
            'total_chars': 0,
            'columns_detected': 1,
            'table_pages': [],
        }
        try:
            self.pdf_path(doc_id).write_bytes(blob)
        except OSError:
            return None
        if not self.write_meta(meta):
            # 元数据写失败 = 文档等于不存在（列表里查不到）。
            # 留着孤儿 PDF 文件只会占磁盘。
            try:
                self.pdf_path(doc_id).unlink()
            except OSError:
                pass
            return None
        return meta

    def read_blob(self, doc_id: str) -> bytes:
        try:
            return self.pdf_path(doc_id).read_bytes()
        except OSError:
            return b''

    def write_pages(self, doc_id: str, pages: list) -> bool:
        try:
            self.pages_path(doc_id).write_text(
                json.dumps(pages, ensure_ascii=False), encoding='utf-8')
            return True
        except OSError:
            return False

    def read_pages(self, doc_id: str) -> list:
        p = self.pages_path(doc_id)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    # ------------------------------------------------------------ job 表
    def create_job(self, doc_id: str) -> dict:
        job = {
            'id': 'job_' + secrets.token_hex(6),
            'document_id': doc_id,
            'status': STATUS_QUEUED,
            'result_status': '',
            'error': '',
            'created_at': now_iso(),
            'started_at': '',
            'finished_at': '',
        }
        with self._lock:
            self._jobs[job['id']] = job
            self._queue.append(job['id'])
            self._queue_cv.notify()
        return dict(job)

    def get_job(self, job_id: str):
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def update_job(self, job_id: str, **fields) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.update(fields)
            return True

    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    # ------------------------------------------------------------ 后台线程
    def start_worker(self) -> None:
        """启动后台解析线程（幂等）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop = False
            self._worker = threading.Thread(target=self._run_loop, name='pdf-parse',
                                            daemon=True)
            self._worker.start()

    def stop_worker(self) -> None:
        with self._queue_cv:
            self._stop = True
            self._queue_cv.notify_all()

    def _run_loop(self) -> None:
        """后台主循环：取一个 job，解析，更新状态。

        一次只处理一个——这是「并发上限 1」的实现点。
        """
        while True:
            with self._queue_cv:
                while not self._queue and not self._stop:
                    self._queue_cv.wait(timeout=0.5)
                if self._stop:
                    return
                if not self._queue:
                    continue
                job_id = self._queue.pop(0)
            try:
                self._process(job_id)
            except Exception:
                # 后台线程绝不能因为一个 job 崩掉就整体退出。
                self.update_job(job_id, status=STATUS_FAILED, result_status=RESULT_CRASHED,
                                error='internal error while parsing', finished_at=now_iso())

    def _process(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return
        doc_id = job['document_id']
        self.update_job(job_id, status=STATUS_RUNNING, started_at=now_iso())
        self.update_meta(doc_id, status=STATUS_RUNNING)

        blob = self.read_blob(doc_id)
        if not blob:
            self._finish(job_id, doc_id, STATUS_FAILED, RESULT_CORRUPT, 'file missing')
            return

        result = pdf_client.extract_in_worker(blob, timeout=PARSE_TIMEOUT,
                                             wait=LOCK_WAIT)
        rstatus = result.get('status') or RESULT_CORRUPT
        error = result.get('error') or ''

        # busy 不是「解析失败」，是「排队时没排上」。
        # 正常路径下本层已串行、撞不到；真撞上了（两个 DocStore 共享 client 锁）
        # 也应该重试而不是把用户的 PDF 判成失败——**假失败比排队更糟**。
        if rstatus == 'busy':
            time.sleep(1.0)
            result = pdf_client.extract_in_worker(blob, timeout=PARSE_TIMEOUT,
                                                 wait=LOCK_WAIT)
            rstatus = result.get('status') or RESULT_CORRUPT
            error = result.get('error') or ''

        # 抽取器给出的 status 词表：ok / no_text / corrupt / encrypted / repaired_ok
        # 加上客户端层的 timeout / worker_crashed。
        if rstatus == RESULT_OK or rstatus == RESULT_REPAIRED or rstatus == RESULT_NO_TEXT:
            # 解析成功 → 落页文本
            pages = result.get('pages') or []
            self.write_pages(doc_id, pages)
            self._finish(job_id, doc_id, STATUS_SUCCEEDED, rstatus, error,
                         total_chars=result.get('total_chars', 0),
                         page_count=result.get('page_count', 0),
                         columns_detected=result.get('columns_detected', 1),
                         table_pages=result.get('table_pages', []))
        elif rstatus == RESULT_TIMEOUT:
            self._finish(job_id, doc_id, STATUS_FAILED, RESULT_TIMEOUT, error)
        elif rstatus == RESULT_CRASHED:
            self._finish(job_id, doc_id, STATUS_FAILED, RESULT_CRASHED, error)
        else:
            # corrupt / encrypted —— 解析有结论但不是成功
            self._finish(job_id, doc_id, STATUS_FAILED, rstatus, error)

    def _finish(self, job_id, doc_id, job_status, result_status, error,
                total_chars=0, page_count=0, columns_detected=1, table_pages=None) -> None:
        self.update_job(job_id, status=job_status, result_status=result_status,
                        error=error, finished_at=now_iso())
        # 走 update_meta（原子读-改-写），别自己 read → 改 → write。
        self.update_meta(doc_id, status=job_status, result_status=result_status,
                         total_chars=total_chars, page_count=page_count,
                         columns_detected=columns_detected, table_pages=table_pages or [])

    # ------------------------------------------------------------ 重启遗留
    def reap_orphans(self) -> list:
        """把**遗留的** running/queued 标成 interrupted。

        场景：进程在解析途中被杀（用户直接关窗 / 机器断电），
        文档元数据里的 status 停在 running/queued。
        启动时必须把它改成 interrupted —— **不假装成功**。

        返回被处理的文档 id 列表（便于测试断言）。
        """
        touched = []
        for meta in self.list_meta():
            st = meta.get('status')
            if st in (STATUS_RUNNING, STATUS_QUEUED):
                self.update_meta(meta.get('id', ''), status=STATUS_INTERRUPTED,
                                 result_status=STATUS_INTERRUPTED,
                                 interrupted_at=now_iso())
                touched.append(meta.get('id', ''))
        return touched

    # ------------------------------------------------------------ 删除
    def delete(self, doc_id: str) -> dict:
        """删除一个文档，**级联**清理：
        原始 PDF + 页文本 + 元数据 + job 表项 + 由它生成的题目草稿。

        返回 {'ok': True, 'id':..., 'removed': {...}} 或 {'error': 'not found'}。
        """
        meta = self.read_meta(doc_id)
        if not meta:
            return {'error': 'not found'}

        removed = {'pdf': False, 'pages': False, 'meta': False,
                   'job': False, 'questions': 0}

        # 1) 原始文件 + 页文本
        for path, key in ((self.pdf_path(doc_id), 'pdf'),
                          (self.pages_path(doc_id), 'pages'),
                          (self.meta_path(doc_id), 'meta')):
            try:
                if path.exists():
                    path.unlink()
                    removed[key] = True
            except OSError:
                pass

        # 2) job 表项
        with self._lock:
            job_id = meta.get('job_id') or ''
            if job_id and job_id in self._jobs:
                del self._jobs[job_id]
                removed['job'] = True
            self._queue = [j for j in self._queue if j != job_id]

        # 3) 由它生成的题目草稿
        removed['questions'] = self._delete_derived_questions(doc_id, meta)
        return {'ok': True, 'id': doc_id, 'removed': removed}

    def _delete_derived_questions(self, doc_id: str, meta: dict) -> int:
        """删除「由该文档生成的题目草稿」。

        识别方式：题目的 source_class 标记里带文档 id。
        题目 id 形如 `X+<doc_id>+<序号>`（见 ExcerptView 的编号规则），
        这里按 doc_id 前缀匹配。

        **只在能确定归属时才删**——宁可漏删也不误删用户的题。
        """
        if not self.store:
            return 0
        doc_prefix = 'X+' + doc_id
        deleted = 0
        try:
            rows = self.store.repo.fetchall(
                'SELECT id FROM questions WHERE id LIKE ?', (doc_prefix + '%',))
        except Exception:
            return 0
        for row in rows:
            qid = row['id'] if isinstance(row, dict) else row[0]
            try:
                r = self.store.delete_custom(qid)
                if r.get('ok'):
                    deleted += 1
            except Exception:
                continue
        return deleted
