# -*- coding: utf-8 -*-
"""生成 tests/fixtures/pdf/ 下的六类 PDF 验收样本，以及可选的 500 页压力样本。

用法:
    python make_fixtures.py                        # 只生成六类小样本（入库）
    python make_fixtures.py --stress --pages 500 --out D:/tmp/stress-500p.pdf

依赖 PyMuPDF（仅生成/测量用，**不是项目运行时依赖**）:
    python -m venv <独立 venv> && pip install PyMuPDF

--------------------------------------------------------------------------
下面每条约束都是 2026-10-20 在本机实测得出的，改代码前先读，不要凭猜测"顺手优化":
--------------------------------------------------------------------------
1. 中文字体用 MuPDF 内建 "china-s"，无需外部字体文件；
   insert_textbox -> get_text round-trip 无损（已实测逐字符一致）。
2. 「扫描件」的做法是: 先把文本渲染成位图，转灰度、加噪声，再作为图片插回
   一个全新 PDF。因此成品**没有文本层**（get_text() 返回 ''），且含 1 张图。
   这正是扫描件的定义特征——不能用"带文本层的 PDF"冒充。
3. 「加密」用 AES-256 双层口令（owner/user 不同）。
   **PyMuPDF 的 open() 对加密文档不抛异常**，而是返回 needs_pass=1 /
   is_encrypted=True 的文档对象。验收断言必须查这两个属性，不能写
   `try: open() except: 通过`——那样会永远通过。
4. 「损坏」= 从字节中部截断（保留 %PDF- 头，砍掉 xref 与 trailer）。
   open() 稳定抛 pymupdf.FileDataError。
   ⚠️ 已实测的阈值表（2026-10-20，PyMuPDF 2.0 / MuPDF 1.28.5，原始 2365 B）：
       截断  5% / 10% / 20% / 35%  -> FileDataError（稳定失败）
       截断 55%                     -> **能打开**，1 页，但 get_text() 返回 ''
                                        （静默数据丢失，比报错更危险）
       中部字节整体翻转              -> **被静默自动修复**，文本完整提取
       破坏全部 stream 对象体        -> **被静默自动修复**，文本完整提取
       xref 偏移全部清零             -> **被静默自动修复**，文本完整提取
   所以「损坏」在验收规范里必须定义为"结构性破坏且不可恢复"，不能定义为
   "随机字节损坏"——后者根本触发不了错误路径。本样本取 25% 截断留足余量；
   若将来 MuPDF 修复能力变强导致本样本能打开，verify_fixtures.py 会立刻失败，
   那是正确的告警信号，不要去调阈值糊过去。
5. insert_textbox 在空间不足时**完全不写入**并返回负值（PyMuPDF 语义），
   所以 fit_textbox() 可以用逐级降字号的方式安全重试。
6. 压力样本默认写到仓库外（--out），仓库内只提交六类小样本；
   压力样本每页是"渲染->灰度->JPEG"的独立位图页，无文本层，
   模拟的是最坏的扫描书场景（导入方必须 OCR）。
"""

import argparse
import os
import random
import sys

import pymupdf
import warnings

warnings.filterwarnings("ignore")  # fitz 弃用告警，本项目只用 pymupdf 命名空间

HERE = os.path.dirname(os.path.abspath(__file__))
A4 = (595.0, 842.0)  # pt
FONT = "china-s"

# --------------------------------------------------------------------------
# 语料：机械设计方向中文内容，保证样本对最终验收"有意义"而不是 lorem ipsum
# --------------------------------------------------------------------------
BODY_CN = (
    "螺净一号是一套面向贝类加工的超声清洗与离心切割一体化装置，整机外形尺寸约 "
    "560 mm × 560 mm × 800 mm。清洗槽有效容积 42 L，超声振板布置于槽体两侧，"
    "工作频率 28 kHz，功率 600 W，可由数显表实时监测。离心切割机构采用 "
    "304 不锈钢转鼓，额定转速 1400 r/min，减速比 1:12。\n"
    "设计计算从传动链入手：电动机额定功率 1.5 kW，同步转速 1440 r/min，"
    "经三角带轮一级减速后输入蜗杆减速器。蜗杆头数取 1，蜗轮齿数取 40，"
    "因此总减速比约 40。转鼓端部线速度校核为 11.6 m/s，满足花甲脱壳的 "
    "离心力要求，同时低于贝肉破碎的临界线速度 14 m/s。\n"
    "强度校核按第三强度理论进行。转鼓壁厚 3 mm，材料 Q235B，许用应力 "
    "157 MPa。满载工况下离心等效外压 0.42 MPa，环向应力 41.7 MPa，"
    "安全系数 3.76。轴径按扭矩法初定 30 mm，再按弯扭合成强度复核算得 "
    "25 mm，考虑键槽削弱取 28 mm。\n"
    "超声清洗的声强分布用有限元仿真校核。槽内平均声强 0.62 W/cm²，"
    "近场区深度约 90 mm。实测清洗 5 min 后泥沙残留率由 8.4% 降至 0.9%，"
    "破损率 1.2%，优于人工搓洗的 3.5%。\n"
    "经济效益测算：单台售价 2.8 万元，物料成本 1.35 万元，年产能 600 台，"
    "达产年营业收入 1.68 亿元。与人工相比，每吨花甲加工人工成本由 "
    "860 元降至 310 元，回收期约 14 个月。\n"
)

