#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PDF 抽取 worker —— **独立进程入口**，本文件是唯一允许持有 MuPDF 的运行时代码。

## 为什么要独立进程（方案【F】，两条都是实测得来的，不是洁癖）
--------------------------------------------------------------------------
1. **固定开销约 117 MB/进程**。实测：2.3 KB 的单页文本 PDF，峰值 RSS 就到 139.2 MB
   （空载基线仅 21.5 MB），而页数几乎不影响它——这是 MuPDF 初始化 + CJK 字体 +
   首次分配的固定成本。不该让 Web 服务常驻背这份开销。
2. **MuPDF 是 C 库，段错误/野指针 Python 的 try/except 抓不住。**
   C 层崩溃是进程级信号，不是异常。在 Web 进程里跑，等于把整站暴露给一个
   不可信 PDF。本文件被 segfault 带走时，调用方进程安然无恙。

## 协议：**全程走临时文件，禁止 PIPE**
--------------------------------------------------------------------------
`extract(输入文件路径, 输出文件路径)`：读入 PDF 字节 → 抽取 → 把 JSON 写进输出文件
→ 退出码 0。

**为什么不用 stdout/PIPE**：踩过两次双死——
  - 写端无人读 → worker 阻塞在 write，整个管道塞满后永不退出；
  - 父进程 read() 等 EOF，而子进程又等父进程读 → 互相等死。
管道是**同步**的，而这里的负载可能写到几百 KB。文件是**异步**的：
worker 写完就退，父进程什么时候读都不冲突。见 `pdf_client.py`。

## 两种启动形态（PyInstaller `--windowed` 打包的陷阱）
--------------------------------------------------------------------------
- **源码运行**：`python app/pdf_worker.py IN OUT`
- **frozen 打包后**：`sys.executable` 就是 exe 自身，**不能再拼脚本路径**
  （拼了会变成 `mech-qbank.exe app/pdf_worker.py ...`，exe 会把脚本名当参数）。
  正确形态是 `[sys.executable, '--pdf-worker', IN, OUT]`，由 exe 的入口
  （`app/api.py`，M3.3 接）识别该分支后转调本文件的 `main()`。

所以本文件同时接受 `IN OUT` 与 `--pdf-worker IN OUT` 两种参数形态。

## ⚠️ `--windowed` 打包的三个约束（历史踩过，写在这里免得再犯）
--------------------------------------------------------------------------
1. `sys.stdout` / `sys.stderr` 在 frozen + windowed 下是 **None**，本文件**一个字都不
   print**——所有输出都写进输出文件或用退出码表达。
2. 不能用 `-m module` 方式自举（frozen 下没有模块搜索路径的概念）。
3. 不能依赖相对路径找脚本，用 `os.path.dirname(os.path.abspath(__file__))`。

## 故障注入（**仅供测试**，生产环境永不设置）
--------------------------------------------------------------------------
环境变量 `QBANK_PDF_FAULT` 可取值（见 `_maybe_fault()`）：
    segv        真实内存访问违例（C 层崩溃，try/except 抓不住的那种）
    abort       os.abort()，SIGABRT
    exit:N      以退出码 N 退出，不写任何输出
    hang        死循环卡住，用来测超时
    silent      退出码 0 但**不写输出文件**
    garbage     写一个不是 JSON 的输出文件
    raise       抛 Python 异常（这才是 try/except 能抓的那类）

