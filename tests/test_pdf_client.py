#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app/pdf_client.py 的测试（worker 进程隔离层）。

## 覆盖
--------------------------------------------------------------------------
a. 正常 PDF → 抽取成功，且结果与直接调 extract() **逐字段一致**（同一份 bytes 比对）
b. worker 非零退出 / 无输出 / 输出是垃圾 → worker_crashed，
   **且调用方进程仍可继续调用**（这一条必须验，否则隔离等于没做）
c. worker 卡住不返回 → 到超时被 kill，返回 timeout，不永久挂起
d. frozen=True / False 两条 spawn 命令分支都正确（纯函数打桩，不真打包）
e. 并发上限 1：第二个请求立刻返回 busy，不排队、不 spawn

外加两条纪律性断言：
   - client **不 import pymupdf**（用 sys.modules 验）
   - client **不用 PIPE**（用源码检查 Popen 调用没有 stdout/stderr=PIPE）

## 跑法
--------------------------------------------------------------------------
    "C:/Users/Administrator/.workbuddy/binaries/python/envs/pdfprobe/Scripts/python.exe" \
        tests/test_pdf_client.py

没有 pymupdf 的环境下：本文件大部分测试仍会跑（client 本身不依赖它），
只有 a（直调比对）需要 pymupdf，会 skip。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), 'app')
_FIX = os.path.join(_HERE, 'fixtures', 'pdf')
for _p in (_APP, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pdf_client                                              # noqa: E402
import pdf_worker                                               # noqa: E402

try:
    import pdf_extract                                          # noqa: E402
    HAS_PYMUPDF = True
except ImportError:                                             # pragma: no cover
    pdf_extract = None
    HAS_PYMUPDF = False

# 超时测试要真等一小段时间，但不必等满 60s。
TEST_TIMEOUT = 2.0


def _blob(name):
    with open(os.path.join(_FIX, name), 'rb') as fh:
        return fh.read()


def _pid_alive(pid):
    """pid 对应的进程是否还活着。

    用 Windows 的 OpenProcess 而非 tasklist：tasklist 按镜像名过滤，
    机器上别的 python 进程会干扰判断，而且它对已退出但句柄未回收的进程
    也可能报错。OpenProcess + GetExitCodeProcess 是确定性的。
    非 Windows 退化为 os.kill(pid, 0)。
    """
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False                      # 进程已不存在
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class TestHappyPath(unittest.TestCase):
    """a. 正常 PDF 走 worker。"""

    def test_matches_direct_extract(self):
        """结果必须与直接调 extract() **完全一致**——worker 只是隔离层，
        不许改变任何抽取语义。"""
        if not HAS_PYMUPDF:
            self.skipTest('需要 pymupdf 做直调比对')
        for name in ('01-chinese-text.pdf', '02-two-column.pdf', '03-table.pdf',
                     '04-scanned.pdf', '05-encrypted.pdf', '06-corrupt.pdf'):
            with self.subTest(sample=name):
                blob = _blob(name)
                direct = dict(pdf_extract.extract(blob))
                via = pdf_client.extract_in_worker(blob)
                self.assertEqual(via['status'], direct['status'])
                self.assertEqual(via['total_chars'], direct['total_chars'])
                self.assertEqual(via['page_count'], direct['page_count'])
                self.assertEqual(via['columns_detected'], direct['columns_detected'])
                self.assertEqual(via['table_pages'], direct['table_pages'])
                self.assertEqual(via['image_coverage'], direct['image_coverage'])
                self.assertEqual(via['repaired'], direct['repaired'])
                self.assertEqual(via['is_encrypted'], direct['is_encrypted'])
                # 逐字段全等（含 pages 里的每页文本）
                self.assertEqual(via, direct)

    def test_empty_blob_short_circuits(self):
        """空字节不该 spawn worker——直接判 corrupt。"""
        r = pdf_client.extract_in_worker(b'')
        self.assertEqual(r['status'], 'corrupt')
        self.assertEqual(r['code'], 'corrupt_document')

    def test_oversize_input_rejected_before_spawn(self):
        """超大输入在进 worker 之前就拒掉，不给「吃穿内存」的机会。"""
        r = pdf_client.extract_in_worker(b'%PDF-1.4\n' + b'x' * (pdf_client.MAX_INPUT_BYTES + 1))
        self.assertEqual(r['status'], 'corrupt')
        self.assertEqual(r['code'], 'payload_too_large')

    def test_result_is_plain_dict(self):
        """返回必须是普通 dict——client 不 import pdf_extract，不能返回它的类。"""
        r = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'))
        self.assertIs(type(r), dict)


class TestCrashIsolation(unittest.TestCase):
    """b. worker 崩溃/异常，调用方必须活着。"""

    def setUp(self):
        self._env_backup = None

    def _run_with_fault(self, fault, timeout=TEST_TIMEOUT):
        """让 worker 带故障注入跑一遍。

        注意改的是**子进程**的环境变量：client 只把 sys.executable 与
        脚本路径传给 Popen，子进程默认继承父进程 os.environ。
        """
        old = os.environ.get('QBANK_PDF_FAULT')
        os.environ['QBANK_PDF_FAULT'] = fault
        try:
            return pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'),
                                               timeout=timeout)
        finally:
            if old is None:
                os.environ.pop('QBANK_PDF_FAULT', None)
            else:
                os.environ['QBANK_PDF_FAULT'] = old

    def test_nonzero_exit_is_worker_crashed(self):
        r = self._run_with_fault('exit:3')
        self.assertEqual(r['status'], 'worker_crashed')
        self.assertEqual(r['code'], 'internal_error')
        self.assertIn('exit code 3', r['error'])

    def test_exit_code_one_carries_detail(self):
        """worker 捕获到异常时应把详情写进输出文件，客户端要能读到。"""
        r = self._run_with_fault('raise')
        self.assertEqual(r['status'], 'worker_crashed')
        self.assertIn('injected fault', r['error'])

    def test_silent_exit_zero_is_still_crashed(self):
        """退出码 0 但没写输出文件 —— 必须判 crashed，不能当成成功返回空结果。

        这是最阴险的一类：只看退出码的实现会在这里返回 status 缺失的脏数据。
        """
        r = self._run_with_fault('silent')
        self.assertEqual(r['status'], 'worker_crashed')
        self.assertIn('wrote no output', r['error'])

    def test_garbage_output_is_crashed(self):
        r = self._run_with_fault('garbage')
        self.assertEqual(r['status'], 'worker_crashed')
        self.assertIn('not readable JSON', r['error'])

    def test_caller_survives_and_can_call_again(self):
        """**本组测试的核心**：worker 崩了之后，调用方进程必须还能继续工作。

        连着「崩 → 正常 → 崩 → 正常」四轮，若调用方被拖死或锁没释放，
        第二轮就会挂起或返回 busy。
        """
        for i in range(2):
            bad = self._run_with_fault('exit:%d' % (7 + i))
            self.assertEqual(bad['status'], 'worker_crashed')
            good = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'))
            self.assertEqual(good['status'], 'ok', '第 %d 轮崩溃后调用方应仍可用' % (i + 1))
            self.assertEqual(good['total_chars'], 653)

    def test_lock_released_after_crash(self):
        """崩溃路径必须释放并发锁——否则一次坏 PDF 就把功能永久锁死。"""
        self._run_with_fault('exit:9')
        self.assertFalse(pdf_client._spawn_lock.locked(),
                         '崩溃后并发锁仍被持有，功能会被永久锁死')

    def test_no_temp_dirs_leaked(self):
        """临时目录必须清理干净（PDF 是用户上传内容，不能留在磁盘上）。"""
        tmp_root = tempfile.gettempdir()
        before = {d for d in os.listdir(tmp_root) if d.startswith('qbank_pdf_')}
        pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'))
        self._run_with_fault('exit:4')
        pdf_client.extract_in_worker(b'')
        after = {d for d in os.listdir(tmp_root) if d.startswith('qbank_pdf_')}
        self.assertEqual(after - before, set(), '泄漏了临时目录: %s' % (after - before))


