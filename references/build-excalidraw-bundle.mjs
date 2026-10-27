import { build } from "esbuild";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const outfile = resolve(here, "vendor", "excalidraw", "excalidraw-export.mjs");

await mkdir(dirname(outfile), { recursive: true });

await build({
  entryPoints: [resolve(here, "excalidraw-export-entry.mjs")],
  outfile,
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2022"],
  sourcemap: false,
  minify: false,
  loader: {
    ".css": "css",
    ".svg": "dataurl",
    ".png": "dataurl",
    ".woff2": "file",
  },
  assetNames: "assets/[name]-[hash]",
  logLevel: "info",
});
