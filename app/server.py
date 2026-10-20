# -*- coding: utf-8 -*-
r"""
mech-qbank · 服务端（标准库 http.server 版，零第三方依赖）

数据层全部在 core.py（Store/判分/排期/打卡/备注），本文件只管 HTTP 传输：
路由、Host/Origin 白名单、静态文件。

同项目还有 app/api.py（FastAPI + RESTful 版），两边共用 core.py，
数据格式与判分行为完全一致（由 tools/smoke_parity.py 与 smoke_test.py 守着）。

设计约定
  · 不做 LLM 批改：选择题/判断题本地自动判分；问答/计算类返回 correct=null，由用户自评
  · 复习间隔：FSRS-6 排期（core.py 的 py-fsrs，fsrs_state 表）；旧固定档位仅作回退
  · 只读题库，进度写进 progress 表（公开模式写 progress.json）
  · 安全：只监听 127.0.0.1，且 Host/Origin 必须在 ALLOWED_HOSTS 白名单内（挡 DNS rebinding）
  · 打包：PyInstaller --windowed（无终端窗口），日志写 exe 同级 qbank.log；
    关闭服务走页面「退出程序」按钮 → GET /api/shutdown（_guard() 保证只有本机能调）

API 一览
  GET  /api/stats      总览（总量/已答/掌握/错题/待复习/今日/难度/题型/来源分布）
  GET  /api/quiz       组卷（n/level/qtype/class/bank/section/only_new）
  GET  /api/review     今日到期复习（n/level/qtype）
  GET  /api/search     题干关键词搜索（q/limit）
  GET  /api/banks      题库（bank）分布
  GET  /api/sections   章节来源（section_path 第一段）分布
  GET  /api/cards      知识点卡搜索（q/chapter/limit）
  GET  /api/chapters   知识点章节列表
  GET  /api/wrong      错题本（按累计错次排序）
  GET  /api/progress   导出进度 JSON
  GET  /api/checkin    学习打卡（连续天数/今日状态/当月日历；答题自动打卡，?month=YYYY-MM 看历史月）
  GET  /api/notes      题目备注列表（按更新时间倒序；每条带题号/题干/题型，指明属于哪道题）
  GET  /api/question   按题号取单题 ?id=（不带答案，备注列表「跳到该题」用）
  GET  /api/shutdown   退出服务（无终端窗口时的关闭方式；页面右下角按钮调用）
  POST /api/grade      判分 {id, given}
  POST /api/mark       记录结果 {id, result: correct|wrong|skip}
  POST /api/progress   导入进度 {progress: {...}}（合并，不覆盖未出现的题）
  POST /api/checkin    手动打卡 {action: "checkin"}（答过题的日子自动算，这是补签）
  POST /api/notes      保存题目备注 {id, note}（note 为空 = 删除该题备注）
"""
from __future__ import annotations
import os
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import core                                          # 数据层单例（STORE/SRV/路径常量都在它上面）
import errors                                       # 统一错误格式 + request_id（纯标准库，与 api.py 同一份）
import service                                       # 业务用例（纯标准库；与 api.py 共享同一份）
import multipart                                     # multipart/form-data 解析（纯标准库，与 api.py 同一份）
import docstore                                      # PDF 文档域（纯标准库；import 的是 pdf_client，不是 pymupdf）
import time                                          # request_context 用它计时
from core import *                                   # noqa: F401,F403 数据层与常量
from core import STORE as _CORE_STORE                # noqa: F401  （保持 STORE 可被外部赋值）
# 关键：上面的 `import *` **会跳过下划线开头的名字**，而本文件用到 _log/_log_exc/_alert，
# 不显式导入就会在 main() 里抛 NameError（2026-10-20 实测：server.py:209 _log 未定义直接退出 1）。
# 三个都要导：_alert 只出现在「启动失败」与「端口全被占用」两条路径上，漏了它们照样崩。
# app/api.py:31 已是同样的写法，两边保持一致。
from core import _alert, _log, _log_exc              # noqa: F401

