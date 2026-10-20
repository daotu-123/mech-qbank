# -*- coding: utf-8 -*-
r"""
mech-qbank · errors 层：统一错误响应格式（两套服务端共用）

## 为什么要有这一层（2026-10-20 后端加固第 3 部分）
--------------------------------------------------------------------------
重构前错误响应有五种形状：

  FastAPI 侧                标准库侧
  ─────────────────         ─────────────────
  {"detail":"not found"}    {"error":"not found"}
  {"detail":"forbidden"}    {"error":"forbidden"}
  {"detail":[{...}]} 422    {"error":"internal error"} 500
  HTTPException(400, msg)
  未捕获异常 → 500 text/plain

同一个"题不存在"，两个服务端给出两种 body；前端无法用一套判断处理。
对公网化更糟：没有 request_id，出问题时无法把用户看到的响应与日志对上。

## 统一格式

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

code 是**稳定英文标识**（前端可依赖它做分支，不要去匹配 message）：
invalid_param / not_found / unauthorized / forbidden /
internal_error / payload_too_large / unsupported_type

request_id 每个请求生成一次，**同时出现在响应体与日志**，是把两者对上的唯一线索。

## 只允许标准库

app/server.py 是零第三方依赖的回退服务端（start.bat 在 fastapi 不可用时启动它），
本模块若 import fastapi 就让那条路径失效。所以这里**只定义格式**，
由两侧各自的 router 负责按各自的协议调用它。

**service 层不认识 HTTP**（见 service.py 的模块说明），因此错误格式也不在 service 层组装——
service 只返回 Result(ok=False, reason='not found')，由 router 调本模块翻译。
"""
from __future__ import annotations

import contextvars
import os
import threading
import time

# ---------------------------------------------------------------- 错误码表
# code → 默认 HTTP 状态码。message 只给人看，可以改；code 是契约，不要改。
CODE_STATUS = {
    'invalid_param': 400,
    'not_found': 404,
    'unauthorized': 401,
    'forbidden': 403,
    'internal_error': 500,
    'payload_too_large': 413,
    'unsupported_type': 415,
}
DEFAULT_CODE = 'internal_error'


def error_body(code: str, message: str = '', request_id: str = '') -> dict:
    """构造统一错误响应体。两侧 router 都调它，保证结构完全一致。"""
    c = code if code in CODE_STATUS else DEFAULT_CODE
    msg = message or c.replace('_', ' ')
    return {'error': {'code': c, 'message': msg, 'request_id': request_id or ''}}


# ---------------------------------------------------------------- request_id
# 用 contextvars 而非 threading.local：FastAPI 的中间件跑在事件循环里，
# 但 ThreadingHTTPServer 走线程，两者都要能拿到「当前请求」。
# contextvars 两边都支持，且天然随 async 任务隔离。
_request_id_var = contextvars.ContextVar('qbank_request_id', default='')
_started_var = contextvars.ContextVar('qbank_request_started', default=0.0)


def new_request_id() -> str:
    """生成一个短且唯一的 request_id。

    用 `时间戳36进制-随机6位` 而不是 uuid4 全长：日志里要能一眼看出时间先后，
    长度也不至于撑爆日志行。**不含任何机器或用户信息**。
    """
    import random
    return '%08x-%s' % (int(time.time()) & 0xFFFFFFFF,
                        '%06x' % random.getrandbits(24))


def set_request_id(rid: str) -> None:
    _request_id_var.set(rid)


def current_request_id() -> str:
    """取当前请求的 id。日志与响应体都用它，保证值一致。"""
    return _request_id_var.get() or ''


def mark_start(t: float) -> None:
    _started_var.set(t)


def elapsed_ms() -> float:
    """距 mark_start 的毫秒数。未标记时返回 0.0（不抛，日志不该影响服务）。"""
    t0 = _started_var.get()
    return round((time.time() - t0) * 1000, 2) if t0 else 0.0


# ---------------------------------------------------------------- 日志
# 脱敏字段名（小写匹配）。凡键名命中，一律替换成 '***'，不打印值。
# 这是**兜底**，不是主要手段——主要手段是根本不把敏感值传进来（见 log_line）。
SENSITIVE_KEYS = frozenset((
    'authorization', 'cookie', 'token', 'access_token', 'refresh_token',
    'password', 'passwd', 'pwd', 'secret', 'api_key', 'apikey',
    'answer', 'given', 'note', 'explanation',   # 答案 / 备注正文
    'progress', 'state', 'streak', 'wrong', 'due', 'last_seen',        # 用户进度正文
))


def _scrub(value):
    """递归脱敏。命中敏感键名返回 '***'；其余原样。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = '***' if str(k).lower() in SENSITIVE_KEYS else _scrub(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def log_line(logger, status: int, method: str, path: str, code: str = '', note: str = ''):
    """写一行结构化日志：request_id / method / path / status / duration_ms / code。

    走 storage 层（不新开文件句柄），与 _log 用同一套输出路径。
    写日志**永远不能抛**——排障手段不该反过来搞垮服务。
    """
    try:
        parts = [
            'ts=%s' % time.strftime('%Y-%m-%dT%H:%M:%S'),
            'rid=%s' % (current_request_id() or '-'),
            'method=%s' % method,
            'path=%s' % path,
            'status=%d' % status,
            'ms=%s' % elapsed_ms(),
        ]
        if code:
            parts.append('code=%s' % code)
        if note:
            parts.append('note=%s' % note)
        logger(' '.join(parts))
    except Exception:
        pass


def scrub(payload: dict) -> dict:
    """对外暴露，供 router 在记录上下文前脱敏。"""
    return _scrub(payload)


_lock = threading.Lock()


def pid_tag() -> str:
    return str(os.getpid())