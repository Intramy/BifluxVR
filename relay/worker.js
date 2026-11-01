// Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
// BifluxVR install-report relay (Cloudflare Worker).
//
// The installer never sees the Discord webhook: it talks to this worker,
// which keeps the webhook as a secret, hands out user numbers, and edits one
// Discord message per install from INSTALLING to SUCCESSFUL or FAILED.
//
// Bindings (see relay/README.md):
//   WEBHOOK      secret  the Discord webhook URL
//   SIGNING_KEY  secret  any long random string
//   REPORTS      KV namespace

const FIELD = /[^\p{L}\p{N} .,:;()\[\]+_\/-]/gu;
const LIMIT = 5; // starts per IP per window
const WINDOW = 600; // seconds

function clean(value, max = 80) {
  return String(value ?? "").replace(FIELD, "").replace(/\s+/g, " ").trim().slice(0, max) || "unknown";
}

async function hmac(key, text) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function line(run, status) {
  return `${run.os}, ${run.distro}, ${run.kernel} - USER COUNT: ${run.count} - ${status}`;
}

async function discord(env, method, path, content) {
  const url = env.WEBHOOK + path + (method === "POST" ? "?wait=true" : "");
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
  });
  if (!r.ok) throw new Error(`discord ${r.status}`);
  return method === "POST" ? (await r.json()).id : null;
}

// The raw IP is never stored or forwarded: only a keyed hash, for 10 minutes.
async function limited(env, request) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const key = "rl:" + (await hmac(env.SIGNING_KEY, ip)).slice(0, 32);
  const hits = parseInt((await env.REPORTS.get(key)) || "0", 10);
  if (hits >= LIMIT) return true;
  await env.REPORTS.put(key, String(hits + 1), { expirationTtl: WINDOW });
  return false;
}

async function start(env, body) {
  let count = parseInt(body.count, 10);
  // A returning machine keeps its number if it can prove it was given it.
  if (!(count > 0 && body.token === (await hmac(env.SIGNING_KEY, `user:${count}`)))) {
    count = parseInt((await env.REPORTS.get("count")) || "0", 10) + 1;
    await env.REPORTS.put("count", String(count));
  }
  const run = { count, os: clean(body.os), distro: clean(body.distro), kernel: clean(body.kernel) };
  const runId = crypto.randomUUID();
  run.message = await discord(env, "POST", "", line(run, "INSTALLING"));
  await env.REPORTS.put(`run:${runId}`, JSON.stringify(run), { expirationTtl: 86400 });
  return { count, token: await hmac(env.SIGNING_KEY, `user:${count}`), run: runId };
}

async function finish(env, body) {
  const key = `run:${clean(body.run, 40)}`;
  const run = JSON.parse((await env.REPORTS.get(key)) || "null");
  if (!run) return null;
  await env.REPORTS.delete(key); // each run reports its result once
  const status = body.ok ? "SUCCESSFUL" : `FAILED: ${clean(body.reason, 300)}`;
  await discord(env, "PATCH", `/messages/${run.message}`, line(run, status));
  return { ok: true };
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method !== "POST" || !["/start", "/finish"].includes(path)) {
      return new Response("BifluxVR install reports\n", { status: 404 });
    }
    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }
    try {
      if (path === "/start") {
        if (await limited(env, request)) return new Response("slow down", { status: 429 });
        return Response.json(await start(env, body));
      }
      const res = await finish(env, body);
      return res ? Response.json(res) : new Response("unknown run", { status: 404 });
    } catch (e) {
      return new Response("relay error", { status: 502 });
    }
  },
};
