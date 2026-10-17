import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/* file:// 双击打开的第二道闸：Vite 默认给产物写 <script type="module" crossorigin>，
 * 而 file:// 下模块脚本必被 CORS 拦（与包格式无关，是标签属性的问题）。
 * 构建产物已是单 IIFE（见 build.rollupOptions），这里把入口标签改写为经典 script。
 * 注意必须补 defer：经典 script 默认在 head 里当场执行，而挂载点 #app 在 body 末尾，
 * 不加 defer 会 mount 到 null 上静默失败（module 天生 defer，改写时容易漏）。
 * dev 模式注入的是 /src/main.js（无 crossorigin），正则不匹配，不受影响。 */
function iifeEntryTag() {
  return {
    name: 'qbank-iife-entry-tag',
    transformIndexHtml: {
      order: 'post',
      handler: (html) => html.replace(
        /<script type="module" crossorigin src="([^"]+)"><\/script>/,
        '<script defer src="$1"></script>')
    }
  }
}

export default defineConfig({
  base: './',
  plugins: [vue(), iifeEntryTag()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  build: {
    outDir: 'dist',
    // file:// 双击打开的关键：ES module 的 <script type=module> 会被 file 协议 CORS 拦，
    // 所以打成单个 IIFE 包（禁代码分割 + 禁动态 import），CSS 走 <link> 不受影响。
    rollupOptions: { output: { format: 'iife', inlineDynamicImports: true } },
    chunkSizeWarningLimit: 4096
  },
  server: {
    port: 5173,
    // 开发时 npm run dev 跑 5173，API 转发到本机 FastAPI（app/api.py，默认 8640）；
    // 同源策略下 guard 只看 Host，代理后 Host 仍是 127.0.0.1，放行
    proxy: { '/api': { target: 'http://127.0.0.1:8640', changeOrigin: false } }
  }
})
