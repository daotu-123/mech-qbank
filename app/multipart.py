#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""multipart/form-data 解析器（纯标准库）。

## 为什么自己写
--------------------------------------------------------------------------
FastAPI 的 `UploadFile` / `File()` 依赖第三方包 `python-multipart`，
而本机 `.venv-build` 里**没有装**它，且本项目明确不引入新依赖
（`app/` 除 `pdf_extract.py` 外只允许标准库）。

实测确认：`.venv-build/Scripts/python.exe -c "import multipart"` →
`ModuleNotFoundError: No module named 'multipart'`。
所以 FastAPI 侧不能用 `File()`，只能自己读 `await request.body()`
然后手工解析；标准库侧（`server.py`）同样手工解析。

**两端共用本文件**——这是「两套服务端行为等价」的基础：
同一个上传请求在 FastAPI 侧和标准库侧必须解析出完全一样的
文件名与字节，否则同一个 PDF 在两个服务端会得到不同的 sha256。

## 只支持本项目需要的那部分
--------------------------------------------------------------------------
- 只取第一个带 `filename` 的 part 作为文件（其余 part 忽略）。
- 只解析 `Content-Disposition` 里的 `name` 与 `filename`。
- 不支持 `Content-Transfer-Encoding`（base64 等）——浏览器上传 PDF 不会用。
- 不支持流式/分块上传——上传体积受 `limit_body` 的 10 MB 上限约束，
  整块读进内存是可接受的。

## 安全性
--------------------------------------------------------------------------
`filename` **只作元数据**，绝不用于拼路径。落盘用的键由调用方随机生成
（见 `app/docstore.py`），所以「../../etc/passwd」这类文件名无害。
本模块会把 filename 里的路径分隔符去掉，但那是**双保险**，
不是主要防线。
"""

from __future__ import annotations

import re

#: 单个 part 的头部最大字节数。防止畸形请求里没有分隔符导致
#: 把整个 body 当头部扫描（那是 O(n²) 的机会）。
MAX_HEADER_BYTES = 16 * 1024

#: 支持的最大 part 数量。超出就停——防畸形请求构造出海量 part 拖慢解析。
MAX_PARTS = 64

_BOUNDARY_RE = re.compile(r'boundary\s*=\s*"?([^";,]+)"?', re.I)
_DISPOSITION_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]*)"', re.I)
_DISPOSITION_FILE_RE = re.compile(r'\bfilename\*?\s*=\s*"([^"]*)"', re.I)


class MultipartError(ValueError):
    """请求体不是合法的 multipart/form-data。"""


class Part(object):
    """一个 part。

    做成普通类而不是 namedtuple：要在测试里比对，且要能被 json 化之前
    先转成 dict。
    """

    __slots__ = ('name', 'filename', 'content_type', 'data')

    def __init__(self, name='', filename='', content_type='', data=b''):
        self.name = name
        self.filename = filename
        self.content_type = content_type
        self.data = data

    def __repr__(self):
        return 'Part(name=%r, filename=%r, content_type=%r, bytes=%d)' % (
            self.name, self.filename, self.content_type, len(self.data))

    def as_dict(self):
        return {'name': self.name, 'filename': self.filename,
                'content_type': self.content_type, 'bytes': len(self.data)}


def boundary_of(content_type: str) -> bytes:
    """从 Content-Type 头里取出 boundary。取不到就抛 MultipartError。"""
    if not content_type:
        raise MultipartError('missing Content-Type')
    m = _BOUNDARY_RE.search(content_type)
    if not m:
        raise MultipartError('no boundary in Content-Type')
    return m.group(1).strip().encode('latin-1', 'replace')


def _safe_filename(name: str) -> str:
    """把 filename 里的路径成分去掉。

    **只是双保险**：真正的防线是落盘时用随机键而不是原始文件名。
    这里只保留最后一段，并去掉控制字符。
    """
    if not name:
        return ''
    name = name.replace('\\', '/')
    name = name.rsplit('/', 1)[-1]
    name = ''.join(ch for ch in name if ch >= ' ' and ch != '\x7f')
    return name[:255]


def _parse_headers(blob: bytes) -> dict:
    """解析 part 头部。返回小写键的字典。"""
    headers = {}
    for line in blob.split(b'\r\n'):
        if not line:
            continue
        if b':' not in line:
            continue
        k, _, v = line.partition(b':')
        try:
            headers[k.strip().lower().decode('latin-1')] = v.strip().decode('latin-1')
        except Exception:
            continue
    return headers


def parse_multipart(body: bytes, content_type: str) -> list:
    """解析 multipart/form-data，返回 Part 列表。

    参数
        body          完整的请求体字节
        content_type  请求的 Content-Type 头

    异常
        MultipartError  格式不合法（调用方转成 400）
    """
    if not body:
        raise MultipartError('empty body')
    boundary = boundary_of(content_type)
    delim = b'--' + boundary

    # 按分隔符切。第一个元素是 preamble（丢弃），最后一个元素以 '--' 结尾表示结束。
    segments = body.split(delim)
    if len(segments) < 2:
        raise MultipartError('no multipart segments found')

    parts = []
    for seg in segments[1:]:
        if seg[:2] == b'--':
            break                                   # 结束标记
        # 去掉分隔符后的 CRLF，以及段末的 CRLF
        seg = seg[2:] if seg[:2] == b'\r\n' else seg.lstrip(b'\r\n')
        if seg.endswith(b'\r\n'):
            seg = seg[:-2]
        if not seg:
            continue

        sep = seg.find(b'\r\n\r\n')
        if sep < 0:
            continue                               # 没有头体分隔，跳过这个 part
        head = seg[:sep]
        if len(head) > MAX_HEADER_BYTES:
            raise MultipartError('part header too large')
        data = seg[sep + 4:]

        headers = _parse_headers(head)
        disp = headers.get('content-disposition', '')
        nm = _DISPOSITION_NAME_RE.search(disp)
        fn = _DISPOSITION_FILE_RE.search(disp)
        parts.append(Part(
            name=nm.group(1) if nm else '',
            filename=_safe_filename(fn.group(1)) if fn else '',
            content_type=headers.get('content-type', ''),
            data=data,
        ))
        if len(parts) >= MAX_PARTS:
            break
    return parts


def first_file(parts: list):
    """取第一个带 filename 的 part。找不到返回 None。

    「带 filename」才算是文件上传——普通表单字段（name 存在但
    filename 不存在）不算。
    """
    for p in parts:
        if p.filename:
            return p
    return None
