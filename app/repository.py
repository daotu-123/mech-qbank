# -*- coding: utf-8 -*-
r"""
mech-qbank · repository 层：SQL 的唯一出口

为什么单独抽这一层（2026-10-20 后端分层重构第 2 步）
--------------------------------------------------------------------------
core.py 原来有 40 处 SQL 散落在 Store 的 20 多个方法里，且大量直接摸
`self.con.execute(...)` —— 绕过了它自己的 `q()` / `x()` 锁包装。于是
「加锁」这件事全靠人记住，直连处就是潜在的竞态。

## 本层的边界（刻意划得很窄）

**只搬「执行 SQL」与「行 → dict」这两件机械动作，不搬任何业务判断。**

具体说：
- ✅ 搬：execute / executemany / fetchall / fetchone / commit 的加锁执行
- ✅ 搬：sqlite3.Row → dict 的转换
- ❌ 不搬：SQL 语句本身（where 条件怎么拼、review 与 only_new 走哪条分支）
- ❌ 不搬：`pick()` 里「先随机抽 n 再按题型标签稳定排序」这类业务规则
- ❌ 不搬：`grade()` 的判分分支、`schedule()` 的 FSRS 逻辑

理由很实际：`pick()` 的排序策略是 2026-10-19 第七轮反复调试的结果
（不能写成 ORDER BY CASE..., RANDOM() LIMIT n，否则单选占满名额），
`grade()` / `schedule()` 是纪律明令禁止改动行为的禁区。
把这些 SQL 搬进 repository 等于连带搬动业务规则，风险与「不改判定行为」
的约束直接冲突。所以 **SQL 语句留在 core.py，只把执行动作收敛过来**。

这样切的好处是：以后若要换存储（换DB、换 ORM），只需要重写本层，
core.py 里那 40 条 SQL 各自带着它的业务上下文，原样不动。

**只允许标准库**：app/server.py 是零依赖回退服务端（start.bat 在 fastapi
不可用时启动它），本模块引入第三方依赖会让该路径直接失效。
因此不 import core —— 连接由调用方传入。
"""
from __future__ import annotations

import sqlite3


class Repository:
    """把「加锁 + 执行 + 取行」三件事绑在一起。

    lock 传 core.Store 的那把 RLock（可重入，因为 mark() 里包着 q()/x()）。
    本类不自己造锁 —— 锁的归属方是数据层的并发策略，不该由执行器决定。
    """

    __slots__ = ('con', 'lock')

    def __init__(self, con, lock):
        self.con = con
        self.lock = lock

    # ---- 执行 ----
    def fetchall(self, sql, args=()) -> list:
        """取多行，返回 sqlite3.Row 列表（保持原样：调用方要用 r['col'] 与 r.keys()）。"""
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def fetchone(self, sql, args=()):
        """取单行，没有则 None。"""
        with self.lock:
            return self.con.execute(sql, args).fetchone()

    def scalar(self, sql, args=()):
        """取第一行第一列（如 COUNT(*)）。调用方负责判断 None。"""
        with self.lock:
            row = self.con.execute(sql, args).fetchone()
            return row[0] if row else None

    def execute(self, sql, args=()) -> sqlite3.Cursor:
        """写操作（INSERT/UPDATE/DELETE），带 commit。返回 cursor 以便取 lastrowid。"""
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur

    def execute_nowait(self, sql, args=()) -> sqlite3.Cursor:
        """写操作，**不 commit**。

        存在的唯一理由：core.py 的 `Store._fsrs_save()` 原本就是裸
        `con.execute()` 不提交，靠随后 mark() 里的写操作顺带落盘。
        FSRS 排期在重构纪律里是「只许挪位置、不许改行为」的禁区，
        所以这里刻意提供不提交的通道，而不是借 execute() 的自动 commit
        顺手「修正」它 —— 那是行为变更，须先问过owner。

        新代码不要用这个，除非你在复刻一个「原本就不提交」的历史行为。
        """
        with self.lock:
            return self.con.execute(sql, args)

    def executemany(self, sql, seq) -> None:
        """批量写，带 commit。"""
        with self.lock:
            self.con.executemany(sql, seq)
            self.con.commit()

    def executescript(self, script: str) -> None:
        """DDL / 建表，带 commit。"""
        with self.lock:
            self.con.executescript(script)
            self.con.commit()

    # ---- 行转换 ----
    @staticmethod
    def to_dict(row) -> dict:
        """sqlite3.Row → 普通 dict。

        注意：core.Store 内部大量代码依赖 Row 的 `r.keys()` / `r['col']`
        （见 _pub_row 的 `if 'section_path' in r.keys()`），
        所以**返回 Row 而非 dict 的方法保持原样**，只有明确需要 dict 的调用方才转。
        """
        return dict(row) if row is not None else {}