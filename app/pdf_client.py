#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PDF worker 客户端 —— 在**调用方进程内**负责 spawn / 超时 / 隔离 / 回收结果。

## 这个文件的硬约束（都是踩过坑定下来的）
--------------------------------------------------------------------------
1. **绝对不许 `import pymupdf`。** 本模块只 import 标准库的
   `subprocess / tempfile / json / os / sys / time / threading`。
   测试 `test_no_pymupdf_in_client` 用 `sys.modules` 断言这一点。
   MuPDF 只允许被 `pdf_extract.py`（worker 侧）引用。

2. **全程走临时文件，禁止 PIPE。**
   - 写端无人读 → worker 阻塞在 write，管道塞满后永不退出；
   - 父进程 `read()` 等 EOF，子进程等父进程读 → 互相等死。
   本模块的 `subprocess.Popen` **不传 stdout/stderr 参数**（不重定向到 PIPE），
   只靠退出码 + 输出文件判断结果。子进程的输出继承父进程，
   在 frozen + windowed 下父进程没有控制台，那些输出自然消失，不影响判定。

3. **并发上限 1。** 单用户场景串行足够。用一个模块级 `threading.Lock`
   做**非阻塞**获取：拿不到立刻返回 `status='busy'`，不排队。
   排队意味着一个坏 PDF 能把后续请求全拖住，而单用户本来就不需要并发。

4. **超时后必须 kill 并 wait 回收**，否则会留下孤儿进程继续吃内存
   （正是我们要防的那 117 MB）。

