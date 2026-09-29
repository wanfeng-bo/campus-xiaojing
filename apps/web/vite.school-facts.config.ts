import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5174,
    proxy: { "/api": { changeOrigin: false, target: process.env.SCHOOL_FACT_PRODUCT_API_ORIGIN ?? "http://127.0.0.1:8001" } },
  },
  build: {
    outDir: "dist-school-facts",
    rollupOptions: { input: fileURLToPath(new URL("./school-facts.html", import.meta.url)) },
  },
});