class TestTimeout(unittest.TestCase):
    """c. worker 卡住 → 到点被 kill。"""

    def test_hang_is_killed_at_timeout(self):
        old = os.environ.get('QBANK_PDF_FAULT')
        os.environ['QBANK_PDF_FAULT'] = 'hang'
        t0 = time.monotonic()
        try:
            r = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'),
                                            timeout=TEST_TIMEOUT)
        finally:
            if old is None:
                os.environ.pop('QBANK_PDF_FAULT', None)
            else:
                os.environ['QBANK_PDF_FAULT'] = old
        elapsed = time.monotonic() - t0
        self.assertEqual(r['status'], 'timeout')
        self.assertEqual(r['code'], 'parse_timeout')
        self.assertIn('was killed', r['error'])
        # 不永久挂起：应在超时后 1 秒内返回（kill + wait 有开销，留 1s 余量）
        self.assertLess(elapsed, TEST_TIMEOUT + 1.0,
                        '超时后未及时返回，耗时 %.2fs' % elapsed)
        # 也不能提前放弃：至少得等满超时时间
        self.assertGreaterEqual(elapsed, TEST_TIMEOUT * 0.9,
                                '提前返回了，耗时仅 %.2fs' % elapsed)

    def test_timeout_does_not_block_next_call(self):
        """超时之后锁必须释放、下一次调用要能正常跑完。"""
        old = os.environ.get('QBANK_PDF_FAULT')
        os.environ['QBANK_PDF_FAULT'] = 'hang'
        try:
            self.assertEqual(
                pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'),
                                             timeout=TEST_TIMEOUT)['status'],
                'timeout')
        finally:
            if old is None:
                os.environ.pop('QBANK_PDF_FAULT', None)
            else:
                os.environ['QBANK_PDF_FAULT'] = old
        good = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'))
        self.assertEqual(good['status'], 'ok')

    def test_no_orphan_process_left(self):
        """超时 kill 后不能留孤儿进程继续吃内存——那正是要防的 117MB。

        **必须验 pid，不能只断言「tasklist 能跑」。** worker 在 hang 时会把自己的
        pid 写到 `<output>.pid`，这里在超时返回后检查该进程确实已不存在。
        上一版这条测试只断言了 `isinstance(out, str)`，等于什么都没验。
        """
        import subprocess
        tmp = tempfile.mkdtemp(prefix='qbank_pdf_orphan_')
        pidfile = os.path.join(tmp, 'out.json.pid')
        env_backup = os.environ.get('QBANK_PDF_FAULT')
        os.environ['QBANK_PDF_FAULT'] = 'hang'
        try:
            # 直接走 Popen，绕过 client 的临时目录清理，才能拿到 pid 文件
            input_path = os.path.join(tmp, 'in.pdf')
            with open(input_path, 'wb') as fh:
                fh.write(_blob('01-chinese-text.pdf'))
            output_path = os.path.join(tmp, 'out.json')
            cmd = pdf_client.worker_command(input_path, output_path)
            proc = subprocess.Popen(cmd)
            deadline = time.monotonic() + 10.0
            while not os.path.exists(pidfile) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(pidfile), 'worker 未写出 pid 文件')
            with open(pidfile, encoding='ascii') as fh:
                pid = int(fh.read().strip())
            pdf_client._kill(proc)
            # 给操作系统一点回收时间
            for _ in range(50):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            self.assertFalse(_pid_alive(pid),
                             '超时后 worker pid=%d 仍在运行（孤儿进程）' % pid)
        finally:
            if env_backup is None:
                os.environ.pop('QBANK_PDF_FAULT', None)
            else:
                os.environ['QBANK_PDF_FAULT'] = env_backup
            for name in os.listdir(tmp):
                try:
                    os.remove(os.path.join(tmp, name))
                except OSError:
                    pass
            try:
                os.rmdir(tmp)
            except OSError:
                pass


