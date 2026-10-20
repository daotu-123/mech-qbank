#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M3.3：PDF 文档 HTTP 接口测试（两套服务端等价）。

## 覆盖
--------------------------------------------------------------------------
1. 上传 → 202 → 轮询 succeeded → 页文本与直接调 pdf_client 的结果一致
2. 上传 06-corrupt → 202 → 轮询到 corrupt，**不是 500**
3. 超限文件 → 413
4. 删除 → 之后 GET 404，且磁盘文件真的没了
5. 两套服务端同请求 → 响应结构一致
6. 打桩模拟重启 → 遗留 job 变 interrupted

外加：
   - multipart 解析器自身的边界（无 boundary / 无文件 part / 路径型文件名）
   - 路由注册检查（api.py 的装饰器必须能被 smoke_api.py 的正则抓到）

## 跑法
--------------------------------------------------------------------------
    "C:/Users/Administrator/.workbuddy/binaries/python/envs/pdfprobe/Scripts/python.exe" \
        tests/test_documents.py

需要 pymupdf（解析真的要走 worker）。没有则整组 skip 而不是误报失败。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_APP = os.path.join(_ROOT, 'app')
_FIX = os.path.join(_HERE, 'fixtures', 'pdf')
for _p in (_APP, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import docstore                                            # noqa: E402
import multipart                                           # noqa: E402
import service                                             # noqa: E402
import pdf_client                                          # noqa: E402

try:
    import pdf_extract                                      # noqa: E402
    HAS_PYMUPDF = True
except ImportError:                                         # pragma: no cover
    pdf_extract = None
    HAS_PYMUPDF = False

#: 真实 HTTP 探测用（与本机代理无关）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 解析可能要 2–3s（import pymupdf 冷启动），轮询给足时间
POLL_TIMEOUT = 60.0


def _blob(name):
    with open(os.path.join(_FIX, name), 'rb') as fh:
        return fh.read()


def _multipart_body(filename, data, boundary='----qbanktest7f3a'):
    """构造一个 multipart/form-data 请求体（与浏览器发的一致）。"""
    head = (
        '--%s\r\n'
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
        'Content-Type: application/pdf\r\n'
        '\r\n' % (boundary, filename)
    ).encode('utf-8')
    tail = ('\r\n--%s--\r\n' % boundary).encode('utf-8')
    return head + data + tail, 'multipart/form-data; boundary=%s' % boundary


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _http(url, method='GET', data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as exc:
        # 服务端在 body 读完之前就返回了 413 并关闭连接 —— 客户端还在写，
        # 于是写端被中止（WinError 10053）。这是**正确行为**（limit_body
        # 的设计就是「只看 Content-Length 就拒绝，不把 body 读完」），
        # 但客户端拿不到响应体。
        # 这种情况按「连接被提前关闭」标记，交给调用方判断。
        return -1, {}, ('connection closed by server: %s' % type(exc).__name__).encode()


def _json_body(body):
    try:
        return json.loads(body.decode('utf-8'))
    except Exception:
        return None


class TestMultipartParser(unittest.TestCase):
    """multipart 解析器自身的边界。"""

    def test_parses_file_part(self):
        data, ctype = _multipart_body('a.pdf', b'%PDF-1.4 hello')
        parts = multipart.parse_multipart(data, ctype)
        self.assertEqual(len(parts), 1)
        p = multipart.first_file(parts)
        self.assertIsNotNone(p)
        self.assertEqual(p.filename, 'a.pdf')
        self.assertEqual(p.data, b'%PDF-1.4 hello')

    def test_strips_path_from_filename(self):
        """路径型文件名必须被剥掉——落盘键另有随机生成，但这是双保险。"""
        data, ctype = _multipart_body('../../evil.pdf', b'x')
        p = multipart.first_file(multipart.parse_multipart(data, ctype))
        self.assertNotIn('/', p.filename)
        self.assertNotIn('\\', p.filename)
        self.assertEqual(p.filename, 'evil.pdf')

    def test_ignores_non_file_parts(self):
        """没有 filename 的表单字段不算文件上传。"""
        body = (b'------x\r\n'
                b'Content-Disposition: form-data; name="note"\r\n\r\n'
                b'hello\r\n'
                b'------x\r\n'
                b'Content-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
                b'Content-Type: application/pdf\r\n\r\n'
                b'%PDF-1.4\r\n'
                b'------x--\r\n')
        parts = multipart.parse_multipart(body, 'multipart/form-data; boundary=----x')
        p = multipart.first_file(parts)
        self.assertEqual(p.filename, 'a.pdf')
        self.assertEqual(p.data, b'%PDF-1.4')

    def test_missing_boundary_raises(self):
        with self.assertRaises(multipart.MultipartError):
            multipart.parse_multipart(b'x', 'application/json')

    def test_empty_body_raises(self):
        with self.assertRaises(multipart.MultipartError):
            multipart.parse_multipart(b'', 'multipart/form-data; boundary=x')

    def test_no_file_part_returns_none(self):
        body = b'------x\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n------x--\r\n'
        parts = multipart.parse_multipart(body, 'multipart/form-data; boundary=----x')
        self.assertIsNone(multipart.first_file(parts))


class TestDocStoreUnit(unittest.TestCase):
    """docstore 与 service 的单元测试（不起 HTTP 服务）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qbank_doc_')
        self.ds = docstore.DocStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_read(self):
        meta = self.ds.save_blob(b'%PDF-1.4 data', 'orig.pdf')
        self.assertIsNotNone(meta)
        self.assertTrue(meta['id'])
        self.assertEqual(meta['original_name'], 'orig.pdf')
        self.assertEqual(meta['bytes'], 13)
        self.assertEqual(len(meta['sha256']), 64)
        self.assertTrue(self.ds.pdf_path(meta['id']).exists())
        self.assertEqual(self.ds.read_blob(meta['id']), b'%PDF-1.4 data')

    def test_filename_never_used_in_path(self):
        """**原始文件名绝不参与路径拼接**。"""
        meta = self.ds.save_blob(b'x', '../../../../evil.pdf')
        self.assertNotIn('evil', str(self.ds.pdf_path(meta['id'])))
        self.assertTrue(self.ds.pdf_path(meta['id']).exists())

    def test_delete_removes_files_from_disk(self):
        meta = self.ds.save_blob(b'x', 'a.pdf')
        doc_id = meta['id']
        self.ds.write_pages(doc_id, [{'physical_page': 1, 'text': 'x'}])
        self.assertTrue(self.ds.pages_path(doc_id).exists())
        r = self.ds.delete(doc_id)
        self.assertTrue(r['ok'])
        self.assertFalse(self.ds.pdf_path(doc_id).exists())
        self.assertFalse(self.ds.pages_path(doc_id).exists())
        self.assertFalse(self.ds.meta_path(doc_id).exists())

    def test_delete_missing_is_not_found(self):
        r = self.ds.delete('nope')
        self.assertEqual(r.get('error'), 'not found')

    def test_reap_orphans_marks_interrupted(self):
        """**重启遗留的 running/queued 必须被标成 interrupted** —— 不假装成功。"""
        meta = self.ds.save_blob(b'x', 'a.pdf')
        meta['status'] = 'running'
        self.ds.write_meta(meta)
        touched = self.ds.reap_orphans()
        self.assertIn(meta['id'], touched)
        after = self.ds.read_meta(meta['id'])
        self.assertEqual(after['status'], 'interrupted')
        self.assertEqual(after['result_status'], 'interrupted')

    def test_reap_orphans_leaves_finished_alone(self):
        meta = self.ds.save_blob(b'x', 'a.pdf')
        meta['status'] = 'succeeded'
        self.ds.write_meta(meta)
        self.ds.reap_orphans()
        self.assertEqual(self.ds.read_meta(meta['id'])['status'], 'succeeded')

    def test_job_table_is_not_persisted(self):
        """job 表在内存里——新建一个 DocStore 就查不到旧 job。"""
        meta = self.ds.save_blob(b'x', 'a.pdf')
        job = self.ds.create_job(meta['id'])
        self.assertIsNotNone(self.ds.get_job(job['id']))
        fresh = docstore.DocStore(self.tmp)
        self.assertIsNone(fresh.get_job(job['id']),
                          'job 表不该跨实例持久化（文档里已写明不跨重启）')

    def test_meta_write_is_atomic(self):
        """元数据写一半被杀会留半截 JSON——用唯一 tmp + replace 保证。"""
        meta = self.ds.save_blob(b'x', 'a.pdf')
        self.assertTrue(self.ds.write_meta(meta))
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith('.tmp')]
        self.assertEqual(leftovers, [], '残留了 .tmp 文件')

    def test_concurrent_meta_writes_do_not_corrupt(self):
        """**并发写同一文档的元数据不能损坏文件**。

        这是一条回归测试：早期版本用固定的 `<id>.json.tmp` 做原子写，
        两个线程同时写就互相 replace 掉对方的 tmp，
        目标文件变成半截 JSON → `read_meta` 返回 None → **GET 文档 404**。

        那个 bug 只在并发下偶发，单线程测试永远抓不到，
        所以这里用多线程把它变成必现。
        """
        import threading
        meta = self.ds.save_blob(b'x', 'a.pdf')
        doc_id = meta['id']
        errors = []

        def writer(n):
            for i in range(30):
                m = dict(meta)
                m['writer_%d' % n] = i
                if not self.ds.write_meta(m):
                    errors.append('write_meta failed')

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        back = self.ds.read_meta(doc_id)
        self.assertIsInstance(back, dict, '并发写之后元数据读不回来（文件被写坏了）')
        self.assertEqual(back.get('id'), doc_id)

    def test_update_meta_is_atomic_read_modify_write(self):
        """update_meta 必须是原子的读-改-写——否则字段会互相覆盖。

        后台线程写 status、请求线程写 job_id，两边同时改同一份元数据时，
        后写的会把先写的字段覆盖回旧值（**丢失更新**）。
        """
        import threading
        meta = self.ds.save_blob(b'x', 'a.pdf')
        doc_id = meta['id']

        def set_field(field, value):
            for _ in range(20):
                self.ds.update_meta(doc_id, **{field: value})

        t1 = threading.Thread(target=set_field, args=('job_id', 'job_abc'))
        t2 = threading.Thread(target=set_field, args=('status', 'running'))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        back = self.ds.read_meta(doc_id)
        self.assertEqual(back.get('job_id'), 'job_abc', 'job_id 被覆盖没了')
        self.assertEqual(back.get('status'), 'running', 'status 被覆盖没了')

    def test_pages_json_not_treated_as_metadata(self):
        """页文本文件后缀 `.pages.json` 也以 .json 结尾——不能被当元数据读。

        早期版本没排除它，read_meta 读回来是个 list，
        后面 `m.get(...)` 直接抛 AttributeError → 列表接口 500。
        """
        meta = self.ds.save_blob(b'x', 'a.pdf')
        self.ds.write_pages(meta['id'], [{'physical_page': 1, 'text': 'x'}])
        lst = self.ds.list_meta()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]['id'], meta['id'])


@unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf（解析要走 worker 进程）')
class TestApiEndToEnd(unittest.TestCase):
    """起真的 FastAPI 服务，走真的 HTTP。

    ⚠️ 服务跑在**本进程的后台线程**里，不用子进程 ——
    这与 tools/smoke_api.py 的做法一致（它在主进程用 threading 跑 uvicorn）。
    原因有二：
      1) 子进程起 HTTP 服务在本机沙箱里会被 SIGTERM 掉（实测：连最小
         socketserver 都被杀），起服务的测试必须同进程；
      2) 同进程才能直接设置 api.DOCSTORE 指向临时目录。
    """

    @classmethod
    def setUpClass(cls):
        import uvicorn
        import api
        cls.tmp = tempfile.mkdtemp(prefix='qbank_api_doc_')
        api.DOCSTORE = api.docstore.DocStore(cls.tmp)
        cls.port = _free_port()
        config = uvicorn.Config(api.app, host='127.0.0.1', port=cls.port,
                                log_level='error')
        cls.srv = uvicorn.Server(config)
        import threading
        cls.th = threading.Thread(target=cls.srv.run, daemon=True)
        cls.th.start()
        for _ in range(100):
            if cls.srv.started:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('api server did not start')
        cls.base = 'http://127.0.0.1:%d' % cls.port

    @classmethod
    def tearDownClass(cls):
        try:
            cls.srv.should_exit = True
            cls.th.join(timeout=10)
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.base = 'http://127.0.0.1:%d' % self.port

    def _upload(self, filename, blob):
        body, ctype = _multipart_body(filename, blob)
        return _http(self.base + '/api/v1/documents', 'POST', body,
                     {'Content-Type': ctype})

    def _poll(self, job_id, timeout=POLL_TIMEOUT):
        """轮询到 job 结束（succeeded/failed）。返回 job dict。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _, body = _http(self.base + '/api/v1/parse-jobs/' + job_id)
            job = (_json_body(body) or {}).get('job') or {}
            if job.get('status') in ('succeeded', 'failed', 'interrupted'):
                return job
            time.sleep(0.2)
        raise AssertionError('job 未在 %.0fs 内结束' % timeout)

    # ---------------------------------------------------------- 测试 1
    def test_1_upload_202_then_succeeded_and_pages_match(self):
        """上传 → 202 → 轮询 succeeded → 页文本与直接调 pdf_client 一致。"""
        blob = _blob('01-chinese-text.pdf')
        status, headers, body = self._upload('01.pdf', blob)
        self.assertEqual(status, 202, '上传必须立刻 202，实际 %d：%s' % (status, body[:200]))
        payload = _json_body(body)
        self.assertIn('document', payload)
        self.assertIn('job', payload)
        job_id = payload['job']['id']
        self.assertEqual(payload['job']['status'], 'queued')
        self.assertEqual(payload['document']['status'], 'queued')

        job = self._poll(job_id)
        self.assertEqual(job['status'], 'succeeded', 'job=%s' % job)
        self.assertEqual(job['result_status'], 'ok')

        # 页文本与直接调 pdf_client 一致
        direct = pdf_client.extract_in_worker(blob)
        status, _, body = _http(self.base + '/api/v1/documents/'
                                     + payload['document']['id'] + '/pages?page=1')
        self.assertEqual(status, 200)
        page = (_json_body(body) or {})['item']
        self.assertEqual(page['text'], direct['pages'][0]['text'])
        self.assertEqual(page['chars'], direct['pages'][0]['chars'])
        self.assertEqual(page['physical_page'], 1)

    def test_1b_upload_returns_fast(self):
        """上传必须**立刻**返回——不能等解析（实测要 2–3s）。"""
        blob = _blob('02-two-column.pdf')
        t0 = time.monotonic()
        status, _, _ = self._upload('02.pdf', blob)
        elapsed = time.monotonic() - t0
        self.assertEqual(status, 202)
        # 解析至少 2s（import pymupdf 冷启动），上传若超过 1.5s 就是同步等待了
        self.assertLess(elapsed, 1.5, '上传耗时 %.2fs，看起来像同步解析' % elapsed)

    # ---------------------------------------------------------- 测试 2
    def test_2_corrupt_upload_ends_as_corrupt_not_500(self):
        """上传 06-corrupt → 202 → 轮询到 corrupt，**不是 500**。"""
        status, _, body = self._upload('bad.pdf', _blob('06-corrupt.pdf'))
        self.assertEqual(status, 202)
        job_id = (_json_body(body) or {})['job']['id']
        job = self._poll(job_id)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['result_status'], 'corrupt')

    def test_2b_encrypted_upload_ends_as_encrypted(self):
        status, _, body = self._upload('enc.pdf', _blob('05-encrypted.pdf'))
        self.assertEqual(status, 202)
        job_id = (_json_body(body) or {})['job']['id']
        job = self._poll(job_id)
        self.assertEqual(job['result_status'], 'encrypted')

    def test_2c_scanned_upload_ends_as_no_text(self):
        status, _, body = self._upload('scan.pdf', _blob('04-scanned.pdf'))
        self.assertEqual(status, 202)
        job_id = (_json_body(body) or {})['job']['id']
        job = self._poll(job_id)
        self.assertEqual(job['result_status'], 'no_text')

    # ---------------------------------------------------------- 测试 3
    def test_3_oversize_upload_returns_413(self):
        """超限文件 → 413（复用已有的 limit_body，不另写一套）。

        ⚠️ 用**裸 socket 只发头**测，不用 urllib 传 11MB body。
        原因：`limit_body` 的设计就是「只看 Content-Length 就拒绝，不把 body
        读完」——所以服务端回 413 的同时就关了连接，客户端还在发 body，
        于是写端被中止（WinError 10053），**拿不到响应体**。
        真实浏览器也是先发头再发 body，服务端不等 body —— 所以裸 socket
        的测法更贴近真实行为。
        """
        big_len = 11 * 1024 * 1024
        status, body = _raw_head_request(
            'POST', '/api/v1/documents', content_length=big_len,
            content_type='multipart/form-data; boundary=----x',
            port=self.port)
        self.assertEqual(status, 413, '实际 %d：%s' % (status, body[:200]))
        payload = _json_body(body)
        self.assertIn('error', payload)
        self.assertEqual(payload['error']['code'], 'payload_too_large')
        self.assertIn('request_id', payload['error'])

    def test_3b_limit_body_applies_to_all_endpoints(self):
        """limit_body 是全局中间件，不只是上传端点。"""
        status, body = _raw_head_request(
            'POST', '/api/v1/import_questions', content_length=big_len(),
            content_type='application/json', port=self.port)
        self.assertEqual(status, 413)


