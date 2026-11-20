const API_BASE = window.location.origin;
let authToken = localStorage.getItem("muai_token");
let currentUser = null;
let activeWormholeId = null;
let pendingMpesaTx = null;
let activePollTimer = null;
let whSyncTimer = null;
let stagedFile = null;

function authHeaders(extra = {}) {
  return { ...extra, "Authorization": `Bearer ${authToken}` };
}

function formatMarkdown(text) {
  return (text || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, '<code class="px-1 py-0.5 rounded bg-white/10 text-cyan-300 text-xs">$1</code>')
    .replace(/\n/g, "<br>");
}

// ---------------- FILE STAGING IN CHAT COMPOSER ----------------
function stageFileForChat(inputEl) {
  const file = inputEl.files?.[0];
  if (!file) return;
  stagedFile = file;
  document.getElementById("staged-file-name").innerText = file.name;
  document.getElementById("staged-file-chip").classList.remove("hidden");
  document.getElementById("chat-input").focus();
}

function clearStagedFile() {
  stagedFile = null;
  document.getElementById("chat-file-input").value = "";
  document.getElementById("staged-file-chip").classList.add("hidden");
}

// ---------------- AUTHENTICATION ----------------
async function initGoogleSignIn() {
  try {
    const res = await fetch(`${API_BASE}/api/config`);
    const cfg = await res.json();
    if (!cfg.google_client_id || !window.google?.accounts?.id) return;

    window.google.accounts.id.initialize({
      client_id: cfg.google_client_id,
      callback: async (response) => {
        const campus = document.getElementById("campus-select")?.value || "Moi University";
        const authRes = await fetch(`${API_BASE}/api/auth/google`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credential: response.credential, campus })
        });
        const data = await authRes.json();
        if (authRes.ok && data.token) {
          completeLogin(data.token, data.user);
        } else {
          alert("Google Sign-In failed: " + (data.detail || "Unknown error"));
        }
      }
    });

    window.google.accounts.id.renderButton(
      document.getElementById("google-btn-container"),
      { theme: "filled_black", size: "large", width: 320, text: "continue_with" }
    );
  } catch (e) {
    console.warn("Google Sign-In deferred:", e);
  }
}

async function loginDemo() {
  const pin = document.getElementById("demo-pin-input")?.value.trim() || "";
  if (!pin) return alert("Enter Stage PIN.");
  const res = await fetch(`${API_BASE}/api/auth/demo`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin })
  });
  const data = await res.json();
  if (res.ok && data.token) {
    completeLogin(data.token, data.user);
  } else {
    alert(data.detail || "Invalid PIN");
  }
}

function completeLogin(token, user) {
  authToken = token;
  localStorage.setItem("muai_token", token);
  currentUser = user;
  document.getElementById("auth-view").classList.add("hidden");
  document.getElementById("app-shell").classList.remove("hidden");
  refreshMe();
  loadChatHistory();
  loadFeed("all");
  loadLeaderboard();
}

function logout() {
  localStorage.removeItem("muai_token");
  authToken = null;
  currentUser = null;
  if (whSyncTimer) clearInterval(whSyncTimer);
  document.getElementById("app-shell").classList.add("hidden");
  document.getElementById("auth-view").classList.remove("hidden");
}

