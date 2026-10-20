# -*- coding: utf-8 -*-
r"""
mech-qbank · PDF 抽取器（M3 第 1 步：纯抽取层）

## 定位
--------------------------------------------------------------------------
本模块是 **M3 的第一步**，只做一件事：把 PDF 字节变成结构化的抽取结果。

**刻意不做**（后续步骤的事，本轮一律不碰）：
- 不碰 HTTP：不 import fastapi，不认识 URL、状态码、请求体
- 不做 worker 化：不 fork、不起子进程、不用 IPC
- 不读写文件：只吃内存里的 bytes，不 open 任何路径
- 不碰数据库：不知道题库、进度、判分、排期是什么

对外只有一个纯函数：

    extract(blob: bytes) -> ExtractResult

同���字节 → 同一结果（无副作用、无缓存、无全局可变状态）。

## 依赖
--------------------------------------------------------------------------
**只依赖 pymupdf**。这是本仓库第一个引入第三方库的模块，也是唯一一个——
其余 `app/*.py` 必须保持纯标准库（`app/server.py` 是零依赖回退服务端，
一旦新层 import 第三方，这条路径就失效）。

因此本模块**只能被 worker 进程入口引用**，不能被 service / api / server 直接
import（方案 docs/pdf-ingest-plan.md 【J】）。分包方案留到 worker 化那一步定。

## 判定链（严格照 docs/pdf-ingest-plan.md §3.1 与【H】，不自创）
--------------------------------------------------------------------------
    open() 抛异常                       → corrupt  / corrupt_document
    doc.needs_pass                      → encrypted / encrypted_document
    doc.page_count == 0                 → corrupt  / corrupt_document
    doc.is_repaired 且 全文 ≥ 50 字符    → repaired_ok（repaired=True）
    doc.is_repaired 且 全文 < 50 字符     → no_text
    全文 < 50 字符                        → no_text
    其余                                → ok

两条容易漏、但必须落 no_text 的情况（不是 ok）：
- 文档完好、结构没坏，但正文就是少于 50 字符（几乎空白的一页）
- 文档完好、结构没坏，是扫描图页（图片覆盖率 > 0.5）

**唯一稳定的损坏信号是 `doc.is_repaired`**，不是任何百分比阈值：
实测同一 30% 字节损失，三种截断方式给出 430 / 549 / 0 字符三个结果，
「损失比例 → 结果」依赖截断位置与文件内部对象布局，不可复现，不能当规格。
详见 tests/fixtures/pdf/README.md 与方案 §1.4。

## 分栏策略（分层，见方案【C】）
--------------------------------------------------------------------------
先用 `get_text("blocks")` 粗筛（几何判据：有没有块明确分居页面两侧），
只有可疑页才升级到 `get_text("words")` 做「行聚类 → 行内切段 → 段归栏」。

实测依据（方案 §1.11）：500 页中文文本样本上，仅 blocks 132.8 MB / 全 words
132.7 MB / 分层 133.7 MB——**差 0.9 MB**。中文没有空格，PyMuPDF 按「行」切词，
500 页只有 5500 个 words。**所以分层的理由是「路径更短、为英文 PDF 留余量」，
不是「省内存」**（后者在本项目被实测否掉）。

## 表格（只保行不保列，见方案【D】）
--------------------------------------------------------------------------
**必须用 blocks 读**。实测 `get_text()` 会把表格拆成散列
（`'Q235B'` `'7.85'` `'212'` `'235'` 各占一行，行关系全丢），
blocks 按 y 聚合才能保住「一行一条记录」。

结构化（`find_tables`）首版不做——实测 500 页要 +100.48 s / +368 MB。
表格页只打 `table_pages` 标记，交给 UI 提示「未结构化，建议对照原表核对」。

表格的识别特征用矢量网格线：**≥2 条横线且 ≥2 条竖线**（构成网格）。
实测 03-table 是横 7 竖 6；01/04/05 是 0，02 双栏样本是 1（一条装饰竖线）——
都不会误判。
"""
from __future__ import annotations

import re

import pymupdf

# ------------------------------------------------------------------ 阈值
# 方案 §3.1 与【H】共用同一个数：全文（或单页）少于 50 字符即视为无可用文本。
MIN_TEXT_CHARS = 50

# 【H】：图片覆盖面积超过页面积这个比例，且文字很少 → 判为扫描件（no_text）。
IMAGE_COVERAGE_NO_TEXT = 0.5