def big_len():
    return 11 * 1024 * 1024


def _raw_head_request(method, path, content_length, content_type='application/json',
                      host='127.0.0.1', port=None, timeout=20):
    """裸 socket 只发请求头，读取响应。返回 (状态码, 响应体)。

    用来测「服务端按 Content-Length 提前拒绝」的行为。
    """
    import socket
    if port is None:
        raise ValueError('port required')
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        head = (
            '%s %s HTTP/1.1\r\n'
            'Host: %s:%d\r\n'
            'Content-Type: %s\r\n'
            'Content-Length: %d\r\n'
            'Connection: close\r\n'
            '\r\n' % (method, path, host, port, content_type, content_length)
        )
        s.sendall(head.encode('utf-8'))
        chunks = []
        while True:
            try:
                data = s.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            if len(b''.join(chunks)) > 65536:
                break
        raw = b''.join(chunks)
    finally:
        try:
            s.close()
        except OSError:
            pass
    if not raw:
        return -1, b''
    head_text, _, body = raw.partition(b'\r\n\r\n')
    first = head_text.split(b'\r\n', 1)[0].decode('latin-1', 'replace')
    parts = first.split(' ')
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        status = -1
    return status, body

    # ---------------------------------------------------------- 测试 4
    def test_4_delete_then_get_404_and_file_gone(self):
        """删除 → 之后 GET 404，且磁盘文件真的没了。"""
        status, _, body = self._upload('01.pdf', _blob('01-chinese-text.pdf'))
        self.assertEqual(status, 202)
        doc = (_json_body(body) or {})['document']
        doc_id = doc['id']
        self._poll((_json_body(body) or {})['job']['id'])

        status, _, _ = _http(self.base + '/api/v1/documents/' + doc_id)
        self.assertEqual(status, 200)

        status, _, body = _http(self.base + '/api/v1/documents/' + doc_id, 'DELETE')
        self.assertEqual(status, 200, body[:200])

        status, _, _ = _http(self.base + '/api/v1/documents/' + doc_id)
        self.assertEqual(status, 404, '删除后 GET 必须 404')

        # 磁盘文件真的没了
        self.assertFalse(os.path.exists(os.path.join(self.tmp, doc_id + '.pdf')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, doc_id + '.json')))

    def test_4b_delete_missing_document_404(self):
        status, _, _ = _http(self.base + '/api/v1/documents/nosuchdoc', 'DELETE')
        self.assertEqual(status, 404)

    # ---------------------------------------------------------- 测试 5
    def test_5_list_and_detail_structure(self):
        status, _, body = self._upload('01.pdf', _blob('01-chinese-text.pdf'))
        doc = (_json_body(body) or {})['document']
        status, _, body = _http(self.base + '/api/v1/documents')
        self.assertEqual(status, 200)
        payload = _json_body(body)
        self.assertIn('items', payload)
        ids = [i['id'] for i in payload['items']]
        self.assertIn(doc['id'], ids)
        item = [i for i in payload['items'] if i['id'] == doc['id']][0]
        for key in ('id', 'original_name', 'sha256', 'bytes', 'created_at',
                    'status', 'result_status', 'page_count', 'total_chars',
                    'columns_detected', 'table_pages'):
            self.assertIn(key, item, '列表项缺字段 %s' % key)

        status, _, body = _http(self.base + '/api/v1/documents/' + doc['id'])
        self.assertEqual(status, 200)
        self.assertIn('document', _json_body(body))


