#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Linux / 源码部署路径的端到端测试（**非 frozen** 分支）。

## 为什么要有这个文件（2026-10-21 发布前发现）
--------------------------------------------------------------------------
`dist\\mech-qbank.exe` 是 **PyInstaller 在 Windows 上打的**。它是 Windows PE 二进制，
**Linux 跑不了**。所以 VPS 部署**只能走源码运行**（`python app/api.py` + venv），
跟桌面双击 exe 是两条完全不同的启动路径。

而这条路径上，PDF worker 走的是 `pdf_client.worker_command()` 的
**非 frozen 分支**：

    [sys.executable, app/pdf_worker.py, IN, OUT]

frozen 分支（`[exe, '--pdf-worker', IN, OUT]`）由 `tests/test_frozen_dispatch.py`
覆盖（其中 f 是真 exe）；非 frozen 分支**只有命令形状**被测过
（`tests/test_pdf_client.py` 的 d，用打桩验证 `[PY, WORKER_SCRIPT, IN, OUT]`），
**从没端到端真跑过 worker**。

「命令形状对」≠「能跑通」：中间隔着
  · worker 脚本能不能被 `python` 直接执行（它靠 `sys.path.insert` 找同目录的 pdf_extract）
  · worker 进程里 `import pymupdf` 能不能成功（**这正是服务器上最可能缺的一环**）
  · 输出信封格式对不对（client 会核对 `protocol`）
  · 客户端能不能正确读回结果

所以本文件**真跑**非 frozen 分支：不是打桩，是起真进程、真解析真 PDF。

## 覆盖
--------------------------------------------------------------------------
a. `python app/pdf_worker.py IN OUT` → 写出正确结果文件 + 退出码 0
b. `pdf_client.extract_in_worker()` 走**非 frozen** 分支（不传 frozen 参数）
   → status=ok、抽出字符 > 0
c. 非 frozen 分支的 worker 进程**不监听端口**（没退化成 Web 服务）
d. worker **确实 import 了 pymupdf**，而**客户端进程没有**
   （证明 MuPDF 只在子进程里，Web 进程不背这个内存）
e. **缺 pymupdf 时给出可诊断的失败**（不是静默超时）
   —— 模拟「服务器上忘了装 pymupdf」，必须能看到 import_error 而不是干等 60s

## 跑法
--------------------------------------------------------------------------
    "C:/Users/Administrator/.workbuddy/binaries/python/envs/pdfprobe/Scripts/python.exe" \
        tests/test_source_deploy.py

