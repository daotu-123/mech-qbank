# -*- coding: utf-8 -*-
r"""
mech-qbank · storage 层：一切文件读写的唯一出口

为什么单独抽这一层（2026-10-20 后端分层重构第 1 步）
--------------------------------------------------------------------------
core.py 原来在 10 处直接摸文件系统（notes.json / progress.json / checkin.json /
qbank.json / qbank.log），路径常量又是模块级可变名（测试要 monkeypatch 它们指向临时目录）。
于是「数据从哪来」这个问题散落在数据层各处，repository 与 service 都无法说自己不碰 I/O。

本模块把文件读写的三个动作收成一个窄接口：
    probe(p)      存在性判断（决定走 sqlite 还是 public 分支）
    read_json(p)  读 JSON，损坏返回默认值而不是抛
    write_json()  写 JSON
外加一个 append_text() 给日志用。

**只允许标准库**。app/server.py 是零第三方依赖的回退服务端（start.bat 在 fastapi
不可用时启动它），任何一层引入第三方依赖都会让这条回退路径直接失效。
本模块因此不 import core —— 路径由调用方传入，这样 core 的模块级常量
（DB / PROGJSON / CKJSON / NOTESJSON）仍是唯一真源，本模块不复制一份。
"""
from __future__ import annotations

import json
from pathlib import Path


def probe(p: Path) -> bool:
    """存在性判断。core.Store.__init__ 用它决定 mode='sqlite' 还是 'public'。"""
    return Path(p).exists()


def read_json(p: Path, default=None):
    """读 JSON 文件。**损坏或缺失一律返回 default，绝不抛**。

    这是照搬原行为，不是新设计：进度文件损坏不该阻止启动（core.py 原注释
    「进度文件损坏不影响启动」），备注文件同理。调用方拿 default 继续跑。
    """
    p = Path(p)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(p: Path, obj, indent=1) -> bool:
    """写 JSON。成功 True；OSError（磁盘满/只读/被占用）False。

    原实现是各自 try/except OSError 后降级（备注写不了就当次运行有效，进度写不了
    只提醒一次不打断刷题）。这里统一返回布尔，由调用方决定怎么降级——
    两处的降级策略不同，不该在storage 层强行统一。
    """
    try:
        Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=indent), encoding='utf-8')
        return True
    except OSError:
        return False


def read_text_bytes(p: Path) -> bytes:
    """静态资源读字节（server.py 发index.html / /static/* 用）。失败抛 OSError 给调用方。"""
    return Path(p).read_bytes()


def append_text(p: str, text: str) -> bool:
    """日志追加（frozen 打包后 sys.stdout 为 None，排障全靠 exe 同级 qbank.log）。"""
    try:
        with open(p, 'a', encoding='utf-8') as f:
            f.write(text)
        return True
    except Exception:
        return False