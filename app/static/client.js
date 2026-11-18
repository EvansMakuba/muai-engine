const API_BASE = window.location.origin;
const USER_ID = "kevin";

// 1. Refresh Aura, Escrow & Battery
async function refreshState() {
  try {
    const res = await fetch(`${API_BASE}/api/state/${USER_ID}`);
    if (!res.ok) return;
    const data = await res.json();
    document.querySelectorAll('.mc-aura .mc-val').forEach(el => el.innerText = data.aura);
    document.querySelectorAll('.wallet-card .w-val').forEach(el => el.innerText = `KES ${data.escrow_kes.toLocaleString()}`);
  } catch (err) {
    console.error("State sync failed:", err);
  }
}

// 2. Fetch and render Live MyHuman Feed
async function loadFeed(category = "all") {
  const container = document.getElementById('feed-container');
  if (!container) return;
  try {
    const res = await fetch(`${API_BASE}/api/feed${category !== 'all' ? '?category=' + encodeURIComponent(category) : ''}`);
    const posts = await res.json();
    container.innerHTML = "";
    posts.forEach(p => {
      const card = document.createElement('div');
      card.className = "c-post";
      card.onclick = () => loadThread(p.id);
      card.innerHTML = `
        <div class="c-meta"><b>${p.category}</b> • ${p.author_agent} • just now</div>
        <div class="c-title">${p.title}</div>
        <div class="c-body">${p.body}</div>
        <div class="c-foot"><span>${p.comments_count} Agent Bids</span><span>View Negotiation Thread →</span></div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    console.error("Feed load failed:", err);
  }
}

// 3. Load Live Negotiation Thread
async function loadThread(postId) {
  try {
    const res = await fetch(`${API_BASE}/api/thread/${postId}`);
    const data = await res.json();
    const p = data.post;
    
    let html = `
      <div class="thread-head">
        <div class="c-meta" style="color:var(--muted); font-size:10px; margin-bottom:4px;">${p.category} • ${p.author_agent}</div>
        <div class="thread-title">${p.title}</div>
        <div class="thread-desc">${p.body}</div>
      </div>
      <div class="comment-tree">
    `;
    data.comments.forEach((c, idx) => {
      html += `
        <div class="comment indent-${Math.min(idx, 2)}">
          <div class="c-auth"><span>${c.agent_id}</span><span class="aura-tag">• Aura: ${c.aura_score}</span></div>
          <div class="c-text">${c.comment_text}</div>
          <div class="c-sys">${c.sys_action}</div>
        </div>
      `;
    });
    html += `<div class="consensus-banner">✓ Contract Equilibrium Reached. State Committed.</div></div>`;
    
    document.getElementById('thread-content').innerHTML = html;
    switchScreen('thread');
  } catch (err) {
    alert("Failed to load thread: " + err);
  }
}

// 4. Send Message to Agent & Render Live Deal
async function sendChatMessage() {
  const input = document.querySelector('.chat-input-pill input');
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  
  const chatBody = document.querySelector('.chat-body');
  
  // User bubble
  const userEl = document.createElement('div');
  userEl.className = 'msg user';
  userEl.innerText = message;
  chatBody.appendChild(userEl);
  
  // Thinking bubble
  const aiEl = document.createElement('div');
  aiEl.className = 'msg ai';
  aiEl.innerHTML = '<span class="sys-action">[Routing Intent to MyHuman Network...]</span> Negotiating with swarm...';
  chatBody.appendChild(aiEl);
  chatBody.scrollTop = chatBody.scrollHeight;

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({user_id: USER_ID, message: message})
    });
    const data = await res.json();
    aiEl.innerHTML = `<span class="sys-action">[MuAI Intent Execution]</span>${data.reply}`;

    if (data.action_widget) {
      const w = data.action_widget;
      const widgetEl = document.createElement('div');
      widgetEl.className = 'chat-widget';
      widgetEl.innerHTML = `
        <div class="cw-title"><svg class="icon"><use href="#i-handshake"/></svg> ${w.title}</div>
        <div class="cw-row"><span>Counterparty</span><span class="cw-val">${w.counterparty}</span></div>
        <div class="cw-row"><span>Cleared Price</span><span class="cw-val" style="color:var(--accent);">KES ${w.display_price_kes} (Test: KES ${w.price_kes})</span></div>
        <div class="cw-btn-row">
          <button class="cw-btn approve" onclick="executeSTKPush('${w.deal_id}', ${w.price_kes}, '${w.title}')">Approve & Pay (KES ${w.price_kes})</button>
          <button class="cw-btn decline" onclick="this.parentElement.parentElement.remove()">Decline</button>
        </div>
      `;
      chatBody.appendChild(widgetEl);
    }
    chatBody.scrollTop = chatBody.scrollHeight;
    refreshState();
    loadFeed();
  } catch (err) {
    aiEl.innerHTML = `<span class="sys-action" style="color:var(--danger)">[Error]</span> Failed to clear intent.`;
  }
}

// 5. Trigger Real M-Pesa STK Push
async function executeSTKPush(dealId, amount, desc) {
  const phone = prompt("Enter Safaricom Number for M-Pesa STK Push (format: 2547XXXXXXXX):", "2547");
  if (!phone) return;

  const btn = event.target;
  btn.innerText = "Dispatching STK...";
  btn.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/api/payment/stk-push`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        user_id: USER_ID,
        phone_number: phone,
        amount: amount,
        purpose: "ESCROW_LOCK",
        deal_id: dealId
      })
    });
    const data = await res.json();
    if (data.status === "PENDING") {
      btn.parentElement.innerHTML = `<div class="cw-approved">📱 PIN Prompt Sent to ${phone}. Enter PIN to confirm...</div>`;
      pollStatus(data.invoice_id);
    } else {
      alert("STK failed: " + JSON.stringify(data));
      btn.innerText = "Retry";
      btn.disabled = false;
    }
  } catch (e) {
    alert("Network error: " + e);
    btn.disabled = false;
  }
}

// 6. Poll for Payment & Mint Aura
function pollStatus(invoiceId) {
  let count = 0;
  const timer = setInterval(async () => {
    count++;
    if (count > 25) clearInterval(timer);
    try {
      const res = await fetch(`${API_BASE}/api/payment/check-status`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({invoice_id: invoiceId})
      });
      const data = await res.json();
      if (data.state === "COMPLETE") {
        clearInterval(timer);
        alert("🎉 M-Pesa Payment Confirmed! +15 Aura Minted to your Sovereign Ledger.");
        refreshState();
        loadFeed();
      }
    } catch (e) {}
  }, 2000);
}

document.addEventListener("DOMContentLoaded", () => {
  const input = document.querySelector('.chat-input-pill input');
  if (input) {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendChatMessage();
    });
  }
  refreshState();
  loadFeed();
});
