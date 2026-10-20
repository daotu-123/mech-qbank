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
import service                                       # 业务用例（纯标准库；与 api.py 共享同一份）
from core import *                                   # noqa: F401,F403 数据层与常量
from core import STORE as _CORE_STORE                # noqa: F401  （保持 STORE 可被外部赋值）
# 关键：上面的 `import *` **会跳过下划线开头的名字**，而本文件用到 _log/_log_exc/_alert，
# 不显式导入就会在 main() 里抛 NameError（2026-10-20 实测：server.py:209 _log 未定义直接退出 1）。
# 三个都要导：_alert 只出现在「启动失败」与「端口全被占用」两条路径上，漏了它们照样崩。
# app/api.py:31 已是同样的写法，两边保持一致。
from core import _alert, _log, _log_exc              # noqa: F401


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
        self.end_headers()
        if body:
            self.wfile.write(body)

    def j(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        if not self._guard():
            return self.j({'error': 'forbidden'}, 403)
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ('/', '/index.html'):
                return self._send(200, (WEB / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if p == '/favicon.ico':
                return self._send(204, b'')
            if p.startswith('/static/'):
                f = (WEB / p[8:]).resolve()
                if f.is_file() and WEB.resolve() in f.parents:   # is_file：目录/不存在都挡掉
                    return self._send(200, f.read_bytes(),
                                      mimetypes.guess_type(str(f))[0] or 'application/octet-stream')
                return self.j({'error': 'not found'}, 404)
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
            return self.j({'error': 'not found'}, 404)
        except Exception:
            _log_exc()                       # 只进控制台/日志，别把异常原文（含库路径/SQL）回给客户端
            return self.j({'error': 'internal error'}, 500)

    def do_POST(self):
        if not self._guard():
            return self.j({'error': 'forbidden'}, 403)
        p = urlparse(self.path).path
        n = clamp_int(self.headers.get('Content-Length') or 0, 0, 0, 10 * 1024 * 1024)
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
            return self.j({'error': 'not found'}, 404)
        except Exception:
            _log_exc()
            return self.j({'error': 'internal error'}, 500)


def main() -> int:
    global STORE, SRV
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