BODY_LEFT = (
    "第一章 机械设计总论\n"
    "机械设计通常分为方案设计、结构设计与工艺设计三个阶段。方案设计决定"
    "功能原理与总体布置，结构设计确定零件形状、尺寸与材料，工艺设计则"
    "解决零件的可制造性与可装配性。三阶段反复迭代，直至满足全部约束。\n"
    "设计约束分为性能约束与边界约束。性能约束包括强度、刚度、寿命、"
    "精度与效率；边界约束包括成本、体积、重量、交货期与安全法规。"
    "任何可行的设计方案都是这一多维约束空间中的一个点。\n"
    "第二章 常用机构\n"
    "平面连杆机构的行程速比系数 K 决定急回特性。K 越大，工作行程速度"
    "越平稳而空回行程越迅速，但极位夹角随之增大，传力角在工作行程的"
    "最小值的改善变得困难。工程上 K 一般取 1.1 至 1.4。\n"
    "凸轮轮廓设计采用反转法。若基圆半径过小，压力角将超过许用值，"
    "机构自锁；若基圆半径过大，则结构尺寸与惯性力同时增加。"
    "因此基圆半径的选取本质上是传力性能与结构紧凑性之间的折中。\n"
)

BODY_RIGHT = (
    "第三章 螺纹连接\n"
    "紧螺栓连接在轴向载荷作用下，螺栓所受总拉力并不等于预紧力与工作"
    "载荷之和，而是取决于螺栓与被连接件的刚度比。刚度比越小，螺栓"
    "分担的增量载荷越大，连接越容易出现疲劳失效。\n"
    "受轴向变载荷的螺栓，除按静强度计算外，还须校核疲劳强度。"
    "应力幅是控制量，减小螺栓刚度或增大被连接件刚度都能降低应力幅。"
    "工程中常用柔性螺栓配合金属垫片来实现。\n"
    "第四章 带传动\n"
    "带传动的弹性滑动与打滑是两个不同现象。弹性滑动是不可避免的"
    "物理现象，引起微小速比损失与带的磨损；打滑则是过载引起的全面"
    "滑动，属于失效模式，应当避免。\n"
    "单根 V 带的基本额定功率与带型、小带轮直径和转速有关。设计时"
    "先确定计算功率，再选带型、定带轮直径、中心距与根数，最后"
    "校核带速与初拉力。\n"
)

TABLE_ROWS = [
    ("材料", "密度 g/cm³", "弹性模量 GPa", "屈服强度 MPa", "典型用途"),
    ("Q235B", "7.85", "212", "235", "机架、转鼓、焊缝结构"),
    ("45 钢", "7.85", "209", "355", "轴类、齿轮、键"),
    ("40Cr", "7.85", "211", "785", "重载轴、蜗杆"),
    ("304 不锈钢", "7.93", "193", "205", "清洗槽、食品接触件"),
    ("HT250", "7.35", "120", "—", "轴承座、减速器箱体"),
    ("ZG270-500", "7.85", "202", "270", "铸件底座、支架"),
]


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def fit_textbox(page, rect, text, sizes=(11, 10.5, 10, 9.5, 9, 8.5, 8),
                lineheight=1.45, color=(0, 0, 0)):
    """逐级降字号直到放得下。insert_textbox 放不下时完全不写入并返回负值，
    所以这里的安全重试不会留下半截内容（PyMuPDF 语义，见模块 docstring 第 5 条）。"""
    for fs in sizes:
        rc = page.insert_textbox(rect, text, fontname=FONT, fontsize=fs,
                                 lineheight=lineheight, color=color)
        if rc >= 0:
            return fs
    raise RuntimeError("text does not fit in %s even at fontsize %s" % (rect, sizes[-1]))


def new_doc(w=A4[0], h=A4[1]):
    doc = pymupdf.open()
    return doc, doc.new_page(width=w, height=h)


