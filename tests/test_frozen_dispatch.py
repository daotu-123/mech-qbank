#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""frozen 分发端到端测试 —— 验 `app/api.py --pdf-worker IN OUT` 这条路径。

## 为什么要有这个文件（2026-10-20 发现的真缺口）
--------------------------------------------------------------------------
打包入口是 `app/api.py`（build/mech-qbank.spec 的 Analysis 第一项），
而 PyInstaller `--windowed` 打包后 `sys.executable` 就是 exe 自身。
于是 `pdf_client.worker_command()` 在 frozen 分支只能这样 spawn：

    [exe, '--pdf-worker', IN, OUT]

**exe 必须自己识别这个标记**。原来的 api.py 结尾只有

    if __name__ == '__main__':
        sys.exit(main())

于是 frozen 下 exe 会无条件去启动 Web 服务（还可能撞端口、弹浏览器），
worker 永不执行 → **所有 PDF 解析必然超时**。
`pdf_client.py` 的测试 d 只验了「命令拼得对」，验不到「exe 认不认这个命令」——
这是两个不同的东西，缺一不可。

## 本文件验什么
--------------------------------------------------------------------------
a. `python app/api.py --pdf-worker IN OUT` 写出正确的结果文件 + 退出码 0
b. **没有监听任何端口**（没退化成 Web 服务）
   · 主证据：worker 跑完进程自己退出了（Web 服务会一直挂着不退出）
   · 辅证：跑的过程中目标端口始终拒连；Windows 上再查 netstat，
     确认该 pid 没有任何 LISTENING 条目
c. 不带 `--pdf-worker` 时**仍然是 Web 服务**（不能把正常启动路径改坏）
d. 参数个数不对时干净退出（退出码 2），而不是掉进 main() 去开端口
e. 源码结构断言：分发必须在 `import core` 之前
   （worker 路径不许碰数据层，也不许拖进 fastapi）

## 跑法
--------------------------------------------------------------------------
    "C:/Users/Administrator/.workbuddy/binaries/python/envs/pdfprobe/Scripts/python.exe" \
        tests/test_frozen_dispatch.py

