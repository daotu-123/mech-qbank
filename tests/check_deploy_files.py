#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""deploy/ 目录里部署工件的静态校验。

## 为什么需要这个文件
--------------------------------------------------------------------------
部署工件是**配置**，不会随代码改动被测试覆盖 —— 一个手误的 unit 文件
要到真部署那一刻才会炸，而那时候是半夜、VPS 上、主人刚买完域名。

所以本文件把「已知的 systemd / Caddy 陷阱」变成可复跑的断言。

## 为什么自己写而不直接用 systemd-analyze
--------------------------------------------------------------------------
本机是 Windows，没有 systemd-analyze；而且 systemd-analyze verify
在**模板**（带 ${...} 占位符）上跑不了 —— 占位符路径不存在会直接报错。
所以这里做的是**静态规则检查**：段、键、必需项、占位符一致性。

真部署时仍然应该跑一次：
    sudo systemd-analyze verify /etc/systemd/system/mech-qbank.service
    sudo caddy validate --config /etc/caddy/Caddyfile

## 覆盖
--------------------------------------------------------------------------
a. systemd unit：段顺序、键归属、必需项齐全、非 root、占位符表
b. Caddyfile：占位符表、关键指令存在、无重复指令、不记 Authorization
c. 三个文件的占位符**互相一致**（域名/端口不能两边不一样）
d. backup.sh：shell 语法（`bash -n`）、子命令、数据文件覆盖、退出码语义

## 跑法
--------------------------------------------------------------------------
    .venv-build/Scripts/python.exe tests/check_deploy_files.py
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DEPLOY = os.path.join(_ROOT, 'deploy')
_UNIT = os.path.join(_DEPLOY, 'mech-qbank.service')
_CADDY = os.path.join(_DEPLOY, 'Caddyfile')
_BACKUP = os.path.join(_DEPLOY, 'backup.sh')
_README = os.path.join(_DEPLOY, 'README.md')
_GITATTR = os.path.join(_ROOT, '.gitattributes')

# ---------------------------------------------------------------- systemd 规则
UNIT_SECTIONS = ('Unit', 'Service', 'Install')

#: 各段的合法键。systemd 会静默忽略不认识的键（只打一条 warning），
#: 所以「写错段」这种错误在真部署时几乎看不出来 —— 这就是本文件存在的意义。
UNIT_KEYS = {
    'Unit': {
        'Description', 'Documentation', 'After', 'Wants', 'Requires',
        'StartLimitIntervalSec', 'StartLimitBurst', 'StartLimitAction',
        'RefuseManualStart', 'ConditionPathExists', 'Before',
    },
    'Service': {
        'Type', 'User', 'Group', 'WorkingDirectory', 'ExecStart', 'ExecStartPre',
        'Environment', 'EnvironmentFile', 'Restart', 'RestartSec',
        'MemoryMax', 'MemoryDenyWriteExecute', 'TasksMax', 'LimitNOFILE',
        'NoNewPrivileges', 'PrivateTmp', 'ProtectSystem', 'ProtectHome',
        'ProtectKernelTunables', 'ProtectKernelModules', 'ProtectControlGroups',
        'RestrictNamespaces', 'RestrictRealtime', 'RestrictSUIDSGID',
        'LockPersonality', 'SystemCallFilter', 'SystemCallArchitectures',
        'ReadWritePaths', 'StandardOutput', 'StandardError', 'SyslogIdentifier',
        'KillMode', 'TimeoutStartSec', 'UMask',
    },
    'Install': {'WantedBy', 'RequiredBy', 'Also', 'Alias'},
}

#: 这些键**必须**在 [Unit] 段。写在 [Service] 段会被静默忽略，
#: 结果是「配置错误导致秒退」时陷入无限重启。
MUST_BE_IN_UNIT = {'StartLimitIntervalSec', 'StartLimitBurst', 'StartLimitAction'}

#: 部署到 VPS 必须有的项。少一条要么起不来、要么裸奔。
UNIT_REQUIRED = {
    'User=': '运行身份（不能是 root）',
    'WorkingDirectory=': '工作目录',
    'ExecStart=': '启动命令',
    'Environment="QBANK_PUBLIC=1"': '公网模式开关',
    'Environment="QBANK_ALLOWED_HOSTS=': 'Host 白名单',
    'Environment="QBANK_PASSWORD_HASH=': '口令哈希',
    'Environment="QBANK_TRUSTED_PROXIES=': '受信反代（漏了限流会被打穿）',
    'Environment="QBANK_NO_BROWSER=1"': '关掉自动开浏览器（headless 必须）',
    'Environment="QBANK_BIND=127.0.0.1"': '只绑回环（由反代挡在前面）',
    'Restart=': '重启策略',
    'ReadWritePaths=': '可写路径（ProtectSystem=strict 下必需）',
    'WantedBy=': '开机自启',
}