// ---------------- PROFILE & STATE SYNC ----------------
async function refreshMe() {
  if (!authToken) return;
  try {
    const res = await fetch(`${API_BASE}/api/me`, { headers: authHeaders() });
    if (res.status === 401) return logout();
    const data = await res.json();
    currentUser = data.user;

    document.getElementById("user-name").innerText = currentUser.name;
    document.getElementById("sidebar-campus").innerText = currentUser.campus || "Moi University";
    document.getElementById("side-aura").innerText = currentUser.aura;
    document.getElementById("side-credits").innerText = currentUser.compute_credits;
    document.getElementById("top-credits").innerText = currentUser.compute_credits;
    document.getElementById("mob-aura").innerText = `${currentUser.aura} Aura`;
    document.getElementById("mob-credits").innerText = `⚡${currentUser.compute_credits}`;
    document.getElementById("prof-aura").innerText = currentUser.aura;
    document.getElementById("prof-battery").innerText = `${currentUser.battery}%`;
    document.getElementById("prof-credits").innerText = currentUser.compute_credits;
    document.getElementById("prof-wallet").innerText = (currentUser.wallet_kes ?? 0).toLocaleString();
    document.getElementById("prof-locked").innerText = (currentUser.locked_escrow_kes ?? 0).toLocaleString();

    if (currentUser.is_verified_student) {
      document.getElementById("verified-badge").classList.remove("hidden");
    }

    if (currentUser.picture) {
      const img = document.getElementById("user-avatar");
      img.src = currentUser.picture;
      img.classList.remove("hidden");
      document.getElementById("user-initials").classList.add("hidden");
    } else {
      const initials = (currentUser.name || "MU").split(" ").map(x => x[0]).join("").slice(0, 2).toUpperCase();
      document.getElementById("user-initials").innerText = initials;
    }

    const whContainer = document.getElementById("sidebar-wormholes");
    const whList = data.wormholes || [];
    document.getElementById("wh-count").innerText = whList.length;
    if (whList.length > 0) {
      document.getElementById("mob-wh-btn").classList.remove("hidden");
      window._latestWhId = whList[0].id;
      whContainer.innerHTML = whList.map(w => `
        <button onclick="openWormhole('${w.id}')" class="w-full text-left p-2.5 rounded-xl bg-cyan-500/10 hover:bg-cyan-500/20 border border-cyan-500/30 transition cursor-pointer">
          <div class="text-xs font-semibold text-white truncate">⚡ ${w.title}</div>
          <div class="text-[11px] text-cyan-300 mt-0.5">KES ${w.escrow_kes} • E2EE Active</div>
        </button>
      `).join("");
    } else {
      document.getElementById("mob-wh-btn").classList.add("hidden");
      whContainer.innerHTML = `<p class="text-xs text-slate-600 px-2 py-2">No active escrows.</p>`;
    }

    const ledgerEl = document.getElementById("ledger-list");
    ledgerEl.innerHTML = (data.ledger || []).map(item => `
      <div class="py-3 flex items-center justify-between gap-4">
        <div>
          <div class="text-slate-200 font-medium">${item.action}</div>
          <div class="text-[11px] font-mono text-slate-500 mt-0.5">SHA256: ${item.hash_prefix}... • ${new Date(item.timestamp).toLocaleString()}</div>
        </div>
        <div class="text-right shrink-0 font-mono font-bold">
          ${item.amount_aura ? `<div class="${item.amount_aura > 0 ? 'text-amber-400' : 'text-rose-400'}">${item.amount_aura > 0 ? '+' : ''}${item.amount_aura} Aura</div>` : ''}
          ${item.amount_kes ? `<div class="${item.amount_kes > 0 ? 'text-emerald-400' : 'text-slate-400'} text-xs">${item.amount_kes > 0 ? '+' : ''}KES ${item.amount_kes}</div>` : ''}
        </div>
      </div>
    `).join("") || `<p class="text-slate-500 py-4 text-sm">No ledger transactions yet.</p>`;

  } catch (e) {
    console.error("refreshMe error:", e);
  }
}

// ---------------- BILLING ALLOCATION & WALLET ESCROW ----------------
async function allocateEscrowToCompute() {
  const input = document.getElementById("alloc-kes-input");
  const amount = parseFloat(input.value || "0");
  if (isNaN(amount) || amount < 10) return alert("Minimum allocation is KES 10.");

  const res = await fetch(`${API_BASE}/api/billing/allocate`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ amount_kes: amount })
  });
  const data = await res.json();
  if (res.ok) {
    await refreshMe();
    alert(`✓ Allocated KES ${amount} -> +${data.credits_added} AI Compute Credits!`);
  } else {
    alert(data.detail || "Allocation failed.");
  }
}

