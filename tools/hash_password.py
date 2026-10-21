#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/hash_password.py — 生成 mech-qbank 的口令哈希（scrypt）。

用法：
    .venv-build/Scripts/python.exe tools/hash_password.py "你的口令"
    .venv-build/Scripts/python.exe tools/hash_password.py            # 交互输入（不回显）

输出形如：
    scrypt$32768$8$1$<salt_b64>$<hash_b64>

把它填进环境变量：
    set QBANK_PASSWORD_HASH=scrypt$32768$8$1$...        (Windows cmd)
    export QBANK_PASSWORD_HASH='scrypt$32768$8$1$...'    (bash)

⚠️ **口令本身不会被写进任何文件**，本脚本只打印哈希。
   哈希可以进版本库（它不是明文），但**明文口令绝对不要**写进
   任何被提交的文件或日志。

⚠️ 交互输入用 getpass，不回显、不进历史。
"""

from __future__ import annotations

import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

from security import hash_password, verify_password  # noqa: E402


def main(argv) -> int:
    if len(argv) > 1:
        password = argv[1]
        interactive = False
    else:
        try:
            password = getpass.getpass('口令（不回显）: ')
            again = getpass.getpass('再输一次: ')
        except (EOFError, KeyboardInterrupt):
            print('\n已取消')
            return 1
        if password != again:
            print('两次输入不一致')
            return 1
        interactive = True

    if not password:
        print('口令不能为空')
        return 1

    hashed = hash_password(password)
    # 自检：刚算出来的哈希必须能验过（防止编码/格式写错）。
    if not verify_password(password, hashed):
        print('自检失败：哈希无法验证（这是 bug，请报告）')
        return 1

    print()
    print('QBANK_PASSWORD_HASH=' + hashed)
    print()
    if not interactive:
        print('⚠️ 你是用命令行参数传的口令，它会留在 shell 历史里。')
        print('   更安全的做法：不带参数运行本脚本，交互输入（不回显）。')
    print('放到公网模式的环境变量里即可，例如 Caddy 的 systemd unit：')
    print('  Environment=QBANK_PASSWORD_HASH=' + hashed[:40] + '...')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