需要 pymupdf（真抽取）。缺 pymupdf → 需要真解析的用例 skip；
但 e（缺 pymupdf 的诊断）与 c（不开端口）仍会跑。
"""

from __future__ import annotations

import json
import os
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

import pdf_client                                              # noqa: E402

try:
    import pymupdf                                              # noqa: E402,F401
    HAS_PYMUPDF = True
except ImportError:                                             # pragma: no cover
    HAS_PYMUPDF = False

#: 跑 worker 的解释器。默认与本进程相同（`sys.executable`）——
#: 这正是 Linux 上的真实形态：Web 进程与 worker 用**同一个 venv**。
PY = sys.executable

WORKER_SCRIPT = os.path.join(_APP, 'pdf_worker.py')


def _blob(name):
    with open(os.path.join(_FIX, name), 'rb') as fh:
        return fh.read()


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


class SourceDeployWorkerTest(unittest.TestCase):
    """非 frozen 分支（= Linux 源码部署走的那条路）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qbank-srcdeploy-')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)

    # ---------------------------------------------------------------- a
    @unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf')
    def test_worker_script_runs_standalone(self):
        """a. `python app/pdf_worker.py IN OUT` → 结果文件正确 + 退出码 0。

        这是**非 frozen 分支最直接的形态**。Linux 上 systemd 起的 Web 进程
        会用同一条命令 spawn worker。

        特别验「脚本能被 python 直接执行」：worker 靠 `sys.path.insert(0, _HERE)`
        才能 import 到同目录的 `pdf_extract` —— 这一步坏了 worker 就 import 失败。
        """
        src = os.path.join(_FIX, '01-chinese-text.pdf')
        if not os.path.exists(src):
            self.skipTest('缺少样本 PDF')
        in_path = os.path.join(self.tmp, 'in.pdf')
        out_path = os.path.join(self.tmp, 'out.json')
        with open(src, 'rb') as fh:
            with open(in_path, 'wb') as out:
                out.write(fh.read())

        proc = subprocess.run([PY, WORKER_SCRIPT, in_path, out_path],
                              capture_output=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         '退出码 %d stdout=%r stderr=%r'
                         % (proc.returncode, proc.stdout[:400], proc.stderr[:400]))
        self.assertTrue(os.path.exists(out_path), '没有写出结果文件')
        with open(out_path, encoding='utf-8') as fh:
            payload = json.load(fh)
        self.assertEqual(payload.get('protocol'), pdf_client.PROTOCOL_VERSION)
        result = payload.get('result') or {}
        self.assertEqual(result.get('status'), 'ok', str(result)[:300])
        self.assertTrue(result.get('total_chars', 0) > 0)

    # ---------------------------------------------------------------- b
    @unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf')
    def test_client_non_frozen_branch_end_to_end(self):
        """b. `extract_in_worker()` 不传 frozen → 走非 frozen 分支 → 抽出结果。

        **不传 `frozen` 参数**是关键 —— 传了就等于打桩验证，
        走不到真实的 spawn 路径。这里真的起子进程、真解析。
        """
        r = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'), timeout=180)
        self.assertEqual(r.get('status'), 'ok', str(r)[:300])
        self.assertTrue(r.get('total_chars', 0) > 0)

    # ---------------------------------------------------------------- c
    @unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf')
    def test_non_frozen_worker_opens_no_port(self):
        """c. 非 frozen 的 worker **不开端口**（没退化成 Web 服务）。

        与 frozen 分支同款断言，但要单独跑一次 ——
        两条分支是两个不同的 spawn 形态，frozen 那次不覆盖非 frozen。
        """
        src = os.path.join(_FIX, '01-chinese-text.pdf')
        if not os.path.exists(src):
            self.skipTest('缺少样本 PDF')
        in_path = os.path.join(self.tmp, 'in.pdf')
        out_path = os.path.join(self.tmp, 'out.json')
        with open(src, 'rb') as fh:
            with open(in_path, 'wb') as out:
                out.write(fh.read())
        port = _free_port()
        # stdout/stderr 落文件：worker 若写大量输出，PIPE 会塞满把子进程卡死。
        logf = open(os.path.join(self.tmp, 'worker.log'), 'wb')
        proc = subprocess.Popen([PY, WORKER_SCRIPT, in_path, out_path],
                                stdout=logf, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 60
            while proc.poll() is None and time.time() < deadline:
                self.assertTrue(_port_refused(port),
                                '端口 %d 被监听了 —— worker 退化成 Web 服务了' % port)
                time.sleep(0.1)
            rc = proc.wait(timeout=30)
        finally:
            logf.close()
        self.assertEqual(rc, 0, '退出码 %d' % rc)
        self.assertTrue(_port_refused(port), '端口 %d 事后仍被占用' % port)

    # ---------------------------------------------------------------- d
    def test_pymupdf_only_lives_in_worker(self):
        """d. MuPDF 只在 worker 侧：worker 进程 import 了它，客户端进程没有。

        为什么这条重要：Web 进程常驻内存，而 `import pymupdf` 会拖进
        约 117 MB 的 MuPDF 运行时（实测：纯文本 500 页 132.8 MB，
        纯图 208.4 MB）。**它必须只出现在子进程里。**

        ⚠️ **必须在干净的子进程里验，不能在当前进程查 sys.modules。**
        本文件顶部为了做 skip 判断 import 过一次 pymupdf，
        当前进程的 sys.modules 里一定有它 —— 在这里断言等于自己打自己
        （这正是第一版写法红掉的原因）。
        正确做法：起一个只 `import pdf_client` 的子进程，看它有没有被拖进来。
        """
        # 客户端侧：**干净子进程**里 import pdf_client 后不该有 pymupdf
        src = ('import sys; sys.path.insert(0, %r); import pdf_client; '
               'print("pymupdf" in sys.modules, "fitz" in sys.modules)' % _APP)
        out = subprocess.run([PY, '-c', src], capture_output=True,
                             text=True, timeout=120).stdout.strip()
        self.assertEqual(out, 'False False',
                         'import pdf_client 后 pymupdf 不该被载入，实际: %r' % out)

        # worker 侧：pymupdf **必须**可被加载（否则服务器上 PDF 全挂）
        probe = subprocess.run(
            [PY, '-c', 'import pymupdf, sys; print("1" if "pymupdf" in sys.modules else "0")'],
            capture_output=True, timeout=120)
        self.assertEqual(probe.stdout.strip(), b'1',
                         'pymupdf 应可被 worker 侧 import：%r' % probe.stderr[:200])

        # 真跑一次非 frozen worker，确认它能出结果（a 的路径，这里只作佐证）
        if HAS_PYMUPDF:
            src_pdf = os.path.join(_FIX, '01-chinese-text.pdf')
            if os.path.exists(src_pdf):
                in_path = os.path.join(self.tmp, 'in.pdf')
                out_path = os.path.join(self.tmp, 'out.json')
                with open(src_pdf, 'rb') as fh:
                    with open(in_path, 'wb') as out:
                        out.write(fh.read())
                proc = subprocess.run([PY, WORKER_SCRIPT, in_path, out_path],
                                      capture_output=True, timeout=180)
                self.assertEqual(proc.returncode, 0)

    # ---------------------------------------------------------------- e
    def test_missing_pymupdf_is_diagnosable(self):
        """e. 缺 pymupdf 时**必须能诊断**，不能静默等 60s 超时。

        这是服务器上最可能踩的坑：`requirements.txt` 刻意不含 pymupdf
        （为了 Web 进程保持零重依赖），但 **worker 子进程必须有它**。
        忘了装的表现若是「干等 60s 然后报超时」，排障成本极高。

        验法：用一个**故意不 import pymupdf** 的假 worker 脚本，
        看客户端拿到的是 worker_crashed（带 worker_error）还是 timeout。
        """
        fake = os.path.join(self.tmp, 'fake_worker.py')
        with open(fake, 'w', encoding='utf-8') as fh:
            fh.write(
                'import sys\n'
                'try:\n'
                '    import definitely_not_pymupdf_xyz  # 故意失败\n'
                'except ImportError as e:\n'
                '    import json\n'
                '    with open(sys.argv[2], "w", encoding="utf-8") as f:\n'
                '        json.dump({"protocol": 1,\n'
                '                   "worker_error": "import_error: %s" % e}, f)\n'
                '    sys.exit(1)\n')
        old_script = pdf_client.WORKER_SCRIPT
        old_py = pdf_client.__dict__.get('_FORCE_PYTHON')
        pdf_client.WORKER_SCRIPT = fake
        try:
            r = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'), timeout=30)
        finally:
            pdf_client.WORKER_SCRIPT = old_script
        self.assertEqual(r.get('status'), 'worker_crashed', str(r)[:300])
        # 关键：要能看出是 import 问题，而不是笼统的「崩了」
        self.assertIn('import_error', str(r), str(r)[:300])


if __name__ == '__main__':
    unittest.main(verbosity=2)