class TestConcurrencyLimit(unittest.TestCase):
    """并发上限 1。"""

    def test_second_call_returns_busy_immediately(self):
        """持锁期间第二个调用必须**立刻**返回 busy，不能排队。"""
        if not pdf_client._spawn_lock.acquire(blocking=False):
            self.skipTest('锁已被其他测试占用')
        try:
            t0 = time.monotonic()
            r = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'))
            elapsed = time.monotonic() - t0
        finally:
            pdf_client._spawn_lock.release()
        self.assertEqual(r['status'], 'busy')
        self.assertEqual(r['code'], 'too_many_requests')
        self.assertLess(elapsed, 1.0, 'busy 判定不该等待，耗时 %.2fs' % elapsed)

    def test_busy_does_not_spawn(self):
        """busy 时不能起子进程——否则「上限 1」名存实亡。"""
        spawned = []
        original = pdf_client.subprocess.Popen

        class _Spy:
            def __init__(self, *a, **kw):
                spawned.append(a)
                raise AssertionError('busy 时不该 spawn')

        if not pdf_client._spawn_lock.acquire(blocking=False):
            self.skipTest('锁已被其他测试占用')
        pdf_client.subprocess.Popen = _Spy
        try:
            r = pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'))
        finally:
            pdf_client.subprocess.Popen = original
            pdf_client._spawn_lock.release()
        self.assertEqual(r['status'], 'busy')
        self.assertEqual(spawned, [])

    def test_lock_free_after_normal_run(self):
        pdf_client.extract_in_worker(_blob('01-chinese-text.pdf'))
        self.assertFalse(pdf_client._spawn_lock.locked())