# 【D】：表格识别——矢量网格线至少 2 横 2 竖才构成表格。
TABLE_MIN_HLINES = 2
TABLE_MIN_VLINES = 2

# 【C】：多栏粗筛时，一侧至少要有这么多个块才认它是一栏。
MIN_BLOCKS_PER_SIDE = 2

# 【E】：页码候选里，至少要有这么多页命中，且偏移一致率达标，才采纳 printed_label。
MIN_LABEL_PAGES = 2
MIN_LABEL_AGREEMENT = 0.8

# 【C】：栏间空隙阈值 = max(中位字高 × 系数, 最小磅值)。
# 系数必须小：实测 02 样本两栏排得很满，栏间实际空隙只有 33pt（= 2.5×字高），
# 段内相邻词的空隙是 11pt（= 0.83×字高）。阈值取 3×字高（39.6pt）会把栏间空隙
# 一起放过，导致整行没被切开、左栏的行被误归到右栏（实测丢 10 行）。
# 取 1.2×字高（≈16pt）能干净分开 11pt 与 33pt。
GAP_HEIGHT_FACTOR = 1.2
GAP_MIN_PT = 6.0

# 页脚/页眉里的页码格式。严格匹配，避免把「第 3 章」这类正文误当成页码。
_LABEL_PATTERNS = (
    re.compile(r'^\s*[—\-–]\s*(\d+)\s*[—\-–]\s*$'),
    re.compile(r'^\s*第\s*(\d+)\s*页\s*$'),
    re.compile(r'^\s*(\d+)\s*$'),
)


# 解析期可能抛的异常。**只把「文件本身的问题」归为 corrupt**——
# 编程缺陷（NameError / TypeError 之类）必须原样抛出让测试立刻失败，
# 绝不能伪装成「用户的文件损坏了」，那会把 bug 埋进线上。
_PARSE_ERRORS = (pymupdf.FileDataError, RuntimeError, ValueError,
                  OSError, MemoryError, OverflowError, ZeroDivisionError)


class ExtractResult(dict):
    """抽取结果。

    做成 dict 子类而不是 dataclass：这份结构后面要经 worker 进程回传、
    要被 json 序列化，dict 最省事（与 app/service.py 的 Result 同一思路）。
    继承 dict 后 `r['status']` 与 `r.status` 都能用。

    字段：
        status           ok / repaired_ok / no_text / corrupt / encrypted
        code             '' | corrupt_document | encrypted_document（仅错误时有）
        pages            每页一项，见 _page_record()
        total_chars      全文提取字符数
        page_count       文档页数
        repaired         doc.is_repaired
        is_encrypted     doc.is_encrypted
        image_coverage   文档级图片覆盖率（0..1），各页面积加权
        columns_detected 文档内检测到的最大栏数（1 或 2）
        table_pages      含表格的页号列表（1 起）
        error            仅失败时填，写明异常类型与消息，便于排查
    """

    @property
    def status(self) -> str:
        return str(self.get('status', ''))

    @property
    def code(self) -> str:
        return str(self.get('code', ''))

    @property
    def pages(self) -> list:
        return self.get('pages', [])

    @property
    def total_chars(self) -> int:
        return int(self.get('total_chars', 0))

    @property
    def page_count(self) -> int:
        return int(self.get('page_count', 0))

    @property
    def repaired(self) -> bool:
        return bool(self.get('repaired', False))

    @property
    def is_encrypted(self) -> bool:
        return bool(self.get('is_encrypted', False))

    @property
    def image_coverage(self) -> float:
        return float(self.get('image_coverage', 0.0))

    @property
    def columns_detected(self) -> int:
        return int(self.get('columns_detected', 1))

    @property
    def table_pages(self) -> list:
        return self.get('table_pages', [])

    @property
    def ok(self) -> bool:
        """能否拿到内容（ok / repaired_ok 都算拿到）。"""
        return self.status in ('ok', 'repaired_ok')


def _blank_result(**kw) -> ExtractResult:
    """失败或空结果时的统一骨架，保证字段集永远齐全。"""
    out = ExtractResult({
        'status': 'corrupt',
        'code': '',
        'pages': [],
        'total_chars': 0,
        'page_count': 0,
        'repaired': False,
        'is_encrypted': False,
        'image_coverage': 0.0,
        'columns_detected': 1,
        'table_pages': [],
        'error': '',
    })
    out.update(kw)
    return out


