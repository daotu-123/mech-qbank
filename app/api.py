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
import time
import webbrowser
from urllib.parse import urlparse

# ---------------------------------------------------------------- 冻结分发（frozen worker 陷阱）
# PyInstaller 打包后 `sys.executable` 就是 exe 自身，pdf_client 只能用
#   [sys.executable, '--pdf-worker', IN, OUT]
# 唤起 worker（不能再拼脚本路径，exe 会把脚本名当普通参数）。
# 所以**本文件必须识别这个标记**，否则 exe 会无条件走下面的 main() 去启动
# Web 服务（还可能撞端口），worker 永不执行 → 所有 PDF 解析必然超时。
#
# 为什么放在模块最前面（import core / fastapi 之前）：
#   · worker 路径**不依赖 fastapi**——解析进程不该拖着整个 Web 框架；
#   · worker 路径**不碰数据层**（core 会建 DATA 目录、连 sqlite），
#     解析一个 PDF 不该有数据层副作用；
#   · frozen + --windowed 下 sys.stdout/stderr 是 None，本分支一个字都不
#     print（结果只走输出文件 + 退出码，与 pdf_worker 的纪律一致）。
#
# 非 frozen 同样支持（测试用 `python app/api.py --pdf-worker IN OUT`
# 模拟 frozen），这样端到端路径不用真打包就能验。
if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--pdf-worker':
    import pdf_worker
    # pdf_worker.main 接受 `--pdf-worker IN OUT` 形态，会自己剥掉这个标记。
    sys.exit(pdf_worker.main(sys.argv[1:]))

import core
# errors 不从 core 传播（core 不依赖它，是反向依赖），所以本文件必须显式导入。
# errors 是纯标准库模块 —— 两套服务端共用同一套错误格式与 request_id 生成。
import errors
# service 不从 core 传播（core 不依赖它，是反向依赖），所以本文件必须显式导入。
# 注意 service 是纯标准库模块 —— 它不认识 fastapi，这样零依赖回退的 server.py
# 也能 import 它，两套服务端共享同一份业务用例。
import service
# multipart 是纯标准库的 multipart/form-data 解析器。
# 为什么不用 FastAPI 的 UploadFile/File()：它们依赖第三方包 python-multipart，
# 本机 .venv-build 里没有装（实测 import multipart → ModuleNotFoundError），
# 而本项目明确不引入新依赖。与 server.py 共用同一份实现，保证两端行为等价。
import multipart
# docstore 是 PDF 文档域（落盘 + job 表 + 后台解析线程），纯标准库。
# 它 import 的是 pdf_client（纯标准库）而不是 pdf_extract（会拖进 pymupdf），
# 所以 Web 进程永远不持有 MuPDF。
import docstore
from core import (ALLOWED_HOSTS, INTERVALS, PORT, PORT_SCAN, Store, _alert, _log, _log_exc,
                  clamp_int)

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal, Optional

app = FastAPI(title='mech-qbank', version='2.0', docs_url=None, redoc_url=None,
              openapi_url=None)          # 本机自用工具，不开 Swagger 减少攻击面
# 请求体上限 10 MB：与 server.py 的 clamp_int(..., 0, 10*1024*1024) 同口径。
# 公网化后反代也应有同等限制，这一层是应用侧的兜底。
MAX_BODY_BYTES = 10 * 1024 * 1024
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
async def limit_body(request: Request, call_next):
    """超大请求体 → 413（统一格式）。

    必须**只看 Content-Length 头**就拒绝，不能等把 body 读完：
    一个 1 GB 的 POST 会先把 1 GB 灌进内存/磁盘才谈得上判断。
    声明缺失时按 0 处理（chunked 传输本机场景不用管）。

    上限 10 MB，与 server.py 的 clamp_int 口径一致。
    """
    raw = request.headers.get('content-length') or ''
    if raw.isdigit() and int(raw) > MAX_BODY_BYTES:
        errors.log_line(core._log, 413, request.method, request.url.path, code='payload_too_large')
        return _err('payload_too_large', 'payload too large', request=request)
    return await call_next(request)


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
        errors.log_line(core._log, 403, request.method, request.url.path, code='forbidden')
        return _err('forbidden', 'forbidden')
    origin = request.headers.get('origin')
    if origin and (urlparse(origin).hostname or '') not in ALLOWED_HOSTS:
        errors.log_line(core._log, 403, request.method, request.url.path, code='forbidden')
        return _err('forbidden', 'forbidden')
    return await call_next(request)


