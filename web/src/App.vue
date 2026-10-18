/* App.vue — 外壳：品牌头（模式徽章）+ 五页导航 + router-view + 页脚退出按钮 */
<template>
  <header>
    <div class="wrap">
      <div class="brand">
        <svg width="36" height="36" viewBox="0 0 108 108" aria-hidden="true">
          <rect width="108" height="108" rx="22" fill="#1b2530"/>
          <g fill="none" stroke="#e8a020" stroke-width="7" stroke-linecap="round">
            <path d="M54 34v-9M54 83v-9M34 54h-9M83 54h-9M40 40l-6.5-6.5M74.5 74.5L68 68M68 40l6.5-6.5M33.5 74.5L40 68"/>
            <circle cx="54" cy="54" r="17"/>
          </g>
          <path d="M47 54.5l5.5 5.5L63 49" fill="none" stroke="#ffffff" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <div>
          <h1>机械题库</h1>
          <div class="sub">本地刷题 · 艾宾浩斯复习 · 知识点卡片　——　选择自动判、问答自评</div>
        </div>
        <span class="mode-pill" :class="{ offline: modeOffline }">{{ modeText }}</span>
      </div>
    </div>
  </header>
  <nav>
    <div class="wrap row" style="gap:0">
      <router-link to="/">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 9.8V20h13V9.8"/></svg>今日</router-link>
      <router-link to="/quiz">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M4 5h16M4 12h10M4 19h7"/></svg>刷题</router-link>
      <router-link to="/review">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 3-6.7M3 4v5h5"/></svg>今日复习</router-link>
      <router-link to="/wrong">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>错题</router-link>
      <router-link to="/cards">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h7v14H4zM13 5h7v14h-7z"/></svg>知识点</router-link>
      <router-link to="/stats">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M5 20V10M12 20V4M19 20v-7"/></svg>进度</router-link>
      <router-link to="/excerpt">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h9l5 5v11H5z"/><path d="M14 4v5h5"/><path d="M8 13h8M8 17h4"/></svg>PDF 摘录</router-link>
    </div>
  </nav>
  <main class="wrap">
    <router-view />
    <footer>
      mech-qbank · 数据来自本地题库，只读源库 · 进度与自定义题库存在本机
      <button v-if="!STATIC" class="ghost" style="margin-left:10px;padding:3px 10px;font-size:12px;vertical-align:middle" @click="quit">退出程序</button>
    </footer>
  </main>
</template>

<script setup>
/* 在线模式：exe 是无终端窗口的（--windowed 打包），Ctrl+C 不可用，靠这个按钮退出服务。
   /api/v1/system/shutdown 服务端有 Host 白名单，外站页面调不动；静态模式（file://）没有服务，不显示。 */
import { computed, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api, quitApp, STATIC } from '@/api'
import { MODE_TEXT } from '@/util'

const stats = ref(null);
const modeText = computed(() => (stats.value && MODE_TEXT[stats.value.mode]) || '公开子集');
const modeOffline = computed(() => stats.value && stats.value.mode === 'static');

onMounted(async () => {
  const s = await api('/stats');
  if (s) stats.value = s;
});

async function quit() {
  try {
    await ElMessageBox.confirm(
      '退出 mech-qbank 服务？\n进度已自动保存，重新开始只需双击 mech-qbank.exe。',
      '退出确认', { type: 'warning', confirmButtonText: '退出', cancelButtonText: '取消' });
  } catch (e) { return; }   // 用户取消
  await quitApp();
  document.body.innerHTML =
    '<p style="text-align:center;padding:48px 16px;color:var(--muted);line-height:2">' +
    '服务已退出，可以关闭此标签页。<br>重新开始：双击 mech-qbank.exe</p>';
}
</script>
