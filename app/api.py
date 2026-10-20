# -*- coding: utf-8 -*-
r"""
mech-qbank · 服务端（FastAPI + RESTful 版）

与 app/server.py（标准库 http.server 版）共用 app/core.py 数据层：
数据格式、判分行为、排期规则完全一致（tools/smoke_parity.py 与 smoke_test.py 守着两边）。
区别只在传输层：本文件是 FastAPI + uvicorn，按 RESTful 规范组织。

RESTful 约定
  · 资源用名词复数：/api/v1/questions、/api/v1/progress、/api/v1/checkins、/api/v1/notes
  · 过滤/组卷/复习/搜索都是「集合的查询参数」：GET /api/v1/questions?level=2&qtype=单选&review=1&q=气缸
  · 动作用标准方法：判分 POST .../grade；记录结果 PUT /api/v1/progress/{id}；
    备注 PUT/DELETE /api/v1/questions/{id}/note；导入 POST /api/v1/progress
  · 非 CRUD 的进程级操作走控制器式端点：POST /api/v1/system/shutdown
  · 成功：200/201，body 直接是资源或 {items:[...]}；判分返回 {id, correct, answer, explanation, self}
  · 失败：4xx/5xx + {"detail": "..."}（FastAPI 标准）；题目不存在 404，参数不合法 422

安全
  · 只监听 127.0.0.1，且 Host/Origin 必须在 ALLOWED_HOSTS 白名单内（挡 DNS rebinding）
  · windowed 打包后 sys.stdout/stderr 为 None：uvicorn 日志配 NullHandler，
    自有输出走 core._log()/_log_exc()，并追加写 exe 同级 qbank.log
"""
from __future__ import annotations
import os
import sys
import threading
import webbrowser
from urllib.parse import urlparse

import core
# service 不从 core 传播（core 不依赖它，是反向依赖），所以本文件必须显式导入。
# 注意 service 是纯标准库模块 —— 它不认识 fastapi，这样零依赖回退的 server.py
# 也能 import 它，两套服务端共享同一份业务用例。
import service
from core import (ALLOWED_HOSTS, INTERVALS, PORT, PORT_SCAN, Store, _alert, _log, _log_exc,
                  clamp_int)

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal, Optional

app = FastAPI(title='mech-qbank', version='2.0', docs_url=None, redoc_url=None,
              openapi_url=None)          # 本机自用工具，不开 Swagger 减少攻击面
STORE: Optional[Store] = None
SRV = None            # uvicorn.Server 实例，供 /api/v1/system/shutdown 用


# ---------------------------------------------------------------- 鉴权挂载点（W3，本轮不实现）
# 目标形态是「公网单用户 + 单账号」（docs §0.2 / §0.3），届时需要一个鉴权中间件。
# 本轮**只留位置，不实现**：AUTH_ENABLED 为 False 时下面什么都不做，行为与重构前完全一致。
#
# 实现时该怎么做（写在这里免得将来重新想）：
#   · 鉴权中间件要挂在 guard **之后**（先过 Host/Origin 白名单，再验身份）。
#     挂载方式（2026-10-20 实测确认，别再搞反）：
#     FastAPI/Starlette 的中间件是**后注册先执行** —— 装饰器写在文件里越靠后，
#     注册越晚，跑得越早。实测：注册顺序 guard→auth 时实际执行 auth→guard；
#     注册顺序 auth→guard 时实际执行 guard→auth。
#     所以要让 auth 跑在 guard **之后**，auth 必须**先注册**，
#     即本装饰器必须写在 guard 装饰器**之前**（也就是本段在下方 guard 之前）。
#   · 身份模型是单账号：口令 Argon2id，会话令牌**服务端签发、短时、可撤销**
#     （不要用自签 JWT——自签无法撤销，与「可撤销」直接冲突）
#   · 令牌卫生三通道（docs §0.4）：日志不得出现明文凭据、令牌不进 URL、Referrer-Policy
#   · 现有 Host/Origin 守卫保留，二者并存
AUTH_ENABLED = False


