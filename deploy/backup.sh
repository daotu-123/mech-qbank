#!/usr/bin/env bash
# mech-qbank · 备份 / 恢复 / 校验脚本
#
# 用法：
#   ./backup.sh backup      # 备份（默认动作）
#   ./backup.sh restore <备份文件>
#   ./backup.sh verify      # 只校验最近一次备份
#   ./backup.sh list        # 列出所有备份
#
# ⚠️ 本文件里所有 ${...} 都是**占位符**，首次使用前必须手工替换
#    （或用环境变量传入，见下方「参数」段）。详见 deploy/README.md。
#
# ================================================================
# 备份什么（这是最容易漏的部分）
# ================================================================
# 数据的落盘位置取决于**模式**（core.py:11-19）：
#
#   全量模式（data/qbank.sqlite 存在）：
#     · 题库 + **全部学习进度** 都在 qbank.sqlite 里
#       （进度写进 progress 表，FSRS 状态写进 fsrs_state 表）
#     · notes.json     题目备注
#     · checkin.json   手动补签
#
#   公开模式（只有 data/public/qbank.json）：
#     · progress.json  进度
#     · notes.json     题目备注
#     · checkin.json   手动补签
#
#   两种模式都有：private/documents/  上传的 PDF 原文件
#
# ⚠️ **只备份 qbank.sqlite 是不够的** —— 备注和打卡在 JSON 文件里。
#    ⚠️ **只备份 JSON 也不够** —— 全量模式的进度在 sqlite 表里。
#
# ================================================================
# 为什么不能只靠 tar 打包 sqlite
# ================================================================
# sqlite 是 WAL 模式的服务端数据库，**正在写的时候直接 tar 会拿到撕裂的快照**。
# 所以本脚本用 `sqlite3 .backup`（在线热备份，WAL 安全）而不是 `cp`。
# 装了 sqlite3 命令行工具时用它；没装时退回到「停服务 + cp」（见 restore 段）。

set -euo pipefail

# ---------------------------------------------------------------- 参数
# 全部可以用环境变量覆盖，方便 systemd timer / crontab 调用：
: "${APP_DIR:=/opt/mech-qbank}"          # 代码目录
: "${DATA_DIR:=${APP_DIR}/data}"          # 数据目录（core.py 的 DATA）
: "${BACKUP_DIR:=/var/backups/mech-qbank}"  # 备份输出目录
: "${KEEP_DAYS:=14}"                      # 保留天数
: "${APP_PORT:=8640}"                     # 服务端口（restore 时用来停/起服务）
: "${SERVICE_NAME:=mech-qbank}"          # systemd 服务名

# 打包时的临时目录
WORK_DIR="$(mktemp -d)"

