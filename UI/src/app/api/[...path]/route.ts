import type { NextRequest } from "next/server";
import { request as requestLoopback, type IncomingMessage } from "node:http";
import { Readable } from "node:stream";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_JSON_BYTES = 8 * 1024 * 1024;
const MAX_UPLOAD_BYTES = 128 * 1024 * 1024;

async function proxy(request: NextRequest) {
  const host = request.headers.get("host") ?? "";
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  if (
    !["127.0.0.1:3000", "localhost:3000"].includes(host) ||
    (origin !== null && origin !== `http://${host}`) ||
    site === "cross-site"
  ) {
    return Response.json({ error: "Forbidden" }, { status: 403 });
  }
  const token = process.env.KID_BRIDGE_TOKEN;
  if (!token) return Response.json({ error: "Start the application using its Python launcher" }, { status: 503 });
  const url = new URL(request.url);
  if (!/^\/api\/(?:[A-Za-z0-9_-]+\/?)+$/.test(url.pathname)) {
    return Response.json({ error: "Invalid API path" }, { status: 400 });
  }
  const limit = url.pathname === "/api/upload" ? MAX_UPLOAD_BYTES : MAX_JSON_BYTES;
  let body: Uint8Array | undefined;
  if (request.method === "POST") {
    const reader = request.body?.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    if (reader) {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > limit) {
          await reader.cancel();
          return Response.json({ error: "Request is too large" }, { status: 413 });
        }
        chunks.push(value);
      }
    }
    body = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  }
  const headers = new Headers({ "X-Kid-Token": token });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);
  try {
    const port = Number(process.env.KID_BRIDGE_PORT ?? 5000);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid local bridge port");
    // Human approvals can outlast fetch/undici's response-header deadline.
    const response = await new Promise<IncomingMessage>((resolve, reject) => {
      const upstream = requestLoopback({hostname:"127.0.0.1", port, path:`${url.pathname}${url.search}`,
        method:request.method, headers:Object.fromEntries(headers)}, resolve);
      upstream.on("error", reject);
      const abort = () => upstream.destroy(new Error("Client disconnected"));
      request.signal.addEventListener("abort", abort, {once:true});
      upstream.on("close", () => request.signal.removeEventListener("abort", abort));
      upstream.setTimeout(0);
      if (request.signal.aborted) abort();
      else upstream.end(body);
    });
    const outgoing = new Headers({ "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
    for (const name of ["content-type", "content-disposition"]) {
      const value = response.headers[name];
      if (typeof value === "string") outgoing.set(name, value);
    }
    return new Response(Readable.toWeb(response) as ReadableStream, { status: response.statusCode ?? 502, headers: outgoing });
  } catch {
    return Response.json({ error: "Local backend is unavailable" }, { status: 502 });
  }
}

export const GET = proxy;
export const POST = proxy;