@app.middleware('http')
async def auth(request: Request, call_next):
    """W3 鉴权占位。当前 AUTH_ENABLED=False，直接放行。

    保留这个函数而不是「等要做鉴权时再加」，是为了让挂载顺序在代码里先固定下来：
    中间件注册顺序一旦在实现时才决定，很容易和 guard 的顺序搞反。

    ⚠️ 本装饰器**必须写在 guard 之前**（不是之后）：后注册先执行，
    先注册才会后跑，从而真正挂在 guard 之后。理由见上方注释。
    """
    if not AUTH_ENABLED:
        return await call_next(request)
    # ---- 实现时在这里校验会话令牌，未登录返回 401 ----
    return await call_next(request)


@app.middleware('http')
async def guard(request: Request, call_next):
    """Host/Origin 白名单 —— 与 server.py 的 _guard() 同语义，挡 DNS rebinding 与跨站调用。

    ⚠️ 公网化时**必须保留**（docs/architecture-baseline.md §0.2）：
    它防的是 DNS rebinding 与跨站调用，与将来 W3 的鉴权**并存且职责不同** ——
    鉴权回答「你是谁」，本守卫回答「请求是否来自允许的来源」，不可互相替代。

    本装饰器**必须写在 auth 之后**（即在文件里更靠下），才能让 auth 挂在它之后执行。
    """
    host = (request.headers.get('host') or '').split(':')[0].strip()
    if host not in ALLOWED_HOSTS:
        return JSONResponse({'detail': 'forbidden'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and (urlparse(origin).hostname or '') not in ALLOWED_HOSTS:
        return JSONResponse({'detail': 'forbidden'}, status_code=403)
    return await call_next(request)


# 开发时前端跑 vite dev server（localhost:5173），跨域打本服务需要 CORS 响应头；
# 只放行 localhost/127.0.0.1 任意端口，与 guard 的白名单同源。生产是同源，用不上。
app.add_middleware(CORSMiddleware,
                   allow_origin_regex=r'https?://(localhost|127\.0\.0\.1)(:\d+)?',
                   allow_methods=['*'], allow_headers=['*'])


def _404():
    return HTTPException(404, 'not found')


def _raise4xx(r):
    """把 service.Result 里的中性错误翻译成 HTTP 异常。

    这是**业务语义 → HTTP 状态码的唯一转换点**。
    service 层不认识 HTTP（它不能 import fastapi，否则零依赖回退路径当场失效），
    所以它只能返回 reason='not found'；翻译成404 还是 400 由本层决定。

    将来加鉴权（W3，见 docs/architecture-baseline.md §0.2）时，
    「未登录 → 401」也是在这里或它上面那层做，不要散进各个端点。
    """
    if r.not_found:
        raise _404()
    if r.get('error'):
        raise HTTPException(400, r['error'])
    return r


# ---------------------------------------------------------------- 请求体
class GradeIn(BaseModel):
    given: str = ''


class MarkIn(BaseModel):
    result: Literal['correct', 'wrong', 'skip'] = 'skip'


class NoteIn(BaseModel):
    note: str = ''


class ImportIn(BaseModel):
    progress: dict = Field(default_factory=dict)


class CustomIn(BaseModel):
    questions: list = Field(default_factory=list)
    mode: str = 'skip'          # skip=撞已导入 id 跳过；cover=覆盖更新


class CustomQIn(BaseModel):
    id: str
    stem: str
    qtype: str = ''
    level: Optional[int] = None
    options: list = Field(default_factory=list)
    answer: str = ''
    explanation: str = ''
    bank: str = ''
    section_path: str = ''
    cat_name: str = ''


# ---------------------------------------------------------------- 题库
@app.get('/api/v1/questions')
def questions(level: Optional[int] = None, qtype: Optional[str] = None,
              bank: Optional[str] = None, section: Optional[str] = None,
              only_new: bool = False, review: bool = False, q: Optional[str] = None,
              limit: int = 10, sclass: Optional[str] = None):
    """组卷/复习/搜索共用一个集合端点：默认随机组卷，review=1 取今日到期，q= 按题干搜索。

    本函数只做「HTTP → 业务用例」的一步转发：参数校验在 FastAPI 里已完成
    （见上面的 Optional[...] 与类型标注），业务取题在 app/service.py。
    """
    return service.pick_questions(STORE, q=q, limit=limit, level=level, qtype=qtype,
                                  sclass=sclass, bank=bank, section=section,
                                  only_new=only_new, review=review)


@app.post('/api/v1/questions/{qid}/grade')
def grade(qid: str, body: GradeIn):
    r = STORE.grade(qid, body.given)
    if r.get('error'):
        raise _404()
    r['id'] = qid
    return r


@app.get('/api/v1/questions/{qid}')
def question(qid: str):
    r = service.get_question(STORE, qid)
    if r.not_found:
        raise _404()
    return r


@app.get('/api/v1/questions/{qid}/note')
def get_note(qid: str):
    r = service.get_note(STORE, qid)
    if r.not_found:
        raise _404()
    return r


@app.put('/api/v1/questions/{qid}/note')
def put_note(qid: str, body: NoteIn):
    return _raise4xx(service.put_note(STORE, qid, body.note))


@app.delete('/api/v1/questions/{qid}/note')
def delete_note(qid: str):
    return _raise4xx(service.put_note(STORE, qid, ''))


@app.get('/api/v1/notes')
def notes(limit: int = 500):
    return {'items': STORE.notes_list(limit)}


# ---------------------------------------------------------------- 自定义题（PDF 摘录编辑器）
@app.post('/api/v1/import_questions')
def import_questions(body: CustomIn):
    """批量导入自定义题（编辑器保存 / JSON 粘贴）。mode: skip | cover。"""
    r = STORE.import_customs(body.questions, cover=body.mode == 'cover')
    return {'ok': True, 'added': r['added'], 'skipped': r['skipped'], 'rejected': r['rejected']}


@app.post('/api/v1/custom')
def create_custom(body: CustomQIn):
    r = STORE.add_custom(body.model_dump(), cover=True)
    if 'err' in r:
        raise HTTPException(400, r['err'])
    return r


@app.get('/api/v1/custom')
def custom_list(limit: int = 500):
    return {'items': STORE.custom_list(clamp_int(limit, 500, 1, 5000))}


@app.delete('/api/v1/custom/{qid}')
def delete_custom(qid: str):
    r = STORE.delete_custom(qid)
    if r.get('error'):
        raise _404()
    return r


# ---------------------------------------------------------------- 进度
@app.get('/api/v1/progress')
def export_progress():
    return {'exported_at': core.now_iso(), 'mode': STORE.mode, 'progress': STORE.export_progress()}


@app.post('/api/v1/progress')
def import_progress(body: ImportIn):
    return {'ok': True, 'imported': STORE.import_progress(body.progress)}


@app.put('/api/v1/progress/{qid}')
def mark(qid: str, body: MarkIn):
    if not STORE.full(qid):
        raise _404()
    return STORE.mark(qid, body.result)


@app.get('/api/v1/wrong')
def wrong(limit: int = 200):
    return {'items': STORE.wrong_list(limit)}


# ---------------------------------------------------------------- 统计与元数据
@app.get('/api/v1/stats')
def stats():
    return STORE.stats()


@app.get('/api/v1/banks')
def banks():
    return {'items': STORE.banks()}


@app.get('/api/v1/sections')
def sections():
    return {'items': STORE.sections()}


@app.get('/api/v1/chapters')
def chapters():
    return {'items': STORE.chapters()}


@app.get('/api/v1/cards')
def cards(q: Optional[str] = None, chapter: Optional[str] = None, limit: int = 500):
    return {'items': STORE.cards(q or '', chapter or '', limit)}


# ---------------------------------------------------------------- 打卡
@app.get('/api/v1/checkins')
def checkin(month: Optional[str] = None):
    return STORE.checkin(month or '')


@app.post('/api/v1/checkins')
def manual_checkin():
    return STORE.manual_checkin()


# ---------------------------------------------------------------- 进程级
@app.post('/api/v1/system/shutdown')
def shutdown():
    """windowed 打包没有 Ctrl+C，页面「退出程序」按钮调它；guard 已保证只有本机能调。"""
    if SRV is not None:
        # uvicorn 0.54 的 Server.shutdown() 是 async def 协程：从线程里同步调用只会创建
        # 一个永不 await 的协程对象，服务不会停（2026-10-17 踩坑）。正确信号是
        # should_exit——serve() 主循环每 tick 检查它，随后自行完成优雅关闭。
        SRV.should_exit = True
    return {'ok': True}


# ---------------------------------------------------------------- 入口
def _pick_port():
    import socket
    ports = [PORT + i for i in range(PORT_SCAN)]
    for p in ports:
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', p))
            s.close()
            return p
        except OSError:
            continue
    return None


def main() -> int:
    global STORE, SRV
    try:
        STORE = Store()
    except Exception:
        _log_exc()
        _alert('启动失败：数据初始化出错，详见 exe 同目录的 qbank.log')
        return 1
    s = STORE.stats()
    _log('mech-qbank（FastAPI）数据模式：%s ｜ 题 %d ｜ 卡片 %d ｜ 已答 %d ｜ 待复习 %d'
         % (s['mode'], s['total'], s['cards'], s['answered'], s['due']))
    _log('  来源分级：%s' % s['by_class'])
    actual = _pick_port()
    if actual is None:
        _log('⚠ 端口 %d–%d 都被占用了。关掉旧实例，或设 QBANK_PORT 换一个端口。'
             % (PORT, PORT + PORT_SCAN - 1))
        _alert('端口 %d–%d 都被占用了。\n关掉旧实例（或从任务管理器结束 mech-qbank），或设 QBANK_PORT 换端口。'
               % (PORT, PORT + PORT_SCAN - 1))
        return 1
    if actual != PORT:
        _log('⚠ %d 被占用，已自动改用 %d' % (PORT, actual))
    _log('mech-qbank 已启动  http://127.0.0.1:%d' % actual)
    _log('  关闭方式：点页面右下角「退出程序」按钮（本程序无终端窗口）')
    if os.environ.get('QBANK_NO_BROWSER') != '1':      # 双击 exe 时自动开界面；设 1 可关掉
        threading.Timer(0.8, lambda: webbrowser.open('http://127.0.0.1:%d/' % actual)).start()
    import uvicorn
    # windowed 下 sys.stderr 为 None，uvicorn 默认日志会 AttributeError；改 NullHandler
    LOGGING = {'version': 1, 'disable_existing_loggers': False,
               'handlers': {'null': {'class': 'logging.NullHandler'}},
               'root': {'handlers': ['null'], 'level': 'INFO'}}
    config = uvicorn.Config(app, host='127.0.0.1', port=actual, log_config=LOGGING)
    SRV = uvicorn.Server(config)
    SRV.run()
    return 0


# 静态前端（vite build 产物；开发时由 vite dev server 直接打 5173，不走这里）
_WEB_DIST = core.ASSETS / 'web' / 'dist'
if _WEB_DIST.is_dir():
    app.mount('/', StaticFiles(directory=str(_WEB_DIST), html=True), name='web')
else:
    @app.get('/')
    def _no_dist():
        return JSONResponse({'detail': 'web/dist 不存在：先跑 npm run build（见 web/README.md）'},
                            status_code=503)


if __name__ == '__main__':
    sys.exit(main())