# ⚠️ 清理 trap **不能改变脚本的退出码**。
#   `trap 'rm -rf ...' EXIT` 里 rm 的退出码会成为整个脚本的退出码 ——
#   万一 rm 失败（权限、只读挂载、沙箱的删除保护），本来成功的操作会被报成失败。
#   正确做法：trap 里先存住 $?，清完再 exit 原来的码。
cleanup() {
	local rc=$?
	rm -rf "${WORK_DIR}" 2>/dev/null || true
	exit "${rc}"
}
trap cleanup EXIT

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { printf '[%s] 错误：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查
check_env() {
	[ -d "${APP_DIR}" ] || die "代码目录不存在：${APP_DIR}（改 APP_DIR 或 deploy/README.md 里的占位符）"
	[ -d "${DATA_DIR}" ] || die "数据目录不存在：${DATA_DIR}"
	command -v tar >/dev/null || die "缺少 tar"
}

# ---------------------------------------------------------------- 停/起服务
# 备份期间**不**停服务（热备份是安全的，见上）。
# 但 restore 必须停 —— 正在写的进程会覆盖恢复的文件。
stop_service() {
	if command -v systemctl >/dev/null && systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
		log "停止服务 ${SERVICE_NAME}"
		systemctl stop "${SERVICE_NAME}"
		SERVICE_WAS_RUNNING=1
	else
		SERVICE_WAS_RUNNING=0
		log "服务未在运行（跳过停止）"
	fi
}

start_service() {
	if [ "${SERVICE_WAS_RUNNING}" = "1" ]; then
		log "启动服务 ${SERVICE_NAME}"
		systemctl start "${SERVICE_NAME}"
	fi
}

# ---------------------------------------------------------------- 备份
do_backup() {
	check_env
	mkdir -p "${BACKUP_DIR}"

	local stamp
	stamp="$(date '+%Y%m%d-%H%M%S')"
	local out="${BACKUP_DIR}/mech-qbank-${stamp}.tar.gz"
	local meta="${BACKUP_DIR}/mech-qbank-${stamp}.meta"

	log "备份 ${DATA_DIR} -> ${out}"

	# 1) sqlite：在线热备份（WAL 安全），不能用 cp
	if [ -f "${DATA_DIR}/qbank.sqlite" ]; then
		if command -v sqlite3 >/dev/null; then
			log "  热备份 sqlite（sqlite3 .backup）"
			sqlite3 "${DATA_DIR}/qbank.sqlite" ".backup '${WORK_DIR}/data/qbank.sqlite'" \
				|| die "sqlite 热备份失败"
		else
			log "  ⚠ 没装 sqlite3 命令行工具 —— 退回「停服务 + cp」（服务会短暂不可用）"
			stop_service
			mkdir -p "${WORK_DIR}/data"
			cp "${DATA_DIR}/qbank.sqlite" "${WORK_DIR}/data/qbank.sqlite"
			# WAL / SHM 也要一起带走，否则恢复后可能读到旧数据
			for ext in -wal -shm; do
				[ -f "${DATA_DIR}/qbank.sqlite${ext}" ] && cp "${DATA_DIR}/qbank.sqlite${ext}" "${WORK_DIR}/data/qbank.sqlite${ext}"
			done
			start_service
		fi
	else
		log "  （无 qbank.sqlite —— 公开模式，进度在 progress.json）"
	fi

	# 2) JSON 文件（进度 / 备注 / 打卡）
	mkdir -p "${WORK_DIR}/data"
	for f in progress.json notes.json checkin.json; do
		if [ -f "${DATA_DIR}/${f}" ]; then
			cp "${DATA_DIR}/${f}" "${WORK_DIR}/data/${f}"
			log "  复制 ${f}"
		fi
	done

	# 3) 上传的 PDF 原文件
	#
	# ⚠️ **必须用 `cp -r src/. dst/` 的形式**（注意 src 后面那个 `/.`）。
	#   `cp -r src dst` 在 dst **不存在**时会把 src 整个变成 dst；
	#   在 dst **已存在**时则变成 dst/src —— 于是出现 documents/documents/ 双层。
	#   恢复时再套一层就变成 documents/documents/documents/，文件越还原越深。
	#   这是实测抓到的真 bug（2026-10-21 本地跑 backup.sh 时发现）。
	if [ -d "${DATA_DIR}/private/documents" ]; then
		mkdir -p "${WORK_DIR}/data/private/documents"
		cp -r "${DATA_DIR}/private/documents/." "${WORK_DIR}/data/private/documents/"
		log "  复制 private/documents/（上传的 PDF）"
	fi

	# 4) 打包
	mkdir -p "${WORK_DIR}/data"
	tar -czf "${out}" -C "${WORK_DIR}" data
	log "打包完成：${out}（$(du -h "${out}" | cut -f1 || true)）"

	# 5) 元信息（便于事后判断这份备份是什么）
	{
		echo "created_at=$(date '+%Y-%m-%d %H:%M:%S')"
		echo "host=$(hostname)"
		echo "data_dir=${DATA_DIR}"
		echo "mode=$(if [ -f "${DATA_DIR}/qbank.sqlite" ]; then echo sqlite; else echo public; fi)"
		echo "sha256=$(sha256sum "${out}" | cut -d' ' -f1 || true)"
	} > "${meta}"

	# 6) 立刻校验（备份完不做校验等于没备份）
	if do_verify "${out}" "${meta}"; then
		log "✓ 备份已校验通过"
	else
		die "备份校验失败 —— 这份备份不能用"
	fi

	# 7) 清理过期备份
	prune_old
	log "备份完成"
}

# ---------------------------------------------------------------- 校验
do_verify() {
	local archive="${1:-}"
	local meta="${2:-}"
	[ -n "${archive}" ] || archive="$(ls -1t "${BACKUP_DIR}"/mech-qbank-*.tar.gz 2>/dev/null | head -1)"
	[ -n "${archive}" ] && [ -f "${archive}" ] || { echo "没有可校验的备份" >&2; return 1; }

	log "校验 ${archive}"

	# a) tar 完整性
	tar -tzf "${archive}" >/dev/null || { echo "✗ tar 包损坏" >&2; return 1; }

	# b) sha256（若有元信息）
	if [ -n "${meta}" ] && [ -f "${meta}" ]; then
		local want got
		want="$(grep '^sha256=' "${meta}" | cut -d= -f2 || true)"
		got="$(sha256sum "${archive}" | cut -d' ' -f1 || true)"
		if [ "${want}" != "${got}" ]; then
			echo "✗ sha256 不匹配：期望 ${want}，实际 ${got}" >&2
			return 1
		fi
		log "  sha256 一致"
	fi

	# c) 关键文件在不在
	local missing=0
	for f in data/notes.json data/checkin.json; do
		if ! tar -tzf "${archive}" | grep -qx "${f}"; then
			log "  ⚠ 备份里没有 ${f}（该文件可能本来就不存在）"
		fi
	done
	# 进度：sqlite（表里）或 progress.json（文件）至少要有一个
	if ! tar -tzf "${archive}" | grep -qx 'data/progress.json' \
	   && ! tar -tzf "${archive}" | grep -qx 'data/qbank.sqlite'; then
		echo "✗ 备份里既没有 progress.json 也没有 qbank.sqlite —— 进度没被备份！" >&2
		missing=1
	fi
	[ "${missing}" = "0" ] || return 1

	# d) sqlite 能不能打开（热备份的完整性）
	if tar -tzf "${archive}" | grep -qx 'data/qbank.sqlite'; then
		tar -xzf "${archive}" -C "${WORK_DIR}" data/qbank.sqlite
		if command -v sqlite3 >/dev/null; then
			local integrity
			integrity="$(sqlite3 "${WORK_DIR}/data/qbank.sqlite" 'PRAGMA integrity_check;' 2>&1 | head -1 || true)"
			[ -n "${integrity}" ] || integrity="?"
			if [ "${integrity}" != "ok" ]; then
				echo "✗ sqlite integrity_check 失败：${integrity}" >&2
				return 1
			fi
			log "  sqlite integrity_check = ok"
			local n
			n="$(sqlite3 "${WORK_DIR}/data/qbank.sqlite" 'SELECT COUNT(*) FROM progress;' 2>/dev/null || echo '?')"
			log "  progress 表行数 = ${n}"
		else
			log "  （没装 sqlite3，跳过 integrity_check）"
		fi
	fi

	# e) JSON 能不能解析
	for j in progress.json notes.json checkin.json; do
		if tar -tzf "${archive}" | grep -qx "data/${j}"; then
			tar -xzf "${archive}" -C "${WORK_DIR}" "data/${j}"
			if ! python3 -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" "${WORK_DIR}/data/${j}" 2>/dev/null; then
				echo "✗ ${j} 不是合法 JSON" >&2
				return 1
			fi
			log "  ${j} JSON 合法"
		fi
	done

	log "  校验通过"
	return 0
}

# ---------------------------------------------------------------- 恢复
do_restore() {
	local archive="${1:-}"
	[ -n "${archive}" ] || die "用法：$0 restore <备份文件>"
	[ -f "${archive}" ] || die "备份文件不存在：${archive}"

	log "恢复前先校验备份"
	do_verify "${archive}" || die "备份校验不通过 —— 拒绝恢复（不拿未经验证的数据覆盖现网）"

	log "恢复 ${archive} -> ${DATA_DIR}"
	stop_service

	# 先把现有数据挪走（不删！出问题还能回来）
	local stash="${WORK_DIR}/current-data"
	mkdir -p "${stash}"
	if [ -d "${DATA_DIR}" ]; then
		cp -r "${DATA_DIR}/." "${stash}/" 2>/dev/null || true
		log "  现网数据已暂存到 ${stash}（恢复后如有问题可从这里找回）"
	fi

	tar -xzf "${archive}" -C "${WORK_DIR}"
	[ -d "${WORK_DIR}/data" ] || die "备份结构异常：里面没有 data/"

	# 覆盖恢复（保留 private/ 下不在备份里的其它内容）
	mkdir -p "${DATA_DIR}"
	for f in qbank.sqlite qbank.sqlite-wal qbank.sqlite-shm progress.json notes.json checkin.json; do
		if [ -f "${WORK_DIR}/data/${f}" ]; then
			cp "${WORK_DIR}/data/${f}" "${DATA_DIR}/${f}"
			log "  恢复 ${f}"
		fi
	done
	if [ -d "${WORK_DIR}/data/private/documents" ]; then
		mkdir -p "${DATA_DIR}/private/documents"
		# ⚠️ 同样是 `src/.` 形式 —— 否则会套出 documents/documents/ 双层
		#   （实测踩到，见备份段注释）。
		cp -r "${WORK_DIR}/data/private/documents/." "${DATA_DIR}/private/documents/"
		log "  恢复 private/documents/"

		# 保险：套层是**静默**的（cp 不报错），所以显式查一次。
		# 双层长这样：private/documents/documents/
		if [ -d "${DATA_DIR}/private/documents/documents" ]; then
			die "恢复后出现 documents/documents/ 双层目录 —— 备份包结构异常，已中止。请检查备份包内容。"
		fi
	fi

	# 权限：恢复出来的文件要归服务用户
	if [ -n "${SERVICE_USER:-}" ]; then
		chown -R "${SERVICE_USER}:${SERVICE_GROUP:-${SERVICE_USER}}" "${DATA_DIR}" 2>/dev/null || true
	fi

	start_service
	log "✓ 恢复完成。验证："
	log "   curl -s http://127.0.0.1:${APP_PORT}/health/ready"
	log "   然后登录看进度/备注/打卡是否回来了"
}

# ---------------------------------------------------------------- 清理
prune_old() {
	local n
	n="$(find "${BACKUP_DIR}" -maxdepth 1 -name 'mech-qbank-*.tar.gz' -mtime "+${KEEP_DAYS}" 2>/dev/null | wc -l || true)"
	[ -n "${n}" ] || n=0
	if [ "${n}" -gt 0 ]; then
		log "清理 ${n} 份超过 ${KEEP_DAYS} 天的旧备份"
		find "${BACKUP_DIR}" -maxdepth 1 -name 'mech-qbank-*.tar.gz' -mtime "+${KEEP_DAYS}" -delete
		find "${BACKUP_DIR}" -maxdepth 1 -name 'mech-qbank-*.meta' -mtime "+${KEEP_DAYS}" -delete
	fi
}

# ---------------------------------------------------------------- 列表
do_list() {
	if [ ! -d "${BACKUP_DIR}" ]; then
		die "备份目录不存在：${BACKUP_DIR}"
	fi
	printf '%-32s %10s  %s\n' "备份文件" "大小" "创建时间"
	local f
	for f in $(ls -1t "${BACKUP_DIR}"/mech-qbank-*.tar.gz 2>/dev/null); do
		local ts size
		# ⚠️ `stat` 的参数在 GNU coreutils 与 BSD（macOS）上不同：
		#   GNU 是 `stat -c '%y'`，BSD 是 `stat -f '%Sm'`。
		#   两个都试、并且**不让失败中断脚本**（`set -e` 下命令替换里的失败
		#   会让整个脚本退出 —— 2026-10-21 本地实测踩到）。
		ts="$(stat -c '%y' "${f}" 2>/dev/null | cut -d. -f1 || true)"
		[ -n "${ts}" ] || ts="$(stat -f '%Sm' "${f}" 2>/dev/null || true)"
		[ -n "${ts}" ] || ts="?"
		size="$(du -h "${f}" 2>/dev/null | cut -f1 || true)"
		[ -n "${size}" ] || size="?"
		printf '%-32s %10s  %s\n' "$(basename "${f}")" "${size}" "${ts}"
	done
}

# ---------------------------------------------------------------- 入口
case "${1:-backup}" in
	backup)  do_backup ;;
	restore) shift; do_restore "${1:-}" ;;
	verify)  do_verify "${2:-}" ;;
	list)    do_list ;;
	*)       echo "用法：$0 {backup|restore <文件>|verify|list}" >&2; exit 2 ;;
esac