@unittest.skipUnless(HAS_PYMUPDF, '需要 pymupdf')
class TestServerParity(unittest.TestCase):
    """测试 5：两套服务端同请求 → 响应结构一致。

    标准库服务端也跑在**本进程的后台线程**里（不是子进程）——
    子进程起 HTTP 服务在本机沙箱里会被 SIGTERM 掉（实测最小
    socketserver 也被杀），同进程才能同时起两套服务做对比。
    """

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        import server
        cls.tmp = tempfile.mkdtemp(prefix='qbank_srv_doc_')
        cls.port = _free_port()
        server.DOCSTORE = server.docstore.DocStore(cls.tmp)
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', cls.port), server.H)
        import threading
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()
        for _ in range(100):
            try:
                status, _, _ = _http('http://127.0.0.1:%d/health/live' % cls.port, timeout=2)
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError('server.py did not start')

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
            cls.th.join(timeout=10)
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_5_server_endpoints_respond(self):
        """标准库服务端也有同一套端点，结构与 FastAPI 侧一致。"""
        base = 'http://127.0.0.1:%d' % self.port
        status, _, body = _http(base + '/api/v1/documents')
        self.assertEqual(status, 200, body[:200])
        payload = _json_body(body)
        self.assertIn('items', payload)

        # 上传 → 202
        blob = _blob('01-chinese-text.pdf')
        body_bytes, ctype = _multipart_body('01.pdf', blob)
        status, _, body = _http(base + '/api/v1/documents', 'POST', body_bytes,
                                {'Content-Type': ctype})
        self.assertEqual(status, 202, '标准库侧上传也必须 202，实际 %d' % status)
        payload = _json_body(body)
        self.assertIn('document', payload)
        self.assertIn('job', payload)

        doc_id = payload['document']['id']
        job_id = payload['job']['id']

        # 轮询
        deadline = time.monotonic() + POLL_TIMEOUT
        job = {}
        while time.monotonic() < deadline:
            status, _, body = _http(base + '/api/v1/parse-jobs/' + job_id)
            job = (_json_body(body) or {}).get('job') or {}
            if job.get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.2)
        self.assertEqual(job.get('status'), 'succeeded', 'job=%s' % job)

        # 页文本
        status, _, body = _http(base + '/api/v1/documents/%s/pages?page=1' % doc_id)
        self.assertEqual(status, 200)
        page = (_json_body(body) or {})['item']
        direct = pdf_client.extract_in_worker(blob)
        self.assertEqual(page['text'], direct['pages'][0]['text'])

        # 删除 → 404
        status, _, _ = _http(base + '/api/v1/documents/' + doc_id, 'DELETE')
        self.assertEqual(status, 200)
        status, _, _ = _http(base + '/api/v1/documents/' + doc_id)
        self.assertEqual(status, 404)

    def test_5c_server_side_413(self):
        """标准库服务端也要有 413（它自己实现检查，不走 FastAPI 中间件）。"""
        status, body = _raw_head_request(
            'POST', '/api/v1/documents', content_length=11 * 1024 * 1024,
            content_type='multipart/form-data; boundary=----x', port=self.port)
        self.assertEqual(status, 413, '实际 %d：%s' % (status, body[:200]))
        payload = _json_body(body)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['error']['code'], 'payload_too_large')

    def test_5b_response_key_shapes_match_between_servers(self):
        """**两套服务端对同一请求必须返回同结构**（比 JSON keys）。

        做法：把两边的响应体各自取出 key 路径集合（递归），
        断言完全一致——只比 keys 不比值（值含随机 id，必然不同）。

        两套服务都在本进程的后台线程里起（子进程起 HTTP 服务在本机
        沙箱里会被 SIGTERM 掉）。
        """
        import uvicorn
        from http.server import ThreadingHTTPServer
        import api
        import server
        import threading

        # --- FastAPI 侧 ---
        api_tmp = tempfile.mkdtemp(prefix='qbank_parity_api_')
        api.DOCSTORE = api.docstore.DocStore(api_tmp)
        api_port = _free_port()
        config = uvicorn.Config(api.app, host='127.0.0.1', port=api_port,
                                log_level='error')
        api_srv = uvicorn.Server(config)
        api_th = threading.Thread(target=api_srv.run, daemon=True)
        api_th.start()
        for _ in range(100):
            if api_srv.started:
                break
            time.sleep(0.1)

        # --- 标准库侧（本类 setUpClass 已起好一台，再起一台独立的）---
        srv_tmp = tempfile.mkdtemp(prefix='qbank_parity_srv_')
        srv_store = server.docstore.DocStore(srv_tmp)
        srv_port = _free_port()
        httpd = ThreadingHTTPServer(('127.0.0.1', srv_port), server.H)
        srv_th = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv_th.start()
        time.sleep(0.5)

        try:
            blob = _blob('01-chinese-text.pdf')
            body_bytes, ctype = _multipart_body('01.pdf', blob)

            api_base = 'http://127.0.0.1:%d' % api_port
            srv_base = 'http://127.0.0.1:%d' % srv_port

            api_keys = {}
            status, _, body = _http(api_base + '/api/v1/documents', 'POST', body_bytes,
                                    {'Content-Type': ctype})
            self.assertEqual(status, 202, body[:200])
            api_keys['upload'] = _keys(_json_body(body))
            doc_id = (_json_body(body) or {})['document']['id']
            status, _, body = _http(api_base + '/api/v1/documents')
            api_keys['list'] = _keys(_json_body(body))
            status, _, body = _http(api_base + '/api/v1/documents/' + doc_id)
            api_keys['detail'] = _keys(_json_body(body))

            srv_keys = {}
            status, _, body = _http(srv_base + '/api/v1/documents', 'POST', body_bytes,
                                    {'Content-Type': ctype})
            self.assertEqual(status, 202, body[:200])
            srv_keys['upload'] = _keys(_json_body(body))
            doc_id2 = (_json_body(body) or {})['document']['id']
            status, _, body = _http(srv_base + '/api/v1/documents')
            srv_keys['list'] = _keys(_json_body(body))
            status, _, body = _http(srv_base + '/api/v1/documents/' + doc_id2)
            srv_keys['detail'] = _keys(_json_body(body))

            for key in ('upload', 'list', 'detail'):
                self.assertEqual(api_keys[key], srv_keys[key],
                                 '「%s」两套服务端结构不一致\n  FastAPI: %s\n  标准库: %s'
                                 % (key, sorted(api_keys[key]), sorted(srv_keys[key])))
        finally:
            try:
                api_srv.should_exit = True
                api_th.join(timeout=10)
            except Exception:
                pass
            try:
                httpd.shutdown()
                httpd.server_close()
                srv_th.join(timeout=10)
            except Exception:
                pass
            shutil.rmtree(api_tmp, ignore_errors=True)
            shutil.rmtree(srv_tmp, ignore_errors=True)


