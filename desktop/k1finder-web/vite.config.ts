import fs from "fs"
import path from "path"
import { fileURLToPath } from "url"
import type { Connect, Plugin, PreviewServer, ViteDevServer } from "vite"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const desktopRoot = path.resolve(__dirname, "..")

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".glb": "model/gltf-binary",
  ".gltf": "model/gltf+json",
  ".bin": "application/octet-stream",
  ".md": "text/markdown; charset=utf-8",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
}

function serveDesktopSubtree(mount: string, dirName: string): Plugin {
  const root = path.join(desktopRoot, dirName)

  function attach(middlewares: Connect.Server) {
    middlewares.use(mount, (req, res, next) => {
      try {
        const raw = decodeURIComponent((req.url || "/").split("?")[0])
        const rel = raw.replace(/^\/+/, "") || "index.html"
        const file = path.normalize(path.join(root, rel))
        if (!file.startsWith(root)) {
          res.statusCode = 403
          res.end("forbidden")
          return
        }
        if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
          next()
          return
        }
        const ext = path.extname(file).toLowerCase()
        res.setHeader("Content-Type", MIME[ext] || "application/octet-stream")
        res.setHeader("Cache-Control", "no-store")
        fs.createReadStream(file).pipe(res)
      } catch {
        next()
      }
    })
  }

  return {
    name: `serve-desktop-${dirName}`,
    configureServer(server: ViteDevServer) {
      attach(server.middlewares)
    },
    configurePreviewServer(server: PreviewServer) {
      attach(server.middlewares)
    },
  }
}

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    serveDesktopSubtree("/localmap-viewer", "localmap-viewer"),
    serveDesktopSubtree("/localmap-data", "localmap-data"),
  ],
  base: "./",
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    fs: {
      allow: [desktopRoot],
    },
  },
  preview: {
    port: 4173,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
})
