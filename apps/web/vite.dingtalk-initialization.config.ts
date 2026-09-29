import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";
import { fileURLToPath, URL } from "node:url";
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: "dist-dingtalk-initialization",
    rollupOptions: { input: fileURLToPath(new URL("./dingtalk-initialization.html", import.meta.url)) },
  },
});