## 为什么不设硬内存上限（Job Objects）
--------------------------------------------------------------------------
方案【F】里提过用 Windows Job Object 限制 worker 内存。本步**未实现**：
Job Object 需要 ctypes 绑定 `kernel32`/`psapi`、要单独处理 `JOB_OBJECT_LIMIT_JOB_MEMORY`
与 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`、还要在 Linux/macOS 上有退化路径，
复杂度和出错面都超出这一步该承担的范围。

**当前兜底是三层：超时（默认 60s）+ 串行（并发上限 1）+ 进程隔离（崩溃不波及调用方）。**
worker 真吃到 4 GB 会被 OS OOM killer 或 Windows 提交限制收拾掉，
最坏情况是这个 worker 进程消失，调用方返回 `worker_crashed`——**Web 进程仍然活着**。
所以缺硬上限不会危及服务，只是「失控 worker 可能短暂占用较多内存」。
若将来要补，必须自带测试，且不能只在 Windows 上生效。

## 返回值
--------------------------------------------------------------------------
始终返回 `app.pdf_extract.ExtractResult` **形状的普通 dict**——
注意：本模块不 import `pdf_extract`（否则就把 pymupdf 带进来了），
所以返回的是 `dict`，调用方按 `r['status']` 取值。
成功时其内容与直接调 `extract(blob)` 完全一致（同一份 bytes 的比对见测试 a）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

#: 默认超时（秒）。60s 覆盖实测最坏情况（500 页纯文本 0.35s、纯图 0.3s，
#: 留了两个数量级余量），同时保证「坏 PDF 最多拖住请求一分钟」。
DEFAULT_TIMEOUT = 60.0

#: 输出文件超过这个大小就判异常——防止坏 PDF 逼 worker 写出巨量文本撑爆磁盘。
MAX_OUTPUT_BYTES = 256 * 1024 * 1024

#: 传多少字节给 worker 之前先看一眼大小上限。超过就直接判 corrupt，
#: 不给「一个 2 GB 的 PDF 把内存吃穿」的机会。
MAX_INPUT_BYTES = 100 * 1024 * 1024

_HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(_HERE, 'pdf_worker.py')

#: 与 worker 的 PROTOCOL_VERSION 对齐。不一致直接判 worker_crashed。
PROTOCOL_VERSION = 1

_spawn_lock = threading.Lock()

# 测试用的钩子：把 sleep 换成空操作，让「超时路径」不必真等 60s。
# 只设 true/false，不接受外部注入的可调用对象（避免被用来做别的）。
_skip_sleep = False


def _set_skip_sleep(flag: bool) -> None:
    """仅供测试：让 poll 循环不真的 sleep。"""
    global _skip_sleep
    _skip_sleep = bool(flag)


def _blank(status: str, code: str, error: str = '') -> dict:
    """构造与 ExtractResult 同形状的失败结果（字段一个不少，便于调用方统一取值）。"""
    return {
        'status': status,
        'code': code,
        'pages': [],
        'total_chars': 0,
        'page_count': 0,
        'repaired': False,
        'is_encrypted': False,
        'image_coverage': 0.0,
        'columns_detected': 1,
        'table_pages': [],
        'error': error,
    }


def _log(message: str) -> None:
    """写一行 worker 相关日志。

    frozen + windowed 下 `sys.stderr` 是 None，直接写会 AttributeError，
    所以整段包在 try 里——**日志失败绝不能反过来搞垮调用方**。
    复用 `app.errors.log_line` 的形状（key=value），便于和现有日志一起 grep。
    """
    try:
        line = 'ts=%s pid=%d event=pdf_worker %s' % (
            time.strftime('%Y-%m-%dT%H:%M:%S'), os.getpid(), message)
        err = getattr(sys, 'stderr', None)
        if err is not None:
            err.write(line + '\n')
            err.flush()
    except Exception:
        pass


def worker_command(input_path: str, output_path: str, frozen=None, python=None):
    """构造 worker 的启动命令。

    **frozen 分支是本文件最容易错的地方**，所以单独抽成纯函数便于测试
    （测试 d 直接打桩验证两条命令的形状，不用真打包）。

    frozen 时 `sys.executable` 就是 exe 自身，**绝不能再拼脚本路径**——
    拼了会变成 `mech-qbank.exe app/pdf_worker.py IN OUT`，exe 把脚本名
    当普通参数，worker 根本不会被调用。非 frozen 才用
    `[sys.executable, 脚本路径, IN, OUT]`。

    参数
        frozen    覆盖 `getattr(sys, 'frozen', False)`；仅供测试打桩
        python    覆盖 `sys.executable`；仅供测试打桩
    """
    is_frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
    exe = sys.executable if python is None else python
    if is_frozen:
        return [exe, '--pdf-worker', input_path, output_path]
    return [exe, WORKER_SCRIPT, input_path, output_path]


def extract_in_worker(blob: bytes, timeout: float = None, frozen=None,
                      python=None, wait: float = 0.0) -> dict:
    """把 PDF 字节交给独立 worker 进程抽取，返回结果 dict。

    参数
        blob      PDF 原始字节
        timeout   秒，默认 60；None 表示用 DEFAULT_TIMEOUT
        frozen    覆盖 frozen 判定，仅供测试打桩
        python    覆盖解释器路径，仅供测试打桩
        wait      等待并发锁的秒数。0 = 不等，拿不到立刻返回 busy（默认）；
                  >0 = 最多等这么久再放弃。docstore 用它实现「排队」语义。

    返回
        成功：与 `pdf_extract.extract(blob)` **内容完全一致**的 dict
        超时：status='timeout', code='parse_timeout'
        崩溃：status='worker_crashed', code='internal_error'
        忙：  status='busy', code='too_many_requests'

    **本函数永远不抛异常。** worker 崩了、被 kill 了、输出是垃圾、
    磁盘满了——都转成上面那三种状态返回。这条是刻意的：
    调用方（将来的 router）不该因为一份坏 PDF 而 500。
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    # ---- 入参前置检查（不进 worker，省一次 spawn）----
    if not blob:
        return _blank('corrupt', 'corrupt_document', 'empty input: 0 bytes')
    if len(blob) > MAX_INPUT_BYTES:
        return _blank('corrupt', 'payload_too_large',
                      'input %d bytes exceeds limit %d' % (len(blob), MAX_INPUT_BYTES))

    # ---- 并发上限 1 ----
    #
    # 默认是**非阻塞**获取：拿不到立刻返回 busy。这是对直接调用方的正确语义
    # （HTTP 请求不该被另一个解析无限期挂住）。
    #
    # 但 docstore 的排队语义需要「等锁」而不是「失败」——那里已经自己
    # 串行化了（一个后台线程一次一个 job），如果这里再 fail-fast，
    # 两个 DocStore 实例（测试里并排跑两套服务端时就会发生）会互相把
    # 对方的 job 打成 status='busy'，那是**假失败**。
    #
    # 所以加一个可选的 wait 参数：等待锁，最多等 wait 秒。
    # 默认 0 = 不等（保持已验证的 fail-fast 行为不变）。
    if wait and wait > 0:
        got = _spawn_lock.acquire(timeout=wait)
    else:
        got = _spawn_lock.acquire(blocking=False)
    if not got:
        _log('note=busy action=rejected')
        return _blank('busy', 'too_many_requests',
                      'another pdf worker is still running')

    workdir = None
    proc = None
    try:
        workdir = tempfile.mkdtemp(prefix='qbank_pdf_')
        input_path = os.path.join(workdir, 'in.pdf')
        output_path = os.path.join(workdir, 'out.json')

        try:
            with open(input_path, 'wb') as fh:
                fh.write(blob)
        except OSError as exc:
            return _blank('worker_crashed', 'internal_error',
                          'cannot write temp input: %s' % exc)

        cmd = worker_command(input_path, output_path, frozen=frozen, python=python)
        _log('cmd=%s timeout=%.1fs' % (' '.join(cmd), timeout))

        try:
            # **不传 stdout/stderr** —— 一旦传 PIPE 就会引入双死风险（见模块 docstring）。
            # 不重定向 = 子进程继承父进程的句柄，frozen 下没有控制台时输出自然丢弃，
            # 而我们的判定完全依赖退出码 + 输出文件，不依赖 stdout。
            proc = subprocess.Popen(cmd)
        except OSError as exc:
            _log('note=spawn_failed error=%s' % type(exc).__name__)
            return _blank('worker_crashed', 'internal_error',
                          'cannot spawn worker: %s' % exc)

        # ---- 等退出，带超时 ----
        # 用「轮询 + 记时间」而不是 proc.wait(timeout=...)：前者能精确区分
        # 「超时」与「worker 自己很快退出」，而且便于测试把 sleep 摘掉。
        deadline = time.monotonic() + timeout
        while True:
            code = proc.poll()
            if code is not None:
                break
            if time.monotonic() >= deadline:
                _kill(proc)
                _log('note=timeout exit_code=%s timeout=%.1fs' % (code, timeout))
                return _blank('timeout', 'parse_timeout',
                              'worker exceeded %.1fs and was killed' % timeout)
            if not _skip_sleep:
                time.sleep(0.01)

        if code != 0:
            # worker 自己报的错误优先展示（它把异常写进了输出文件）
            detail = _read_worker_error(output_path)
            _log('note=nonzero_exit exit_code=%s detail=%s' % (code, detail[:120]))
            return _blank('worker_crashed', 'internal_error',
                          'worker exit code %s: %s' % (code, detail or '(no detail)'))

        # ---- 退出码 0，读输出文件 ----
        try:
            size = os.path.getsize(output_path)
        except OSError:
            _log('note=no_output exit_code=0')
            return _blank('worker_crashed', 'internal_error',
                          'worker exited 0 but wrote no output file')
        if size > MAX_OUTPUT_BYTES:
            _log('note=output_too_large bytes=%d' % size)
            return _blank('worker_crashed', 'internal_error',
                          'worker output %d bytes exceeds limit %d' % (size, MAX_OUTPUT_BYTES))

        try:
            with open(output_path, 'r', encoding='utf-8') as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            _log('note=bad_output error=%s' % type(exc).__name__)
            return _blank('worker_crashed', 'internal_error',
                          'worker output is not readable JSON: %s' % exc)

        if not isinstance(payload, dict):
            return _blank('worker_crashed', 'internal_error',
                          'worker output is %s, expected object' % type(payload).__name__)

        protocol = payload.get('protocol')
        if protocol != PROTOCOL_VERSION:
            _log('note=protocol_mismatch got=%s want=%s' % (protocol, PROTOCOL_VERSION))
            return _blank('worker_crashed', 'internal_error',
                          'worker protocol %s != client %s' % (protocol, PROTOCOL_VERSION))

        if payload.get('worker_error'):
            _log('note=worker_error detail=%s' % str(payload['worker_error'])[:120])
            return _blank('worker_crashed', 'internal_error', str(payload['worker_error']))

        result = payload.get('result')
        if not isinstance(result, dict) or not result.get('status'):
            return _blank('worker_crashed', 'internal_error',
                          'worker output has no usable result')

        # 补齐缺失字段：worker 侧版本若少返回某个键，这里不让调用方 KeyError。
        out = _blank(result.get('status', 'corrupt'), result.get('code', ''),
                     result.get('error', ''))
        for key, value in result.items():
            out[key] = value
        return out

    except Exception as exc:                       # noqa: BLE001 - 兜底，见函数 docstring
        _log('note=client_exception error=%s' % type(exc).__name__)
        return _blank('worker_crashed', 'internal_error',
                      'client error: %s: %s' % (type(exc).__name__, exc))
    finally:
        if proc is not None and proc.poll() is None:
            _kill(proc)
        _spawn_lock.release()
        if workdir:
            _cleanup(workdir)


def _kill(proc) -> None:
    """kill 并 wait 回收。不 wait 会留下僵尸，Windows 上更会留下孤儿进程。"""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _cleanup(workdir: str) -> None:
    """删临时目录。逐个删而不是 shutil.rmtree——目录是我们自己建的平铺结构，
    且绝不能因为清理失败而抛（那会把上面的返回码吃掉）。"""
    try:
        for name in os.listdir(workdir):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
        os.rmdir(workdir)
    except OSError:
        pass


def _read_worker_error(output_path: str) -> str:
    """尽力读出 worker 写下的错误详情。读不到就返回空串，不抛。"""
    try:
        with open(output_path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            return str(payload.get('worker_error', ''))
    except Exception:
        pass
    return ''
