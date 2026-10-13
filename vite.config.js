import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiProxy = process.env.SAFEVISION_API_PROXY || process.env.VITE_API_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("/node_modules/recharts/")) return "charts";
          if (id.includes("/node_modules/lucide-react/")) return "icons";
          if (/\/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react-vendor";
          return undefined;
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": apiProxy,
      "/health": apiProxy,
    },
  },
});
