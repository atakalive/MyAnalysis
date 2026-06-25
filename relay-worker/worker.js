// chatdock.html is bundled as a Text module (see wrangler.toml `rules`).
import CHATDOCK_HTML from "./chatdock.html";

// MyAnalysis meeting relay — Cloudflare Worker (Issue #42).
//
// Bridges the host ToolWindow (chat dock + analysis view) and remote guests
// over HTTP. Storage = KV (metadata, sessions, tabs, messages, presence) + R2
// (view PNGs). Durable Objects are intentionally out of scope (KV/R2 access is
// kept behind thin helpers so a future DO migration can swap them).
//
// TIME UNITS: internal time is SECONDS (`now = Math.floor(Date.now()/1000)`).
// The ONLY milliseconds exceptions are (a) message id `ts13`, (b) view version
// `vv`, (c) message bucket `Math.floor(ts_ms/60000)`. Everything else is seconds.
//
// MESSAGE ROUTING (the core invariant — kills echo / loops / loss / LIST blowup):
//   in:{ch}:{bucket}:{sid}:{mid}   guests POST /msg  (secret) — role=user, origin=guest
//   out:{ch}:{bucket}:{sid}:{mid}  host  POST /out   (admin)  — origin=host (user+assistant)
// The host worker reads ONLY `in:` (GET /inbound) and never reads its own `out:`,
// so a feedback loop is structurally impossible. Group cross-visibility (guest A
// sees guest B) is served by guests reading `in:` via GET /poll.
//
// CURSOR / LOOKBACK (loss-free + bounded cost): KV list() is prefix-only and
// cannot do a suffix range (mid >= since), so the bucket (60s) is placed at the
// FRONT of the key. Receivers keep a `seen` set and fetch from a lookback floor
// `floor_ts = max(0, latest_seen_ts - LOOKBACK_MS)`. Each poll scans only the
// newest <=MAX_BUCKETS buckets (always including bucket(now)) and filters
// `mid >= since`. Loss-free invariant: MAX_BUCKETS*60000 >= LOOKBACK_MS +
// max_poll_interval (default 120000 >= 15000 + 30000). Poll interval is clamped
// [1s, 30s] on every client. Cursors are built from the WORKER clock only
// (server_now_ms in every response) so client clock skew can't shift `since`.

const LOOKBACK_MS = 15000;     // re-scan window for eventually-consistent KV
const MAX_BUCKETS = 2;         // buckets scanned per poll (always incl. bucket(now))
const BUCKET_MS = 60000;       // 60s message bucket
const MSG_TTL = 86400;         // message key TTL (seconds) = 24h
const PRES_TTL = 90;           // presence key TTL (seconds)
const HB_GRACE = 120;          // heartbeat liveness grace (seconds)
const TTL_MIN = 3600;          // channel ttl clamp min (1h)
const TTL_MAX = 86400;         // channel ttl clamp max (24h)

function nowSec() { return Math.floor(Date.now() / 1000); }
function nowMs() { return Date.now(); }
function bucketOf(ms) { return Math.floor(ms / BUCKET_MS); }

function pad13(n) { return String(n).padStart(13, "0"); }

// mid = "<ts13>-<rand13>". ts13 = Date.now() (13 digits, fixed width). rand13 =
// 64-bit crypto random in base36, padStart(13). Both fixed width so lexical
// order == time order. Uniqueness is probabilistic (2^-64/pair) — get-check is
// NOT done (KV get→put is not CAS; collision probability is negligible but not
// theoretically zero, best-effort).
function genMid() {
  const ts = nowMs();
  const buf = new Uint8Array(8);
  crypto.getRandomValues(buf);
  let v = 0n;
  for (const b of buf) v = (v << 8n) | BigInt(b);
  const rand13 = v.toString(36).padStart(13, "0").slice(-13);
  // Return ts too so callers derive the bucket from the SAME clock reading as the
  // mid (avoids a 1-bucket mismatch at a bucket boundary from a second Date.now()).
  return { mid: `${pad13(ts)}-${rand13}`, ts };
}

// Guests fetch with an Authorization header (a non-simple header), which makes
// every request CORS-preflighted unless the page is same-origin. Allow any
// origin: there are no cookies/credentials (auth is a Bearer header), so a
// wildcard ACAO is safe and lets the guest client run from any origin
// (workers.dev, a custom host, or a local file:// whose Origin is "null").
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-max-age": "86400",
};

function json(obj, status = 200, extra = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...CORS, ...extra },
  });
}

function bearer(request) {
  const h = request.headers.get("Authorization") || "";
  const m = h.match(/^Bearer\s+(.+)$/);
  return m ? m[1] : "";
}