需要 pymupdf（真抽取）与 fastapi（验 Web 路径）都在的环境。
缺 pymupdf → a/d 之外的相关用例 skip；缺 fastapi → c skip。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_APP = os.path.join(_ROOT, 'app')
_FIX = os.path.join(_HERE, 'fixtures', 'pdf')
for _p in (_APP, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import pymupdf  # noqa: F401
    HAS_PYMUPDF = True
except ImportError:                                   # pragma: no cover
    HAS_PYMUPDF = False

try:
    import fastapi  # noqa: F401
    HAS_FASTAPI = True
except ImportError:                                   # pragma: no cover
    HAS_FASTAPI = False

#: 沙箱里跑 api.py 时用的解释器。与本进程同一个即可（子进程要能 import fastapi）。
PY = sys.executable


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_refused(port: int) -> bool:
    try:
        socket.create_connection(('127.0.0.1', port), timeout=1).close()
        return False
    except OSError:
        return True


def _listening_ports_of(pid: int) -> set:
    """Windows 上用 netstat 查某 pid 的 LISTENING 端口集合。

    非 Windows 或 netstat 不可用 → 返回空集（调用方据此跳过该子断言，
    绝不让「查不到」被当成「没有」——那正是断言里的经典错误）。
    """
    if os.name != 'nt':
        return set()
    try:
        out = subprocess.run(['netstat', '-ano'], capture_output=True, timeout=20).stdout
    except Exception:
        return set()
    if isinstance(out, bytes):
        out = out.decode('latin1', 'replace')
    ports = set()
    for line in out.splitlines():
        if 'LISTENING' not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5 or parts[-1] != str(pid):
            continue
        local = parts[1]
        if ':' in local:
            ports.add(int(local.rsplit(':', 1)[1]))
    return ports


def _build_sandbox(tmp: str) -> str:
    """复制 app/ 与公开题库到临时「沙箱根」。

    为什么必须隔离：core.ROOT 是 `Path(__file__).parent.parent`，随 app/ 位置走。
    直接在仓库里跑 api.py 会读写真实的 data/（用户的进度、备注）——
    测试绝不能碰用户数据。
    公开模式（只放 data/public/qbank.json，不放 qbank.sqlite）保证
    沙箱里连 sqlite 都不碰。
    """
    sandbox = os.path.join(tmp, 'sandbox')
    os.makedirs(os.path.join(sandbox, 'app'), exist_ok=True)
    for name in os.listdir(_APP):
        if name.endswith('.py'):
            shutil.copy2(os.path.join(_APP, name), os.path.join(sandbox, 'app', name))
    os.makedirs(os.path.join(sandbox, 'data', 'public'), exist_ok=True)
    shutil.copy2(os.path.join(_ROOT, 'data', 'public', 'qbank.json'),
                 os.path.join(sandbox, 'data', 'public', 'qbank.json'))
    return sandbox


def _child_env(port: int) -> dict:
    env = dict(os.environ, QBANK_NO_BROWSER='1', QBANK_PORT=str(port), QBANK_PORT_SCAN='1')
    for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
        env.pop(k, None)
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


class FrozenDispatchTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qbank-frozen-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sandbox = _build_sandbox(self.tmp)

    # ---------------------------------------------------------------- a
    @unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf')
    def test_worker_argv_writes_result_and_exits_zero(self):
        """a. `--pdf-worker IN OUT` → 结果文件正确 + 退出码 0。"""
        src = os.path.join(_FIX, '01-chinese-text.pdf')
        if not os.path.exists(src):
            self.skipTest('缺少样本 PDF')
        in_path = os.path.join(self.tmp, 'in.pdf')
        out_path = os.path.join(self.tmp, 'out.json')
        shutil.copy2(src, in_path)

        proc = subprocess.run([PY, os.path.join(self.sandbox, 'app', 'api.py'),
                               '--pdf-worker', in_path, out_path],
                              cwd=self.sandbox, env=_child_env(_free_port()),
                              capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         '退出码 %d，stdout=%r stderr=%r' % (proc.returncode,
                                                          proc.stdout[:400], proc.stderr[:400]))
        self.assertTrue(os.path.exists(out_path), '没有写出结果文件')
        with open(out_path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        self.assertEqual(payload.get('protocol'), 1)
        result = payload.get('result') or {}
        self.assertEqual(result.get('status'), 'ok', str(result)[:200])
        self.assertTrue(result.get('total_chars', 0) > 0)

    # ---------------------------------------------------------------- b
    @unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf')
    def test_worker_argv_starts_no_server(self):
        """b. worker 路径**不开端口**。

        主证据：进程自己退出了（Web 服务会一直挂着）。
        辅证：目标端口全程拒连 + netstat 里该 pid 无 LISTENING 条目。
        """
        src = os.path.join(_FIX, '01-chinese-text.pdf')
        if not os.path.exists(src):
            self.skipTest('缺少样本 PDF')
        in_path = os.path.join(self.tmp, 'in.pdf')
        out_path = os.path.join(self.tmp, 'out.json')
        shutil.copy2(src, in_path)
        port = _free_port()
        # stdout/stderr 落文件而不是 PIPE：worker 若真写了大量输出，
        # PIPE 会塞满后把子进程卡死（这正是 pdf_client 禁用 PIPE 的同一原因）。
        logf = open(os.path.join(self.tmp, 'worker.log'), 'wb')
        proc = subprocess.Popen([PY, os.path.join(self.sandbox, 'app', 'api.py'),
                                 '--pdf-worker', in_path, out_path],
                                cwd=self.sandbox, env=_child_env(port),
                                stdout=logf, stderr=subprocess.STDOUT)
        saw_listening = set()
        deadline = time.time() + 60
        while proc.poll() is None and time.time() < deadline:
            saw_listening |= _listening_ports_of(proc.pid)
            self.assertTrue(_port_refused(port),
                            '端口 %d 被监听了 —— worker 路径退化成 Web 服务了' % port)
            time.sleep(0.1)
        rc = proc.wait(timeout=30)
        logf.close()
        self.assertEqual(rc, 0, '退出码 %d' % rc)
        self.assertFalse(saw_listening,
                         'worker 进程监听了端口 %s' % sorted(saw_listening))
        self.assertTrue(_port_refused(port), '端口 %d 事后仍被占用' % port)

    # ---------------------------------------------------------------- c
    @unittest.skipUnless(HAS_FASTAPI, '需要 fastapi')
    def test_without_flag_still_serves_http(self):
        """c. 不带 `--pdf-worker` 时仍然是 Web 服务（正常启动路径没被改坏）。"""
        port = _free_port()
        env = _child_env(port)
        # 日志落文件：Web 服务会持续往 stdout 写启动日志，PIPE 塞满会把子进程卡死。
        logf = open(os.path.join(self.tmp, 'web.log'), 'wb')
        proc = subprocess.Popen([PY, os.path.join(self.sandbox, 'app', 'api.py')],
                                cwd=self.sandbox, env=env,
                                stdout=logf, stderr=subprocess.STDOUT)
        try:
            code, body = self._poll_http(port, '/api/v1/questions?limit=1', proc)
            self.assertEqual(code, 200, 'HTTP %s，body=%r' % (code, body[:200]))
            data = json.loads(body.decode('utf-8'))
            self.assertIsInstance(data.get('items'), list)
        finally:
            proc.kill()
            try:
                proc.wait(timeout=15)
            except Exception:
                pass
            try:
                logf.close()
            except Exception:
                pass

    def _poll_http(self, port, path, proc, tries=100, gap=0.25):
        """轮询端口拿 HTTP 响应。子进程秒退（崩溃）时提前放弃。"""
        for _ in range(tries):
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
                    conn.sendall(('GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                                  'Connection: close\r\n\r\n' % path).encode('ascii'))
                    raw = b''
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                head, _, body = raw.partition(b'\r\n\r\n')
                status = int(head.split(b' ')[1]) if b' ' in head else 0
                return status, body
            except OSError:
                time.sleep(gap)
        return 0, b''

    # ---------------------------------------------------------------- d
    def test_bad_arg_count_exits_cleanly(self):
        """d. 参数个数不对 → 干净退出（码 2），不落进 main() 去开端口。"""
        port = _free_port()
        proc = subprocess.run([PY, os.path.join(self.sandbox, 'app', 'api.py'),
                               '--pdf-worker'],
                              cwd=self.sandbox, env=_child_env(port),
                              capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 2, '退出码 %d（stdout=%r）'
                         % (proc.returncode, proc.stdout[:200]))
        self.assertTrue(_port_refused(port), '错误参数时竟然开了端口')

    # ---------------------------------------------------------------- f
    @unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf')
    def test_real_frozen_exe_via_client(self):
        """f. **真 exe** 走 client 的 frozen 分支（frozen=True + python=exe）。

        前面的 a/b 是用 `python app/api.py --pdf-worker` 模拟 frozen；
        这条是真的跑 dist\\mech-qbank.exe，覆盖 pdf_client.worker_command()
        的 frozen 分支 + exe 的 argv 分发的**组合**。
        没有打过包就 skip（不要求每次测试都先打包）。
        """
        exe = os.path.join(_ROOT, 'dist', 'mech-qbank.exe')
        if not os.path.exists(exe):
            self.skipTest('没有 dist\\mech-qbank.exe（先跑 build_exe.bat）')
        src = os.path.join(_FIX, '01-chinese-text.pdf')
        if not os.path.exists(src):
            self.skipTest('缺少样本 PDF')
        with open(src, 'rb') as fh:
            blob = fh.read()
        import pdf_client
        r = pdf_client.extract_in_worker(blob, timeout=180, frozen=True, python=exe)
        self.assertEqual(r.get('status'), 'ok', str(r)[:300])
        self.assertTrue(r.get('total_chars', 0) > 0)

    # ---------------------------------------------------------------- e
    def test_dispatch_precedes_core_import(self):
        """e. 源码结构：分发必须在 `import core` 之前。

        为什么这是结构性断言而不是风格偏好：worker 进程不该建 DATA 目录、
        不该连 sqlite、更不该拖进 fastapi（PyMuPDF 冷启动已经要 2s，
        再叠上 Web 框架的 import 时间，worker 的 60s 超时预算就白给了）。
        """
        with open(os.path.join(_APP, 'api.py'), 'r', encoding='utf-8') as fh:
            src = fh.read()
        idx_dispatch = src.find('--pdf-worker')
        idx_core = src.find('\nimport core')
        self.assertNotEqual(idx_dispatch, -1, 'api.py 里找不到 --pdf-worker 分发')
        self.assertNotEqual(idx_core, -1, 'api.py 里找不到 import core')
        self.assertLess(idx_dispatch, idx_core,
                        '分发必须写在 import core 之前（worker 路径不许碰数据层）')


if __name__ == '__main__':
    unittest.main(verbosity=2)