def to_gray(pix):
    """转灰度并丢掉 alpha 通道。"""
    g = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    if g.alpha:
        g = pymupdf.Pixmap(g, 0)
    return g


def salt_noise(gray, seed, amp=16, step=97):
    """稀疏椒盐噪声。用切片赋值实现（C 速度），避免逐像素 Python 循环——
    500 页压力样本如果逐像素循环要跑几分钟。"""
    rng = random.Random(seed)
    data = bytearray(gray.samples)
    hits = len(data[::step])
    # 只替换、不叠加：随机落在一个中性灰度带内，视觉上等同扫描噪点
    band = bytes(rng.randint(96, 200) for _ in range(hits))
    data[::step] = band
    return pymupdf.Pixmap(pymupdf.csGRAY, gray.width, gray.height,
                          bytes(data), False)


# --------------------------------------------------------------------------
# 六类样本
# --------------------------------------------------------------------------
def make_chinese_text(path):
    """01 中文文本：单栏正文，有完整文本层，提取结果应与 BODY_CN 一致。"""
    doc, page = new_doc()
    fs = fit_textbox(page, pymupdf.Rect(48, 48, A4[0] - 48, A4[1] - 48), BODY_CN)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return {"fontsize": fs}


def make_two_column(path):
    """02 双栏：左右两栏独立排版。验收要点是读取顺序——
    朴素实现常把两栏文字按 y 坐标交错成一行一行。"""
    doc, page = new_doc()
    w, h = A4
    left = pymupdf.Rect(48, 56, 48 + (w - 96 - 28) / 2, h - 56)
    right = pymupdf.Rect(48 + (w - 96 - 28) / 2 + 28, 56, w - 48, h - 56)
    # 栏间分隔线
    page.draw_line(pymupdf.Point(w / 2, 56), pymupdf.Point(w / 2, h - 56),
                   color=(0.75, 0.75, 0.75), width=0.6)
    f1 = fit_textbox(page, left, BODY_LEFT)
    f2 = fit_textbox(page, right, BODY_RIGHT)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return {"fontsize_left": f1, "fontsize_right": f2}


def make_table(path):
    """03 表格：真实画线的表格。验收要点是行列对齐与表头识别。"""
    doc, page = new_doc()
    w, h = A4
    ncol = len(TABLE_ROWS[0])
    nrow = len(TABLE_ROWS)
    x0, x1 = 44, w - 44
    y0 = 60
    rowh = 34
    colw = (x1 - x0) / ncol
    # 表头浅灰底
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y0 + rowh),
                   color=(0.4, 0.4, 0.4), fill=(0.90, 0.90, 0.90), width=0.8)
    for r in range(nrow):
        y = y0 + r * rowh
        page.draw_line(pymupdf.Point(x0, y), pymupdf.Point(x1, y),
                       color=(0.35, 0.35, 0.35), width=0.7)
    for c in range(ncol + 1):
        x = x0 + c * colw
        page.draw_line(pymupdf.Point(x, y0), pymupdf.Point(x, y0 + nrow * rowh),
                       color=(0.35, 0.35, 0.35), width=0.7)
    for r, row in enumerate(TABLE_ROWS):
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x0 + c * colw + 4, y0 + r * rowh + 3,
                                x0 + (c + 1) * colw - 4, y0 + (r + 1) * rowh - 3)
            if r == 0:
                fit_textbox(page, rect, cell, sizes=(10, 9.5, 9, 8.5),
                            lineheight=1.25, color=(0, 0, 0))
            else:
                page.insert_textbox(rect, cell, fontname=FONT, fontsize=9.5,
                                    lineheight=1.25, align=1 if c else 0)
    # 表下注记，让页面不空
    fit_textbox(page, pymupdf.Rect(48, y0 + nrow * rowh + 20, w - 48, h - 48),
                "注：表中许用应力按常温静载取值，疲劳设计需再乘 0.8 的折减系数；"
                "不锈钢密度随牌号略有差异，表中为平均值。")
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return {"rows": nrow, "cols": ncol}


def make_scanned(path):
    """04 扫描件：位图页，无文本层。验收要点是 get_text() 必须为空，
    且实现方必须走 OCR 而不是假装能提取文本。"""
    src, spage = new_doc()
    fit_textbox(spage, pymupdf.Rect(48, 48, A4[0] - 48, A4[1] - 48), BODY_CN,
                sizes=(11, 10.5, 10, 9.5, 9))
    gray = to_gray(spage.get_pixmap(dpi=150))
    noisy = salt_noise(gray, seed=20261020)
    src.close()
    doc, page = new_doc()
    page.insert_image(pymupdf.Rect(0, 0, A4[0], A4[1]), pixmap=noisy)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return {"image_px": (noisy.width, noisy.height), "dpi": 150}


