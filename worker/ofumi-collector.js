// ───────────────────────────────────────────────────────────────
//  御焚き上げ番付 収集Worker (Cloudflare Workers + KV) — 依存ゼロ
//
//  本番(Tier1)の自動収集先。ofumi.sh --submit が POST /offer する。
//  banzuke/index.html の API_BASE にこのWorkerのURLを入れると自動収集へ切替わる。
//
//  デプロイ(オーナー4手順):
//    1) wrangler kv:namespace create BANZUKE   → 出力 id を wrangler.toml に貼る
//    2) wrangler secret put OFFER_SALT          → 任意のランダム文字列
//    3) wrangler deploy
//    4) 公開URLを banzuke/index.html の API_BASE に貼る
// ───────────────────────────────────────────────────────────────

// 火位(称号)の単一定義。tools/ofumi/ranks.json と同期させること。
const RANKS = [
  { id: "hokuchi",   min: 0,          name: "火口" },
  { id: "tomoshibi", min: 1000000,    name: "灯火" },
  { id: "kagaribi",  min: 10000000,   name: "篝火" },
  { id: "takibi",    min: 100000000,  name: "焚火" },
  { id: "homura",    min: 300000000,  name: "焔" },
  { id: "taika",     min: 1000000000, name: "大火" },
  { id: "neppa",     min: 1500000000, name: "熱波" },
];

const ALLOWED_ORIGIN = "https://imai-design.github.io";
const MAX_BODY = 16384;          // 16KB
const IMPOSSIBLE = 100000000000; // 1000億トークン
const DISCLAIMER = "自己申告の Claude Code 消費トークンに基づく推定値。claude.ai／API 直叩き分は含まず。水増しの完全防止は不可。";

function fireRank(total) {
  let c = RANKS[0];
  for (const r of RANKS) if (total >= r.min) c = r;
  return c;
}
function corsHeaders(extra) {
  return Object.assign({
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
  }, extra || {});
}
function json(obj, status, extra) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: Object.assign({ "content-type": "application/json; charset=utf-8" }, corsHeaders(extra)),
  });
}
async function sha256hex(msg) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(msg));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
async function hmacHex(key, msg) {
  const k = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(key || "ofumi"), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(msg || ""));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// 検証 + allowlist 詰め替え(機密キーが来たら拒否)
function validate(b) {
  if (!b || b.v !== 1) return { error: "v must be 1" };
  const danger = ["cwd", "projectName", "project", "gitBranch", "sessionId", "filePath", "files", "uuid", "requestId", "byModel"];
  for (const k of danger) if (k in b) return { error: "forbidden key: " + k };
  const h = b.handle;
  const HANDLE_RE = /^[\p{L}\p{N} 　_・ー〜＿!?！？.\-]{2,24}$/u;
  if (typeof h !== "string" || !HANDLE_RE.test(h.trim())) return { error: "bad handle" };
  if (typeof b.anonId !== "string" || !/^ak_[0-9a-f]{16}$/.test(b.anonId)) return { error: "bad anonId" };
  const tk = b.tokens || {};
  // 非数値は黙ってゼロ化せず fail-fast。負値・NaN・文字列は 400。
  const numField = (x) => (typeof x === "number" && Number.isFinite(x) && x >= 0) ? Math.floor(x) : null;
  const i = numField(tk.input), o = numField(tk.output), cc = numField(tk.cacheCreation), cr = numField(tk.cacheRead);
  let total = numField(tk.total);
  if ([i, o, cc, cr, total].some((x) => x === null)) return { error: "bad tokens (must be non-negative numbers)" };
  const s = i + o + cc + cr;
  if (s > 0 && Math.abs(total - s) > Math.max(1, s * 0.001)) total = s;
  const flags = [];
  if (total > IMPOSSIBLE) flags.push("impossible");
  const fr = fireRank(total);
  const mem = ["学徒", "信徒", "神"].includes(b.membership) ? b.membership : "";
  const week = ((b.periods || {}).week || {}).total || 0;
  return {
    rec: {
      handle: h.trim(), anonId: b.anonId, membership: mem,
      fireRank: fr.id, rankTitle: fr.name,
      tokens: { input: i, output: o, cacheCreation: cc, cacheRead: cr, total },
      weekTotal: (typeof week === "number" && week > 0) ? Math.floor(week) : 0,
      estCostUsd: (typeof b.costUsd === "number" && b.costUsd > 0) ? Math.round(b.costUsd * 100) / 100 : null,
      lastOfferingAt: typeof b.measuredAt === "string" ? b.measuredAt : "",
      flags,
    },
  };
}

