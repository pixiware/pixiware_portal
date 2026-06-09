import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../../static/dashboard-bg',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: 'dashboard-bg.js',
        chunkFileNames: 'dashboard-bg-[name].js',
        assetFileNames: 'dashboard-bg.[ext]',
      },
    },
  },
});