def make_encrypted(path, user_pw="user-secret", owner_pw="owner-secret"):
    """05 加密：AES-256 双层口令。验收要点见模块 docstring 第 3 条——
    断言 needs_pass / is_encrypted，不要断言 open() 抛异常。"""
    doc, page = new_doc()
    fit_textbox(page, pymupdf.Rect(48, 48, A4[0] - 48, A4[1] - 48),
                "本文件为加密样本，用于验收「打开前必须发现需要口令」这条路径。\n\n" + BODY_CN[:400])
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
             owner_pw=owner_pw, user_pw=user_pw, garbage=4, deflate=True)
    doc.close()
    return {"user_pw": user_pw, "owner_pw": owner_pw,
            "algorithm": "AES-256 (PDF_ENCRYPT_AES_256)"}


def make_corrupt(path):
    """06 损坏：中部截断，保留 %PDF 头、破坏尾部 xref。
    open() 稳定抛 pymupdf.FileDataError（见 docstring 第 4 条）。"""
    tmp = path + ".intact.tmp"
    doc, page = new_doc()
    fit_textbox(page, pymupdf.Rect(48, 48, A4[0] - 48, A4[1] - 48), BODY_CN)
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    raw = open(tmp, "rb").read()
    os.remove(tmp)
    # 25% 截断：见 docstring 第 4 条的实测阈值表。55% 会被 MuPDF 修复成
    # "能打开但文本全空"，那是静默数据丢失，不能拿它当错误路径样本。
    keep = max(64, int(len(raw) * 0.25))
    with open(path, "wb") as fh:
        fh.write(raw[:keep])
    return {"intact_bytes": len(raw), "kept_bytes": keep}


# --------------------------------------------------------------------------
# 压力样本（不入库）
# --------------------------------------------------------------------------
def make_stress(path, pages=500, dpi=120, quality=65, text_every=5):
    """500 页压力样本：每页是"渲染->灰度->JPEG"的独立位图页，无文本层，
    模拟最坏的扫描书场景。每 text_every 页插一页真实文本页做成混合内容。
    返回实际页数与实际文件大小——以实测为准，不预设 50 MB。"""
    doc = pymupdf.open()
    for i in range(pages):
        if i % text_every == 0:
            page = doc.new_page(width=A4[0], height=A4[1])
            fit_textbox(page, pymupdf.Rect(48, 48, A4[0] - 48, A4[1] - 48),
                        "第 %d 页\n" % (i + 1) + BODY_CN)
            continue
        tmp, tp = new_doc()
        fit_textbox(tp, pymupdf.Rect(48, 48, A4[0] - 48, A4[1] - 48),
                    "第 %d 页\n" % (i + 1) + BODY_CN, sizes=(11, 10.5, 10, 9.5, 9))
        gray = to_gray(tp.get_pixmap(dpi=dpi))
        noisy = salt_noise(gray, seed=1000 + i)
        tmp.close()
        page = doc.new_page(width=A4[0], height=A4[1])
        page.insert_image(pymupdf.Rect(0, 0, A4[0], A4[1]),
                          stream=noisy.tobytes("jpeg", quality))
    doc.save(path, garbage=4, deflate=True)
    n = doc.page_count
    doc.close()
    return {"pages": n, "bytes": os.path.getsize(path), "dpi": dpi, "quality": quality}


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stress", action="store_true", help="生成压力样本（不写入仓库）")
    ap.add_argument("--pages", type=int, default=500)
    ap.add_argument("--dpi", type=int, default=120)
    ap.add_argument("--quality", type=int, default=65)
    ap.add_argument("--out", default=None, help="压力样本输出路径（默认仓库外）")
    args = ap.parse_args()

    jobs = [
        ("01-chinese-text.pdf", make_chinese_text),
        ("02-two-column.pdf", make_two_column),
        ("03-table.pdf", make_table),
        ("04-scanned.pdf", make_scanned),
        ("05-encrypted.pdf", make_encrypted),
        ("06-corrupt.pdf", make_corrupt),
    ]
    for name, fn in jobs:
        info = fn(os.path.join(HERE, name))
        size = os.path.getsize(os.path.join(HERE, name))
        print("%-22s %8d B  %s" % (name, size, info))

    if args.stress:
        out = args.out or os.path.join(os.path.expanduser("~"), "pdf-stress.pdf")
        info = make_stress(out, pages=args.pages, dpi=args.dpi, quality=args.quality)
        print("stress -> %s" % out)
        print("stress info: %s" % info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