# ---------------------------------------------------------------- 请求追踪（request_id + 访问日志）
# ⚠️ 本装饰器必须写在 auth 与 guard **之后**（即文件里更靠下）。
# FastAPI/Starlette 是**后注册先执行**（实测见 guard 的 docstring），
# 所以「写在后面」= 最后注册 = **最先跑** = 最外层。
# 必须最外层才能保证：
#   · 每个请求都拿到 request_id（哪怕随后被 guard 以 403 拒绝）
#   · 记录的是最终状态码与完整耗时（含被拒的请求）
#   · 异常处理器与服务层能拿到同一个 id
#
# request_id 同时写两处，缺一不可：
#   · contextvar（errors 模块）—— 供 **service 层**与结构化日志用
#   · request.state       —— 供 **异常处理器**用。原因：Starlette 的异常处理跑在
#     ExceptionMiddleware 里，**不在 BaseHTTPMiddleware 的 contextvar 作用域内**，
#     只靠 contextvar 会读空值（实测：异常处理器拿到的 rid 是空串，
#     导致 X-Request-Id 头整个缺失）。
@app.middleware('http')
async def request_context(request: Request, call_next):
    rid = errors.new_request_id()
    errors.set_request_id(rid)
    errors.mark_start(time.time())
    request.state.request_id = rid          # 给异常处理器 / 端点用，跨中间件边界可靠
    try:
        response = await call_next(request)
    except Exception:
        errors.log_line(core._log, 500, request.method, request.url.path,
                        code='internal_error')
        raise
    # 响应头也带一份：排障时浏览器 Network 面板就能看到，不用翻响应体
    response.headers['X-Request-Id'] = rid
    errors.log_line(core._log, response.status_code, request.method, request.url.path)
    return response


# 开发时前端跑 vite dev server（localhost:5173），跨域打本服务需要 CORS 响应头；
# 只放行 localhost/127.0.0.1 任意端口，与 guard 的白名单同源。生产是同源，用不上。
app.add_middleware(CORSMiddleware,
                   allow_origin_regex=r'https?://(localhost|127\.0\.0\.1)(:\d+)?',
                   allow_methods=['*'], allow_headers=['*'])


def _404():
    return HTTPException(404, 'not found')


def _raise4xx(r):
    """把 service.Result 里的中性错误翻译成 HTTP 异常。

    这是**业务语义 → HTTP 状态码的唯一转换点**，且必须留在 service 边界之外 ——
    service.py 只返回 Result(ok=False, reason='not found')，不认识 HTTP
    （它不能 import fastapi，否则零依赖回退路径当场失效）。
    响应体的组装不在这里做：统一由 app/errors.py + 下面的异常处理器完成，
    这样 FastAPI 侧与标准库侧的 body 结构才能保证完全一致。

    将来加鉴权（W3，见 docs/architecture-baseline.md §0.2）时，
    「未登录 → 401」也是在这里或它上面那层做，不要散进各个端点。
    """
    if r.not_found:
        raise _404()
    if r.get('error'):
        raise HTTPException(400, r['error'])
    return r


def _rid_of(request=None) -> str:
    """取当前请求的 id。

    先看 request.state（异常处理器跑在 contextvar 作用域之外，只能靠它），
    再退回 contextvar（service 层与结构化日志走这条）。
    两者由 request_context 中间件同时写入，值必然一致。
    """
    try:
        rid = getattr(request.state, 'request_id', '') if request is not None else ''
    except Exception:
        rid = ''
    return rid or errors.current_request_id()


def _err(code_name, message, status=None, request=None):
    """统一错误响应的唯一构造出口。

    三件事在这里一次做完，保证任何来源的错误都带齐三样：
      1. 统一 body（errors.error_body）
      2. X-Request-Id 响应头（异常处理器跑在 request_context 中间件**之外**，
         所以不能指望 request_context 补这个头 —— 必须各自带上）
      3. 结构化日志一行
    """
    rid = _rid_of(request)
    resp = JSONResponse(status_code=status or errors.CODE_STATUS.get(code_name, 500),
                        content=errors.error_body(code_name, message, rid))
    if rid:
        resp.headers['X-Request-Id'] = rid
    return resp


# ---------------------------------------------------------------- 统一错误响应
# 五类来源归一到同一个 body：{"error":{"code","message","request_id"}}
@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    code = {404: 'not_found', 403: 'forbidden', 401: 'unauthorized',
            413: 'payload_too_large', 415: 'unsupported_type',
            422: 'invalid_param'}.get(exc.status_code, 'invalid_param')
    return _err(code, str(exc.detail), status=exc.status_code, request=request)


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    """FastAPI 的入参校验失败（422）也走统一格式，不再吐FastAPI 原生的detail 数组。

    只取第一条错误的 msg：原生数组结构复杂且不稳定，前端不该依赖它。
    """
    detail = ''
    try:
        errs = exc.errors()
        if errs:
            detail = str(errs[0].get('msg') or '').replace('Value error, ', '')
    except Exception:
        pass
    return _err('invalid_param', detail, status=422, request=request)


@app.exception_handler(Exception)
async def _unhandled_exc(request: Request, exc: Exception):
    """兜底：任何未捕获异常都回 500，且**不回显异常原文**。

    原文可能含库路径与 SQL（core.py 已有同口径的安全基线，§8-b），一律只进日志。
    """
    core._log_exc()
    errors.log_line(core._log, 500, request.method, request.url.path,
                    code='internal_error')
    return _err('internal_error', 'internal error', request=request)


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


