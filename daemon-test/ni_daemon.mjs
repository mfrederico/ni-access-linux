#!/usr/bin/env node
/**
 * NTKDaemon replacement — Node.js ZMQ server
 * Speaks the NI protobuf protocol so Native Access can communicate.
 *
 * Ports:
 *   tcp://127.0.0.1:5146 — REQ/REP
 *   tcp://127.0.0.1:5563 — PUB
 */

import { Reply, Publisher } from "zeromq";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { homedir } from "os";
import { join } from "path";

const REQ_PORT = 5146;
const PUB_PORT = 5563;
const TOKEN_FILE = join(homedir(), ".ni-daemon-tokens.json");
const DOWNLOAD_DIR = join(homedir(), "NI-Downloads");
const APP_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJpYXQiOjE2MzQ3MzA1MjYsInN1YiI6ImFwcGxpY2F0aW9uIiwiZGF0YSI6eyJuYW1lIjoiTmF0aXZlQWNjZXNzIiwidmVyc2lvbiI6IjIuMCJ9LCJleHAiOjI1MzQwMjMwMDc5OX0.U6EQdp8WNcOyYFIHWw9tGUDUCEtxSuLmqEOfLB2UCZMYUkmsV5TItuKPbPCg5-_s7Ls3_4vbMDpisfGqXretddhVnBg-UoSJB4vj4RZtZq29_KaSly9cFA2A5lVbCDEM1bKNkKfNSyfDM6Whkdu2ub3aqt3LgAg7dfMVI3-_MY24txhZNW8xQ44M1nVsiUkpMk7nqrhIwcnb7EX-DPLbIQQ2NCLtoEGiA9eeCu19RvekxTxbttghDptkFBYqs_6CTiKmg98BkU8kQn2225LuzLIeD43vA6yHGyPwyvZloO1Pid5TcRH5qjqjLcfnCk65lSEGR39fZY_AnuDQAtF4tg";

let storedTokens = {};
let pub = null;
// Track active deployments for progress reporting
const activeDeployments = new Map(); // deploymentId -> { upid, title, progress, state }

// ============================================================================
// Protobuf helpers
// ============================================================================
function encodeVarint(value) {
  const bytes = [];
  while (value > 0x7f) { bytes.push((value & 0x7f) | 0x80); value >>>= 7; }
  bytes.push(value & 0x7f);
  return Buffer.from(bytes);
}

function decodeVarint(buf, offset = 0) {
  let result = 0, shift = 0;
  while (offset < buf.length) {
    const byte = buf[offset++];
    result |= (byte & 0x7f) << shift;
    if (!(byte & 0x80)) break;
    shift += 7;
  }
  return [result, offset];
}

function encodeField(fieldNum, wireType, value) {
  const tag = encodeVarint((fieldNum << 3) | wireType);
  if (wireType === 0) return Buffer.concat([tag, encodeVarint(value)]);
  if (wireType === 2) {
    const data = typeof value === "string" ? Buffer.from(value, "utf-8") : Buffer.from(value);
    return Buffer.concat([tag, encodeVarint(data.length), data]);
  }
  if (wireType === 5) { // 32-bit float
    const fb = Buffer.alloc(4); fb.writeFloatLE(typeof value === "number" ? value : 0);
    return Buffer.concat([tag, fb]);
  }
  return tag;
}

function decodeFields(buf) {
  const fields = [];
  let offset = 0;
  while (offset < buf.length) {
    const [tag, o1] = decodeVarint(buf, offset); offset = o1;
    const fieldNum = tag >>> 3, wireType = tag & 7;
    if (wireType === 0) { const [v, o2] = decodeVarint(buf, offset); offset = o2; fields.push([fieldNum, wireType, v]); }
    else if (wireType === 2) { const [len, o2] = decodeVarint(buf, offset); offset = o2; fields.push([fieldNum, wireType, buf.slice(offset, offset + len)]); offset += len; }
    else if (wireType === 5) { fields.push([fieldNum, wireType, buf.readFloatLE(offset)]); offset += 4; }
    else if (wireType === 1) { fields.push([fieldNum, wireType, buf.readBigUInt64LE(offset)]); offset += 8; }
    else break;
  }
  return fields;
}