class TestWorkerCommand(unittest.TestCase):
    """d. frozen / 非 frozen 两条命令分支。纯函数打桩，不真打包。"""

    def test_non_frozen_uses_script_path(self):
        cmd = pdf_client.worker_command('IN', 'OUT', frozen=False, python='PY')
        self.assertEqual(cmd, ['PY', pdf_client.WORKER_SCRIPT, 'IN', 'OUT'])

    def test_frozen_uses_worker_flag_and_no_script(self):
        """frozen 分支**绝不能带脚本路径**。

        带了就变成 `mech-qbank.exe app/pdf_worker.py IN OUT`，exe 会把
        脚本名当普通参数，worker 永远不会被调用——而且不报错，只是静默失效。
        """
        cmd = pdf_client.worker_command('IN', 'OUT', frozen=True, python='EXE')
        self.assertEqual(cmd, ['EXE', '--pdf-worker', 'IN', 'OUT'])
        self.assertNotIn(pdf_client.WORKER_SCRIPT, cmd,
                         'frozen 分支混入了脚本路径，exe 会把脚本名当参数')
        self.assertNotIn('pdf_worker.py', ' '.join(cmd))

    def test_default_reads_sys_frozen(self):
        """不传 frozen 时必须读 sys.frozen（PyInstaller 注入的属性）。"""
        saved = getattr(sys, 'frozen', None)
        had = hasattr(sys, 'frozen')
        try:
            sys.frozen = True
            self.assertEqual(pdf_client.worker_command('IN', 'OUT', python='EXE')[1],
                             '--pdf-worker')
            sys.frozen = False
            self.assertEqual(pdf_client.worker_command('IN', 'OUT', python='PY')[1],
                             pdf_client.WORKER_SCRIPT)
        finally:
            if had:
                sys.frozen = saved
            else:
                delattr(sys, 'frozen')

    def test_worker_script_exists(self):
        self.assertTrue(os.path.isfile(pdf_client.WORKER_SCRIPT))

    def test_worker_main_accepts_both_argv_shapes(self):
        """worker 自身要同时接受 `IN OUT` 与 `--pdf-worker IN OUT`。

        打桩 run() 只验参数解析，不真跑抽取。
        """
        calls = []
        saved = pdf_worker.run
        pdf_worker.run = lambda i, o: calls.append((i, o)) or 0
        try:
            self.assertEqual(pdf_worker.main(['A', 'B']), 0)
            self.assertEqual(pdf_worker.main(['--pdf-worker', 'A', 'B']), 0)
            self.assertEqual(calls, [('A', 'B'), ('A', 'B')])
        finally:
            pdf_worker.run = saved

    def test_worker_main_rejects_bad_argv(self):
        """参数个数不对必须返回非 0 —— 不能静默成功。"""
        for bad in ([], ['A'], ['A', 'B', 'C']):
            self.assertNotEqual(pdf_worker.main(bad), 0,
                                '参数 %r 应被拒绝' % (bad,))


