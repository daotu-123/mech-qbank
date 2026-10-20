# -*- coding: utf-8 -*-
r"""
mech-qbank · PDF 抽取器测试（M3 第 1 步）

## 怎么跑
--------------------------------------------------------------------------
本测试需要 **pymupdf**。它不在项目运行时依赖里（项目其余部分必须保持
零第三方依赖），所以要跑在装了 pymupdf 的解释器上：

    "C:/Users/Administrator/.workbuddy/binaries/python/envs/pdfprobe/Scripts/python.exe" \
        tests/test_pdf_extract.py

装了 pymupdf 就能跑；没装会全部 skip（不会误报失败）。

## 覆盖
--------------------------------------------------------------------------
- 六类样本（tests/fixtures/pdf/）：01 中文 / 02 双栏 / 03 表格 / 04 扫描件 /
  05 加密 / 06 损坏 —— 逐样本断言 status 与关键字段
- 三条边界（**本文件现场造，不入库**）：空字节 / 非 PDF 字节 / 只保留 5% 的截断件
- 两条回归：页码众数偏移校正；内容流交错的双栏（记录已知代价）

## 不覆盖（实现时必须补）
--------------------------------------------------------------------------
- 真乱码样本、图文混排样本（方案附节列出的未验证项）
- 英文 PDF（中文按行切词，words 数与英文差 10–20 倍）
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'app')
FIXTURES = os.path.join(HERE, 'fixtures', 'pdf')

if APP not in sys.path:
    sys.path.insert(0, APP)

try:
    import pymupdf
    HAS_PYMUPDF = True
except ImportError:                                # pragma: no cover
    HAS_PYMUPDF = False

if HAS_PYMUPDF:
    import pdf_extract


def _blob(name):
    with open(os.path.join(FIXTURES, name), 'rb') as fh:
        return fh.read()


@unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf（跑在 pdfprobe 解释器上）')
class TestSixFixtures(unittest.TestCase):
    """六类样本的判定与字段。"""

    def test_01_chinese_text(self):
        r = pdf_extract.extract(_blob('01-chinese-text.pdf'))
        self.assertEqual(r.status, 'ok')
        self.assertEqual(r.code, '')
        self.assertEqual(r.total_chars, 653)
        self.assertEqual(r.page_count, 1)
        self.assertEqual(r.columns_detected, 1)
        self.assertEqual(r.table_pages, [])
        self.assertFalse(r.repaired)
        self.assertFalse(r.is_encrypted)
        self.assertEqual(r.image_coverage, 0.0)
        p = r.pages[0]
        self.assertEqual(p['physical_page'], 1)
        self.assertEqual(p['printed_label'], 1)
        self.assertEqual(p['chars'], 653)
        self.assertFalse(p['has_images'])
        self.assertIn('螺净一号', p['text'])

    def test_02_two_column(self):
        r = pdf_extract.extract(_blob('02-two-column.pdf'))
        self.assertEqual(r.status, 'ok')
        # 分层策略：blocks 粗筛判出多栏 → 升级 words → 栏数 2
        self.assertEqual(r.columns_detected, 2)
        self.assertEqual(r.pages[0]['columns'], 2)
        # 栏序必须是「先左栏整段、再右栏整段」，不能左右逐行交错
        text = r.pages[0]['text']
        left_at = text.find('第一章 机械设计总论')
        right_at = text.find('第二章 常用机构')
        self.assertGreaterEqual(left_at, 0)
        self.assertGreater(right_at, left_at)
        # 右栏首章必须排在左栏末章之后（不是每行交错）
        self.assertGreater(text.find('第四章 带传动'), right_at)

    def test_03_table(self):
        r = pdf_extract.extract(_blob('03-table.pdf'))
        self.assertEqual(r.status, 'ok')
        self.assertEqual(r.table_pages, [1])
        self.assertTrue(r.pages[0]['has_table'])
        # 表格必须按行可读：一条记录一行，不能被拆成一格一行
        lines = r.pages[0]['text'].splitlines()
        self.assertIn('材料 密度 g/cm 弹性模量 GPa 屈服强度 MPa 典型用途', lines)
        self.assertTrue(any(l.startswith('Q235B 7.85 212 235') for l in lines),
                        '表格记录应在一行内，实际=%r' % lines)

    def test_04_scanned(self):
        r = pdf_extract.extract(_blob('04-scanned.pdf'))
        self.assertEqual(r.status, 'no_text')
        self.assertEqual(r.code, '')
        self.assertEqual(r.total_chars, 0)
        self.assertGreater(r.image_coverage, 0.5)
        self.assertTrue(r.pages[0]['has_images'])
        self.assertFalse(r.repaired)

    def test_05_encrypted(self):
        r = pdf_extract.extract(_blob('05-encrypted.pdf'))
        self.assertEqual(r.status, 'encrypted')
        self.assertEqual(r.code, 'encrypted_document')
        self.assertTrue(r.is_encrypted)
        self.assertEqual(r.total_chars, 0)

    def test_06_corrupt(self):
        r = pdf_extract.extract(_blob('06-corrupt.pdf'))
        self.assertEqual(r.status, 'corrupt')
        self.assertEqual(r.code, 'corrupt_document')
        self.assertEqual(r.page_count, 0)


@unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf（跑在 pdfprobe 解释器上）')
class TestBoundaries(unittest.TestCase):
    """三条边界，现场造，不入库。"""

    def test_empty_bytes(self):
        r = pdf_extract.extract(b'')
        self.assertEqual(r.status, 'corrupt')
        self.assertEqual(r.code, 'corrupt_document')
        self.assertEqual(r.page_count, 0)

    def test_not_a_pdf(self):
        r = pdf_extract.extract(b'hello world, this is not a pdf at all')
        self.assertEqual(r.status, 'corrupt')
        self.assertEqual(r.code, 'corrupt_document')

    def test_truncated_to_5_percent(self):
        """只保留 5% 的截断件。

        实测这一档 MuPDF 能打开，但 **page_count == 0**（极短文件被当成
        「无 xref 的老式 PDF」扫描修复）——所以它必须落 corrupt，
        而不是 ok 或 no_text。这条是 page_count 硬信号存在的理由。
        """
        raw = _blob('01-chinese-text.pdf')
        r = pdf_extract.extract(raw[:int(len(raw) * 0.05)])
        self.assertEqual(r.status, 'corrupt')
        self.assertEqual(r.code, 'corrupt_document')


@unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf（跑在 pdfprobe 解释器上）')
class TestPageLabel(unittest.TestCase):
    """页码众数偏移校正（方案【E】）。"""

    @staticmethod
    def _make_paged(start_label, pages=3):
        doc = pymupdf.open()
        for i in range(pages):
            pg = doc.new_page(width=595, height=842)
            pg.insert_textbox(pymupdf.Rect(48, 60, 547, 300),
                              '正文第 %d 个物理页。' % (i + 1),
                              fontname='china-s', fontsize=11)
            pg.insert_textbox(pymupdf.Rect(48, 780, 547, 810),
                              '- %d -' % (start_label + i),
                              fontname='china-s', fontsize=9, align=1)
        blob = doc.tobytes()
        doc.close()
        return blob

    def test_offset_is_applied(self):
        r = pdf_extract.extract(self._make_paged(12))
        self.assertEqual(r.status, 'ok')
        self.assertEqual([p['printed_label'] for p in r.pages], [12, 13, 14])
        self.assertEqual([p['physical_page'] for p in r.pages], [1, 2, 3])

    def test_fallback_to_physical_page(self):
        """没有页码的文档必须回退到 physical_page，不能瞎编。"""
        r = pdf_extract.extract(_blob('01-chinese-text.pdf'))
        self.assertEqual(r.pages[0]['printed_label'], 1)


@unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf（跑在 pdfprobe 解释器上）')
class TestRegression(unittest.TestCase):
    """回归：内容流交错的双栏。"""

    @staticmethod
    def _make_interleaved():
        """左右栏共用同一 y、且在内容流里逐行交错写。

        这类 PDF 上**朴素 get_text() 会完全串行**（实测右栏首行在偏移 11、
        左栏末行在 638），所以它是 words 级分栏算法存在的理由。
        """
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        y = 60.0
        for i in range(30):
            page.insert_textbox(pymupdf.Rect(48, y, 290, y + 20),
                                '左栏第%02d行数据记录' % i, fontname='china-s', fontsize=9)
            page.insert_textbox(pymupdf.Rect(305, y, 547, y + 20),
                                '右栏第%02d行校核说明' % i, fontname='china-s', fontsize=9)
            y += 24.0
        blob = doc.tobytes()
        doc.close()
        return blob

    def test_interleaved_keeps_all_content(self):
        """**已知代价**：分层的 blocks 粗筛在这种 PDF 上会漏判（块被内容流合并），
        于是走 blocks 路径，栏序是交错的（最坏等于朴素行为，不会更差）。

        这里断言的是**不丢内容**——30 行左右栏文字都要在——而不是栏序正确。
        栏序正确需要取消分层（全量 words），实测额外代价仅 +0.9MB / +0.04s，
        见 docs/pdf-ingest-plan.md §1.11。
        """
        r = pdf_extract.extract(self._make_interleaved())
        self.assertEqual(r.status, 'ok')
        text = r.pages[0]['text']
        for i in range(30):
            self.assertIn('左栏第%02d行数据记录' % i, text)
            self.assertIn('右栏第%02d行校核说明' % i, text)

    def test_pure_function(self):
        """纯函数：同一输入两次调用结果一致，没有副作用。"""
        blob = _blob('02-two-column.pdf')
        a = pdf_extract.extract(blob)
        b = pdf_extract.extract(blob)
        self.assertEqual(a, b)
        self.assertIsNot(a.pages, b.pages)      # 结果是各自独立的副本


if __name__ == '__main__':
    print('提示：需要 pymupdf，请用 pdfprobe 解释器运行本文件。')
    unittest.main(verbosity=2)
