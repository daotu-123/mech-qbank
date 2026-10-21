/* LoginDialog.vue — 公网模式的登录框（单账号）
 *
 * ## 什么时候出现
 * 只有服务端说「需要鉴权」（`/auth/status` 返回 auth_required=true）时才出现。
 * **本机模式（默认）永远不出现** —— 那是 M2 之前的行为，一个字节没变。
 *
 * ## 令牌卫生
 * · 口令只往服务端发，**不存任何地方**（不存 localStorage、不存 sessionStorage、
 *   不写日志）。输完就忘。
 * · 令牌由 auth.js 管理：access 在内存、refresh 在 sessionStorage。
 *   本组件**不碰**令牌存储。
 * · 令牌绝不出现在 URL —— 登录走 POST body。
 */
<template>
  <el-dialog v-model="visible" title="登录 mech-qbank" width="380px"
             :close-on-click-modal="false" :close-on-press-escape="false"
             :show-close="false" align-center>
    <div class="sub" style="margin-bottom:12px">
      这台服务器启用了登录保护。输入口令继续。
    </div>
    <el-input v-model="pwd" type="password" placeholder="口令"
              show-password autofocus @keyup.enter="submit" />
    <div v-if="err" class="err" style="margin-top:8px">{{ err }}</div>
    <template #footer>
      <span class="meta" style="float:left;line-height:32px">
        口令不会保存在本机
      </span>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="busy" @click="submit">登录</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, watch } from 'vue'
import { login } from '@/auth'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'loggedIn'])

const visible = ref(props.modelValue)
const pwd = ref('')
const err = ref('')
const busy = ref(false)

watch(() => props.modelValue, (v) => { visible.value = v; if (v) err.value = '' })

async function submit() {
  if (busy.value) return
  if (!pwd.value) { err.value = '请输入口令'; return }
  busy.value = true
  err.value = ''
  const ok = await login(pwd.value)
  busy.value = false
  if (ok) {
    pwd.value = ''                 // 口令不留痕
    visible.value = false
    emit('update:modelValue', false)
    emit('loggedIn')
  } else {
    err.value = '口令不对'
  }
}
</script>

<style scoped>
.err { color: var(--bad); font-size: 13px; }
</style>