function buildHeader() {
  const version = Buffer.concat([encodeField(1, 0, 8), encodeField(3, 0, 1)]);
  return encodeField(1, 2, version);
}

function buildResponse(fieldNum, body = Buffer.alloc(0)) {
  return Buffer.concat([encodeField(1, 2, buildHeader()), encodeField(fieldNum, 2, body)]);
}

// ============================================================================
// Logging
// ============================================================================
function log(level, msg) {
  const ts = new Date().toISOString();
  console.log(`[${ts}] [daemon] [${level}] ${msg}`);
}

// ============================================================================
// Token persistence
// ============================================================================
function loadTokens() {
  try {
    if (existsSync(TOKEN_FILE)) {
      storedTokens = JSON.parse(readFileSync(TOKEN_FILE, "utf-8"));
      log("info", `Loaded tokens (${storedTokens.access_token?.length ?? 0} chars)`);
    }
  } catch (e) { log("error", `Failed to load tokens: ${e.message}`); }
}

function saveTokens() {
  try {
    writeFileSync(TOKEN_FILE, JSON.stringify(storedTokens));
    log("info", "Tokens saved");
  } catch (e) { log("error", `Failed to save tokens: ${e.message}`); }
}

// ============================================================================
// HTTP helpers
// ============================================================================
async function httpJson(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Accept": "application/json", "User-Agent": "NativeAccess/3.24.0", ...options.headers },
    method: options.method || "GET",
    body: options.body ? JSON.stringify(options.body) : undefined,
    ...(options.body ? { headers: { ...options.headers, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "NativeAccess/3.24.0" } } : {}),
  });
  return resp.json();
}

async function httpText(url, headers = {}) {
  const resp = await fetch(url, { headers: { "User-Agent": "NativeAccess/3.24.0", ...headers } });
  return resp.text();
}

// ============================================================================
// PUB helpers
// ============================================================================
async function publishEvent(fieldNum, body = Buffer.alloc(0)) {
  if (!pub) return;
  const msg = buildResponse(fieldNum, body);
  try {
    await pub.send(["", msg]);
    log("debug", `PUB sent field ${fieldNum} (${msg.length} bytes)`);
  } catch (e) {
    log("error", `PUB send failed field ${fieldNum}: ${e.message}`);
  }
}

// ============================================================================
// Request handlers
// ============================================================================
function handleVersion() {
  log("info", "daemonVersionRequest");
  const body = Buffer.concat([encodeField(1, 0, 1), encodeField(2, 0, 30), encodeField(3, 0, 0), encodeField(4, 2, "0")]);
  return buildResponse(72, body);
}

function handlePreferences() {
  log("info", "getPreferencesRequest");
  const prefs = Buffer.concat([
    encodeField(1, 2, join(homedir(), "NI-Downloads")),
    encodeField(2, 2, join(homedir(), "NI-Instruments")),
    encodeField(3, 2, "/usr/lib/vst"),
  ]);
  return buildResponse(50, encodeField(1, 2, prefs));
}

function handleSetPreferences() {
  log("info", "setPreferencesRequest");
  return buildResponse(52);
}

function handleActiveDeployments() {
  log("info", `activeDeploymentsRequest (${activeDeployments.size} active)`);
  if (activeDeployments.size === 0) return buildResponse(74);

  // Build ActiveDeployment entries
  // state: 0=UNSPECIFIED, 1=QUEUED, 2=DOWNLOADING, 3=INSTALLING, 4=COMPLETED
  let entries = Buffer.alloc(0);
  for (const [deploymentId, dep] of activeDeployments) {
    const entry = Buffer.concat([
      encodeField(1, 2, deploymentId),
      encodeField(2, 2, dep.upid),
      encodeField(3, 2, dep.title || ""),
      encodeField(4, 5, dep.progress || 0),
      encodeField(5, 0, dep.state || 2), // DOWNLOADING
    ]);
    entries = Buffer.concat([entries, encodeField(1, 2, entry)]);
  }
  return buildResponse(74, entries);
}

function handleSubscriptions() {
  log("info", "subscriptionsRequest");
  return buildResponse(78);
}

function handleKompleteHdds() {
  log("info", "currentKompleteHddsRequest");
  return buildResponse(68);
}

