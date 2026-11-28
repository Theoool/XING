import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发期把 /api 代理到后端，前端因此永远同源：
// 不依赖后端的 CORS 配置，也不会把 localhost:8000 写死进代码。
const apiTarget = process.env.VITE_API_PROXY ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
})
