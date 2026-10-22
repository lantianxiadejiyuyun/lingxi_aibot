import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: process.env.LINGXI_DEV_API || 'http://127.0.0.1:8000', ws: true },
      '/p': process.env.LINGXI_DEV_API || 'http://127.0.0.1:8000',
    },
  },
  build: { target: 'es2022', sourcemap: false },
})
