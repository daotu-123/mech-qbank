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

## 分栏与文本抽取：**统一走 words，只有一条路径**
--------------------------------------------------------------------------
所有页一律用 `get_text("words")` 做「行聚类 → 行内切段 → 段归栏」，
判出单栏/双栏，输出「先左栏整段、再右栏整段」。

**为什么取消「blocks 粗筛 + 可疑页才升级 words」的分层**（owner 2026-10-20 决定）：
- 分层的实测收益只有 +0.9 MB / +0.04 s（500 页中文，见方案 §1.11），
  换来的是一个**确定的正确性缺陷**：内容流左右逐行交错的 PDF 上，
  blocks 会被内容流合并、粗筛漏判，栏序退化成交错。
- 少一条分支就少一类 bug。双栏恰恰是最容易出错的地方，
  同一份逻辑不该有两套路径。
- **英文 PDF 的 words 数比中文多 10–50 倍，而英文双栏远比中文常见**——
  分层在英文场景下更容易误走 blocks 路径，正好踩在缺陷上。

所以 `get_text("blocks")` **不再用于抽取**，只用于表格识别（数矢量网格线）。

## 表格（只保行不保列，见方案【D】）
--------------------------------------------------------------------------
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

# 【C】：判「真的是两栏」时，每一栏至少要占全页字符数的这个比例。
# 用字符数而不是段数——单栏文档的右对齐行内编号会造出「零星几段」，
# 段数占比刚过线就把整页误判成双栏（实测复现）。字符数才是「成片内容」。
MIN_COLUMN_CHAR_RATIO = 0.15

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


# ------------------------------------------------------------------ 文本抽取
def _text_by_words(page) -> tuple:
    """**唯一**的文本抽取路径：行聚类 → 行内切段 → 段归栏 → 先左后右、栏内自上而下。

    返回 (文本, 栏数)。

    为什么必须到词级：朴素 get_text() 在「内容流左右逐行交错」的 PDF 上会
    **完全串行**（实测右栏首行在偏移 11、左栏末行在 638）。按坐标重排才正确。

    表格也走这条路径：**视觉行就是表格行**，所以「一条记录一行」天然成立
    （03-table 实测输出「Q235B 7.85 212 235 机架、转鼓、焊缝结」一行），
    不需要为表格单开一条 blocks 分支。

    单栏判定：栏间空隙必须**跨越页宽中线**，且左右两侧段数各不少于全页 20%，
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

    # 5) 校验这真的是「两栏」，而不只是「有个跨越中线的空隙」。
    #
    # 判据用**字符数占比**，不用段数占比。实测教训：单栏文档里的
    # **右对齐行内编号**（教材常见排版）与左侧正文之间有个大空隙，
    # 而且这个空隙**跨越页宽中线**——和真正的栏间空隙特征一样。
    # 用段数占比判会被骗过去：单栏页里右段是「零星几个」（每页 1–2 段），
    # 占全页段数 28.6%，刚过 20% 的线 → 整页被误判成双栏（实测复现）。
    #
    # 栏是「成片的内容」，不是「零星几个词」。所以看字符数：
    #   真双栏 → 两边字数相当（02 样本 58%，交错样本 50%）
    #   单栏+右对齐编号 → 右侧只有一两个字符，占比 < 1%
    left_chars = sum(len(w[4]) for seg in left for w in seg)
    right_chars = sum(len(w[4]) for seg in right for w in seg)
    total_chars = left_chars + right_chars
    if not total_chars or \
            left_chars < total_chars * MIN_COLUMN_CHAR_RATIO or \
            right_chars < total_chars * MIN_COLUMN_CHAR_RATIO:
        # 不是两栏 → 按单栏输出（保留行聚类结果，不动内容）
        lines = [' '.join(w[4] for w in sorted(r['ws'], key=lambda w: w[0])) for r in rows]
        return ('\n'.join(lines) + '\n') if lines else '', 1

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

        # 唯一路径：所有页一律走 words 级（不再有 blocks 粗筛分支）。
        text, cols = _text_by_words(page)
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
