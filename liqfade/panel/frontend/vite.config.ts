import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The bundle is committed under ../static/ so the VPS never needs Node.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../static", emptyOutDir: true },
  // `npm run dev` proxies the API to a locally running `liqfade panel`.
  server: { proxy: { "/api": "http://127.0.0.1:8787" } },
});
