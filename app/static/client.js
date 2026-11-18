// Client integration for MuAI Hackathon Demo
const API_BASE = window.location.origin;
const USER_ID = "kevin";

// 1. Fetch live user state (Aura, Escrow, Battery)
async function refreshState() {
  try {
    const res = await fetch(`${API_BASE}/api/state/${USER_ID}`);
    if (!res.ok) return;
    const data = await res.json();
    
    // Update Profile / Ledger figures if present in DOM
    const auraEls = document.querySelectorAll('.mc-aura .mc-val, .lh-val');
    auraEls.forEach(el => el.innerText = data.aura);
    
    const walletEls = document.querySelectorAll('.wallet-card .w-val');
    walletEls.forEach(el => el.innerText = `KES ${data.escrow_kes.toLocaleString()}`);
  } catch (err) {
    console.error("State refresh failed:", err);
  }
}

// 2. Send Chat to DeepSeek
async function sendChatMessage(customMsg = null) {
  const input = document.querySelector('.chat-input-pill input');
  const message = customMsg || input.value.trim();
  if (!message) return;
  
  if (!customMsg) input.value = "";
  
  const chatBody = document.querySelector('.chat-body');
  
  // Render user bubble
  const userBubble = document.createElement('div');
  userBubble.className = 'msg user';
  userBubble.innerText = message;
  chatBody.appendChild(userBubble);
  chatBody.scrollTop = chatBody.scrollHeight;
  
  // Render loading placeholder
  const loadingBubble = document.createElement('div');
  loadingBubble.className = 'msg ai';
  loadingBubble.innerHTML = '<span class="sys-action">[DeepSeek: Cognitive Reasoning...]</span> Thinking...';
  chatBody.appendChild(loadingBubble);
  chatBody.scrollTop = chatBody.scrollHeight;

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: USER_ID, message: message })
    });
    
    const data = await res.json();
    
    // Replace loading with real AI response
    loadingBubble.innerHTML = `
      <span class="sys-action">[DeepSeek Core Engine]</span>
      ${data.reply}
    `;

    // If DeepSeek returned a deal widget, render it dynamically
    if (data.action_widget) {
      const widget = data.action_widget;
      const widgetEl = document.createElement('div');
      widgetEl.className = 'chat-widget';
      widgetEl.innerHTML = `
        <div class="cw-title"><svg class="icon"><use href="#i-handshake"/></svg> ${widget.title}</div>
        <div class="cw-row"><span>Counterparty</span><span class="cw-val">${widget.counterparty}</span></div>
        <div class="cw-row"><span>Price</span><span class="cw-val" style="color:var(--accent);">KES ${widget.price_kes}</span></div>
        <div class="cw-btn-row">
          <button class="cw-btn approve" onclick="executeSTKPush('${widget.deal_id}', ${widget.price_kes}, '${widget.title}')">Approve & Pay M-Pesa</button>
          <button class="cw-btn decline" onclick="this.parentElement.parentElement.remove()">Decline</button>
        </div>
      `;
      chatBody.appendChild(widgetEl);
    }
    chatBody.scrollTop = chatBody.scrollHeight;
  } catch (err) {
    loadingBubble.innerHTML = `<span class="sys-action" style="color:var(--danger)">[Error]</span> Failed to reach AI node.`;
  }
}

// 3. Trigger Real M-Pesa STK Push from Browser
async function executeSTKPush(dealId, amount, description) {
  const phone = prompt("Enter Safaricom Phone Number for M-Pesa (format: 2547XXXXXXXX):", "2547");
  if (!phone) return;

  const btn = event.target;
  btn.innerText = "Dispatching STK...";
  btn.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/api/payment/stk-push`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
      btn.parentElement.innerHTML = `
        <div class="cw-approved" style="background:rgba(0,229,255,0.1); border-color:var(--accent); color:var(--accent);">
          📱 PIN Prompt Sent to ${phone}. Enter PIN...
        </div>
      `;
      // Poll invoice status
      pollInvoiceStatus(data.invoice_id);
    } else {
      alert("STK Push Failed: " + JSON.stringify(data));
      btn.innerText = "Retry M-Pesa";
      btn.disabled = false;
    }
  } catch (err) {
    alert("Connection Error: " + err);
    btn.disabled = false;
  }
}

// 4. Poll invoice status until COMPLETE
async function pollInvoiceStatus(invoiceId) {
  let attempts = 0;
  const interval = setInterval(async () => {
    attempts++;
    if (attempts > 30) clearInterval(interval); // 60s timeout

    try {
      const res = await fetch(`${API_BASE}/api/payment/check-status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invoice_id: invoiceId })
      });
      const data = await res.json();
      
      if (data.state === "COMPLETE") {
        clearInterval(interval);
        alert("🎉 M-Pesa Payment Confirmed! +15 Aura Minted to Ledger.");
        refreshState();
        
        // Auto-open wormhole notification
        const trayPill = document.querySelector('.ait-scroll');
        if (trayPill) {
          trayPill.innerHTML += `<span class="ait-pill" onclick="openWormhole('react')">⚡ Deal Active</span>`;
        }
      }
    } catch (e) {
      console.log("Polling...", e);
    }
  }, 2000);
}

// Attach Enter key listener to chat input
document.addEventListener("DOMContentLoaded", () => {
  const input = document.querySelector('.chat-input-pill input');
  if (input) {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendChatMessage();
    });
  }
  refreshState();
});
