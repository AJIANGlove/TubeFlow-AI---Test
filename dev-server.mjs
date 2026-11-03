import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer, request as proxyRequest } from "node:http";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const port = Number(process.env.PORT || 4173);
const apiOrigin = new URL(process.env.API_ORIGIN || "http://127.0.0.1:8000");
const apiPrefixes = ["/identity", "/narration", "/storyboard-groups", "/group-videos", "/projects", "/health", "/elevenlabs-pool"];
const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".txt": "text/plain; charset=utf-8"
};

const server = createServer((request, response) => {
  const requestPath = decodeURIComponent((request.url || "/").split("?")[0]);
  if (apiPrefixes.some((prefix) => requestPath === prefix || requestPath.startsWith(`${prefix}/`))) {
    const upstream = proxyRequest({ hostname: apiOrigin.hostname, port: apiOrigin.port, path: request.url, method: request.method, headers: { ...request.headers, host: apiOrigin.host } }, (proxied) => {
      response.writeHead(proxied.statusCode || 502, proxied.headers);
      proxied.pipe(response);
    });
    upstream.on("error", () => { response.writeHead(502, { "Content-Type": "application/json" }); response.end(JSON.stringify({ error: { code: "backend_unavailable", message: "TubeFlow backend is unavailable" } })); });
    request.pipe(upstream);
    return;
  }
  const relativePath = requestPath === "/" ? "index.html" : requestPath.replace(/^\/+/, "");
  const filePath = normalize(join(root, relativePath));

  if (!filePath.startsWith(normalize(root)) || !existsSync(filePath) || !statSync(filePath).isFile()) {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Not found");
    return;
  }

  response.writeHead(200, {
    "Content-Type": mimeTypes[extname(filePath).toLowerCase()] || "application/octet-stream",
    "Cache-Control": "no-store"
  });
  createReadStream(filePath).pipe(response);
});

server.listen(port, "127.0.0.1", () => {
  console.log(`TubeFlow AI prototype: http://127.0.0.1:${port}`);
});