#: PDF 文档域实例（落盘 + job 表 + 后台解析线程）。在 main() 里初始化。
#: 与 api.py 的 DOCSTORE 是同一份实现（app/docstore.py），保证两端行为等价。
DOCSTORE = None


def _n_arg(q: dict, default: int = 10) -> int:
    """统一读取「一次取多少题」的参数。

    历史遗留：前端 api.js 一直传 query 参数 `limit`，而本文件早期读的是 `n`，
    导致前端传的数量被静默忽略、永远落到默认值；且 /api/review 的默认曾是 20
    与 /api/quiz 的 10 不一致。此处统一为：同时接受 n 与 limit，默认 10。
    """
    for key in ('n', 'limit'):
        v = q.get(key)
        if v not in (None, ''):
            try:
                return int(v[0] if isinstance(v, (list, tuple)) else v)
            except (TypeError, ValueError):
                continue
    return default


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = 'mech-qbank/1.0'
    protocol_version = 'HTTP/1.1'   # keep-alive：前端一连多个 API 调用少握手
    timeout = 60                    # 空闲连接 60s 自动收，避免线程堆积

    def log_message(self, *a):        # 静音
        pass

    def _guard(self) -> bool:
        """只放行 127.0.0.1 / localhost 的请求，挡 DNS rebinding 与跨站调用。

        场景：用户开着 App 时访问恶意页面，该页面把域名重绑到 127.0.0.1 后即与本服务同源，
        fetch('/api/progress') 会被浏览器放行 —— 全部进度/备注/打卡记录随之泄露。
        """
        host = (self.headers.get('Host') or '').split(':')[0].strip()
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get('Origin')
        if origin:
            if (urlparse(origin).hostname or '') not in ALLOWED_HOSTS:
                return False
        return True

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # 与 api.py 一致：request_id 同时出现在响应头与响应体，
        # 排障时看浏览器 Network 面板就能与日志对上，不用翻 body。
        rid = errors.current_request_id()
        if rid:
            self.send_header('X-Request-Id', rid)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def j(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def jerr(self, code_name, message=''):
        """统一错误响应，与 api.py 的 errors.error_body() 完全同结构。

        本文件是**零依赖回退服务端**，所以错误格式必须用纯标准库实现；
        但结构与 FastAPI 侧共用 app/errors.py 的同一个函数，因此天然一致。
        """
        status = errors.CODE_STATUS.get(code_name, 500)
        return self.j(errors.error_body(code_name, message, errors.current_request_id()), status)

    def _ctx(self):
        """每个请求进入时调用：生成 request_id 并开始计时。"""
        rid = errors.new_request_id()
        errors.set_request_id(rid)
        errors.mark_start(time.time())
        return rid

    def _logreq(self, status, note=''):
        """结构化访问日志：request_id / method / path / status / ms。"""
        u = urlparse(self.path)
        errors.log_line(_log, status, self.command, u.path, note=note)

    def do_GET(self):
        self._ctx()
        if not self._guard():
            self._logreq(403, 'forbidden')
            return self.jerr('forbidden')
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            # 健康检查与 api.py 等价：live 不碰数据层，ready 真的读一次
            if p == '/health/live':
                self._logreq(200)
                return self.j({'status': 'ok'})
            if p == '/health/ready':
                try:
                    row = STORE.q('SELECT 1 AS ok')
                    if not row or row[0][0] != 1:
                        raise RuntimeError('unexpected probe result')
                    self._logreq(200)
                    return self.j({'status': 'ready'})
                except Exception:
                    _log_exc()
                    self._logreq(503, 'data layer unavailable')
                    return self.jerr('internal_error', 'data layer unavailable')
            if p in ('/', '/index.html'):
                return self._send(200, (WEB / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if p == '/favicon.ico':
                return self._send(204, b'')
            if p.startswith('/static/'):
                f = (WEB / p[8:]).resolve()
                if f.is_file() and WEB.resolve() in f.parents:   # is_file：目录/不存在都挡掉
                    return self._send(200, f.read_bytes(),
                                      mimetypes.guess_type(str(f))[0] or 'application/octet-stream')
                self._logreq(404)
                return self.jerr('not_found')
            if p == '/api/stats':
                return self.j(STORE.stats())
            if p == '/api/quiz':
                # 取题走 service，与 api.py 的 /api/v1/questions 同一份用例。
                # 参数照原样透传：level/class/bank/section 传字符串（None 或值），
                # only_new/review 传布尔 —— 与重构前的调用形式完全一致。
                return self.j(service.pick_questions(
                    STORE, q=None, limit=_n_arg(q), level=q.get('level'), qtype=q.get('qtype'),
                    sclass=q.get('class'), bank=q.get('bank'), section=q.get('section'),
                    only_new=q.get('only_new') == '1', review=q.get('review') == '1'))
            if p == '/api/review':
                # 与 /api/quiz、api.py、api.js 保持同一默认（10），参数名同时接受 n 与 limit
                return self.j(service.pick_questions(
                    STORE, q=None, limit=_n_arg(q), level=q.get('level'), qtype=q.get('qtype'),
                    review=True))
            if p == '/api/search':
                return self.j(service.pick_questions(
                    STORE, q=q.get('q', ''), limit=q.get('limit', 50)))
            if p == '/api/banks':
                return self.j({'items': STORE.banks()})
            if p == '/api/sections':
                return self.j({'items': STORE.sections()})
            if p == '/api/cards':
                return self.j({'items': STORE.cards(q.get('q', ''), q.get('chapter', ''),
                                                    q.get('limit', 500))})
            if p == '/api/chapters':
                return self.j({'items': STORE.chapters()})
            if p == '/api/wrong':
                return self.j({'items': STORE.wrong_list()})
            if p == '/api/progress':
                return self.j({'exported_at': now_iso(), 'mode': STORE.mode,
                               'progress': STORE.export_progress()})
            if p == '/api/checkin':
                return self.j(STORE.checkin(q.get('month', '')))
            if p == '/api/notes':
                return self.j({'items': STORE.notes_list(q.get('limit', 500))})
            if p == '/api/question':
                # 业务取题走 service（与 api.py 的 /api/v1/questions/{qid} 同一份用例），
                # 本层只负责翻译成协议响应体。
                # ⚠️ 注意：题不存在时原实现返回 {'item': None} 而不是 404，
                # 这个「有响应体但内容为 null」的形状是既有契约，本轮不改。
                r = service.get_question(STORE, q.get('id', ''))
                return self.j({'item': r.get('item')})
            if p == '/api/shutdown':
                # windowed 打包后没有控制台可按 Ctrl+C，只能从页面退出；_guard() 已保证只有本机能调
                def _stop():
                    try:
                        SRV.shutdown()
                    except Exception:
                        pass
                threading.Thread(target=_stop, daemon=True).start()
                return self.j({'ok': True})

            # ---- PDF 文档（与 api.py 的 /api/v1/documents* 等价）----
            # 解析是异步的：上传立刻 202 + job id，前端轮询 /api/v1/parse-jobs/{id}。
            if p == '/api/v1/documents':
                if DOCSTORE is None:
                    self._logreq(503, 'document store not ready')
                    return self.jerr('internal_error', 'document store not ready')
                return self.j(service.list_documents(DOCSTORE))
            if p.startswith('/api/v1/documents/'):
                rest = p[len('/api/v1/documents/'):]
                parts = [x for x in rest.split('/') if x]
                if not parts:
                    self._logreq(404)
                    return self.jerr('not_found')
                doc_id = parts[0]
                if len(parts) == 2 and parts[1] == 'pages':
                    if DOCSTORE is None:
                        self._logreq(503, 'document store not ready')
                        return self.jerr('internal_error', 'document store not ready')
                    try:
                        page = int(q.get('page', '1'))
                    except (TypeError, ValueError):
                        page = 1
                    r = service.get_document_page(DOCSTORE, doc_id, page)
                    if r.not_found:
                        self._logreq(404)
                        return self.jerr('not_found')
                    return self.j(r)
                if len(parts) == 1:
                    if DOCSTORE is None:
                        self._logreq(503, 'document store not ready')
                        return self.jerr('internal_error', 'document store not ready')
                    r = service.get_document(DOCSTORE, doc_id)
                    if r.not_found:
                        self._logreq(404)
                        return self.jerr('not_found')
                    return self.j(r)
            if p.startswith('/api/v1/parse-jobs/'):
                job_id = p[len('/api/v1/parse-jobs/'):]
                if not job_id:
                    self._logreq(404)
                    return self.jerr('not_found')
                if DOCSTORE is None:
                    self._logreq(503, 'document store not ready')
                    return self.jerr('internal_error', 'document store not ready')
                r = service.get_job(DOCSTORE, job_id)
                if r.not_found:
                    self._logreq(404)
                    return self.jerr('not_found')
                return self.j(r)
            self._logreq(404)
            return self.jerr('not_found')
        except Exception:
            _log_exc()                       # 只进控制台/日志，别把异常原文（含库路径/SQL）回给客户端
            self._logreq(500, 'internal_error')
            return self.jerr('internal_error', 'internal error')

    def do_POST(self):
        self._ctx()
        if not self._guard():
            self._logreq(403, 'forbidden')
            return self.jerr('forbidden')
        p = urlparse(self.path).path
        # Content-Length 超过上限 → 413（统一格式），不读 body 也不进业务逻辑。
        # 上限 10 MB 与重构前一致；api.py 侧由 uvicorn/Starlette 同样按此语义处理。
        raw_len = self.headers.get('Content-Length')
        if raw_len and raw_len.isdigit() and int(raw_len) > 10 * 1024 * 1024:
            self._logreq(413, 'payload too large')
            return self.jerr('payload_too_large', 'payload too large')
        n = clamp_int(raw_len or 0, 0, 0, 10 * 1024 * 1024)

        # ---- PDF 上传（multipart/form-data）----
        # 必须**先读原始字节**再判类型：JSON 分支要 dict，multipart 分支要 bytes。
        # 与 api.py 共用 app/multipart.py，保证两端解析出同一个文件名与同一份字节。
        ctype = self.headers.get('Content-Type') or ''
        if p == '/api/v1/documents':
            if DOCSTORE is None:
                self._logreq(503, 'document store not ready')
                return self.jerr('internal_error', 'document store not ready')
            raw = self.rfile.read(n) if n else b''
            try:
                parts = multipart.parse_multipart(raw, ctype)
            except multipart.MultipartError as exc:
                self._logreq(400, 'invalid multipart')
                return self.jerr('invalid_param', 'invalid multipart: %s' % exc)
            part = multipart.first_file(parts)
            if part is None:
                self._logreq(400, 'no file part')
                return self.jerr('invalid_param', 'no file part in multipart body')
            r = service.create_document(DOCSTORE, part.filename, part.data)
            if not r.ok:
                self._logreq(400, r.reason)
                return self.jerr('invalid_param', r.reason)
            # **202**：已受理，解析在后台进行。调用方拿 job.id 轮询。
            return self.j(r, 202)

        try:
            body = json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        try:
            if p == '/api/grade':
                return self.j(STORE.grade(str(body.get('id', '')), str(body.get('given', ''))))
            if p == '/api/mark':
                return self.j(STORE.mark(str(body.get('id', '')),
                                         body.get('result') if body.get('result') in ('correct', 'wrong', 'skip')
                                         else 'skip'))
            if p == '/api/progress':
                return self.j({'ok': True, 'imported': STORE.import_progress(body.get('progress') or {})})
            if p == '/api/checkin':
                return self.j(STORE.manual_checkin())
            if p == '/api/notes':
                return self.j(STORE.set_note(str(body.get('id', '')), str(body.get('note', ''))))
            self._logreq(404)
            return self.jerr('not_found')
        except Exception:
            _log_exc()
            self._logreq(500, 'internal_error')
            return self.jerr('internal_error', 'internal error')

    def do_DELETE(self):
        """DELETE /api/v1/documents/{id} —— 删除文档（级联）。

        server.py 原来只有 GET/POST，这组端点是 M3.3 新增的。
        """
        self._ctx()
        if not self._guard():
            self._logreq(403, 'forbidden')
            return self.jerr('forbidden')
        p = urlparse(self.path).path
        try:
            if p.startswith('/api/v1/documents/'):
                doc_id = p[len('/api/v1/documents/'):].strip('/')
                if doc_id and '/' not in doc_id:
                    if DOCSTORE is None:
                        self._logreq(503, 'document store not ready')
                        return self.jerr('internal_error', 'document store not ready')
                    r = service.delete_document(DOCSTORE, doc_id)
                    if r.not_found:
                        self._logreq(404)
                        return self.jerr('not_found')
                    return self.j(r)
            self._logreq(404)
            return self.jerr('not_found')
        except Exception:
            _log_exc()
            self._logreq(500, 'internal_error')
            return self.jerr('internal_error', 'internal error')


def main() -> int:
    global STORE, SRV, DOCSTORE
    try:
        STORE = Store()
    except Exception:
        _log_exc()
        _alert('启动失败：数据初始化出错，详见 exe 同目录的 qbank.log')
        return 1
    s = STORE.stats()
    _log('mech-qbank 数据模式：%s ｜ 题 %d ｜ 卡片 %d ｜ 已答 %d ｜ 待复习 %d'
         % (s['mode'], s['total'], s['cards'], s['answered'], s['due']))
    _log('  来源分级：%s' % s['by_class'])
    # 文档域：与 api.py 同一份实现（app/docstore.py）。
    # 启动时把**遗留的** running/queued 标成 interrupted —— 不假装成功。
    try:
        DOCSTORE = docstore.DocStore(DATA / 'private' / 'documents', store=STORE)
        orphans = DOCSTORE.reap_orphans()
        if orphans:
            _log('  文档：%d 个任务因上次进程中断被标记为 interrupted' % len(orphans))
    except Exception:
        _log_exc()
        _alert('启动失败：文档存储初始化出错，详见 exe 同目录的 qbank.log')
        return 1
    srv = None
    ports = [PORT + i for i in range(PORT_SCAN)]      # 上次没关干净时自动换端口，双击就能起
    for port in ports:
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), H)
            break
        except OSError:
            continue
    if srv is None:
        _log('⚠ 端口 %d–%d 都被占用了。关掉旧实例，或设 QBANK_PORT 换一个端口。'
             % (ports[0], ports[-1]))
        _alert('端口 %d–%d 都被占用了。\n关掉旧实例（或从任务栏/任务管理器结束 mech-qbank），或设 QBANK_PORT 换端口。'
               % (ports[0], ports[-1]))
        return 1
    actual = srv.server_address[1]
    if actual != PORT:
        _log('⚠ %d 被占用，已自动改用 %d' % (PORT, actual))
    _log('mech-qbank 已启动  http://127.0.0.1:%d' % actual)
    _log('  关闭方式：点页面右下角「退出程序」按钮（本程序无终端窗口）')
    SRV = srv
    if os.environ.get('QBANK_NO_BROWSER') != '1':      # 双击 exe 时自动开界面；设 1 可关掉
        threading.Timer(0.8, lambda: webbrowser.open('http://127.0.0.1:%d/' % actual)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