真实 segfault/abort 在 Windows 上会弹「应用已停止工作」对话框，
所以测试默认用 `exit:N` / `hang` / `silent`，`segv` 单独手动验。
"""

from __future__ import annotations

import json
import os
import sys
import time

# 让「python app/pdf_worker.py」这种直接运行也能 import 到同目录的 pdf_extract
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

#: worker 协议版本。客户端会核对，不一致直接判 worker_crashed——
#: 防止「客户端已升级、exe 还是旧版」时读到看不懂的输出。
PROTOCOL_VERSION = 1

#: 输出文件最大多大。超过就当 worker 异常（正常 PDF 文本不会到这个量级）。
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


def _maybe_fault(output_path: str = ''):
    """按 QBANK_PDF_FAULT 注入故障。**只在测试里用**。

    返回值是**要交给进程的退出码**：`None` 表示「不注入，继续正常流程」。
    这里必须用返回值而不是就地 return——就地 return 只会退出本函数，
    `run()` 会继续往下跑完整抽取，测试就测不到「退出码 0 但无输出」这种情况了
    （第一次实现就踩了这个坑：silent 模式跑出了完整抽取结果）。
    """
    mode = os.environ.get('QBANK_PDF_FAULT', '')
    if not mode:
        return None
    if mode == 'segv':
        import ctypes
        ctypes.string_at(0)                      # 真实访问违例，进程直接消失
    if mode == 'abort':
        os.abort()
    if mode.startswith('exit:'):
        return int(mode[5:]) or 1
    if mode == 'hang':
        # 把自己的 pid 写进输入文件旁边，让测试能确认「超时后这个进程真的死了」。
        # 这不是为了调试，是为了**可证伪**：不验 pid 的话，「没有孤儿进程」只是断言。
        try:
            with open(output_path + '.pid', 'w', encoding='ascii') as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass
        while True:
            time.sleep(3600)
    if mode == 'raise':
        raise RuntimeError('injected fault: raise')
    # 'silent' / 'garbage' 也在这里处理：**必须在 import pymupdf 之前**。
    # 实测 import pymupdf 冷启动就要 2.0s，放到抽取之后才处理会让「退出码 0
    # 但无输出」这类用例先撞上客户端超时，测到的就成了 timeout 而不是
    # worker_crashed——测试就会验错东西。
    if mode == 'silent':
        return 0                                # 退出码 0 但什么都不写
    if mode == 'garbage':
        with open(output_path, 'w', encoding='utf-8') as fh:
            fh.write('this is not json at all')
        return 0
    # 未知值：当作无故障继续跑（便于临时手敲环境变量调试）
    return None


def _read_input(path: str) -> bytes:
    with open(path, 'rb') as fh:
        return fh.read()


def _write_output(path: str, payload: dict) -> None:
    """原子写：先写同目录临时文件再 replace，避免客户端读到半截 JSON。

    os.replace 在 Windows 上对已存在的目标是原子的（需先删或直接覆盖，
    Python 3.3+ 的 os.replace 在 Windows 用 MoveFileEx 允许覆盖）。
    """
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def run(input_path: str, output_path: str) -> int:
    """真正的抽取流程。返回退出码。

    拆成独立函数是为了让测试能直接调它（不经过 argv 解析）。
    """
    injected = _maybe_fault(output_path)
    if injected is not None:
        return injected

    # 延迟到真正要用时才 import：让「worker 存在但环境没装 pymupdf」这件事
    # 表现为一次干净的 ImportError，而不是模块加载期就炸。
    from pdf_extract import extract            # noqa: PLC0415 - 故意的延迟导入

    blob = _read_input(input_path)
    result = extract(blob)

    payload = {
        'protocol': PROTOCOL_VERSION,
        'result': dict(result),
    }
    _write_output(output_path, payload)
    return 0


def main(argv=None) -> int:
    """进程入口。接受 `IN OUT` 或 `--pdf-worker IN OUT` 两种形态。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == '--pdf-worker':
        args = args[1:]                         # frozen 分支由 exe 入口剥掉这个标记
    if len(args) != 2:
        # frozen + windowed 下 stderr 是 None，写不出去；退出码是唯一可靠的通道。
        return 2
    try:
        return run(args[0], args[1])
    except ImportError as exc:
        # 环境没装 pymupdf：客户端会看到「无输出 + 退出码 1」→ worker_crashed。
        # 这里不能 print（frozen 下无处可写），所以只能靠退出码。
        _write_error(args[1], 'import_error: %s' % exc)
        return 1
    except Exception as exc:                    # noqa: BLE001 - 兜底，绝不让异常逃出 worker
        # 抽取器本身不该抛（它内部已把文件类异常转成 status='corrupt'）。
        # 真到这里说明是 worker 自己的 bug，必须让调用方看到「崩了」而不是「解析成功」。
        _write_error(args[1], '%s: %s' % (type(exc).__name__, exc))
        return 1


def _write_error(output_path: str, message: str) -> None:
    """出错时也要写输出文件，让客户端能区分「崩了」与「解析出 corrupt」。"""
    try:
        _write_output(output_path, {
            'protocol': PROTOCOL_VERSION,
            'worker_error': message,
        })
    except Exception:
        pass


if __name__ == '__main__':
    sys.exit(main())