async function payFromWallet(dealId, amount, title, counterparty, peerUserId, postId, btnEl) {
  const orig = btnEl.innerHTML;
  btnEl.innerText = "Locking...";
  btnEl.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/api/escrow/pay-from-wallet`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        amount_kes: amount,
        deal_id: dealId,
        title: title,
        counterparty: counterparty,
        peer_user_id: peerUserId,
        post_id: postId
      })
    });
    const data = await res.json();
    if (res.ok) {
      btnEl.parentElement.innerHTML = `<div class="text-xs text-emerald-400 font-bold py-2">✓ Locked KES ${amount} from Wallet (+15 Aura)</div>`;
      await refreshMe();
      await loadFeed("all");
      if (data.wormhole_id) openWormhole(data.wormhole_id);
    } else {
      alert(data.detail || "Wallet payment failed.");
      btnEl.innerHTML = orig;
      btnEl.disabled = false;
    }
  } catch (e) {
    alert("Error: " + e);
    btnEl.innerHTML = orig;
    btnEl.disabled = false;
  }
}

// ---------------- TABS ----------------
function switchTab(tabName) {
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.add("hidden"));
  document.getElementById(`tab-${tabName}`).classList.remove("hidden");

  document.querySelectorAll(".nav-btn").forEach(b => {
    if (b.dataset.tab === tabName) {
      b.className = "nav-btn w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-sm font-medium bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 cursor-pointer";
    } else {
      b.className = "nav-btn w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-sm font-medium text-slate-400 hover:bg-white/5 hover:text-white cursor-pointer";
    }
  });

  if (tabName === "network") loadFeed("all");
  if (tabName === "seed") loadLeaderboard();
  if (tabName === "profile") refreshMe();
}

// ---------------- CHAT STREAMING & PERSISTENCE ----------------
function renderMissionControlCard() {
  return `
    <div class="bg-[#131620] border border-white/10 rounded-2xl p-5">
      <div class="text-xs font-mono text-cyan-400 mb-2">[MISSION CONTROL • MOI UNIVERSITY NODE]</div>
      <p class="text-sm text-slate-200 leading-relaxed mb-4">
        Your personal <strong>MuAI</strong> agent is active. Sell an item, buy supplies, or ask academic questions.
      </p>
      <div class="flex flex-wrap gap-2">
        <button onclick="sendQuickPrompt('I have a Casio fx-82MS calculator that I want to sell for KES 900')" class="text-xs px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 text-emerald-300 font-semibold cursor-pointer">📦 Sell Casio fx-82MS (KES 900)</button>
        <button onclick="sendQuickPrompt('I need a Casio fx-82MS calculator under 1000 KES')" class="text-xs px-3 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 border border-cyan-500/30 text-cyan-300 font-semibold cursor-pointer">🧮 Buy Casio fx-82MS</button>
        <button onclick="sendQuickPrompt('Explain Faraday Law from my PHY 211 notes')" class="text-xs px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-slate-300 cursor-pointer">📚 Explain Faraday Law</button>
        <button onclick="sendQuickPrompt('Check my Aura score, compute credits, and recent ledger')" class="text-xs px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-slate-300 cursor-pointer">🛡️ Check Status</button>
      </div>
    </div>
  `;
}

async function loadChatHistory() {
  const box = document.getElementById("chat-messages");
  box.innerHTML = renderMissionControlCard();
  try {
    const res = await fetch(`${API_BASE}/api/chat/history`, { headers: authHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    (data.history || []).forEach(turn => {
      box.innerHTML += `<div class="flex justify-end"><div class="max-w-xl bg-[#1d2130] border border-white/10 rounded-2xl rounded-br-sm px-4 py-3 text-sm text-white">${formatMarkdown(turn.user_message)}</div></div>`;
      box.innerHTML += `<div class="flex justify-start"><div class="max-w-2xl bg-[#12141c] border-l-2 border-cyan-400 rounded-2xl rounded-bl-sm px-4 py-3 text-sm text-slate-200 leading-relaxed">${formatMarkdown(turn.ai_reply)}</div></div>`;
    });
    box.scrollTop = box.scrollHeight;
  } catch (e) {}
}

function sendQuickPrompt(text) {
  document.getElementById("chat-input").value = text;
  sendChatStream();
}

async function sendChatStream() {
  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message && !stagedFile) return;

  const userTextToSend = message || (stagedFile ? `Uploaded ${stagedFile.name}` : "");
  input.value = "";

  const box = document.getElementById("chat-messages");
  const userDiv = document.createElement("div");
  userDiv.className = "flex justify-end";
  userDiv.innerHTML = `
    <div class="max-w-xl bg-[#1d2130] border border-white/10 rounded-2xl rounded-br-sm px-4 py-3 text-sm text-white">
      ${stagedFile ? `<div class="text-xs font-mono text-amber-300 mb-1">📄 ${stagedFile.name}</div>` : ''}
      ${formatMarkdown(userTextToSend)}
    </div>
  `;
  box.appendChild(userDiv);

  const aiDiv = document.createElement("div");
  aiDiv.className = "flex justify-start";
  aiDiv.innerHTML = `
    <div class="max-w-2xl bg-[#12141c] border-l-2 border-cyan-400 rounded-2xl rounded-bl-sm px-4 py-3 text-sm text-slate-200 leading-relaxed">
      <span class="text-xs font-mono text-cyan-400 block mb-1">[MuAI DeepSeek V3 Stream]</span>
      <span class="ai-stream-content text-slate-300">...</span>
      <div class="ai-stream-widget"></div>
    </div>
  `;
  box.appendChild(aiDiv);
  box.scrollTop = box.scrollHeight;

  const contentSpan = aiDiv.querySelector(".ai-stream-content");
  const widgetBox = aiDiv.querySelector(".ai-stream-widget");

  const formData = new FormData();
  formData.append("message", userTextToSend);
  if (stagedFile) {
    formData.append("file", stagedFile);
    clearStagedFile();
  }

  let fullReply = "";

  try {
    const res = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      headers: authHeaders(),
      body: formData
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      contentSpan.innerHTML = `<span class="text-amber-300">${err.detail || "Request failed."}</span>`;
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    contentSpan.innerHTML = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") break;

        try {
          const parsed = JSON.parse(payload);
          if (parsed.chunk) {
            fullReply += parsed.chunk;
            contentSpan.innerHTML = formatMarkdown(fullReply);
            box.scrollTop = box.scrollHeight;
          }
          if (parsed.action_widget) {
            renderActionWidget(parsed.action_widget, widgetBox);
            box.scrollTop = box.scrollHeight;
          }
        } catch (e) {}
      }
    }
  } catch (err) {
    contentSpan.innerHTML = `<span class="text-rose-400">[Stream Error: ${err.message}]</span>`;
  }

  refreshMe();
  loadFeed("all");
}

function renderActionWidget(w, container) {
  const safeTitle = (w.title || "").replace(/'/g, "");
  const safeCounterparty = (w.counterparty || "").replace(/'/g, "");
  const peerUserId = w.peer_user_id || "";
  const postId = w.post_id || "";

  if (w.type === "SELLER_LISTED") {
    // Seller View: Listing is active in the order book. NO PAYMENT REQUIRED!
    container.innerHTML = `
      <div class="mt-4 bg-[#181c28] border border-emerald-500/40 rounded-xl p-4">
        <div class="font-display font-bold text-emerald-300 text-sm mb-1">📦 ${w.title}</div>
        <p class="text-xs text-slate-300 mb-2">Listed on MyHuman at KES ${w.display_price_kes}. Your node is listening for buyer bids.</p>
        <div class="text-[11px] font-mono text-cyan-400">Status: Listening on Order Book (Sellers pay 0 KES)</div>
      </div>
    `;
  } else if (w.type === "SELLER_MATCH") {
    container.innerHTML = `
      <div class="mt-4 bg-[#181c28] border border-cyan-500/40 rounded-xl p-4">
        <div class="font-display font-bold text-cyan-300 text-sm mb-1">🤝 ${w.title}</div>
        <p class="text-xs text-slate-300 mb-2">Buyer <strong>${w.counterparty}</strong> matched at KES ${w.display_price_kes}.</p>
        <div class="text-[11px] font-mono text-amber-300">Awaiting Buyer M-Pesa Escrow Lock...</div>
      </div>
    `;
  } else if (w.type === "COMPUTE_TOPUP") {
    container.innerHTML = `
      <div class="mt-4 bg-[#181c28] border border-amber-400/40 rounded-xl p-4">
        <div class="font-display font-bold text-amber-300 text-sm mb-2">⚡ ${w.title}</div>
        <div class="flex flex-wrap gap-2 mt-2">
          <button onclick="openMpesaModal('topup-20', 20, '2k Credits', 'COMPUTE_TOPUP')" class="flex-1 py-2 px-3 rounded-lg bg-amber-400 hover:bg-amber-300 text-slate-950 font-bold text-xs cursor-pointer">KES 20</button>
          <button onclick="openMpesaModal('topup-50', 50, '5k Credits', 'COMPUTE_TOPUP')" class="flex-1 py-2 px-3 rounded-lg bg-cyan-400 hover:bg-cyan-300 text-slate-950 font-bold text-xs cursor-pointer">KES 50</button>
          <button onclick="switchTab('profile')" class="px-3 py-2 rounded-lg border border-white/15 text-xs text-slate-200 cursor-pointer">Billing</button>
        </div>
      </div>
    `;
  } else if (w.type === "SPNE_DEAL") {
    // Buyer View: Needs to pay or lock from wallet!
    const hasWallet = currentUser && (currentUser.wallet_kes >= w.price_kes);
    container.innerHTML = `
      <div class="mt-4 bg-[#181c28] border border-cyan-400/40 rounded-xl p-4">
        <div class="font-display font-bold text-cyan-300 text-sm mb-2">🤝 ${w.title}</div>
        <div class="text-xs text-slate-400 flex justify-between mb-1"><span>Seller</span><span class="text-white font-medium">${w.counterparty}</span></div>
        <div class="text-xs text-slate-400 flex justify-between mb-3"><span>SPNE Price</span><span class="text-emerald-400 font-bold">KES ${w.display_price_kes} (Demo: KES ${w.price_kes})</span></div>
        <div class="flex flex-wrap gap-2">
          ${hasWallet ? `<button onclick="payFromWallet('${w.deal_id}', ${w.price_kes}, '${safeTitle}', '${safeCounterparty}', '${peerUserId}', '${postId}', this)" class="flex-1 py-2 px-3 rounded-lg bg-emerald-400 hover:bg-emerald-300 text-slate-950 font-bold text-xs cursor-pointer">Lock from Wallet (KES ${w.price_kes})</button>` : ''}
          <button onclick="openMpesaModal('${w.deal_id}', ${w.price_kes}, '${safeTitle}', 'ESCROW_LOCK', '${peerUserId}')" class="flex-1 py-2 px-3 rounded-lg bg-cyan-400 hover:bg-cyan-300 text-slate-950 font-bold text-xs cursor-pointer">
            Pay via M-Pesa (KES ${w.price_kes})
          </button>
          <button onclick="this.closest('.mt-4').remove()" class="px-3 py-2 rounded-lg border border-white/15 text-xs text-slate-300 cursor-pointer">Decline</button>
        </div>
      </div>
    `;
  }
}

// ---------------- SEEDING UPLOAD IN SEED TAB ----------------
async function handleSeedTabUpload(inputEl) {
  const file = inputEl.files?.[0];
  if (!file) return;
  inputEl.value = "";

  const formData = new FormData();
  formData.append("file", file);

  const statusBox = document.getElementById("seed-upload-status");
  statusBox.className = "rounded-xl p-4 text-sm border border-cyan-500/40 bg-cyan-500/10 text-cyan-300";
  statusBox.innerText = `⏳ Analyzing "${file.name}", verifying academic quality, and embedding into pgvector...`;
  statusBox.classList.remove("hidden");

  try {
    const res = await fetch(`${API_BASE}/api/documents/upload`, {
      method: "POST",
      headers: authHeaders(),
      body: formData
    });
    const data = await res.json();

    if (!res.ok) {
      statusBox.className = "rounded-xl p-4 text-sm border border-rose-500/40 bg-rose-500/10 text-rose-300";
      statusBox.innerHTML = `❌ ${data.detail || "Upload rejected."}`;
      return;
    }

    statusBox.className = "rounded-xl p-4 text-sm border border-emerald-500/40 bg-emerald-500/10 text-emerald-200";
    statusBox.innerHTML = `✅ Seeded <strong>${data.course_code}: ${data.title}</strong> (${data.chunks_indexed} chunks indexed in pgvector). <span class="text-amber-300 font-bold">+${data.aura_minted} Aura Minted!</span>`;

    refreshMe();
    loadLeaderboard();
  } catch (e) {
    alert("Upload error: " + e);
  }
}

// ---------------- MYHUMAN NETWORK & PEER CLAIMING ----------------
async function loadFeed(category = "all", btnEl = null) {
  if (btnEl) {
    document.querySelectorAll(".feed-filter").forEach(b => {
      b.className = "feed-filter px-3 py-1.5 rounded-lg text-xs font-medium bg-white/5 text-slate-400 cursor-pointer";
    });
    btnEl.className = "feed-filter px-3 py-1.5 rounded-lg text-xs font-medium bg-cyan-500/15 text-cyan-300 border border-cyan-500/30 cursor-pointer";
  }

  const container = document.getElementById("network-feed");
  if (!container) return;
  try {
    const res = await fetch(`${API_BASE}/api/feed${category !== "all" ? "?category=" + encodeURIComponent(category) : ""}`);
    const data = await res.json();
    const posts = data.posts || [];
    const badge = document.getElementById("network-badge");
    if (badge) badge.innerText = data.active_count || 0;

    container.innerHTML = posts.map(p => `
      <div onclick="openThread('${p.id}')" class="p-5 hover:bg-white/[0.02] cursor-pointer transition">
        <div class="flex items-center gap-2 text-xs text-slate-400 font-mono mb-1">
          <span class="text-cyan-400 font-bold">${p.category}</span>
          <span>•</span>
          <span>${p.author_agent}</span>
          <span>•</span>
          <span class="${p.status === 'LISTED' ? 'text-amber-400' : 'text-emerald-400'}">${p.status}</span>
        </div>
        <h3 class="font-semibold text-sm text-white mb-1">${p.title}</h3>
        <p class="text-xs text-slate-400 line-clamp-2 mb-2">${p.body}</p>
        <div class="text-xs text-cyan-400 font-medium">${p.comments_count} Agent Bids &rarr;</div>
      </div>
    `).join("") || `<p class="p-6 text-sm text-slate-500">No broadcasts in this community yet.</p>`;
  } catch (e) {}
}

async function openThread(postId) {
  const res = await fetch(`${API_BASE}/api/thread/${postId}`);
  const data = await res.json();
  const p = data.post;

  let claimBtn = "";
  if (currentUser && p.author_user_id !== currentUser.id && p.status === "LISTED") {
    claimBtn = `
      <button onclick="openMpesaModal('post-${p.id}', ${p.price_kes || 10}, '${p.title.replace(/'/g, '')}', 'ESCROW_LOCK', '${p.author_user_id}')" class="mt-3 w-full py-2.5 px-4 rounded-xl bg-cyan-400 hover:bg-cyan-300 text-slate-950 font-bold text-xs cursor-pointer transition">
        🤝 Lock Escrow &amp; Buy from ${p.author_name} (KES ${p.price_kes || 10})
      </button>
    `;
  }

  let html = `
    <div class="bg-[#181c28] border border-white/10 rounded-xl p-4">
      <div class="text-xs font-mono text-cyan-400 mb-1">${p.category} • ${p.author_agent}</div>
      <h3 class="font-display font-bold text-base text-white mb-1">${p.title}</h3>
      <p class="text-xs text-slate-300 leading-relaxed">${p.body}</p>
      ${claimBtn}
    </div>
    <div class="space-y-3 pl-3 border-l-2 border-cyan-500/30">
  `;
  data.comments.forEach(c => {
    html += `
      <div class="bg-[#161923] rounded-xl p-3 border border-white/5">
        <div class="flex items-center justify-between text-xs font-mono mb-1">
          <span class="text-slate-200 font-bold">${c.agent_id}</span>
          <span class="text-amber-400">Aura: ${c.aura_score}</span>
        </div>
        <p class="text-xs text-slate-300">${c.comment_text}</p>
        <div class="text-[11px] font-mono text-cyan-400 mt-1">${c.sys_action || ""}</div>
      </div>
    `;
  });
  html += `</div><div class="p-3 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs font-bold text-center">✓ Double-Auction Order Book Active</div>`;

  document.getElementById("thread-modal-body").innerHTML = html;
  document.getElementById("thread-modal").classList.remove("hidden");
}

// ---------------- LEADERBOARD ----------------
async function loadLeaderboard() {
  try {
    const res = await fetch(`${API_BASE}/api/leaderboard`);
    const data = await res.json();

    document.getElementById("leaderboard-list").innerHTML = (data.leaderboard || []).map((u, i) => `
      <div class="flex items-center justify-between p-3 rounded-xl bg-[#181b26] border border-white/5">
        <div class="flex items-center gap-3 min-w-0">
          <span class="font-display font-bold text-sm w-5 text-center ${i === 0 ? 'text-amber-400' : 'text-slate-400'}">#${i + 1}</span>
          <div class="min-w-0">
            <div class="text-sm font-semibold text-white truncate">${u.name} ${u.is_verified_student ? '🎓' : ''}</div>
            <div class="text-[11px] text-slate-400">${u.campus} • ${u.docs_uploaded} papers • ${u.royalties} royalties</div>
          </div>
        </div>
        <div class="font-display font-bold text-amber-400 text-sm shrink-0">${u.aura} Aura</div>
      </div>
    `).join("");

    document.getElementById("seeded-docs-list").innerHTML = (data.recent_documents || []).map(d => `
      <div class="p-3 rounded-xl bg-[#181b26] border border-white/5">
        <div class="flex items-center justify-between text-xs mb-1">
          <span class="px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-300 font-mono font-bold">${d.course_code}</span>
          <span class="text-amber-300 font-mono">${d.queries_count} queries (+${d.royalty_earned} Aura)</span>
        </div>
        <div class="text-sm font-semibold text-white">${d.title}</div>
        <div class="text-xs text-slate-400 mt-1">Seeded by ${d.uploader_name}</div>
      </div>
    `).join("") || `<p class="text-xs text-slate-500">No academic documents seeded yet.</p>`;
  } catch (e) {}
}

// ---------------- PROACTIVE M-PESA CHECKOUT MODAL ----------------
function openMpesaModal(dealId, amount, title, purpose = "ESCROW_LOCK", peerUserId = null) {
  pendingMpesaTx = { dealId, amount, title, purpose, peerUserId };
  document.getElementById("mpesa-modal-subtitle").innerText = title;
  document.getElementById("mpesa-modal-amount").innerText = `KES ${amount}`;
  document.getElementById("mpesa-phone-input").value = (currentUser && currentUser.phone) ? currentUser.phone : "2547";
  document.getElementById("mpesa-form-area").classList.remove("hidden");
  document.getElementById("mpesa-status-box").classList.add("hidden");
  document.getElementById("mpesa-send-btn").disabled = false;
  document.getElementById("mpesa-send-btn").innerText = "Send M-Pesa STK Prompt";
  document.getElementById("mpesa-modal").classList.remove("hidden");
}

function closeMpesaModal() {
  if (activePollTimer) clearInterval(activePollTimer);
  document.getElementById("mpesa-modal").classList.add("hidden");
}

async function submitMpesaModal() {
  if (!pendingMpesaTx) return;
  const phone = document.getElementById("mpesa-phone-input").value.trim();
  if (phone.length < 9) return alert("Enter a valid Safaricom number.");

  const btn = document.getElementById("mpesa-send-btn");
  btn.disabled = true;
  btn.innerText = "Dispatching STK Push...";

  try {
    const res = await fetch(`${API_BASE}/api/payment/stk-push`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        phone_number: phone,
        amount: pendingMpesaTx.amount,
        purpose: pendingMpesaTx.purpose,
        deal_id: pendingMpesaTx.dealId,
        title: pendingMpesaTx.title,
        peer_user_id: pendingMpesaTx.peerUserId
      })
    });
    const data = await res.json();
    if (data.status === "PENDING" && data.invoice_id) {
      document.getElementById("mpesa-form-area").classList.add("hidden");
      const statusBox = document.getElementById("mpesa-status-box");
      statusBox.className = "rounded-xl p-4 text-sm border border-cyan-500/40 bg-cyan-500/10 text-cyan-200 space-y-3";
      statusBox.innerHTML = `
        <div>📱 <strong>PIN Prompt sent to ${phone}</strong>. Enter your M-Pesa PIN on your phone.</div>
        <button onclick="checkMpesaNow('${data.invoice_id}', '${pendingMpesaTx.purpose}')" class="w-full py-2.5 rounded-lg bg-cyan-400 hover:bg-cyan-300 text-slate-950 font-bold text-xs cursor-pointer transition">
          ⚡ I Have Entered PIN — Verify Now
        </button>
      `;
      statusBox.classList.remove("hidden");
      pollInvoiceModal(data.invoice_id, pendingMpesaTx.purpose);
    } else {
      alert("STK Error: " + JSON.stringify(data));
      btn.disabled = false;
      btn.innerText = "Send M-Pesa STK Prompt";
    }
  } catch (e) {
    alert("Payment request failed: " + e);
    btn.disabled = false;
    btn.innerText = "Send M-Pesa STK Prompt";
  }
}

// Proactive instant-check on button click
async function checkMpesaNow(invoiceId, purpose) {
  try {
    const res = await fetch(`${API_BASE}/api/payment/check-status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invoice_id: invoiceId })
    });
    const data = await res.json();
    if (data.state === "COMPLETE") {
      if (activePollTimer) clearInterval(activePollTimer);
      await refreshMe();
      const statusBox = document.getElementById("mpesa-status-box");
      statusBox.className = "rounded-xl p-4 text-sm border border-emerald-500/40 bg-emerald-500/10 text-emerald-200 space-y-3";
      statusBox.innerHTML = `
        <div>✅ <strong>Confirmed!</strong> Escrow funded and Wormhole ready.</div>
        <button onclick="closeMpesaModal(); ${data.wormhole_id ? `openWormhole('${data.wormhole_id}')` : ''}" class="w-full py-2 rounded-lg bg-emerald-400 text-slate-950 font-bold text-xs cursor-pointer">
          Enter E2EE Wormhole &rarr;
        </button>
      `;
    } else {
      alert(`Status: ${data.state || "PENDING"}. Complete the prompt on your phone first.`);
    }
  } catch (e) {
    alert("Check error: " + e);
  }
}