# ------------------------------------------------------------------ 几何工具
def _page_area(page) -> float:
    r = page.rect
    return max(1.0, abs(r.width * r.height))


def _image_coverage(page) -> float:
    """该页图片覆盖面积 / 页面积。

    多个图片可能重叠，所以对面积求和后再限幅到 1.0。
    """
    area = _page_area(page)
    covered = 0.0
    for info in page.get_image_info():
        x0, y0, x1, y1 = info['bbox']
        covered += abs((x1 - x0) * (y1 - y0))
    return min(1.0, covered / area)


def _grid_lines(page) -> tuple:
    """统计矢量直线里的横线数与竖线数（用于识别表格网格）。"""
    h = v = 0
    for dr in page.get_drawings():
        for item in dr.get('items', []):
            if item[0] != 'l':
                continue
            p1, p2 = item[1], item[2]
            if abs(p1.y - p2.y) < 1.0:
                h += 1
            elif abs(p1.x - p2.x) < 1.0:
                v += 1
    return h, v


def _looks_multicolumn(blocks, width) -> bool:
    """粗筛：这一页有没有可能分栏。

    判据是几何而非数值：必须有 >=2 个块**整体**在页面中线左侧，
    且 >=2 个块**整体**在右侧。块被内容流合并的 PDF 会判不出（漏判），
    此时退回单栏输出——最坏等于现状，不会更差。

    实测：01 单栏 False、02 双栏 True、03 表格 False（左半块 0）、
    04 扫描件 False（无块）、05 加密 False。
    """
    tb = [b for b in blocks if b[6] == 0 and b[4].strip()]
    if len(tb) < MIN_BLOCKS_PER_SIDE * 2:
        return False
    mid = width * 0.5
    left = [b for b in tb if b[2] < mid]
    right = [b for b in tb if b[0] > mid]
    return len(left) >= MIN_BLOCKS_PER_SIDE and len(right) >= MIN_BLOCKS_PER_SIDE


# ------------------------------------------------------------------ 文本抽取
def _text_by_blocks(page, blocks) -> str:
    """blocks 路径：按 y 聚成行，块内换行折成空格。

    块内换行必须折掉——表格里一个块是「一行记录」，块内 \\n 是「同一个单元格里
    的换行」，不折的话一行记录会被拆成几行，行关系又丢了（这正是 get_text()
    的毛病）。折成空格后 03-table 的输出是：
        材料 密度 g/cm 弹性模量 GPa 屈服强度 MPa 典型用途
        Q235B 7.85 212 235 机架、转鼓、焊缝结

    每行后面都带 \\n（含最后一行）：这样 chars 口径与 PyMuPDF 的 get_text() 一致
    （01 样本 653 字符，两者相等），下游拼接也不用再补分隔符。
    """
    ordered = sorted(blocks, key=lambda b: (round(b[1], 1), b[0]))
    lines = [b[4].strip().replace('\n', ' ') for b in ordered if b[4].strip()]
    if not lines:
        return ''
    return '\n'.join(lines) + '\n'


