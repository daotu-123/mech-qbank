# -*- coding: utf-8 -*-
"""验收 tests/fixtures/pdf/ 下的六类样本：逐个证明它真的具备应有的定义特征。

这不是"跑通就算了"的冒烟，而是一条条断言样本的可区分性。任何一类失去特征
（比如扫描件突然有了文本层、加密件不再需要口令），这条脚本必须失败。

用法:
    python verify_fixtures.py            # 全部通过则退出码 0
    python verify_fixtures.py --verbose  # 额外打印每类的实测细节

依赖 PyMuPDF（仅验收用，不是项目运行时依赖）。
"""

import argparse
import hashlib
import os
import sys

import pymupdf
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))

FAILS = []
CHECKS = [0]


def check(cond, label, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILS.append(label)
    return bool(cond)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def p(name):
    return os.path.join(HERE, name)


# --------------------------------------------------------------------------
def verify_chinese_text(verbose):
    path = p("01-chinese-text.pdf")
    doc = pymupdf.open(path)
    check(doc.page_count == 1, "01 单页", "实际 %d" % doc.page_count)
    text = doc[0].get_text()
    doc.close()
    # round-trip：语料里的关键句必须完整回来
    probe = "清洗槽有效容积 42 L，超声振板布置于槽体两侧"
    check(probe in text, "01 文本层可提取且与语料一致", repr(text[:60]))
    check(len(text) > 300, "01 文本量足够（%d 字符）" % len(text))
    if verbose:
        print("       提取首行: %r" % text.strip().splitlines()[0][:50])


def verify_two_column(verbose):
    path = p("02-two-column.pdf")
    doc = pymupdf.open(path)
    page = doc[0]
    text = page.get_text()
    mid = page.rect.width / 2
    pw = page.rect.width
    left = right = 0
    for block in page.get_text("blocks"):
        x0 = block[0]
        if x0 < mid - 10:
            left += 1
        elif x0 > mid + 10:
            right += 1
    doc.close()
    check("平面连杆机构的行程速比系数" in text, "02 左栏语料在文本层中")
    check("紧螺栓连接在轴向载荷作用下" in text, "02 右栏语料在文本层中")
    check(left > 0 and right > 0, "02 两栏在几何上确实分居页面两侧",
          "左块 %d / 右块 %d" % (left, right))
    if verbose:
        print("       左栏块 %d，右栏块 %d，页宽 %.0f" % (left, right, pw))


def verify_table(verbose):
    path = p("03-table.pdf")
    doc = pymupdf.open(path)
    text = doc[0].get_text()
    # 表格线是矢量绘制，数一下画线宽度
    drawings = doc[0].get_drawings()
    lines = sum(1 for d in drawings for it in d["items"] if it[0] == "l")
    doc.close()
    for cell in ("弹性模量 GPa", "屈服强度 MPa", "HT250", "ZG270-500", "304 不锈钢"):
        check(cell in text, "03 表内容 %r 可提取" % cell)
    check(lines >= 12, "03 表格线为矢量绘制（线段 %d 条）" % lines, "实际 %d" % lines)
    if verbose:
        print("       矢量线段 %d 条，文本 %d 字符" % (lines, len(text)))


def verify_scanned(verbose):
    path = p("04-scanned.pdf")
    doc = pymupdf.open(path)
    page = doc[0]
    text = page.get_text()
    images = page.get_images(full=True)
    # 位图本身的分辨率
    img = page.get_image_info()
    doc.close()
    check(text.strip() == "", "04 无文本层（get_text 为空）", repr(text[:40]))
    check(len(images) == 1, "04 恰好 1 张位图", "实际 %d" % len(images))
    if img:
        w, h = img[0]["width"], img[0]["height"]
        check(w >= 1000 and h >= 1400, "04 位图分辨率达 150dpi 级别",
              "%dx%d" % (w, h))
        if verbose:
            print("       位图 %dx%d px，文件 %d B" % (w, h, os.path.getsize(path)))


def verify_encrypted(verbose):
    path = p("05-encrypted.pdf")
    # 关键行为（实测 2026-10-20）：open() 对加密文档不抛异常，
    # 而是返回 needs_pass=1 / is_encrypted=True 的对象。
    doc = pymupdf.open(path)
    check(doc.needs_pass == 1, "05 open() 不抛错但 needs_pass=1")
    check(doc.is_encrypted is True, "05 is_encrypted=True")
    ok_user = doc.authenticate("user-secret")
    check(bool(ok_user), "05 正确用户口令可认证")
    text = doc[0].get_text() if ok_user else ""
    check("加密样本" in text, "05 认证后可提取文本")
    doc.close()

    doc2 = pymupdf.open(path)
    bad = doc2.authenticate("wrong-password")
    check(not bad, "05 错误口令认证失败", "返回 %r" % bad)
    doc2.close()

    doc3 = pymupdf.open(path)
    ok_owner = doc3.authenticate("owner-secret")
    check(bool(ok_owner), "05 owner 口令也可认证（双层口令语义）")
    doc3.close()

    if verbose:
        print("       用户口令与 owner 口令均可用；错误口令被拒")


def verify_corrupt(verbose):
    path = p("06-corrupt.pdf")
    raw = open(path, "rb").read()
    check(raw[:5] == b"%PDF-", "06 仍以 %PDF- 开头（头部完好）")
    raised = None
    try:
        doc = pymupdf.open(path)
        doc.close()
    except Exception as exc:  # pymupdf.FileDataError
        raised = exc
    check(raised is not None, "06 open() 抛异常（截断触发结构性错误）",
          "未抛异常" if raised is None else "")
    if raised is not None:
        check(type(raised).__name__ == "FileDataError",
              "06 异常类型为 FileDataError", type(raised).__name__)
        if verbose:
            print("       异常: %s: %s" % (type(raised).__name__, raised))


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("== 样本清单 ==")
    for name in sorted(os.listdir(HERE)):
        if name.lower().endswith(".pdf"):
            print("  %-22s %8d B  sha256:%s" % (name, os.path.getsize(p(name)), sha256(p(name))))

    print("== 逐类验收 ==")
    for fn in (verify_chinese_text, verify_two_column, verify_table,
               verify_scanned, verify_encrypted, verify_corrupt):
        print("[%s]" % fn.__name__.replace("verify_", ""))
        fn(args.verbose)

    print("== 结果 ==")
    print("通过 %d / 失败 %d" % (CHECKS[0] - len(FAILS), len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  失败项: %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