async function handleOffer(req, env) {
  const ip = req.headers.get("CF-Connecting-IP") || "";
  const ipHash = (await hmacHex(env.OFFER_SALT, ip)).slice(0, 16);

  // レート制限: 同一ipHash 60req/10min
  const rlKey = "rl:" + ipHash;
  const cnt = parseInt((await env.BANZUKE.get(rlKey)) || "0", 10);
  if (cnt >= 60) return json({ error: "rate limited" }, 429);
  await env.BANZUKE.put(rlKey, String(cnt + 1), { expirationTtl: 600 });

  const text = await req.text();
  if (text.length > MAX_BODY) return json({ error: "payload too large" }, 413);
  let body;
  try { body = JSON.parse(text); } catch (e) { return json({ error: "invalid json" }, 400); }

  const v = validate(body);
  if (v.error) return json({ error: v.error }, 400);
  const rec = v.rec;

  const existing = await env.BANZUKE.get("rec:" + rec.anonId, "json");
  if (existing) {
    if (body.claimToken !== existing.claimToken) return json({ error: "forbidden (claimToken mismatch)" }, 403);
    if ((existing.updateCount || 0) >= 6) return json({ error: "too many updates today" }, 429);
    rec.claimToken = existing.claimToken;
    rec.updateCount = (existing.updateCount || 0) + 1;
  } else {
    rec.claimToken = crypto.randomUUID().replace(/-/g, "");
    rec.updateCount = 0;
  }
  rec.serverAt = new Date().toISOString();
  rec.ipHash = ipHash;
  await env.BANZUKE.put("rec:" + rec.anonId, JSON.stringify(rec));

  // 監査ログ(ハッシュチェーン)
  const prevHash = (await env.BANZUKE.get("audit:head")) || "";
  const payloadStr = JSON.stringify({ h: rec.handle, t: rec.tokens.total, at: rec.serverAt });
  const entryHash = await sha256hex(prevHash + payloadStr);
  await env.BANZUKE.put("audit:" + rec.serverAt + ":" + entryHash.slice(0, 8),
    JSON.stringify({ handle: rec.handle, total: rec.tokens.total, at: rec.serverAt, ipHash, prevHash, hash: entryHash }));
  await env.BANZUKE.put("audit:head", entryHash);

  const resp = { ok: true, handle: rec.handle, fireRank: rec.fireRank, rank_title: rec.rankTitle, flags: rec.flags };
  if (rec.updateCount === 0) resp.claimToken = rec.claimToken; // 初回のみ返す
  return json(resp);
}

async function handleBanzuke(url, env) {
  const period = url.searchParams.get("period") === "weekly" ? "weekly" : "total";
  const includeFlagged = url.searchParams.get("includeFlagged") === "1";
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "100", 10) || 100, 500);

  const list = await env.BANZUKE.list({ prefix: "rec:" });
  const entries = [];
  for (const k of list.keys) {
    const r = await env.BANZUKE.get(k.name, "json");
    if (!r) continue;
    if (!includeFlagged && (r.flags || []).includes("impossible")) continue;
    const total = period === "weekly" ? (r.weekTotal || 0) : r.tokens.total;
    if (period === "weekly" && total <= 0) continue;
    entries.push({
      handle: r.handle, membership: r.membership, fireRank: r.fireRank,
      tokens: { total }, estCostUsd: period === "weekly" ? null : r.estCostUsd,
      lastOfferingAt: r.lastOfferingAt, flags: r.flags || [],
    });
  }
  entries.sort((a, b) => b.tokens.total - a.tokens.total);
  const top = entries.slice(0, limit);
  top.forEach((e, i) => { e.rank = i + 1; });
  return json({
    schema: "banzuke/v1", generatedAt: new Date().toISOString(), source: "worker",
    measurement: "claude-code-only", disclaimer: DISCLAIMER,
    total_entries: entries.length, entries: top,
  }, 200, { "Cache-Control": "public, max-age=60" });
}

async function handleGetOffer(handle, env) {
  handle = decodeURIComponent(handle);
  const list = await env.BANZUKE.list({ prefix: "rec:" });
  for (const k of list.keys) {
    const r = await env.BANZUKE.get(k.name, "json");
    if (r && r.handle === handle) {
      const { claimToken, ipHash, ...pub } = r; // claimToken/ipHash は絶対返さない
      return json(pub);
    }
  }
  return json({ error: "not found" }, 404);
}

async function handleAudit(url, env) {
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10) || 50, 200);
  const list = await env.BANZUKE.list({ prefix: "audit:" });
  const items = [];
  for (const k of list.keys) {
    if (k.name === "audit:head") continue;
    const e = await env.BANZUKE.get(k.name, "json");
    if (e) { const { ipHash, ...pub } = e; items.push(pub); } // ipHash は公開しない
  }
  items.sort((a, b) => (a.at < b.at ? 1 : -1));
  return json({ count: items.length, entries: items.slice(0, limit) });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;
    const m = req.method;
    if (m === "OPTIONS") return new Response(null, { headers: corsHeaders() });
    try {
      if (m === "POST" && path === "/offer") return await handleOffer(req, env);
      if (m === "GET" && path === "/banzuke") return await handleBanzuke(url, env);
      if (m === "GET" && path.startsWith("/offer/")) return await handleGetOffer(path.slice("/offer/".length), env);
      if (m === "GET" && path === "/audit") return await handleAudit(url, env);
      if (m === "GET" && (path === "/" || path === "/health")) return json({ ok: true, service: "ofumi-collector" });
    } catch (e) {
      return json({ error: "internal", detail: String(e && e.message || e) }, 500);
    }
    return json({ error: "not found" }, 404);
  },
};