def _text_by_words(page) -> tuple:
    """words 路径：行聚类 → 行内切段 → 段归栏 → 先左后右、栏内自上而下。

    返回 (文本, 栏数)。

    必要性：朴素 get_text() 在「内容流左右逐行交错」的 PDF 上会**完全串行**
    （实测右栏首行在偏移 11、左栏末行在 638）。words 级按坐标重排才正确。

    单栏判定：gutter 必须落在页宽 30%–70%，且左右两侧段数各不少于全页 20%，
    否则按单栏输出（避免把正常单栏的段间距误当成栏间空隙）。
    """
    words = page.get_text('words')
    if not words:
        return '', 1

    heights = sorted(w[3] - w[1] for w in words)
    med_h = heights[len(heights) // 2] or 10.0
    tol = max(2.0, med_h * 0.5)

    # 1) 行聚类（按 y 中心）
    rows = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc = (w[1] + w[3]) / 2
        if rows and abs(yc - rows[-1]['yc']) <= tol:
            rows[-1]['ws'].append(w)
            rows[-1]['yc'] = sum((x[1] + x[3]) / 2 for x in rows[-1]['ws']) / len(rows[-1]['ws'])
        else:
            rows.append({'yc': yc, 'ws': [w]})

    # 2) 行内切段
    thr = max(med_h * GAP_HEIGHT_FACTOR, GAP_MIN_PT)
    for r in rows:
        ws = sorted(r['ws'], key=lambda w: w[0])
        segs, cur = [], [ws[0]]
        for i in range(len(ws) - 1):
            if ws[i + 1][0] - ws[i][2] > thr:
                segs.append(cur)
                cur = [ws[i + 1]]
            else:
                cur.append(ws[i + 1])
        segs.append(cur)
        r['segs'] = segs

    # 3) 定栏间边界。
    #
    # 不能用「取最宽的那个空隙」——栏内某一行恰好很短（末行只有两个字）时，
    # 它与右栏之间的空隙最宽，但空隙中点会被拉到偏左的位置（实测 02 样本：
    # 最宽空隙 241.5pt，中点 190，而真正的栏间中点在 295），用它当边界会整页错乱。
    #
    # 稳定特征是：**栏间空隙必然跨越页宽中线**，段内空隙不会（同一栏的两个词
    # 都在中线同一侧）。所以只取「跨越中线」的相邻段对，用它们中点的中位数定位。
    width = page.rect.width
    mid = width * 0.5
    crosses = []
    for r in rows:
        for i in range(len(r['segs']) - 1):
            a, b = r['segs'][i], r['segs'][i + 1]
            a_x1 = max(w[2] for w in a)
            b_x0 = min(w[0] for w in b)
            if a_x1 < mid < b_x0:
                crosses.append((a_x1 + b_x0) / 2.0)

    gutter = None
    if crosses:
        crosses.sort()
        gx = crosses[len(crosses) // 2] if len(crosses) % 2 else \
            (crosses[len(crosses) // 2 - 1] + crosses[len(crosses) // 2]) / 2.0
        if width * 0.2 <= gx <= width * 0.8:
            counts = {0: 0, 1: 0}
            for r in rows:
                for s in r['segs']:
                    cx = (min(w[0] for w in s) + max(w[2] for w in s)) / 2
                    counts[0 if cx < gx else 1] += 1
            total = counts[0] + counts[1]
            if total and counts[0] >= total * 0.2 and counts[1] >= total * 0.2:
                gutter = gx

    if gutter is None:
        lines = [' '.join(w[4] for w in sorted(r['ws'], key=lambda w: w[0])) for r in rows]
        return ('\n'.join(lines) + '\n') if lines else '', 1

    # 4) 段归栏，先左后右，栏内自上而下
    left, right = [], []
    for r in rows:
        for s in r['segs']:
            cx = (min(w[0] for w in s) + max(w[2] for w in s)) / 2
            (left if cx < gutter else right).append(s)

    def render(segs):
        return '\n'.join(' '.join(w[4] for w in sorted(s, key=lambda w: w[0])) for s in segs)

    text = render(left) + ('\n' + render(right) if right else '')
    return (text + '\n') if text else '', 2


# ------------------------------------------------------------------ 页码
def _label_candidate(lines) -> int:
    """从页面的首行/末行里找页码候选。找不到返回 0。"""
    for cand in (lines[-1], lines[0]) if lines else ():
        cand = cand.strip()
        for pat in _LABEL_PATTERNS:
            m = pat.match(cand)
            if m:
                return int(m.group(1))
    return 0


def _resolve_labels(pages) -> None:
    """用众数偏移校正 printed_label，就地改写 pages。

    【E】实测：正文从 12 开始编页码时，physical 1/2/3 → printed 12/13/14，
    偏移恒为 11。直接拿 physical 当 printed 会差 11 页。

    采纳条件：命中页数 >= 2，且偏移一致率 >= 80%。取不到就回退 physical_page。
    """
    hits = []
    for p in pages:
        lines = [ln for ln in p['text'].splitlines() if ln.strip()]
        lab = _label_candidate(lines)
        if lab:
            hits.append((p['physical_page'], lab))

    if len(hits) < MIN_LABEL_PAGES:
        for p in pages:
            p['printed_label'] = p['physical_page']
        return

    offsets = {}
    for phys, lab in hits:
        offsets.setdefault(lab - phys, []).append(phys)
    mode_off, members = max(offsets.items(), key=lambda kv: (len(kv[1]), -abs(kv[0])))
    agreement = len(members) / float(len(hits))
    if agreement < MIN_LABEL_AGREEMENT:
        for p in pages:
            p['printed_label'] = p['physical_page']
        return

    label_of = dict(hits)
    for p in pages:
        p['printed_label'] = label_of.get(p['physical_page'], p['physical_page'])


# ------------------------------------------------------------------ 主流程
def extract(blob: bytes) -> ExtractResult:
    """把 PDF 字节抽成结构化结果。

    参数
        blob: PDF 文件的原始字节。空字节 / 非 PDF 字节都会被安全地判成 corrupt。

    返回
        ExtractResult（dict 子类）。**永远不抛异常**——解析失败也要返回
        status='corrupt'，让调用方按统一错误码处理（方案【G】：
        「解析异常 → 新码 corrupt_document，进程不崩」）。
    """
    if not blob:
        return _blank_result(status='corrupt', code='corrupt_document',
                             error='empty input: 0 bytes')

    try:
        doc = pymupdf.open(stream=blob, filetype='pdf')
    except Exception as exc:                       # noqa: BLE001 - 任何打开失败都算损坏
        return _blank_result(status='corrupt', code='corrupt_document',
                             error='%s: %s' % (type(exc).__name__, exc))

    try:
        return _extract_open(doc)
    except _PARSE_ERRORS as exc:                   # 文件本身的问题 → 统一按损坏处理
        return _blank_result(status='corrupt', code='corrupt_document',
                             page_count=doc.page_count if _alive(doc) else 0,
                             error='%s: %s' % (type(exc).__name__, exc))
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _alive(doc) -> bool:
    try:
        return doc.page_count >= 0
    except Exception:
        return False


def _extract_open(doc) -> ExtractResult:
    """文档已成功打开后的全部判定。"""
    # --- 判定 2：加密（needs_pass）必须在任何内容读取之前判定 ---
    if doc.needs_pass:
        return _blank_result(status='encrypted', code='encrypted_document',
                             page_count=doc.page_count,
                             is_encrypted=True,
                             error='document requires a password')

    # --- 判定 3：0 页的文档直接判损坏（实测「保留 5% 截断」得到 0 页）---
    if doc.page_count == 0:
        return _blank_result(status='corrupt', code='corrupt_document',
                             repaired=bool(getattr(doc, 'is_repaired', False)),
                             is_encrypted=bool(doc.is_encrypted),
                             error='document has 0 pages')

    repaired = bool(getattr(doc, 'is_repaired', False))

    # --- 逐页抽取 ---
    pages = []
    total_chars = 0
    cover_num = 0.0
    cover_den = 0.0
    columns_detected = 1
    table_pages = []

    for index in range(doc.page_count):
        page = doc[index]
        blocks = [b for b in page.get_text('blocks') if b[6] == 0 and b[4].strip()]

        if _looks_multicolumn(blocks, page.rect.width):
            text, cols = _text_by_words(page)
        else:
            text, cols = _text_by_blocks(page, blocks), 1
        if cols > columns_detected:
            columns_detected = cols

        chars = len(text)
        total_chars += chars

        cov = _image_coverage(page)
        area = _page_area(page)
        cover_num += cov * area
        cover_den += area

        hlines, vlines = _grid_lines(page)
        has_table = hlines >= TABLE_MIN_HLINES and vlines >= TABLE_MIN_VLINES

        pages.append({
            'physical_page': index + 1,
            'printed_label': index + 1,      # 稍后由 _resolve_labels 校正
            'text': text,
            'chars': chars,
            'has_images': bool(page.get_images()),
            'image_coverage': round(cov, 4),
            'columns': cols,
            'has_table': has_table,
            'status': 'no_text' if chars < MIN_TEXT_CHARS else 'ok',
        })
        if has_table:
            table_pages.append(index + 1)

    _resolve_labels(pages)
    image_coverage = (cover_num / cover_den) if cover_den else 0.0

    # --- 判定 4~7：文档级状态 ---
    if repaired:
        status = 'repaired_ok' if total_chars >= MIN_TEXT_CHARS else 'no_text'
    elif total_chars < MIN_TEXT_CHARS:
        # 完好但几乎没文字：扫描图页（图片覆盖率 > 0.5）与纯空白页都落 no_text，
        # 不能判 ok——否则用户会拿到一个「成功但什么也没有」的结果。
        status = 'no_text'
    else:
        status = 'ok'

    return ExtractResult({
        'status': status,
        'code': '',
        'pages': pages,
        'total_chars': total_chars,
        'page_count': doc.page_count,
        'repaired': repaired,
        'is_encrypted': bool(doc.is_encrypted),
        'image_coverage': round(image_coverage, 4),
        'columns_detected': columns_detected,
        'table_pages': table_pages,
        'error': '',
    })