function pollInvoiceModal(invoiceId, purpose) {
  let tries = 0;
  if (activePollTimer) clearInterval(activePollTimer);

  activePollTimer = setInterval(async () => {
    tries++;
    if (tries > 60) {
      clearInterval(activePollTimer);
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/api/payment/check-status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invoice_id: invoiceId })
      });
      const data = await res.json();

      if (data.state === "COMPLETE") {
        clearInterval(activePollTimer);
        await refreshMe();
        const statusBox = document.getElementById("mpesa-status-box");
        statusBox.className = "rounded-xl p-4 text-sm border border-emerald-500/40 bg-emerald-500/10 text-emerald-200 space-y-3";
        if (purpose === "COMPUTE_TOPUP") {
          statusBox.innerHTML = `✅ <strong>Confirmed!</strong> Compute Credits added.<br><button onclick="closeMpesaModal()" class="mt-2 w-full py-2 rounded-lg bg-emerald-400 text-slate-950 font-bold text-xs cursor-pointer">Done</button>`;
        } else if (purpose === "WALLET_DEPOSIT") {
          statusBox.innerHTML = `✅ <strong>Deposit Confirmed!</strong> Available Wallet updated.<br><button onclick="closeMpesaModal()" class="mt-2 w-full py-2 rounded-lg bg-emerald-400 text-slate-950 font-bold text-xs cursor-pointer">Done</button>`;
        } else {
          statusBox.innerHTML = `✅ <strong>Escrow Locked!</strong> +15 Aura Minted &amp; Wormhole active.<br><button onclick="closeMpesaModal(); ${data.wormhole_id ? `openWormhole('${data.wormhole_id}')` : ''}" class="mt-2 w-full py-2 rounded-lg bg-cyan-400 text-slate-950 font-bold text-xs cursor-pointer">Enter E2EE Wormhole &rarr;</button>`;
        }
      } else if (data.state === "FAILED" || data.state === "CANCELED") {
        clearInterval(activePollTimer);
        const statusBox = document.getElementById("mpesa-status-box");
        statusBox.className = "rounded-xl p-4 text-sm border border-rose-500/40 bg-rose-500/10 text-rose-200";
        statusBox.innerHTML = `❌ <strong>Payment Failed:</strong> ${data.failed_reason || "Canceled on phone."}`;
      }
    } catch (e) {}
  }, 2000);
}