class TestDiscipline(unittest.TestCase):
    """纪律性断言：这些是设计约束，必须由测试守住。"""

    def test_client_does_not_import_pymupdf(self):
        """**Web 进程不得持有 MuPDF。** client 只能 import 标准库那几个。

        这条必须在**干净子进程**里验：本测试文件自己 import 了 pdf_extract
        （为了 a 的直调比对），当前进程的 sys.modules 里一定有 pymupdf，
        在这里断言等于自己打自己。
        """
        import subprocess
        src = (
            'import sys; sys.path.insert(0, %r); import pdf_client; '
            'print("pymupdf" in sys.modules, "fitz" in sys.modules)'
            % _APP
        )
        out = subprocess.run([sys.executable, '-c', src],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        self.assertEqual(out, 'False False',
                         'import pdf_client 后 pymupdf 不该被载入，实际: %r' % out)

    def test_client_imports_only_whitelisted_stdlib(self):
        """client 只许 import 标准库那几个白名单模块（不得有第三方库）。"""
        src = open(pdf_client.__file__, encoding='utf-8').read()
        imports = re.findall(r'^\s*(?:import|from)\s+([A-Za-z_][\w.]*)', src, re.M)
        allowed = {'subprocess', 'tempfile', 'json', 'os', 'sys', 'time',
                   'threading', '__future__'}
        extra = {m.split('.')[0] for m in imports} - allowed
        self.assertEqual(extra, set(), 'client 引入了非白名单模块: %s' % extra)

    def test_client_does_not_import_pdf_extract(self):
        """client 连 pdf_extract 都不能 import —— 那会把 pymupdf 拖进来。"""
        src = open(pdf_client.__file__, encoding='utf-8').read()
        code = re.sub(r'#[^\n]*', '', src)
        self.assertNotRegex(code, r'^\s*import\s+pdf_extract',
                            'client 不该 import pdf_extract')

    def test_client_never_uses_pipe(self):
        """**禁止 PIPE**：写端无人读会卡死，read() 等 EOF 会永久阻塞。"""
        src = open(pdf_client.__file__, encoding='utf-8').read()
        code = re.sub(r'#[^\n]*', '', src, flags=re.M)
        code = re.sub(r'""".*?"""', '', code, flags=re.S)
        for bad in ('subprocess.PIPE', 'stdout=subprocess', 'stderr=subprocess',
                    '.communicate(', 'check_output('):
            self.assertNotIn(bad, code, 'client 里出现了 %r' % bad)

    def test_pymupdf_only_referenced_by_extractor(self):
        """全仓库范围：真正 import pymupdf 的只有 pdf_extract.py。"""
        app_dir = os.path.dirname(pdf_client.__file__)
        offenders = []
        for name in sorted(os.listdir(app_dir)):
            if not name.endswith('.py'):
                continue
            path = os.path.join(app_dir, name)
            code = open(path, encoding='utf-8').read()
            code = re.sub(r'#[^\n]*', '', code)
            if re.search(r'^\s*import\s+pymupdf|^\s*from\s+pymupdf', code, re.M):
                offenders.append(name)
        self.assertEqual(offenders, ['pdf_extract.py'],
                         'import pymupdf 的文件应只有 pdf_extract.py，实际: %s' % offenders)

    def test_worker_never_prints(self):
        """frozen + windowed 下 stdout/stderr 是 None，print 会 AttributeError。"""
        src = open(pdf_worker.__file__, encoding='utf-8').read()
        code = re.sub(r'#.*', '', src)
        code = re.sub(r'""".*?"""', '', code, flags=re.S)
        code = re.sub(r"'''.*?'''", '', code, flags=re.S)
        self.assertNotRegex(code, r'(?<![\w.])print\s*\(', 'worker 里不该有 print')

    def test_protocol_versions_match(self):
        self.assertEqual(pdf_client.PROTOCOL_VERSION, pdf_worker.PROTOCOL_VERSION)

    def test_worker_output_is_valid_json_envelope(self):
        """输出必须是带 protocol 的信封，客户端才能区分「旧版 exe」与「解析失败」。"""
        tmp = tempfile.mkdtemp(prefix='qbank_pdf_test_')
        try:
            out = os.path.join(tmp, 'out.json')
            src = os.path.join(tmp, 'in.pdf')
            with open(src, 'wb') as fh:
                fh.write(_blob('01-chinese-text.pdf'))
            self.assertEqual(pdf_worker.run(src, out), 0)
            with open(out, encoding='utf-8') as fh:
                payload = json.load(fh)
            self.assertEqual(payload['protocol'], pdf_worker.PROTOCOL_VERSION)
            self.assertEqual(payload['result']['status'], 'ok')
        finally:
            for name in os.listdir(tmp):
                try:
                    os.remove(os.path.join(tmp, name))
                except OSError:
                    pass
            os.rmdir(tmp)


if __name__ == '__main__':
    unittest.main(verbosity=2)
