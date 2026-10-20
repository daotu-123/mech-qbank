# -*- coding: utf-8 -*-
r"""
mech-qbank · service 层：业务用例（两套服务端共享）

## 为什么要有这一层（2026-10-20 后端分层重构第 3 步）
--------------------------------------------------------------------------
重构前每个 HTTP 端点都把三件事绞在一起：

    ① 取数 / 业务判断  ② HTTP 状态码判定（404 / 400）  ③ 响应体组装

例如 `GET /api/v1/questions/{qid}/note` 原长这样（api.py）：

    if not STORE.full(qid):        # ② HTTP 校验
        raise _404()
    for it in STORE.notes_list(2000):   # ① 业务：从列表里找那一条
        if it['id'] == qid:
            return it              # ③ 协议：直接返回业务对象
    return {'id': qid, 'note': '', 'updated_at': ''}

问题不是「写得乱」，而是**同一段业务逻辑要在两套服务端里各写一遍**：
app/api.py（FastAPI，RESTful）与 app/server.py（标准库回退，if 路由链）。
两边一旦对「找不到该返回什么」的理解出现偏差，就是两个行为不同的产品。

## 本层的职责

只管 ① —— **纯业务，不认识 HTTP**：
- 不 import fastapi / starlette / pydantic
- 不知道 404 / 400 / 422 的区别
- 不知道 URL 长什么样
- 需要向调用方报告「题不存在」时，返回一个**中性的结果对象**（如 ok=False, reason='not found'），
  由 router 层翻译成 HTTPException / 404 状态码

**只允许标准库**：app/server.py 是零依赖回退服务端（start.bat 在 fastapi
不可用时启动它），本模块若 import fastapi 就会让该路径直接失效。

## 为什么不把「翻译成 HTTP 错误」也放进来

因为那样 service 就会 import fastapi，零依赖回退路径当场失效。
所以错误翻译留在 router 层，两套服务端各翻译一次——
这正是「router 管 HTTP、service 管业务」这条线的意义：
同一份业务，两种协议，各自适配。
"""
from __future__ import annotations


class Result(dict):
    """业务结果。

    刻意做成 dict 的子类而不是 dataclass：
    - service 的返回值最终要原样变成 HTTP 响应体，dict 最省事
    - 继承 dict 后 `r.get('ok')` / `r['items']` 都能用，router 无需转换
    - 需要表达「失败了但不是 HTTP 层面的失败」时，用 ok=False + reason
    """

    @property
    def ok(self) -> bool:
        """成功与否。**成功时默认 True** —— 大多数 Result 直接包装 store 的返回值，
        那些 dict 里没有 'ok' 键（如 {'items': [...]}），不该被当成失败。"""
        return bool(self.get('ok', True))

    @property
    def reason(self) -> str:
        return str(self.get('reason') or '')

    @property
    def not_found(self) -> bool:
        """是否应当翻译成 HTTP 404。router 层据此raise 404。"""
        return self.reason == 'not found'


# ---------------------------------------------------------------- 题目备注
def get_note(store, qid: str) -> Result:
    """取单题备注；没有则返回空壳（不报错）。

    语义要点：备注可以不存在（题存在但没写过备注），这**不是错误**。
    原实现用 `for it in STORE.notes_list(2000)` 线性找，保持原样——
    改成按 key 直查要动 notes_list 的返回结构，超出「纯结构调整」范围。
    """
    if not store.full(qid):
        return Result(ok=False, reason='not found')
    for it in store.notes_list(2000):
        if it['id'] == qid:
            return Result(it)
    return Result({'id': qid, 'note': '', 'updated_at': ''})


def put_note(store, qid: str, note: str) -> Result:
    """写备注。note 传空串即删除（沿用 Store.set_note 的既有语义）。"""
    return Result(store.set_note(qid, note))


# ---------------------------------------------------------------- 组卷
def pick_questions(store, *, q, limit, level=None, qtype=None, sclass=None,
                   bank=None, section=None, only_new=False, review=False) -> Result:
    """组卷 / 复习 / 搜索共用的取题用例（`q` 非空则按题干搜索）。

    对应 GET /api/v1/questions：三种模式共用一个集合端点。
    参数全部原样透传给 store，**不做任何额外校验**——
    limit 的夹取、level 的范围都已在 store.pick() 内完成，
    在这里重复一遍就是两处口径，正是要消除的东西。
    """
    if q:
        return Result({'items': store.search(q, limit)})
    return Result({'items': store.pick(limit, level, qtype, sclass, bank,
                                       section, only_new, review)})