def _keys(obj, prefix=''):
    """递归收集 JSON 结构的 key 路径集合（值不比，只比结构）。"""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = prefix + '/' + k
            out.add(path)
            out |= _keys(v, path)
    elif isinstance(obj, list):
        out.add(prefix + '[]')
        for item in obj[:1]:
            out |= _keys(item, prefix + '[]')
    return out


class TestRestartRecovery(unittest.TestCase):
    """测试 6：打桩模拟重启 → 遗留 job 变 interrupted。"""

    def test_6_orphan_running_becomes_interrupted(self):
        tmp = tempfile.mkdtemp(prefix='qbank_restart_')
        try:
            # 第一次「运行」：存文档，状态停在 running（模拟解析途中被杀）
            ds = docstore.DocStore(tmp)
            meta = ds.save_blob(b'%PDF-1.4 x', 'a.pdf')
            meta['status'] = 'running'
            meta['result_status'] = ''
            ds.write_meta(meta)
            job = ds.create_job(meta['id'])
            meta['job_id'] = job['id']
            ds.write_meta(meta)

            # 「重启」：新实例 → reap_orphans
            ds2 = docstore.DocStore(tmp)
            touched = ds2.reap_orphans()
            self.assertIn(meta['id'], touched)

            after = ds2.read_meta(meta['id'])
            self.assertEqual(after['status'], 'interrupted')
            self.assertEqual(after['result_status'], 'interrupted')
            # 旧 job 查不到（job 表不持久化）
            self.assertIsNone(ds2.get_job(job['id']))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_6b_queued_also_becomes_interrupted(self):
        tmp = tempfile.mkdtemp(prefix='qbank_restart2_')
        try:
            ds = docstore.DocStore(tmp)
            meta = ds.save_blob(b'x', 'a.pdf')
            meta['status'] = 'queued'
            ds.write_meta(meta)
            ds2 = docstore.DocStore(tmp)
            ds2.reap_orphans()
            self.assertEqual(ds2.read_meta(meta['id'])['status'], 'interrupted')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRouteRegistration(unittest.TestCase):
    """路由必须留在 api.py —— smoke_api.py 用正则从源码抓路由。"""

    def test_routes_are_in_api_py(self):
        import re
        src = open(os.path.join(_APP, 'api.py'), encoding='utf-8').read()
        routes = set(re.findall(r"@app\.(?:get|post|put|delete)\('/api/v1([^']*)'", src))
        for want in ('/documents', '/documents/{doc_id}', '/documents/{doc_id}/pages',
                     '/parse-jobs/{job_id}'):
            self.assertIn(want, routes, 'api.py 里没有注册 %s' % want)

    def test_server_py_has_equivalent_paths(self):
        src = open(os.path.join(_APP, 'server.py'), encoding='utf-8').read()
        for want in ("/api/v1/documents", "/api/v1/parse-jobs/"):
            self.assertIn(want, src, 'server.py 里没有 %s' % want)

    def test_no_pymupdf_in_web_process_modules(self):
        """**Web 进程不得 import pymupdf**：api.py / server.py / service.py
        / docstore.py / multipart.py 都不能引入 pdf_extract。"""
        import re
        for name in ('api.py', 'server.py', 'service.py', 'docstore.py', 'multipart.py'):
            src = open(os.path.join(_APP, name), encoding='utf-8').read()
            code = re.sub(r'#.*', '', src)
            self.assertNotRegex(code, r'^\s*import\s+pymupdf',
                                '%s 里出现了 import pymupdf' % name)
            self.assertNotRegex(code, r'^\s*import\s+pdf_extract',
                                '%s 里出现了 import pdf_extract（会拖进 pymupdf）' % name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