async function handleAuth0AccessToken() {
  log("info", "auth0AccessTokenRequest");
  if (storedTokens.access_token) {
    const body = Buffer.concat([
      encodeField(1, 2, storedTokens.access_token),
      encodeField(2, 2, storedTokens.id_token || ""),
    ]);
    return buildResponse(34, body);
  }
  return buildResponse(3); // empty success
}

async function handleAuth0Login(fields) {
  let authCode = "", redirectUri = "", codeVerifier = "";
  for (const [fn, wt, val] of fields) {
    if (fn === 32 && Buffer.isBuffer(val)) {
      for (const [ifn, iwt, iv] of decodeFields(val)) {
        if (ifn === 1 && Buffer.isBuffer(iv)) authCode = iv.toString();
        if (ifn === 2 && Buffer.isBuffer(iv)) redirectUri = iv.toString();
        if (ifn === 3 && Buffer.isBuffer(iv)) codeVerifier = iv.toString();
      }
    }
  }
  log("info", `auth0LoginRequest: code=${authCode.slice(0, 20)}... redirect=${redirectUri}`);

  try {
    const data = await httpJson("https://auth.native-instruments.com/oauth/token", {
      method: "POST",
      body: {
        grant_type: "authorization_code",
        client_id: "GgcQZ2OCSvzqgVL7RSAoErQRNB9S59kh",
        code: authCode, redirect_uri: redirectUri, code_verifier: codeVerifier,
      },
    });
    if (data.access_token) {
      log("info", "Token exchange successful!");
      storedTokens = data;
      saveTokens();
      publishEvent(69); // userLoggedInEvent
      const body = Buffer.concat([
        encodeField(1, 2, data.access_token),
        encodeField(2, 2, data.id_token || ""),
      ]);
      return buildResponse(34, body);
    }
    log("error", `Token exchange failed: ${JSON.stringify(data)}`);
  } catch (e) { log("error", `Token exchange error: ${e.message}`); }
  return buildResponse(3);
}

async function handleUserInfo() {
  log("info", "userInfoRequest");
  try {
    const user = await httpJson("https://auth.native-instruments.com/userinfo", {
      headers: { "Authorization": `Bearer ${storedTokens.access_token}` },
    });
    log("info", `User: ${user.email} (${user.native_id})`);
    const body = Buffer.concat([
      encodeField(1, 2, user.native_id || user.sub || ""),
      encodeField(2, 2, user.email || ""),
      encodeField(3, 2, user.nickname || ""),
    ]);
    return buildResponse(43, body);
  } catch (e) { log("error", `UserInfo error: ${e.message}`); }
  return buildResponse(3);
}

async function handleKnownProducts() {
  log("info", "knownProductsRequest");
  const token = storedTokens.access_token;
  if (!token) return buildResponse(48);

  try {
    const headers = { "Authorization": `Bearer ${token}`, "X-NI-App-Token": APP_TOKEN };
    const [productsData, artifactsData] = await Promise.all([
      httpJson("https://api.native-instruments.com/v1/users/me/products", { headers }),
      httpJson("https://api.native-instruments.com/v2/download/me/full-products", { headers }),
    ]);

    const products = productsData.response_body?.products || [];
    const artifacts = artifactsData.artifacts || [];
    const titleMap = {};
    for (const a of artifacts) { if (a.upid && !titleMap[a.upid]) titleMap[a.upid] = a.product_title; }

    log("info", `Fetched ${products.length} products, ${Object.keys(titleMap).length} titles`);

    let entries = Buffer.alloc(0);
    for (const p of products) {
      const title = titleMap[p.upid];
      if (!title) continue;
      const product = Buffer.concat([
        encodeField(1, 2, p.upid),
        encodeField(3, 2, title),
        encodeField(7, 0, 0),  // installed = false
        encodeField(11, 0, 1), // activationState = ACTIVATED
        encodeField(12, 0, 1), // isOwned = true
        encodeField(15, 0, 1), // isInstallable = true
      ]);
      entries = Buffer.concat([entries, encodeField(1, 2, product)]);
    }

    // Don't publish productListRefreshedEvent here — it causes an infinite loop
    // The event should only be published after refreshProductListRequest
    return buildResponse(48, entries);
  } catch (e) { log("error", `KnownProducts error: ${e.message}`); }
  return buildResponse(48);
}

