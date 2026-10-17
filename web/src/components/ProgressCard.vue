/* ProgressCard.vue — 进度备份：导出 JSON（在线=下载文件，离线=可复制文本）/ 导入合并 */
<template>
  <div class="card"><b>进度备份</b>
    <div class="row" style="margin-top:8px">
      <button class="ghost" @click="exportProg">导出进度 JSON</button>
      <button class="ghost" @click="fileRef && fileRef.click()">导入进度 JSON</button>
      <input ref="fileRef" type="file" accept=".json,application/json" class="hide" @change="onFile">
    </div>
    <textarea v-if="STATIC" v-model="exportBox" readonly style="margin-top:10px;min-height:110px;font-family:Consolas,monospace;font-size:12px"
      placeholder="离线模式（APK）下，导出会显示在这里：长按全选复制即可备份；也可把备份 JSON 粘贴到这里，点下方按钮导入"></textarea>
    <button v-if="STATIC" class="ghost" style="margin-top:8px" @click="importFromText">从文本导入进度</button>
    <div class="meta" style="margin-top:6px">导出可用于备份；换电脑/换库后导入即合并恢复（不覆盖未出现的题）</div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus';
import { api, STATIC } from '@/api';
import { bump } from '@/store/bus';

const exportBox = ref('');
const fileRef = ref(null);

function parseProg(t) {
  const d = JSON.parse(t);
  const prog = d && typeof d === 'object' ? (d.progress || d) : null;
  if (!prog || typeof prog !== 'object') throw new Error('bad format');
  return prog;
}

async function exportProg() {
  const r = await api('/progress');
  if (!r) return;
  const n = Object.keys(r.progress || {}).length;
  if (STATIC) {   // 手机上没有方便的文件下载：改成可复制文本
    exportBox.value = JSON.stringify(r, null, 1);
    ElMessage.success('已生成 ' + n + ' 条进度文本，长按复制即可备份');
    return;
  }
  const blob = new Blob([JSON.stringify(r, null, 1)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'mech-qbank-progress-' + new Date().toISOString().slice(0, 10) + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
  ElMessage.success('已导出 ' + n + ' 条进度');
}

async function importProg(prog) {
  const r = await api('/progress', { progress: prog });
  if (r) { ElMessage.success('已导入 ' + r.imported + ' 条进度'); bump(); }
}

function onFile(e) {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  const rd = new FileReader();
  rd.onload = async () => {
    try { await importProg(parseProg(rd.result)); }
    catch (err) { ElMessage.error('文件解析失败：需要本工具导出的进度 JSON'); }
  };
  rd.onerror = () => ElMessage.error('文件读取失败');
  rd.readAsText(f);
}

async function importFromText() {
  const txt = (exportBox.value || '').trim();
  if (!txt) { ElMessage.error('先把导出的进度 JSON 粘贴进上面的文本框'); return; }
  try { await importProg(parseProg(txt)); exportBox.value = ''; }
  catch (err) { ElMessage.error('解析失败：需要本工具导出的进度 JSON'); }
}
</script>
