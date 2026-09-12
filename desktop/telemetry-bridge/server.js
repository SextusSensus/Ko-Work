#!/usr/bin/env node
/**
 * K1 Local Map telemetry bridge
 * - Static files from desktop/
 * - WebSocket /ws/telemetry
 * - Mock Booster-like odom + optional feed.json watch
 * Default port 8742 (avoid 3000/5173/8080).
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const { WebSocketServer } = require('ws');

const PORT = Number(process.env.PORT || 8742);
const DESKTOP_ROOT = path.resolve(__dirname, '..');
const VIEWER_DIR = path.join(DESKTOP_ROOT, 'localmap-viewer');
const DATA_DIR = path.join(DESKTOP_ROOT, 'localmap-data');
const DEFAULT_FEED = path.join(VIEWER_DIR, 'feed.json');

const args = process.argv.slice(2);
function flag(name, def) {
  const i = args.indexOf(name);
  if (i < 0) return def;
  const n = args[i + 1];
  if (n == null || n.startsWith('-')) return true;
  return n;
}
const MOCK = !args.includes('--no-mock');
const HZ = Number(flag('--hz', 15)) || 15;
const FEED_PATH = path.resolve(String(flag('--feed', DEFAULT_FEED)));
const DOMAIN = String(flag('--domain', 'warehouse-bay-a'));

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.md': 'text/markdown; charset=utf-8',
  '.ico': 'image/x-icon',
};

const state = {
  mode: MOCK ? 'mock' : 'feed',
  hz: HZ,
  domain_id: DOMAIN,
  battery: 87,
  clients: 0,
  lastOdom: null,
  startedAt: Date.now(),
};

let odom = { x: 0.4, y: 0.0, z: 0, yaw: 0.05, vx: 0.35, vy: 0, wz: 0.12 };
let phase = 0;

function readJsonSafe(p) {
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch (_) {
    return null;
  }
}

function sendJson(res, code, obj) {
  const body = JSON.stringify(obj, null, 2);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(body);
}

function safeJoin(root, urlPath) {
  const decoded = decodeURIComponent((urlPath || '/').split('?')[0]);
  const rel = decoded.replace(/^\/+/, '');
  const full = path.normalize(path.join(root, rel));
  if (!full.startsWith(root)) return null;
  return full;
}

function serveStatic(req, res) {
  let urlPath = req.url || '/';
  if (urlPath === '/') urlPath = '/localmap-viewer/index.html';
  if (urlPath.startsWith('/api/')) return false;

  const filePath = safeJoin(DESKTOP_ROOT, urlPath);
  if (!filePath) {
    res.writeHead(403);
    res.end('forbidden');
    return true;
  }

  fs.stat(filePath, (err, st) => {
    if (err || !st.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('not found: ' + urlPath);
      return;
    }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Cache-Control': ext === '.json' ? 'no-store' : 'public, max-age=30',
      'Access-Control-Allow-Origin': '*',
    });
    fs.createReadStream(filePath).pipe(res);
  });
  return true;
}

function writeJsonSafe(p, obj) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj, null, 2) + '\n', 'utf8');
}

function slugifyDomain(name) {
  const s = String(name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48);
  return s || ('domain-' + Date.now());
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (e) {
        reject(e);
      }
    });
    req.on('error', reject);
  });
}

/** Create an EMPTY domain (no occupancy cells, no instances/props). */
function createEmptyDomainOnDisk(payload) {
  const name = String((payload && payload.name) || '').trim();
  if (!name) {
    const err = new Error('name required');
    err.status = 400;
    throw err;
  }
  const id = slugifyDomain((payload && payload.id) || name);
  const dir = path.join(DATA_DIR, 'domains', id);
  if (fs.existsSync(dir) && fs.existsSync(path.join(dir, 'manifest.json'))) {
    const err = new Error('domain already exists');
    err.status = 409;
    err.id = id;
    throw err;
  }
  const now = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  fs.mkdirSync(dir, { recursive: true });
  const occupancy = (payload && payload.occupancy) || {
    domain_id: id,
    res_m: 0.08,
    range_m: 3.5,
    pose: { x: 0, y: 0, yaw: 0 },
    trail: [],
    cells: [],
  };
  occupancy.domain_id = id;
  occupancy.cells = Array.isArray(occupancy.cells) ? occupancy.cells : [];
  const instances = (payload && payload.instances) || {
    domain_id: id,
    updated: now,
    notes: 'operator-created empty domain',
    instances: [],
  };
  instances.domain_id = id;
  instances.instances = [];
  const manifest = {
    id,
    name,
    created: now,
    updated: now,
    run_count: 0,
    cell_count: 0,
    notes: 'operator-created-empty',
  };
  writeJsonSafe(path.join(dir, 'occupancy.json'), occupancy);
  writeJsonSafe(path.join(dir, 'instances.json'), instances);
  writeJsonSafe(path.join(dir, 'manifest.json'), manifest);

  const regPath = path.join(DATA_DIR, 'domains.json');
  const reg = readJsonSafe(regPath) || { active: null, domains: [] };
  if (!Array.isArray(reg.domains)) reg.domains = [];
  const meta = { id, name, updated: now, run_count: 0, cell_count: 0 };
  const idx = reg.domains.findIndex((d) => d && d.id === id);
  if (idx >= 0) reg.domains[idx] = meta;
  else reg.domains.push(meta);
  reg.active = id;
  writeJsonSafe(regPath, reg);
  state.domain_id = id;
  return { ok: true, id, name, empty: true, registry: reg };
}