function constantTimeEqual(a, b) {
  // Compare as bytes in constant time (length-leaking only).
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  if (ea.length !== eb.length) return false;
  let diff = 0;
  for (let i = 0; i < ea.length; i++) diff |= ea[i] ^ eb[i];
  return diff === 0;
}

async function sha256hex(s) {
  const data = new TextEncoder().encode(s);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function isAdmin(request, env) {
  const key = env.RELAY_ADMIN_KEY || "";
  return key && constantTimeEqual(bearer(request), key);
}

// Guest liveness gate. Reads ONLY ch:{ch}:meta and ch:{ch}:hb. Returns
// {ok, status} — 401 on secret mismatch, 410 on missing/expired/stale. On
// failure NO other key (and no R2) is touched.
async function checkGuest(request, env, ch) {
  const metaRaw = await env.RELAY_KV.get(`ch:${ch}:meta`);
  if (!metaRaw) return { ok: false, status: 410 };
  let meta;
  try { meta = JSON.parse(metaRaw); } catch { return { ok: false, status: 410 }; }
  const secret = bearer(request);
  const secretHash = await sha256hex(secret);
  if (!constantTimeEqual(secretHash, meta.secret_hash || "")) return { ok: false, status: 401 };
  const now = nowSec();
  if (now >= (meta.expires_at || 0)) return { ok: false, status: 410 };
  const hbRaw = await env.RELAY_KV.get(`ch:${ch}:hb`);
  const hb = hbRaw ? parseInt(hbRaw, 10) || 0 : 0;
  if (now - hb >= HB_GRACE) return { ok: false, status: 410 };
  return { ok: true, meta };
}

async function readSessions(env, ch) {
  const raw = await env.RELAY_KV.get(`ch:${ch}:sessions`);
  if (!raw) return [];
  try { return JSON.parse(raw); } catch { return []; }
}

async function readTabs(env, ch) {
  const raw = await env.RELAY_KV.get(`ch:${ch}:tabs`);
  if (!raw) return [];
  try { return JSON.parse(raw); } catch { return []; }
}

// Restore the lookback floor + comparison string. When `since` is absent or
// "RESET" this is a reanchor: floor = now_ms - LOOKBACK_MS (server clock).
function computeSince(since, now_ms) {
  if (!since || since === "RESET") {
    const floorTs = Math.max(0, now_ms - LOOKBACK_MS);
    return { floorTs, sinceCmp: `${pad13(floorTs)}-${"0".repeat(13)}` };
  }
  const tsPart = parseInt(since.slice(0, 13), 10);
  const floorTs = Number.isFinite(tsPart) ? Math.max(0, tsPart) : 0;
  return { floorTs, sinceCmp: since };
}

// Scan `kinds` (in:/out:) message keys across the newest <=MAX_BUCKETS buckets
// (always including bucket(now); never older buckets below the floor — an idle
// client must still see current new messages, not just stale floor buckets),
// filter mid >= sinceCmp (and optional sid), return ascending by mid.
async function scanMessages(env, ch, kinds, sidFilter, sinceCmp, floorTs, now_ms) {
  const startBucket = Math.max(bucketOf(floorTs), bucketOf(now_ms) - (MAX_BUCKETS - 1));
  const endBucket = bucketOf(now_ms);
  const collected = [];
  for (const kind of kinds) {
    for (let b = startBucket; b <= endBucket; b++) {
      const prefix = `${kind}:${ch}:${b}:`;
      let cursor;
      do {
        const res = await env.RELAY_KV.list({ prefix, cursor });
        for (const k of res.keys) {
          const rest = k.name.slice(prefix.length); // sid:mid (sid may contain ':')
          const idx = rest.lastIndexOf(":");
          if (idx < 0) continue;
          const sid = rest.slice(0, idx);
          const mid = rest.slice(idx + 1);
          if (sidFilter !== null && sid !== sidFilter) continue;
          if (mid < sinceCmp) continue;
          collected.push({ key: k.name, sid, mid });
        }
        cursor = res.list_complete ? undefined : res.cursor;
      } while (cursor);
    }
  }
  const out = [];
  for (const c of collected) {
    const valRaw = await env.RELAY_KV.get(c.key);
    if (!valRaw) continue;
    try { out.push(JSON.parse(valRaw)); } catch { /* skip corrupt */ }
  }
  out.sort((a, b) => (a.mid < b.mid ? -1 : a.mid > b.mid ? 1 : 0));
  return out;
}

async function readViewVersions(env, ch) {
  const versions = {};
  const prefix = `vv:${ch}:`;
  let cursor;
  do {
    const res = await env.RELAY_KV.list({ prefix, cursor });
    for (const k of res.keys) {
      const tab = k.name.slice(prefix.length);
      const v = await env.RELAY_KV.get(k.name);
      if (v) versions[tab] = parseInt(v, 10) || 0;
    }
    cursor = res.list_complete ? undefined : res.cursor;
  } while (cursor);
  return versions;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const seg = url.pathname.split("/").filter((s) => s.length > 0).map(decodeURIComponent);
    const method = request.method;

    // CORS preflight: the guest's Authorization header forces a preflight on any
    // cross-origin request. Answer it for every route before auth/routing.
    if (method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    // GET / (unauthenticated) → serve the guest HTML client.
    if (seg.length === 0 && method === "GET") {
      return new Response(CHATDOCK_HTML, {
        headers: { "content-type": "text/html; charset=utf-8", ...CORS },
      });
    }

    // ---- host (admin-keyed) routes ----
    if (seg[0] === "admin" && seg[1] === "channel") {
      if (method === "POST" && seg.length === 2) {
        if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
        let body;
        try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
        const ch = String(body.ch || "");
        if (!ch) return json({ error: "missing ch" }, 400);
        const ttl = Math.min(TTL_MAX, Math.max(TTL_MIN, parseInt(body.ttl_sec, 10) || TTL_MIN));
        const now = nowSec();
        const expires_at = now + ttl;
        await env.RELAY_KV.put(`ch:${ch}:meta`,
          JSON.stringify({ secret_hash: String(body.secret_hash || ""), expires_at }));
        await env.RELAY_KV.put(`ch:${ch}:hb`, String(now));
        return json({ expires_at, server_now_ms: nowMs() });
      }
      if (method === "DELETE" && seg.length === 3) {
        if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
        const ch = seg[2];
        // Delete the channel metadata keys. in:/out:/vv:/pres: are left to TTL
        // (avoid a large LIST+delete); R2 view/{ch}/* is left too (small, R2
        // lifecycle handles it — see wrangler.toml). Revocation is server-
        // enforced but eventually-consistent (~60s KV propagation window).
        await env.RELAY_KV.delete(`ch:${ch}:meta`);
        await env.RELAY_KV.delete(`ch:${ch}:hb`);
        await env.RELAY_KV.delete(`ch:${ch}:sessions`);
        await env.RELAY_KV.delete(`ch:${ch}:tabs`);
        return json({ ok: true });
      }
    }

    if (seg[0] === "sessions" && seg.length === 2 && method === "PUT") {
      if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
      const ch = seg[1];
      let body;
      try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
      await env.RELAY_KV.put(`ch:${ch}:sessions`, JSON.stringify(Array.isArray(body) ? body : []));
      return json({ ok: true });
    }

    if (seg[0] === "tabs" && seg.length === 2 && method === "PUT") {
      if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
      const ch = seg[1];
      let body;
      try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
      await env.RELAY_KV.put(`ch:${ch}:tabs`, JSON.stringify(Array.isArray(body) ? body : []));
      return json({ ok: true });
    }

    if (seg[0] === "heartbeat" && seg.length === 2 && method === "PUT") {
      if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
      await env.RELAY_KV.put(`ch:${seg[1]}:hb`, String(nowSec()));
      return json({ ok: true });
    }

    // POST /out/{ch}/{sid} (host, admin) — no sessions whitelist (host is inside
    // the trust boundary; in-flight responses must always write). text empty → 400.
    if (seg[0] === "out" && seg.length === 3 && method === "POST") {
      if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
      const ch = seg[1], sid = seg[2];
      let body;
      try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
      const text = String(body.text || "");
      if (!text.trim()) return json({ error: "empty text" }, 400);
      const { mid, ts } = genMid();
      const bucket = bucketOf(ts);
      const val = { text, name: String(body.name || ""), role: String(body.role || "assistant"),
                    origin: "host", mid, sid };
      await env.RELAY_KV.put(`out:${ch}:${bucket}:${sid}:${mid}`, JSON.stringify(val),
        { expirationTtl: MSG_TTL });
      return json({ mid });
    }

    // GET /inbound/{ch}?since=<mid> (host) — reads in: ONLY, all sid.
    if (seg[0] === "inbound" && seg.length === 2 && method === "GET") {
      if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
      const ch = seg[1];
      const now_ms = nowMs();
      const { floorTs, sinceCmp } = computeSince(url.searchParams.get("since"), now_ms);
      const messages = await scanMessages(env, ch, ["in"], null, sinceCmp, floorTs, now_ms);
      return json({ messages, server_now_ms: now_ms });
    }

    // GET /presence/{ch} (host) — participant list.
    if (seg[0] === "presence" && seg.length === 2 && method === "GET") {
      if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
      const ch = seg[1];
      const out = [];
      const prefix = `pres:${ch}:`;
      let cursor;
      do {
        const res = await env.RELAY_KV.list({ prefix, cursor });
        for (const k of res.keys) {
          const raw = await env.RELAY_KV.get(k.name);
          if (raw) { try { out.push(JSON.parse(raw)); } catch { /* skip */ } }
        }
        cursor = res.list_complete ? undefined : res.cursor;
      } while (cursor);
      return json(out);
    }

    // ---- guest (secret-keyed) routes ----

    // POST /msg/{ch}/{sid} (guest) — whitelist sid ∈ sessions (403). text → 400.
    if (seg[0] === "msg" && seg.length === 3 && method === "POST") {
      const ch = seg[1], sid = seg[2];
      const live = await checkGuest(request, env, ch);
      if (!live.ok) return json({ error: "not live" }, live.status);
      const sessions = await readSessions(env, ch);
      if (!sessions.some((s) => s.id === sid)) return json({ error: "session not published" }, 403);
      let body;
      try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
      const text = String(body.text || "");
      if (!text.trim()) return json({ error: "empty text" }, 400);
      let name = String(body.name || "").trim();
      if (!name) name = "Guest";
      const { mid, ts } = genMid();
      const bucket = bucketOf(ts);
      const val = { text, name, role: "user", origin: "guest", mid, sid };
      await env.RELAY_KV.put(`in:${ch}:${bucket}:${sid}:${mid}`, JSON.stringify(val),
        { expirationTtl: MSG_TTL });
      return json({ mid });
    }

    // POST /presence/{ch} (guest).
    if (seg[0] === "presence" && seg.length === 2 && method === "POST") {
      const ch = seg[1];
      const live = await checkGuest(request, env, ch);
      if (!live.ok) return json({ error: "not live" }, live.status);
      let body;
      try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
      const pid = String(body.pid || "");
      if (!pid) return json({ error: "missing pid" }, 400);
      let name = String(body.name || "").trim();
      if (!name) name = "Guest";
      const val = { pid, name, sid: String(body.sid || ""), tab: String(body.tab || ""), last: nowSec() };
      await env.RELAY_KV.put(`pres:${ch}:${pid}`, JSON.stringify(val), { expirationTtl: PRES_TTL });
      return json({ ok: true });
    }

    // PUT/GET /view/{ch}/{tab} — tabs whitelist (403).
    if (seg[0] === "view" && seg.length === 3) {
      const ch = seg[1], tab = seg[2];
      if (method === "PUT") {
        if (!isAdmin(request, env)) return json({ error: "unauthorized" }, 401);
        const tabs = await readTabs(env, ch);
        if (!tabs.includes(tab)) return json({ error: "tab not published" }, 403);
        const buf = await request.arrayBuffer();
        await env.RELAY_R2.put(`view/${ch}/${tab}`, buf,
          { httpMetadata: { contentType: "image/png" } });
        await env.RELAY_KV.put(`vv:${ch}:${tab}`, String(nowMs()), { expirationTtl: MSG_TTL });
        return json({ ok: true });
      }
      if (method === "GET") {
        const live = await checkGuest(request, env, ch);
        if (!live.ok) return json({ error: "not live" }, live.status);
        const tabs = await readTabs(env, ch);
        if (!tabs.includes(tab)) return json({ error: "tab not published" }, 403);
        const obj = await env.RELAY_R2.get(`view/${ch}/${tab}`);
        if (!obj) return new Response(null, { status: 204, headers: CORS });
        return new Response(obj.body, { headers: { "content-type": "image/png", ...CORS } });
      }
    }

    // GET /poll/{ch}/{sid}?since=<mid> (guest) — unified poll. sid as path param.
    if (seg[0] === "poll" && seg.length === 3 && method === "GET") {
      const ch = seg[1], sid = seg[2];
      const live = await checkGuest(request, env, ch);
      if (!live.ok) return json({ error: "not live" }, live.status);
      const now_ms = nowMs();
      const sessions = await readSessions(env, ch);
      const tabs = await readTabs(env, ch);
      const view_versions = await readViewVersions(env, ch);
      let messages = [];
      // sid ∈ sessions check: if opted-out, return empty messages (revokes read,
      // symmetric to GET /view 403). sessions/tabs/alive still flow.
      if (sessions.some((s) => s.id === sid)) {
        const { floorTs, sinceCmp } = computeSince(url.searchParams.get("since"), now_ms);
        messages = await scanMessages(env, ch, ["in", "out"], sid, sinceCmp, floorTs, now_ms);
      }
      return json({ messages, sessions, tabs, view_versions, alive: true, server_now_ms: now_ms });
    }

    return json({ error: "not found" }, 404);
  },
};
