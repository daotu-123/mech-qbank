/* pdfdocs.js — PDF 文档域的前端通道（在线）
 *
 * ## 为什么单独一个文件，不塞进 api.js
 * ---------------------------------------------------------------------------
 * 三个原因，每一个都是硬约束逼出来的：
 *
 * 1. **上传必须要 XHR，不能用 fetch。** fetch 的 upload 进度在主流浏览器里
 *    还拿不到（只有 request.body 的 stream 形态能，而它不支持 FormData +
 *    不能设 Content-Type 边界）。要真实进度条只能上 XHR。
 *    （这里没有假进度条：条上显示的是 `upload.loaded / upload.total` 真值。）
 *
 * 2. **api.js 的 STATIC 分流对文档域不适用。** file:// 下没有服务器，
 *    文档域**整块不可用**——不是「降级成 localStorage 版本」。
 *    所以这里不做分流，而是显式返回 `OFFLINE` 让调用方显示「需要连接服务器」。
 *
 * 3. **零新依赖。** 只有 XMLHttpRequest + Promise + setTimeout。
 *
 * ## 状态词表（与服务端 app/docstore.py 一致，别自己造）
 * ---------------------------------------------------------------------------
 *   文档/任务状态：queued / running / succeeded / failed / interrupted
 *   失败细分（result_status 字段）：corrupt / encrypted / no_text
 *                                  timeout / worker_crashed / interrupted
 *
 * 前端**只按 status / result_status 分支**，不解析 error 文本——
 * 那是给人看的，不是给代码看的。
 */

/** file:// 下没有服务器，文档域整块不可用。 */
export const OFFLINE = { offline: true };

/** 服务端请求体上限，与 app/api.py 的 MAX_BODY_BYTES 同口径（10 MB）。 */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

const V1 = '/api/v1';

/** 任务状态 → 人话。顺序即展示顺序。 */
export const JOB_STATUS_TEXT = {
  queued: '排队中',
  running: '解析中',
  succeeded: '已完成',
  failed: '未通过',
  interrupted: '已中断',
};

/** result_status → 人话 + 可操作建议。
 *  **每种失败的建议必须不同**：全都写「解析失败」等于没写。 */
export const RESULT_TEXT = {
  ok: { label: '正常', tone: 'green', hint: '' },
  repaired_ok: {
    label: '已修复', tone: 'amber',
    hint: '文件有损坏但已尽力修复，个别字符可能不准，重要内容请对照原文件核对。',
  },
  no_text: {
    label: '无可用文字', tone: 'amber',
    hint: '这页是扫描图或纯图片，没有可抽取的文字层。可用「PDF 摘录」页手工粘贴录入，'
      + '或先用 OCR 工具转出文字层再上传。',
  },
  corrupt: {
    label: '文件损坏', tone: 'red',
    hint: 'PDF 结构不完整。多半是下载中断或文件被截断——重新下载一遍通常就好了，'
      + '不必重试上传同一个文件。',
  },
  encrypted: {
    label: '已加密', tone: 'red',
    hint: '文件带密码保护。本工具不解密，请先用阅读器另存一份无密码的副本再上传。',
  },
  timeout: {
    label: '解析超时', tone: 'red',
    hint: '解析超过 60 秒仍未结束。可能是超大文件或文件结构异常，可以先试着用阅读器'
      + '另存一份（重新生成结构通常更快）。',
  },
  worker_crashed: {
    label: '解析进程异常', tone: 'red',
    hint: '解析进程崩了（不是你的文件一定有问题）。可以重试一次；'
      + '若反复出现，请把文件名记下来反馈。',
  },
  busy: {
    label: '服务忙', tone: 'amber',
    hint: '已有一个文档在解析，稍等几秒再试。',
  },
  interrupted: {
    label: '已中断', tone: 'amber',
    hint: '解析过程中服务被关闭了。点「重新解析」再来一次。',
  },
};

/** 文档状态 → 徽章色调。 */
export function statusTone(status, resultStatus) {
  if (status === 'succeeded') {
    return (RESULT_TEXT[resultStatus] || RESULT_TEXT.ok).tone;
  }
  if (status === 'failed') return 'red';
  if (status === 'interrupted') return 'amber';
  return 'gray';    // queued / running
}

/** 失败细分的人话 + 建议；成功或未知 → 空。 */
export function resultHint(resultStatus) {
  const r = RESULT_TEXT[resultStatus];
  return r ? r.hint : '';
}

export function resultLabel(resultStatus) {
  const r = RESULT_TEXT[resultStatus];
  return r ? r.label : '';
}

/** 字节数 → 人话。 */
export function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}

/** ISO 时间 → 本地可读（去掉 T 与 Z 尾巴的原始串在离线时更好读，这里两种都兜住）。 */
export function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso).slice(0, 19).replace('T', ' ');
  const p = (x) => String(x).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

/* ---------------------------------------------------------------- 列表 / 详情 */

/** 文档列表（只取摘要，不含页文本）。失败抛错，调用方负责提示。 */
export async function listDocuments() {
  const r = await fetch(V1 + '/documents');
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return (await r.json()).items || [];
}

export async function getDocument(id) {
  const r = await fetch(V1 + '/documents/' + encodeURIComponent(id));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return (await r.json()).document;
}