# ---------------------------------------------------------------- PDF 文档
# 解析一律异步：上传立刻 202 + job id，前端轮询 GET /parse-jobs/{id}。
# 实测端到端 2.2–3.1s（其中 import pymupdf 冷启动 2.0s），同步等待会像卡死。
#
# ⚠️ 为什么不用 FastAPI 的 UploadFile/File()：它们依赖第三方包
# `python-multipart`，本机 .venv-build 里**没有装**（实测
# `import multipart` → ModuleNotFoundError），而本项目明确不引入新依赖。
# 所以这里读原始 body，用 app/multipart.py 自己解析——纯标准库，
# 且与 server.py 共用同一份实现，保证两端行为等价。
#
# ⚠️ 这组端点**不 import pymupdf**：解析走 app/pdf_client.py，
# 由它 spawn 独立 worker 进程，Web 进程始终不持有 MuPDF。

DOCSTORE = None            # app/docstore.py 的实例，在 main() 里初始化


@app.post('/api/v1/documents')
async def create_document(request: Request):
    """上传 PDF（multipart/form-data）→ **立刻 202** + {document, job}。

    202 的含义是「已受理，解析在后台进行」，不是「已完成」。
    调用方拿 job.id 去轮询 GET /parse-jobs/{id}。
    """
    if DOCSTORE is None:
        raise HTTPException(503, 'document store not ready')
    body = await request.body()
    try:
        parts = multipart.parse_multipart(body, request.headers.get('content-type', ''))
    except multipart.MultipartError as exc:
        raise HTTPException(400, 'invalid multipart: %s' % exc)
    part = multipart.first_file(parts)
    if part is None:
        raise HTTPException(400, 'no file part in multipart body')
    r = service.create_document(DOCSTORE, part.filename, part.data)
    if not r.ok:
        raise HTTPException(400, r.reason)
    return JSONResponse(status_code=202, content=dict(r))


@app.get('/api/v1/documents')
def list_documents():
    if DOCSTORE is None:
        raise HTTPException(503, 'document store not ready')
    return service.list_documents(DOCSTORE)


@app.get('/api/v1/documents/{doc_id}')
def get_document(doc_id: str):
    if DOCSTORE is None:
        raise HTTPException(503, 'document store not ready')
    r = service.get_document(DOCSTORE, doc_id)
    if r.not_found:
        raise _404()
    return r


@app.get('/api/v1/documents/{doc_id}/pages')
def get_document_pages(doc_id: str, page: int = 1):
    if DOCSTORE is None:
        raise HTTPException(503, 'document store not ready')
    r = service.get_document_page(DOCSTORE, doc_id, page)
    if r.not_found:
        raise _404()
    return r


@app.delete('/api/v1/documents/{doc_id}')
def delete_document(doc_id: str):
    if DOCSTORE is None:
        raise HTTPException(503, 'document store not ready')
    r = service.delete_document(DOCSTORE, doc_id)
    if r.not_found:
        raise _404()
    return r


@app.get('/api/v1/parse-jobs/{job_id}')
def get_parse_job(job_id: str):
    if DOCSTORE is None:
        raise HTTPException(503, 'document store not ready')
    r = service.get_job(DOCSTORE, job_id)
    if r.not_found:
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


# ---------------------------------------------------------------- 健康检查
# 放在 /api/v1 之外：健康检查是基础设施接口，不是业务 API。
# 两套服务端都要有等价行为（app/server.py 的 /health/* 见对应实现）。
@app.get('/health/live')
def health_live():
    """存活探针：**只表示进程活着**，不碰数据层。

    刻意不查数据库：数据层出问题应该由 /health/ready 报出来，
    而不是让编排器把一个「数据暂时不可用」的服务重启一遍 ——
    重启解决不了磁盘/权限问题，只会掩盖它。
    """
    return {'status': 'ok'}


@app.get('/health/ready')
def health_ready():
    """就绪探针：**真的去读数据层**。不可用时返回 503 而不是 200。

    只做一次极轻量的读（COUNT 1），不跑 stats() 这类重查询——
    健康检查会按秒级频率打，跑重查询等于自己制造负载。
    """
    try:
        row = STORE.q('SELECT 1 AS ok')
        if not row or row[0][0] != 1:
            raise RuntimeError('unexpected probe result')
        return {'status': 'ready'}
    except Exception:
        _log_exc()
        errors.log_line(core._log, 503, 'GET', '/health/ready', code='internal_error')
        return _err('internal_error', 'data layer unavailable', status=503)


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
    global STORE, SRV, DOCSTORE
    try:
        STORE = Store()
    except Exception:
        _log_exc()
        _alert('启动失败：数据初始化出错，详见 exe 同目录的 qbank.log')
        return 1
    # 文档域：落盘目录在 <DATA>/documents。
    # 启动时把**遗留的** running/queued 标成 interrupted —— 不假装成功
    # （进程上次是在解析途中被杀的）。
    try:
        DOCSTORE = docstore.DocStore(core.DATA / 'private' / 'documents', store=STORE)
        orphans = DOCSTORE.reap_orphans()
        if orphans:
            _log('  文档：%d 个任务因上次进程中断被标记为 interrupted' % len(orphans))
    except Exception:
        _log_exc()
        _alert('启动失败：文档存储初始化出错，详见 exe 同目录的 qbank.log')
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
