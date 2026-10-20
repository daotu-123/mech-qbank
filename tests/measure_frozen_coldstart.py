#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量 frozen exe 的 `--pdf-worker` 冷启动耗时，并与非 frozen 对比。

## 为什么必须实测，不能估
--------------------------------------------------------------------------
PyInstaller `--onefile` 每次运行都要把整个包（含 PyMuPDF 的 105 MB 资源）
解到临时目录 _MEIPASS。这段开销在源码运行下**完全不存在**，
所以「非 frozen 1.776s」不能拿来推断 frozen 的表现。

本脚本量三件事：
  1. exe 的 `--pdf-worker` 冷启动（首次，含解包）
  2. exe 的 `--pdf-worker` 热启动（第二次，_MEIPASS 可能已被系统缓存）
  3. 同进程内 `python app/api.py --pdf-worker` 的对照

## 跑法
--------------------------------------------------------------------------
    "C:/Users/Administrator/.workbuddy/binaries/python/envs/pdfprobe/Scripts/python.exe" \
        tests/measure_frozen_coldstart.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FIX = os.path.join(_HERE, 'fixtures', 'pdf')
_EXE = os.path.join(_ROOT, 'dist', 'mech-qbank.exe')


def _time_cmd(cmd, cwd, timeout=300):
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    return time.perf_counter() - t0, proc.returncode


def main() -> int:
    if not os.path.exists(_EXE):
        print('找不到 %s —— 先跑 build_exe.bat' % _EXE)
        return 2
    tmp = tempfile.mkdtemp(prefix='qbank-cold-')
    try:
        in_path = os.path.join(tmp, 'in.pdf')
        shutil.copy2(os.path.join(_FIX, '01-chinese-text.pdf'), in_path)

        # ---- 1/2. exe 冷启动与热启动 ----
        exe_times = []
        for i in range(3):
            out_path = os.path.join(tmp, 'out%d.json' % i)
            dt, rc = _time_cmd([_EXE, '--pdf-worker', in_path, out_path], cwd=tmp)
            ok = os.path.exists(out_path)
            exe_times.append(dt)
            print('exe 第 %d 次: %.3f s  rc=%d  写出结果=%s' % (i + 1, dt, rc, ok))
            if not ok:
                print('  !! 没写出结果文件 —— exe 里缺 pymupdf 或分发没生效')

        # ---- 3. 非 frozen 对照 ----
        api_py = os.path.join(_ROOT, 'app', 'api.py')
        out_path = os.path.join(tmp, 'out_src.json')
        dt, rc = _time_cmd([sys.executable, api_py, '--pdf-worker', in_path, out_path],
                           cwd=_ROOT)
        src_ok = os.path.exists(out_path)
        print('非 frozen (python app/api.py): %.3f s  rc=%d  写出结果=%s'
              % (dt, rc, src_ok))

        print()
        print('exe 首次 %.3f s / 最小 %.3f s / 最大 %.3f s'
              % (exe_times[0], min(exe_times), max(exe_times)))
        print('非 frozen %.3f s' % dt)
        print('解包附加开销（首次 - 非 frozen）: %+.3f s' % (exe_times[0] - dt))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
