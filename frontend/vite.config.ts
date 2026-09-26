import path from "node:path";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react-swc";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const packageJson = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8")) as { version?: string };
const appVersion = packageJson.version ?? "0.0.0";

const proxyTarget = process.env.API_PROXY_TARGET || "http://localhost:8000";
// Behind a portless proxy (scripts/dev.sh web) the target is the proxy itself
// and API_PROXY_HOST names the api route. Rewriting Host is what routes the
// request to the api instead of back to this dev server, and it survives api
// restarts, which change the api's own port.
const proxyHost = process.env.API_PROXY_HOST;
const apiProxy = proxyHost ? { target: proxyTarget, headers: { host: proxyHost } } : proxyTarget;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  resolve: {
    alias: {
      "@": path.resolve(path.dirname(fileURLToPath(import.meta.url)), "./src"),
    },
  },
  server: {
    proxy: {
      "/api": apiProxy,
      "/v1": apiProxy,
      "/health": apiProxy,
    },
  },
  build: {
    outDir: "../kiro/static",
    emptyOutDir: true,
  },
});
