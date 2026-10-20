# -*- coding: utf-8 -*-
"""按目标样本实测 PyMuPDF 的峰值 RSS，而不是照抄任何预设数字。

方法论（为什么这么做，改之前先读）:
1. **一个文件一个全新子进程**。峰值 RSS 是进程生命周期内的最大值，
   如果在同一进程里连测六个文件，第一个文件的峰值会被后续文件顶掉、
   后来的文件又继承了前面的高地。分子进程测，PeakWorkingSetSize 才归得住。
2. 用 Windows `GetProcessMemoryInfo` 的 `PeakWorkingSetSize`（真正的峰值工作集字节数）。
   ⚠️ 曾打算用 `resource.getrusage().ru_maxrss` 做交叉校验，**但 Windows 版
   Python 没有 resource 模块**（实测 ModuleNotFoundError），所以该字段恒为 null，
   不是漏测。Windows 上只有 PeakWorkingSetSize 这一个可信来源。
3. **额外测一个空载基线**（只 import pymupdf、不碰任何 PDF），
   报告时给出绝对值与"扣除基线后的增量"，两者都要看。
4. 工作负载定义为一次"导入"该做的事：打开 -> 取页数 -> 逐页提取文本层
   -> 逐页统计位图 -> 渲染第 1 页到 150 dpi（OCR/缩略图的前置步）。
   只测打开不算测，那会严重低估。

用法:
    python measure_rss.py                      # 测本目录六类样本
    python measure_rss.py --file X.pdf         # 测单个文件
    python measure_rss.py --file X.pdf --password user-secret
    python measure_rss.py --file stress.pdf --no-render

依赖 PyMuPDF（仅测量用，不是项目运行时依赖）。
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
MARKER = "@@JSON@@"

# 空载基线：只 import pymupdf，不打开任何文件
BASELINE_SRC = "import pymupdf, warnings; warnings.filterwarnings('ignore'); print('ok')"

WORKLOAD_DOC = """\
工作负载: open -> page_count -> 逐页 get_text() -> 逐页 get_image_info()
          -> 渲染第 1 页 @150dpi
"""


def peak_working_set_bytes():
    """Windows 峰值工作集（字节）。失败时返回 None，由 ru_maxrss 兜底。"""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                          ctypes.byref(pmc), pmc.cb):
            return None
        return int(pmc.PeakWorkingSetSize)
    except Exception:
        return None


def ru_maxrss_bytes():
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Windows 上 ru_maxrss 单位是字节；Linux 是 KB。按平台折算成字节。
        return int(v if os.name == "nt" else v * 1024)
    except Exception:
        return None


def child_main(path, password, render):
    """在子进程里跑单文件工作负载，结束时打印一行 JSON。
    打开/解析失败也要报数——失败路径的内存占用同样是要测的。"""
    import warnings
    warnings.filterwarnings("ignore")
    import pymupdf

    info = {"path": path, "pages": 0, "text_chars": 0, "images": 0,
            "error": None, "error_type": None}
    try:
        doc = pymupdf.open(path)
        if doc.needs_pass:
            if not password:
                raise RuntimeError("FILE_NEEDS_PASSWORD")
            if not doc.authenticate(password):
                raise RuntimeError("FILE_BAD_PASSWORD")
        info["pages"] = doc.page_count
        for page in doc:
            info["text_chars"] += len(page.get_text())
            info["images"] += len(page.get_image_info())
        if render and doc.page_count:
            doc[0].get_pixmap(dpi=150)
        info["rendered_first_page"] = bool(render)
        doc.close()
    except Exception as exc:
        info["error"] = str(exc)[:120]
        info["error_type"] = type(exc).__name__

    info["peak_ws_bytes"] = peak_working_set_bytes()
    info["ru_maxrss_bytes"] = ru_maxrss_bytes()
    print(MARKER + json.dumps(info, ensure_ascii=False))
    return 0


def run_child(path, password=None, render=True):
    cmd = [PY, os.path.abspath(__file__), "--one", path]
    if password:
        cmd += ["--password", password]
    if not render:
        cmd += ["--no-render"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    for line in p.stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    raise RuntimeError("子进程未返回结果。stderr=%s" % p.stderr[-500:])


def human(n):
    if n is None:
        return "n/a"
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="测单个 PDF")
    ap.add_argument("--password", default=None)
    ap.add_argument("--no-render", action="store_true", help="跳过渲染首页")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    if args.file:
        res = run_child(args.file, args.password, not args.no_render)
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print("%s\n  pages=%s text=%s images=%s peak_ws=%s ru_maxrss=%s"
                  % (res["path"], res["pages"], res["text_chars"],
                     res["images"], human(res["peak_ws_bytes"]),
                     human(res["ru_maxrss_bytes"])))
        return 0

    # ---- 批量：本目录六类样本 ----
    order = ["01-chinese-text.pdf", "02-two-column.pdf", "03-table.pdf",
             "04-scanned.pdf", "05-encrypted.pdf", "06-corrupt.pdf"]
    render = not args.no_render

    base = subprocess.run([PY, "-c", BASELINE_SRC], capture_output=True, text=True)
    if base.returncode != 0:
        raise RuntimeError("基线子进程失败: %s" % base.stderr[-300:])
    base_peak = run_child_baseline()

    rows = []
    for name in order:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            continue
        pw = "user-secret" if name.startswith("05-") else None
        try:
            res = run_child(path, pw, render)
        except Exception as exc:
            res = {"path": path, "error": str(exc)[:120]}
        rows.append(res)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0

    print(WORKLOAD_DOC)
    print("空载基线（仅 import pymupdf）: %s" % human(base_peak))
    print("")
    print("%-22s %8s %7s %7s %12s %12s" %
          ("样本", "页数", "文本字", "位图数", "峰值WS", "扣除基线"))
    print("-" * 76)
    for r in rows:
        if r.get("error"):
            print("%-22s   ERROR %s: %s" % (os.path.basename(r["path"]),
                                            r.get("error_type"), r["error"]))
            continue
        name = os.path.basename(r["path"])
        delta = (r["peak_ws_bytes"] - base_peak) if r["peak_ws_bytes"] else None
        print("%-22s %8d %7d %7d %12s %12s" %
              (name, r["pages"], r["text_chars"], r["images"],
               human(r["peak_ws_bytes"]), human(delta)))
    return 0


def run_child_baseline():
    """基线也要在子进程里测，否则拿到的是父进程（已含本脚本）的峰值。"""
    src = (
        "import json,sys;sys.path.insert(0,%r);"
        "import measure_rss as m;"
        "print(%r+json.dumps({'peak_ws_bytes':m.peak_working_set_bytes(),"
        "'ru_maxrss_bytes':m.ru_maxrss_bytes()}))" % (HERE, MARKER)
    )
    p = subprocess.run([PY, "-c", src], capture_output=True, text=True)
    for line in p.stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])["peak_ws_bytes"]
    return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--one":
        ap = argparse.ArgumentParser()
        ap.add_argument("--one", required=True)
        ap.add_argument("--password", default=None)
        ap.add_argument("--no-render", action="store_true")
        a = ap.parse_args()
        sys.exit(child_main(a.one, a.password, not a.no_render))
    sys.exit(main())