async function handleStartDeployments(fields) {
  const deployments = [];
  for (const [fn, wt, val] of fields) {
    if (fn === 91 && Buffer.isBuffer(val)) {
      for (const [ifn, iwt, iv] of decodeFields(val)) {
        if (ifn === 1 && Buffer.isBuffer(iv)) {
          const depFields = decodeFields(iv);
          const upid = depFields.find(f => f[0] === 1 && Buffer.isBuffer(f[2]))?.[2]?.toString() || "";
          if (upid) deployments.push(upid);
        }
      }
    }
  }
  log("info", `startDeploymentsRequest: ${deployments.length} deployments`);

  // Generate deployment IDs and build response with results
  const deploymentsWithIds = [];
  let resultsBody = Buffer.alloc(0);
  for (const upid of deployments) {
    const deploymentId = crypto.randomUUID();
    deploymentsWithIds.push({ upid, deploymentId });
    // DeploymentResult: upid (field 1), type (field 2), deploymentId (field 3)
    const result = Buffer.concat([
      encodeField(1, 2, upid),
      encodeField(2, 0, 1), // DEPLOYMENT_TYPE_FULL
      encodeField(3, 2, deploymentId),
    ]);
    resultsBody = Buffer.concat([resultsBody, encodeField(1, 2, result)]);
  }

  // Start downloads in background — events published from there
  processDeployments(deploymentsWithIds);

  // Return response with deployment results
  return buildResponse(92, resultsBody);
}

async function processDeployments(deploymentsWithIds) {
  const token = storedTokens.access_token;
  if (!token) return;
  mkdirSync(DOWNLOAD_DIR, { recursive: true });

  const headers = { "Authorization": `Bearer ${token}`, "X-NI-App-Token": APP_TOKEN };
  const artifactsData = await httpJson("https://api.native-instruments.com/v2/download/me/full-products", { headers });
  const allArtifacts = artifactsData.artifacts || [];

  for (const { upid, deploymentId } of deploymentsWithIds) {
    const matching = allArtifacts.filter(a => a.upid === upid);
    let artifact = null;
    for (const pref of ["linux", "nativeos", "pc", "all"]) {
      artifact = matching.find(a => a.platform?.includes(pref));
      if (artifact) break;
    }
    if (!artifact) artifact = matching[0];
    if (!artifact) { log("error", `No artifact for ${upid}`); continue; }

    const filename = artifact.target_file;
    const updateId = artifact.update_id;
    log("info", `Downloading ${filename}...`);

    const evBody = Buffer.concat([encodeField(1, 2, deploymentId), encodeField(2, 2, upid)]);

    // Track this deployment
    activeDeployments.set(deploymentId, { upid, title: artifact.product_title || filename, progress: 0, state: 1 });

    await publishEvent(57, evBody); // downloadEnqueuedEvent
    await publishEvent(90);         // downloadQueueChangedEvent

    activeDeployments.get(deploymentId).state = 2; // DOWNLOADING
    await publishEvent(20, evBody); // downloadStartedEvent

    try {
      const mlText = await httpText(
        `https://api.native-instruments.com/v2/download/links/${upid}/${updateId}`, { ...headers }
      );
      const urlMatch = mlText.match(/<url>([^<]+)<\/url>/);
      if (!urlMatch) { log("error", "No URL in metalink"); continue; }
      const cdnUrl = urlMatch[1];

      const resp = await fetch(cdnUrl);
      const total = parseInt(resp.headers.get("content-length") || artifact.filesize || 0);
      const dest = join(DOWNLOAD_DIR, filename);
      const fileHandle = await import("fs").then(fs => fs.createWriteStream(dest));

      let downloaded = 0, lastPct = 0;
      const reader = resp.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        fileHandle.write(value);
        downloaded += value.length;
        const pct = total > 0 ? downloaded / total : 0;
        if (Math.floor(pct * 20) > lastPct) {
          lastPct = Math.floor(pct * 20);
          const progBody = Buffer.concat([
            encodeField(1, 2, deploymentId),
            encodeField(2, 5, pct),
            encodeField(3, 2, upid),
          ]);
          publishEvent(21, progBody); // downloadProgressedEvent
          // Update tracked state
          const dep = activeDeployments.get(deploymentId);
          if (dep) dep.progress = pct;
          log("info", `${filename}: ${Math.round(pct * 100)}%`);
        }
      }
      fileHandle.end();
      log("info", `Downloaded: ${dest} (${downloaded} bytes)`);

      await publishEvent(23, evBody); // downloadSucceededEvent

      const dep = activeDeployments.get(deploymentId);
      if (dep) dep.state = 3; // INSTALLING
      await publishEvent(24, evBody); // installationStartedEvent

      if (dep) dep.state = 4; // COMPLETED
      await publishEvent(26, evBody); // installationSucceededEvent

      // Remove from active deployments
      activeDeployments.delete(deploymentId);
      await publishEvent(90); // downloadQueueChangedEvent
      log("info", `Completed: ${filename}`);
    } catch (e) {
      log("error", `Download failed: ${e.message}`);
      publishEvent(22, evBody); // downloadFailedEvent
    }
  }
}