function handleApi(req, res) {
  const u = new URL(req.url, 'http://127.0.0.1');
  if (u.pathname === '/api/status') {
    sendJson(res, 200, {
      ok: true,
      ...state,
      lastOdom: state.lastOdom,
      uptime_s: Math.round((Date.now() - state.startedAt) / 1000),
      ws: `ws://127.0.0.1:${PORT}/ws/telemetry`,
      live_url: `http://127.0.0.1:${PORT}/localmap-viewer/index.html?live=1&domain=${encodeURIComponent(state.domain_id)}`,
    });
    return true;
  }
  if (u.pathname === '/api/domains') {
    if (req.method === 'POST') {
      readBody(req)
        .then((body) => {
          try {
            const created = createEmptyDomainOnDisk(body || {});
            sendJson(res, 201, created);
          } catch (e) {
            sendJson(res, e.status || 500, { error: e.message || String(e), id: e.id });
          }
        })
        .catch((e) => sendJson(res, 400, { error: 'invalid JSON', detail: String(e) }));
      return true;
    }
    const reg = readJsonSafe(path.join(DATA_DIR, 'domains.json')) || { active: null, domains: [] };
    sendJson(res, 200, reg);
    return true;
  }
  const occMatch = u.pathname.match(/^\/api\/occupancy\/([^/]+)$/);
  if (occMatch) {
    const id = decodeURIComponent(occMatch[1]);
    const occ = readJsonSafe(path.join(DATA_DIR, 'domains', id, 'occupancy.json'));
    if (!occ) {
      sendJson(res, 404, { error: 'domain not found', id });
      return true;
    }
    sendJson(res, 200, occ);
    return true;
  }
  return false;
}

const server = http.createServer((req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    });
    res.end();
    return;
  }
  if (handleApi(req, res)) return;
  if (serveStatic(req, res)) return;
  res.writeHead(404);
  res.end('not found');
});

const wss = new WebSocketServer({ noServer: true });
const clients = new Set();

function broadcast(obj) {
  const raw = JSON.stringify(obj);
  for (const ws of clients) {
    if (ws.readyState === 1) ws.send(raw);
  }
}

server.on('upgrade', (req, socket, head) => {
  const u = new URL(req.url || '/', 'http://127.0.0.1');
  if (u.pathname !== '/ws/telemetry') {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit('connection', ws, req);
  });
});

wss.on('connection', (ws) => {
  clients.add(ws);
  state.clients = clients.size;
  ws.send(JSON.stringify({
    type: 'status',
    t: Date.now() / 1000,
    mode: state.mode,
    connected: true,
    hz: state.hz,
    domain_id: state.domain_id,
    battery: state.battery,
    clients: state.clients,
  }));
  if (state.lastOdom) ws.send(JSON.stringify(state.lastOdom));
  const occ = readJsonSafe(path.join(DATA_DIR, 'domains', state.domain_id, 'occupancy.json'));
  if (occ) {
    ws.send(JSON.stringify({ type: 'occupancy', ...occ, domain_id: occ.domain_id || state.domain_id }));
  }
  ws.on('close', () => {
    clients.delete(ws);
    state.clients = clients.size;
  });
  ws.on('message', (buf) => {
    try {
      const msg = JSON.parse(String(buf));
      if (msg && msg.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong', t: Date.now() / 1000 }));
      }
    } catch (_) { /* ignore */ }
  });
});