# ---------------------------------------------------------------- 单题
def get_question(store, qid: str) -> Result:
    """取单题。**不下发 answer**（防答案泄露是既有红线，见 _pub_row）。"""
    row = store.full(qid)
    if not row:
        return Result(ok=False, reason='not found')
    return Result({'item': store._pub_row(row)})


# ---------------------------------------------------------------- PDF 文档
# 这一组用例是 M3.3 新增的，操作对象是「PDF 文档 + 解析任务」，
# 不是题库里的题。两套服务端（api.py / server.py）共用同一份实现，
# 保证同一个请求在两边得到**结构完全一致**的响应。
#
# ⚠️ 这里 import 的是 docstore（纯标准库）与 pdf_client（纯标准库），
# **不是** pdf_extract——后者会 import pymupdf，把 MuPDF 拖进 Web 进程。
# 解析一律经 pdf_client 走独立 worker 进程。


def create_document(docstore, filename: str, blob: bytes) -> Result:
    """落盘一个 PDF 并建解析任务，**立即返回**（不等待解析）。

    返回 {'document': {...}, 'job': {...}}。
    解析在后台线程里跑，调用方拿 job id 去轮询 GET /parse-jobs/{id}。

    ⚠️ 顺序很要紧：**先建 job，再一次性写入含 job_id 的元数据**。
    反过来（先写元数据再补 job_id）会有一个窗口：后台线程在这两步之间
    读到了 job_id 为空的元数据，把它覆盖回去 → job_id 丢失，
    之后 GET /parse-jobs/{id} 查不到、删除时也级联不掉 job。
    """
    if not blob:
        return Result(ok=False, reason='empty file')
    meta = docstore.save_blob(blob, filename)
    if not meta:
        return Result(ok=False, reason='cannot store file')
    job = docstore.create_job(meta['id'])
    meta['job_id'] = job['id']
    # ⚠️ 这一步必须在 job 建好之后、**且后台线程启动之前**完成，
    # 否则 worker 可能先读到 job_id 为空的元数据。
    if not docstore.write_meta(meta):
        return Result(ok=False, reason='cannot store metadata')
    docstore.start_worker()
    return Result({'document': meta, 'job': job})


def list_documents(docstore) -> Result:
    """文档列表。**只给摘要**，不返回页文本（那个可能很大）。"""
    items = []
    for meta in docstore.list_meta():
        items.append({
            'id': meta.get('id', ''),
            'original_name': meta.get('original_name', ''),
            'sha256': meta.get('sha256', ''),
            'bytes': meta.get('bytes', 0),
            'created_at': meta.get('created_at', ''),
            'status': meta.get('status', ''),
            'result_status': meta.get('result_status', ''),
            'page_count': meta.get('page_count', 0),
            'total_chars': meta.get('total_chars', 0),
            'columns_detected': meta.get('columns_detected', 1),
            'table_pages': meta.get('table_pages', []),
        })
    return Result({'items': items})


def get_document(docstore, doc_id: str) -> Result:
    """文档详情。不存在 → not_found（router 翻成 404）。"""
    meta = docstore.read_meta(doc_id)
    if not meta:
        return Result(ok=False, reason='not found')
    return Result({'document': meta})


def get_job(docstore, job_id: str) -> Result:
    """解析任务状态。**job 表不跨重启持久化**，重启后查旧 id 会 404。"""
    job = docstore.get_job(job_id)
    if not job:
        return Result(ok=False, reason='not found')
    return Result({'job': job})


def get_document_page(docstore, doc_id: str, page: int) -> Result:
    """取某一页的文本。page 从 1 起。

    越界 → not_found。文档不存在 → not_found。
    """
    meta = docstore.read_meta(doc_id)
    if not meta:
        return Result(ok=False, reason='not found')
    pages = docstore.read_pages(doc_id)
    if not pages:
        return Result(ok=False, reason='not found')
    if page < 1 or page > len(pages):
        return Result(ok=False, reason='not found')
    return Result({'document_id': doc_id, 'page': page,
                   'page_count': len(pages), 'item': pages[page - 1]})


def delete_document(docstore, doc_id: str) -> Result:
    """删除文档，**级联**清理 PDF/页文本/元数据/job/由它生成的题目草稿。"""
    r = docstore.delete(doc_id)
    if r.get('error'):
        return Result(ok=False, reason='not found')
    return Result(r)