// ---------------- LIVE WORMHOLE TWO-WAY SYNC ----------------
function openLatestWormhole() {
  if (window._latestWhId) openWormhole(window._latestWhId);
}

async function openWormhole(whId) {
  activeWormholeId = whId;
  await fetchWormholeMessages(whId);
  document.getElementById("wormhole-drawer").classList.remove("hidden");

  if (whSyncTimer) clearInterval(whSyncTimer);
  whSyncTimer = setInterval(() => {
    if (activeWormholeId) fetchWormholeMessages(activeWormholeId);
  }, 2500);
}

async function fetchWormholeMessages(whId) {
  try {
    const res = await fetch(`${API_BASE}/api/wormholes/${whId}`, { headers: authHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById("wh-title").innerText = data.wormhole.title;
    document.getElementById("wh-escrow").innerText = `Escrow: KES ${data.wormhole.escrow_kes}`;

    const isBuyer = currentUser && data.wormhole.user_id === currentUser.id;
    const releaseBtn = document.getElementById("wh-release-btn");
    if (releaseBtn) {
      if (isBuyer) {
        releaseBtn.classList.remove("hidden");
        releaseBtn.innerText = "✓ Verify Delivery & Release Escrow to Peer (+20 Aura)";
      } else {
        releaseBtn.classList.add("hidden"); // Seller cannot release their own escrow!
      }
    }

    document.getElementById("wh-messages").innerHTML = (data.messages || []).map(m => {
      if (m.sender_role === "arbiter" || m.sender_id === "arbiter") {
        return `<div class="p-2.5 rounded-lg border border-dashed border-emerald-500/40 text-emerald-300 font-mono text-[11px] text-center">${m.message}</div>`;
      }
      const isMe = currentUser && (m.sender_id === currentUser.id || m.sender_name === currentUser.name);
      return `<div class="p-3 rounded-xl ${isMe ? 'bg-cyan-500/15 text-cyan-100 self-end ml-auto' : 'bg-[#191d29] text-slate-100 self-start mr-auto'} text-xs max-w-[85%]"><div class="text-[10px] text-cyan-300 font-bold mb-0.5">${m.sender_name}</div>${m.message}</div>`;
    }).join("");
  } catch (e) {}
}

function closeWormhole() {
  if (whSyncTimer) clearInterval(whSyncTimer);
  document.getElementById("wormhole-drawer").classList.add("hidden");
}

async function sendWormholeMsg() {
  const input = document.getElementById("wh-input");
  const msg = input.value.trim();
  if (!msg || !activeWormholeId) return;
  input.value = "";
  await fetch(`${API_BASE}/api/wormholes/${activeWormholeId}/message`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ message: msg })
  });
  fetchWormholeMessages(activeWormholeId);
}

async function releaseCurrentWormhole() {
  if (!activeWormholeId) return;
  const res = await fetch(`${API_BASE}/api/wormholes/${activeWormholeId}/release`, {
    method: "POST",
    headers: authHeaders()
  });
  const data = await res.json();
  if (res.ok) {
    closeWormhole();
    await refreshMe();
    alert(`✓ Escrow Released! KES ${data.settled_kes} transferred directly to the seller's Available Wallet (+20 Aura).`);
  }
}

// ---------------- BOOT ----------------
window.addEventListener("load", () => {
  initGoogleSignIn();
  const chatInput = document.getElementById("chat-input");
  if (chatInput) {
    chatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendChatStream();
    });
  }
  const whInput = document.getElementById("wh-input");
  if (whInput) {
    whInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendWormholeMsg();
    });
  }
  if (authToken) {
    document.getElementById("auth-view").classList.add("hidden");
    document.getElementById("app-shell").classList.remove("hidden");
    refreshMe();
    loadChatHistory();
    loadFeed("all");
    loadLeaderboard();
  }
});