function stepMock(dt) {
  phase += dt;
  // gentle warehouse aisle walk: forward with slow yaw weave
  const speed = 0.42 + 0.08 * Math.sin(phase * 0.35);
  odom.wz = 0.18 * Math.sin(phase * 0.55);
  odom.vx = speed;
  odom.vy = 0.02 * Math.sin(phase * 0.9);
  odom.yaw += odom.wz * dt;
  odom.x += (Math.cos(odom.yaw) * odom.vx - Math.sin(odom.yaw) * odom.vy) * dt;
  odom.y += (Math.sin(odom.yaw) * odom.vx + Math.cos(odom.yaw) * odom.vy) * dt;
  // soft bounds bounce inside ~±5 m bay
  if (Math.abs(odom.x) > 5.5 || Math.abs(odom.y) > 4.5) {
    odom.yaw += Math.PI * 0.55;
    odom.x = Math.max(-5.2, Math.min(5.2, odom.x));
    odom.y = Math.max(-4.2, Math.min(4.2, odom.y));
  }
  state.battery = Math.max(12, 92 - ((Date.now() - state.startedAt) / 1000) * 0.01);
}

function emitOdom() {
  const msg = {
    type: 'odom',
    t: Date.now() / 1000,
    x: +odom.x.toFixed(4),
    y: +odom.y.toFixed(4),
    z: 0,
    yaw: +odom.yaw.toFixed(4),
    vx: +odom.vx.toFixed(4),
    vy: +odom.vy.toFixed(4),
    wz: +odom.wz.toFixed(4),
  };
  state.lastOdom = msg;
  broadcast(msg);
}

let lastTick = Date.now();
if (MOCK) {
  setInterval(() => {
    const now = Date.now();
    const dt = Math.min(0.1, (now - lastTick) / 1000);
    lastTick = now;
    stepMock(dt);
    emitOdom();
  }, Math.max(20, Math.round(1000 / HZ)));
}

setInterval(() => {
  broadcast({
    type: 'status',
    t: Date.now() / 1000,
    mode: state.mode,
    connected: true,
    hz: state.hz,
    domain_id: state.domain_id,
    battery: +state.battery.toFixed(1),
    clients: state.clients,
  });
}, 2000);

// Watch feed.json — when Sky Connect writes occupancy, push to clients
let feedMtime = 0;
function pollFeed() {
  fs.stat(FEED_PATH, (err, st) => {
    if (err || !st.isFile()) return;
    if (st.mtimeMs <= feedMtime) return;
    feedMtime = st.mtimeMs;
    const data = readJsonSafe(FEED_PATH);
    if (!data) return;
    if (data.pose && !MOCK) {
      odom.x = data.pose.x || 0;
      odom.y = data.pose.y || 0;
      odom.yaw = data.pose.yaw || 0;
      emitOdom();
    }
    broadcast({
      type: 'occupancy',
      domain_id: data.domain_id || state.domain_id,
      res_m: data.res_m || 0.08,
      range_m: data.range_m || 3.5,
      pose: data.pose || { x: odom.x, y: odom.y, yaw: odom.yaw },
      trail: data.trail || [],
      cells: data.cells || [],
    });
  });
}
setInterval(pollFeed, 500);
pollFeed();

// Periodically nudge occupancy pose from live odom so HUD + cells stay coherent in mock mode
if (MOCK) {
  setInterval(() => {
    const base = readJsonSafe(path.join(DATA_DIR, 'domains', state.domain_id, 'occupancy.json'));
    if (!base) return;
    broadcast({
      type: 'occupancy',
      domain_id: base.domain_id || state.domain_id,
      res_m: base.res_m || 0.08,
      range_m: base.range_m || 4.5,
      pose: { x: +odom.x.toFixed(3), y: +odom.y.toFixed(3), yaw: +odom.yaw.toFixed(3) },
      trail: base.trail || [],
      cells: base.cells || [],
    });
  }, 4000);
}

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[k1-telemetry] http://127.0.0.1:${PORT}/`);
  console.log(`[k1-telemetry] ws   ws://127.0.0.1:${PORT}/ws/telemetry`);
  console.log(`[k1-telemetry] live http://127.0.0.1:${PORT}/localmap-viewer/index.html?live=1&domain=${DOMAIN}`);
  console.log(`[k1-telemetry] mode=${state.mode} hz=${HZ} feed=${FEED_PATH}`);
});
