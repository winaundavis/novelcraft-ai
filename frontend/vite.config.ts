import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const apiProxyTarget = process.env.NOVELCRAFT_PROXY_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'chart-vendor': ['recharts'],
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    allowedHosts: ['localhost', '127.0.0.1'],
    // 不去看测试产物目录。
    //
    // 默认的 watcher 会把整个项目目录都盯上，而 Playwright 每次运行都会往
    // `playwright-report/` 和 `test-results/` 写文件 —— 这些写入会被 Vite 判成
    // 「项目文件变了」，于是向**所有**已连接的客户端广播一次整页重载：作者正在用的
    // 工作台会被无故刷掉（状态清空、页签跳回默认），测试自己也会被这些重载打断，
    // 表现为一批互不相干的用例同时失败。这两个目录跟前端源码毫无关系，直接忽略。
    watch: {
      ignored: ['**/playwright-report/**', '**/test-results/**'],
    },
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