/** 取第 page 页（从 1 起）的文本。 */
export async function getPage(id, page) {
  const r = await fetch(V1 + '/documents/' + encodeURIComponent(id)
    + '/pages?page=' + encodeURIComponent(page));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}

export async function deleteDocument(id) {
  const r = await fetch(V1 + '/documents/' + encodeURIComponent(id), { method: 'DELETE' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}

/** 查任务状态。**404 是正常结果**（job 表不跨重启），不当异常。 */
export async function getJob(jobId) {
  const r = await fetch(V1 + '/parse-jobs/' + encodeURIComponent(jobId));
  if (r.status === 404) return { job: null, gone: true };
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return { job: (await r.json()).job, gone: false };
}

/* ---------------------------------------------------------------- 上传（XHR） */

/**
 * 上传一个 PDF。**用 XHR 以拿到真实上传进度**，并支持取消。
 *
 * @param file      File 对象
 * @param onProgress (loaded, total) => void   真进度，不是假动画
 * @returns {promise, cancel}  promise 兑现 {document, job}（HTTP 202 的 body）
 *
 * 失败时 reject 一个带 `code` 的 Error：'too_large' / 'bad_request' / 'network' / 'http'
 * —— 上层据此给**不同**的提示（「文件太大」不是「上传失败」）。
 */
export function uploadDocument(file, onProgress) {
  let xhr = null;
  const promise = new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    xhr = new XMLHttpRequest();
    xhr.open('POST', V1 + '/documents', true);

    // ⚠️ 不设 Content-Type：XHR 会自动带 multipart 边界。
    //    手写 multipart/form-data 会漏掉 boundary 参数，服务端解析必然失败。
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
    };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (e) { /* 非 JSON */ }
      if (xhr.status === 202) { resolve(body); return; }
      const err = new Error(errText(body) || ('HTTP ' + xhr.status));
      err.code = xhr.status === 413 ? 'too_large'
        : (xhr.status >= 400 && xhr.status < 500 ? 'bad_request' : 'http');
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () => {
      const err = new Error('连接中断');
      err.code = 'network';
      reject(err);
    };
    xhr.ontimeout = () => {
      const err = new Error('上传超时');
      err.code = 'network';
      reject(err);
    };
    xhr.onabort = () => {
      const err = new Error('已取消');
      err.code = 'aborted';
      reject(err);
    };
    xhr.send(fd);
  });
  return { promise, cancel: () => { if (xhr) xhr.abort(); } };
}

/** 从统一错误体里取人话（app/errors.py：{"error":{code,message,request_id}}）。 */
function errText(body) {
  if (!body) return '';
  if (body.error && body.error.message) return body.error.message;
  if (body.detail) return String(body.detail);
  return '';
}

/* ---------------------------------------------------------------- 轮询 */

/**
 * 轮询解析任务直到有结论。
 *
 * @param jobId       任务 id
 * @param tick        (elapsedSec, job) => void   每轮回调，用于显示「已等待 N 秒」
 * @param maxSeconds  上限（默认 180）。到点返回 {timeout:true} 而不是死等。
 *
 * ## 为什么要「已等待 N 秒」而不是假进度条
 * 实测 frozen exe 首次解析冷启动 **7.28s**（onefile 解包 4.34s +
 * import pymupdf 1.78s + 抽取 0.46s）。这个时间是真实且可预期的，
 * 如实报出来比一根编出来的进度条有用得多——用户知道等 7 秒是正常的，
 * 才知道等 30 秒不正常。
 *
 * ## 为什么必须能重试
 * 网络抖一下就永久卡在「解析中」是最糟的失败形态：用户既不知道成没成，
 * 也走不了。所以**单次请求失败只累计一次退避，连续 N 次才放弃**，
 * 且放弃时明确告知「连接中断，可能已在服务端完成，请刷新列表」。
 */
export function pollJob(jobId, tick, maxSeconds = 180, fetchJob = getJob) {
  const start = Date.now();
  let fails = 0;
  const MAX_FAILS = 5;
  let stopped = false;

  const stop = () => { stopped = true; };

  const run = () => new Promise((resolve) => {
    if (stopped) return resolve({ stopped: true });
    const elapsed = (Date.now() - start) / 1000;
    if (elapsed > maxSeconds) {
      return resolve({ timeout: true, elapsed });
    }
    fetchJob(jobId).then((r) => {
      fails = 0;
      const job = r.job;
      if (tick) tick(elapsed, job);
      if (!job) {
        // 404：任务表不跨重启。要么真没了，要么服务重启过——都该如实报。
        return resolve({ gone: true, elapsed });
      }
      if (job.status === 'succeeded' || job.status === 'failed'
          || job.status === 'interrupted') {
        return resolve({ job, elapsed });
      }
      setTimeout(run, 900);
    }).catch(() => {
      fails += 1;
      if (tick) tick(elapsed, null);
      if (fails >= MAX_FAILS) return resolve({ offline: true, elapsed });
      // 退避重试，最长 3s。别用固定 900ms——网络真断了会连打 5 次。
      setTimeout(run, Math.min(900 * fails, 3000));
    });
  });

  // 把 jobId 挂在句柄上：调用方要能按 jobId 停掉某一个（列表里每行都有「不再跟踪」）
  return { jobId, promise: run, stop };
}