# ---------------------------------------------------------------- Caddy 规则
#: Caddyfile 里同一 site 块内重复的指令会被 Caddy 拒绝
#: （"duplicate directive"）。2026-10-21 初版就写了两个 request_body 块。
CADDY_REQUIRED = (
    'reverse_proxy',
    'request_body',
    'log',
)

#: 两边必须一致的占位符（域名/端口不一致 = 403 或 502）
SHARED_PLACEHOLDERS = ('DOMAIN', 'APP_PORT')


def _read(path: str) -> str:
    with io.open(path, encoding='utf-8') as fh:
        return fh.read()


def _placeholders(text: str) -> set:
    return set(re.findall(r'\$\{([A-Z_]+)\}', text))


def _which(cmd):
    for d in os.environ.get('PATH', '').split(os.pathsep):
        p = os.path.join(d, cmd)
        if os.path.exists(p):
            return p
    return None


class DeployFilesTest(unittest.TestCase):

    def setUp(self):
        for p in (_UNIT, _CADDY, _BACKUP, _README):
            self.assertTrue(os.path.exists(p), '缺少文件：%s' % p)

    # ---------------------------------------------------------------- a
    def test_unit_sections_and_keys(self):
        """a. systemd unit：段顺序正确、键归属正确。"""
        text = _read(_UNIT)
        section = None
        order = []
        for i, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith('#') or line.startswith(';'):
                continue
            m = re.match(r'^\[([^\]]+)\]$', line)
            if m:
                section = m.group(1)
                order.append(section)
                self.assertIn(section, UNIT_SECTIONS,
                              '第 %d 行：未知 section [%s]' % (i, section))
                continue
            self.assertIsNotNone(section, '第 %d 行：不在任何 section 里' % i)
            self.assertIn('=', line, '第 %d 行：不是 key=value：%s' % (i, line))
            key = line.split('=', 1)[0].strip()
            if key in MUST_BE_IN_UNIT:
                self.assertEqual(section, 'Unit',
                                 '第 %d 行：%s 必须写在 [Unit] 段，写在 [%s] 段会被静默忽略'
                                 % (i, key, section))
            else:
                self.assertIn(key, UNIT_KEYS[section],
                              '第 %d 行：[%s] 段里没有 %s 这个键（systemd 会忽略）'
                              % (i, section, key))
        self.assertEqual(order[0], 'Unit', '[Unit] 必须是第一个 section')
        for s in UNIT_SECTIONS:
            self.assertIn(s, order, '缺少 section [%s]' % s)

    def test_unit_required_items(self):
        """a. systemd unit：部署必需项一个都不能少。"""
        text = _read(_UNIT)
        for key, desc in UNIT_REQUIRED.items():
            self.assertIn(key, text, '缺少必需项：%s（%s）' % (key, desc))

    def test_unit_not_root(self):
        """a. systemd unit 不能用 root 跑。"""
        m = re.search(r'^User=(\S+)', _read(_UNIT), re.M)
        self.assertIsNotNone(m, '没有 User= 行')
        self.assertNotIn(m.group(1).strip('"\''), ('root', '0'),
                         'User= 是 root —— 不要用 root 跑服务')

    def test_unit_readwritepaths_has_dash_prefix(self):
        """a. ReadWritePaths 指向不存在的路径会**直接让单元启动失败**。

        systemd 的行为：路径没有 `-` 前缀且不存在 → 挂载命名空间设置失败 →
        单元起不来（报 "Failed to set up mount namespacing"）。
        带 `-` 表示「不存在就忽略」。
        """
        for m in re.finditer(r'^ReadWritePaths=(.+)$', _read(_UNIT), re.M):
            self.assertTrue(m.group(1).strip().startswith('-'),
                            'ReadWritePaths=%s 建议加 `-` 前缀' % m.group(1).strip())

    def test_unit_placeholder_table_complete(self):
        """a. unit 里用到的每个占位符，README 的表里都要有。"""
        unit_ph = _placeholders(_read(_UNIT))
        readme = _read(_README)
        for ph in sorted(unit_ph):
            self.assertIn('${%s}' % ph, readme,
                          '占位符 ${%s} 没在 deploy/README.md 的表里列出来' % ph)

    # ---------------------------------------------------------------- b
    def test_caddyfile_no_duplicate_directive(self):
        """b. Caddyfile：同一 site 块内不能有重复指令（Caddy 会拒绝加载）。"""
        text = _read(_CADDY)
        # 只取 site 块（第一个 { 之后到配对的 }）
        start = text.find('{', text.find('${DOMAIN}'))
        end = text.find('\n}', start)
        block = text[start:end]
        seen = set()
        for line in block.splitlines():
            s = line.strip()
            if not s or s.startswith('#') or s.startswith('}'):
                continue
            # 只看块的第一层指令（缩进为 1 个 tab）
            if not line.startswith('\t') or line.startswith('\t\t'):
                continue
            key = s.split()[0]
            self.assertNotIn(key, seen,
                             'Caddyfile 里有重复指令 %s（Caddy 会报 duplicate directive）' % key)
            seen.add(key)

    def test_caddyfile_required_directives(self):
        """b. Caddyfile：反代 / 大文件放行 / 日志 三样都要有。"""
        text = _read(_CADDY)
        for d in CADDY_REQUIRED:
            self.assertIn(d, text, 'Caddyfile 缺少 %s 指令' % d)

    def test_caddyfile_does_not_log_authorization(self):
        """b. Caddyfile **不许**记 Authorization 头（令牌会进日志）。"""
        text = _read(_CADDY)
        # 只查非注释行
        code = '\n'.join(l for l in text.splitlines() if not l.strip().startswith('#'))
        self.assertNotIn('Authorization', code,
                         'Caddyfile 里出现了 Authorization —— 令牌会被记进日志')

    # ---------------------------------------------------------------- c
    def test_shared_placeholders_match(self):
        """c. Caddyfile 与 unit 的共享占位符必须一致（域名/端口）。"""
        caddy_ph = _placeholders(_read(_CADDY))
        unit_ph = _placeholders(_read(_UNIT))
        for ph in SHARED_PLACEHOLDERS:
            self.assertIn(ph, caddy_ph, 'Caddyfile 缺少占位符 ${%s}' % ph)
            self.assertIn(ph, unit_ph, 'unit 缺少占位符 ${%s}' % ph)

    def test_readme_has_placeholder_table(self):
        """c. README 必须有「你需要改哪几行」的表。"""
        readme = _read(_README)
        self.assertIn('你需要改哪几行', readme)
        for ph in sorted(_placeholders(_read(_UNIT)) | _placeholders(_read(_CADDY))):
            self.assertIn(ph, readme, 'README 的表里没提占位符 %s' % ph)

    def test_readme_covers_three_gaps(self):
        """c. README 必须讲清三个部署缺口（owner 明确要求的）。"""
        readme = _read(_README)
        self.assertIn('跑不了', readme)                   # 缺口1：exe 是 Windows 二进制
        self.assertIn('requirements-server.txt', readme)  # 缺口2：pymupdf
        self.assertIn('QBANK_NO_BROWSER', readme)          # 缺口3：headless
        self.assertIn('webbrowser', readme)

    # ---------------------------------------------------------------- d
    def test_backup_shell_syntax(self):
        """d. backup.sh：shell 语法必须正确（`bash -n`）。"""
        if not _which('bash'):
            self.skipTest('没有 bash')
        proc = subprocess.run(['bash', '-n', _BACKUP], capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         'backup.sh 语法错误：%s' % proc.stderr.decode('utf-8', 'replace')[:400])

    def test_backup_subcommands(self):
        """d. backup.sh：四个子命令都要在。"""
        text = _read(_BACKUP)
        for cmd in ('backup', 'restore', 'verify', 'list'):
            self.assertIn('\t%s)' % cmd, text, 'backup.sh 少了 %s 子命令' % cmd)

    def test_backup_uses_src_dot_form(self):
        """d. backup.sh 的目录复制必须用 `src/.` 形式。

        `cp -r src dst` 在 dst **已存在**时会变成 dst/src → 出现
        documents/documents/ 双层；恢复时再套一层。
        这是 2026-10-21 本地实跑 backup.sh 时抓到的真 bug。
        """
        text = _read(_BACKUP)
        self.assertIn('documents/.', text,
                      'backup.sh 里的目录复制没用 `src/.` 形式（会套出 documents/documents/）')
        self.assertNotIn('cp -r "${DATA_DIR}/private/documents" ', text,
                         'backup.sh 里还有会套层的 cp 写法')
        self.assertNotIn('cp -r "${WORK_DIR}/data/private/documents" ', text,
                         'backup.sh 里还有会套层的 cp 写法')

    def test_backup_covers_all_data_files(self):
        """d. backup.sh 必须备全：sqlite + 三个 JSON + 上传的 PDF。"""
        text = _read(_BACKUP)
        for f in ('qbank.sqlite', 'progress.json', 'notes.json', 'checkin.json'):
            self.assertIn(f, text, 'backup.sh 没有备份 %s' % f)
        self.assertIn('private/documents', text, 'backup.sh 没有备份上传的 PDF')

    def test_backup_exit_code_preserved(self):
        """d. 清理用的 trap 不能改变脚本的退出码。

        `trap 'rm -rf ...' EXIT` 里 rm 的退出码会成为整个脚本的退出码 ——
        万一 rm 失败（权限、只读挂载），本来成功的操作会被报成失败。
        """
        text = _read(_BACKUP)
        self.assertIn('local rc=$?', text,
                      'backup.sh 的 EXIT trap 没保存退出码')
        self.assertIn('exit "${rc}"', text,
                      'backup.sh 的 EXIT trap 没还原退出码')

    # ---------------------------------------------------------------- e
    def test_gitattributes_forces_lf_for_deploy_files(self):
        """e. `.gitattributes` 必须强制部署工件用 LF。

        ⚠️ 这是**真部署阻断**，2026-10-21 实测发现：
        本机 `core.autocrlf=true`，clone 出来的 `deploy/backup.sh` 有 350 个 CR 字节，
        shebang 变成 `#!/usr/bin/env bash\\r` → Linux 上直接执行报
        `bad interpreter: /usr/bin/env: 'bash\\r'`。
        同样的问题会坑 systemd unit（\\r 粘在 Environment 的值里）与 Caddyfile。

        这条断言守的就是 `.gitattributes` 里那几行 `eol=lf`。
        """
        self.assertTrue(os.path.exists(_GITATTR), '缺少 .gitattributes')
        attrs = _read(_GITATTR)
        for path in ('deploy/backup.sh', 'deploy/Caddyfile'):
            self.assertRegex(attrs, r'(?m)^%s\s+text\s+eol=lf\s*$' % re.escape(path),
                             '.gitattributes 缺 %s 的 eol=lf 规则（Linux 上会坏）' % path)
        self.assertRegex(attrs, r'(?m)^deploy/\*\.service\s+text\s+eol=lf\s*$',
                         '.gitattributes 缺 deploy/*.service 的 eol=lf 规则')
        # 通配规则也要在（将来新增 .sh 不该再踩同一个坑）
        self.assertRegex(attrs, r'(?m)^\*\.sh\s+text\s+eol=lf\s*$',
                         '.gitattributes 缺 *.sh 的 eol=lf 规则')

    def test_deploy_files_currently_have_no_cr(self):
        """e. 仓库里的部署工件本身不能含 CR（工作区里就该是 LF）。

        这是对上面那条的**直接**检查：.gitattributes 规则生效与否取决于
        git 的 checkout 行为，而这条直接看工作区文件的内容。
        """
        for p in (_UNIT, _CADDY, _BACKUP):
            with open(p, 'rb') as fh:
                blob = fh.read()
            self.assertNotIn(b'\r\n', blob,
                             '%s 里有 CRLF —— 在 Linux 上会出问题' % os.path.basename(p))
            self.assertNotIn(b'\r', blob,
                             '%s 里有 CR 字符 —— 在 Linux 上会出问题' % os.path.basename(p))

    def test_backup_shebang_has_no_cr(self):
        """e. backup.sh 的 shebang 必须是干净的 LF。

        `#!/usr/bin/env bash\\r` 在 Linux 上 → `bad interpreter`。
        """
        with open(_BACKUP, 'rb') as fh:
            first = fh.readline()
        self.assertTrue(first.startswith(b'#!'),
                        'backup.sh 第一行不是 shebang：%r' % first[:40])
        self.assertNotIn(b'\r', first,
                         'backup.sh 的 shebang 带 CR —— Linux 上会报 bad interpreter')


if __name__ == '__main__':
    unittest.main(verbosity=2)