function handleRefreshProductList() {
  log("info", "refreshProductListRequest");
  setTimeout(() => publishEvent(85), 1000); // productListRefreshedEvent
  return buildResponse(3); // successResponse
}

// ============================================================================
// Request router
// ============================================================================
async function handleRequest(raw) {
  const fields = decodeFields(raw);
  const fieldNums = new Set(fields.map(f => f[0]));

  if (fieldNums.has(71)) return handleVersion();
  if (fieldNums.has(49)) return handlePreferences();
  if (fieldNums.has(51)) return handleSetPreferences();
  if (fieldNums.has(33)) return handleAuth0AccessToken();
  if (fieldNums.has(32)) return handleAuth0Login(fields);
  if (fieldNums.has(42)) return handleUserInfo();
  if (fieldNums.has(47)) return handleKnownProducts();
  if (fieldNums.has(73)) return handleActiveDeployments();
  if (fieldNums.has(77)) return handleSubscriptions();
  if (fieldNums.has(67)) return handleKompleteHdds();
  if (fieldNums.has(87)) return handleRefreshProductList();
  if (fieldNums.has(91)) return handleStartDeployments(fields);

  log("info", `Unknown request (fields: ${[...fieldNums]})`);
  return buildResponse(3);
}

// ============================================================================
// Main
// ============================================================================
async function main() {
  log("info", "=".repeat(60));
  log("info", "NI Daemon (Node.js) starting...");
  log("info", `REQ/REP: tcp://127.0.0.1:${REQ_PORT}`);
  log("info", `PUB:     tcp://127.0.0.1:${PUB_PORT}`);
  log("info", "=".repeat(60));

  loadTokens();

  const rep = new Reply({ linger: 0 });
  await rep.bind(`tcp://127.0.0.1:${REQ_PORT}`);
  log("info", `REQ/REP listening on ${REQ_PORT}`);

  pub = new Publisher({ linger: 0 });
  await pub.bind(`tcp://127.0.0.1:${PUB_PORT}`);
  log("info", `PUB listening on ${PUB_PORT}`);

  // Startup sequence (delayed for subscribers to connect)
  let firstRequest = false;

  // Heartbeat loop
  setInterval(() => {
    const body = encodeField(1, 0, 3); // HEARTBEAT
    publishEvent(44, body);
  }, 30000);

  // REQ/REP loop
  for await (const [msg] of rep) {
    try {
      if (!firstRequest) {
        firstRequest = true;
        // Send startup events after first request (subscriber should be connected)
        setTimeout(() => {
          publishEvent(44, encodeField(1, 0, 1)); // STARTUP_STARTED
          log("info", "Published STARTUP_STARTED");
          setTimeout(() => {
            publishEvent(44, encodeField(1, 0, 2)); // STARTUP_ENDED
            log("info", "Published STARTUP_ENDED");
          }, 1000);
        }, 1500);
      }

      const response = await handleRequest(msg);
      await rep.send(response);
    } catch (e) {
      log("error", `Request error: ${e.message}`);
      await rep.send(buildResponse(3));
    }
  }
}

main().catch(e => { console.error(e); process.exit(1); });
