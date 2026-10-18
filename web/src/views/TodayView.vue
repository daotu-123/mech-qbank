/* TodayView.vue — 今日学习台：只回答「今天做什么」
 *
 * 设计依据（竞品调研）：
 *   · RemNote Exam Scheduler：有截止日期才有紧迫感
 *   · Logseq：用「日」组织而非「功能」组织
 *   · Homepage 等开源仪表盘：每个卡只放 1–3 个数字，详情点进去
 *   · 牛客/考试星：组卷式专项练习 + 每日任务
 *
 * 这个页面替换原来的「刷题」作为首页（刷题移到 /quiz）。
 * 它不做筛选、不做组卷，只给三个明确入口 + 一个数字概览。
 */
<template>
  <section>
    <!-- 今日一句话 -->
    <div class="card today-hero">
      <div class="today-line">
        <b>{{ headline }}</b>
      </div>
      <div class="today-meta">
        <span v-if="stats.due > 0">待复习 <b>{{ stats.due }}</b> 题</span>
        <span v-else>今日复习已清空 ✓</span>
        <span class="dot">·</span>
        <span>错题 <b>{{ stats.wrong || 0 }}</b></span>
        <span class="dot">·</span>
        <span>连续打卡 <b>{{ checkin.streak || 0 }}</b> 天</span>
      </div>
    </div>

    <!-- 三个入口 -->
    <div class="card today-actions">
      <button class="today-btn primary" :disabled="stats.due === 0" @click="goReview">
        <span class="tb-title">开始复习</span>
        <span class="tb-sub">{{ stats.due > 0 ? stats.due + ' 道到期' : '暂无到期' }}</span>
      </button>
      <button class="today-btn" @click="goQuiz">
        <span class="tb-title">继续刷题</span>
        <span class="tb-sub">随机组卷</span>
      </button>
      <button class="today-btn" :disabled="(stats.wrong || 0) === 0" @click="goWrong">
        <span class="tb-title">攻错题</span>
        <span class="tb-sub">{{ (stats.wrong || 0) > 0 ? stats.wrong + ' 道待重练' : '暂无错题' }}</span>
      </button>
    </div>

    <!-- 一行进度 -->
    <div class="card today-progress">
      <span>已答 <b>{{ stats.answered || 0 }}</b> / {{ stats.total || 0 }}</span>
      <span class="dot">·</span>
      <span>正确 <b>{{ stats.correct || 0 }}</b></span>
      <span class="dot">·</span>
      <span>今日 <b>{{ stats.today || 0 }}</b></span>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'

const router = useRouter()
const stats = ref({})
const checkin = ref({})

const headline = computed(() => {
  const due = stats.value.due || 0
  if (due > 0) return `今天要复习 ${due} 道题`
  const answered = stats.value.answered || 0
  if (answered === 0) return '从第一道题开始吧'
  return '今天没有到期复习，可以继续刷题'
})

async function load() {
  try {
    const [s, c] = await Promise.all([
      api('/stats').catch(() => ({})),
      api('/checkins').catch(() => ({})),
    ])
    stats.value = s || {}
    checkin.value = c || {}
  } catch (e) { /* 离线/接口异常时保持空态 */ }
}

function goReview() { router.push('/review') }
function goQuiz() { router.push('/quiz') }
function goWrong() { router.push('/wrong') }

onMounted(load)
</script>
