'use strict';
/* ParcelPilot Support Intelligence — front end.

   Three screens, one at a time: sign-in, the customer portal, the employee
   portal. The two portals are built separately rather than as one screen with
   things greyed out, because a customer has no business seeing that a Signal
   Board exists. The server enforces this regardless — see app/core/principal.py
   — but the interface should not advertise doors it will not open.
*/

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c;
  if (txt !== undefined) e.textContent = txt; return e; };

let SESSION = null;     // {session_id, display_name, role, account_id, scope, snapshot}
let PORTAL = null;      // 'customer' | 'staff'
let USERS = [];
let BUSY = false;

/* Which DOM belongs to which portal. Everything else in this file is shared. */
const PANE = {
  customer: { messages: '#custMessages', input: '#custInput',
              send: '#custSend', chips: '#custSuggestions' },
  staff:    { messages: '#staffMessages', input: '#staffInput',
              send: '#staffSend', chips: '#staffSuggestions' },
};
const pane = (k) => $(PANE[PORTAL][k]);

const SUGGESTIONS = {
  customer: [
    "Can I cancel ORD-1001 without a cancellation fee?",
    "My pickup is running late — do I get a service credit?",
    "How quickly will you respond to a critical outage?",
    "Is bulk upload included in my plan?",
  ],
  staff: [
    "Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.",
    "A pickup is three hours late because of carrier fault. Should I get a service credit?",
    "Is TKT-505 within SLA?",
    "What needs my attention right now?",
    "LumenWorks says bulk upload fails on a 4,200-row CSV. What do I tell them?",
  ],
};

/* Starter questions for a chosen ticket or order. Generic samples ("Can I
   cancel ORD-1001") are noise once the customer has already told us what this
   is about. */
function contextSuggestions(context) {
  return context.kind === 'order'
    ? [`What is happening with ${context.ref}?`,
       'Can I cancel this without a fee?',
       'Am I owed a service credit?']
    : [`What is the status of ${context.ref}?`,
       'When will someone respond?',
       'What can I do in the meantime?'];
}

/* A customer sees what the assistant is doing in plain language; staff see the
   real tool name, because they are the ones who need to audit the routing. */
const FRIENDLY = {
  search_policy_documents: 'Reading ParcelPilot policy documents',
  lookup_account: 'Checking your account',
  lookup_order: 'Looking up the order',
  lookup_tickets: 'Checking support tickets',
  evaluate_policy_decision: 'Applying the policy rules',
  propose_action: 'Preparing an action for your confirmation',
  get_operational_signals: 'Reviewing operational signals',
  get_proactive_outreach: 'Reviewing proactive outreach',
};

const escapeHtml = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');


/* ==================================================================== */
/* Screens                                                              */
/* ==================================================================== */

function showScreen(id) {
  $$('.screen').forEach(s => s.classList.toggle('active', s.id === id));
}

function showLoginStep(id) {
  $$('.login-step').forEach(s => s.classList.toggle('active', s.id === id));
}


/* ==================================================================== */
/* Sign in                                                              */
/* ==================================================================== */

async function boot() {
  try {
    const h = await (await fetch('/api/health')).json();
    $('#loginSnapshot').textContent = `dataset snapshot · ${h.snapshot}`;
  } catch { $('#loginSnapshot').textContent = 'could not reach the server'; }
  USERS = (await (await fetch('/api/users')).json()).users;
}

function openIdentityStep(portal) {
  PORTAL = portal;
  const isCust = portal === 'customer';
  $('#identityLabel').textContent = isCust
    ? 'Which account are you signing in for?'
    : 'Which employee are you?';

  const list = $('#identityList');
  list.innerHTML = '';
  USERS.filter(u => (u.role === 'customer') === isCust).forEach((u, i) => {
    const row = el('button', 'identity');
    row.style.animationDelay = `${i * 45}ms`;
    const av = el('span', 'avatar', u.display_name.trim()[0].toUpperCase());
    const meta = el('span', 'imeta');
    meta.appendChild(el('span', 'iname', u.display_name.replace(/\s*\(.*\)$/, '')));
    meta.appendChild(el('span', 'irole', u.account_id
      ? `${u.display_name.replace(/^[^(]*\(|\)$/g, '')} · ${u.account_id}`
      : u.role.replace(/_/g, ' ')));
    row.appendChild(av); row.appendChild(meta);
    row.appendChild(el('span', 'igo', '→'));
    row.onclick = () => signIn(u.key);
    list.appendChild(row);
  });
  showLoginStep('step-identity');
}

async function signIn(userKey) {
  const r = await fetch('/api/session', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_key: userKey }) });
  if (!r.ok) { alert('Could not start a session.'); return; }
  SESSION = await r.json();
  SESSION.user_key = userKey;      // openSession() re-authenticates with it
  PORTAL = SESSION.role === 'customer' ? 'customer' : 'staff';

  if (PORTAL === 'customer') {
    $('#custSnapshot').textContent = `dataset snapshot · ${SESSION.snapshot}`;
    $('#custWho').textContent = SESSION.display_name;
    $('#custScope').textContent = `account ${SESSION.account_id}`;
  } else {
    $('#staffSnapshot').textContent = `dataset snapshot · ${SESSION.snapshot}`;
    $('#staffWho').textContent = SESSION.display_name;
    $('#staffScope').textContent = SESSION.scope;
    switchView('chat');
  }

  pane('messages').innerHTML = '';
  if (PORTAL === 'staff') {
    addSystem('Signed in with cross-account access. Everything you do here is '
              + 'recorded in the audit log.');
    renderChatEmpty();
    renderTraceEmpty();
    renderSuggestions();
    showScreen('screen-staff');
    pane('input').focus();
  } else {
    // A customer is asked what this is about BEFORE the chat opens, so the
    // assistant starts with the record in hand instead of asking for it.
    showScreen('screen-customer');
    showCustView('picker');
    loadPicker();
  }
}

function signOut() {
  SESSION = null; PORTAL = null;
  CONVO_SELECTED = null; CONVO_SCOPE = 'customers';
  showLoginStep('step-portal');
  showScreen('screen-login');
}

function renderSuggestions(context) {
  const box = pane('chips'); box.innerHTML = '';
  const list = (PORTAL === 'customer' && context && context.ref)
    ? contextSuggestions(context)
    : SUGGESTIONS[PORTAL];
  list.forEach(s => {
    const c = el('button', 'chip', s);
    c.onclick = () => { pane('input').value = s; send(); };
    box.appendChild(c);
  });
}


/* ==================================================================== */
/* Chat rendering                                                        */
/* ==================================================================== */

/* The opening screen of the assistant.

   It replaces a lone italic line stranded above 550px of empty column. What it
   says is chosen deliberately: the three lines below are the system's
   CONSTRAINTS, not its features. A support agent's first question about a tool
   like this is "how do I know it isn't making this up", and answering that
   before they type is worth more than a welcome message. */
const EMPTY_POINTS = {
  staff: [
    ['Every figure is traced', 'Amounts, deadlines and limits come from the policy engine or a cited clause. An answer asserting a number no tool returned is withheld, not shown.'],
    ['Conflicts are surfaced', 'Superseded policies and wrong past resolutions are still searched, so the system can tell you they disagree with current authority.'],
    ['Nothing is executed', 'Credits, escalations and emails are prepared for your confirmation. The agent cannot commit them.'],
  ],
  customer: [
    ['Answers come from your contract', 'Your agreement is checked before the general policy, because it usually overrides it.'],
    ['Scoped to your account', 'Only your own orders, tickets and agreement are visible in this session.'],
    ['No guesses', 'If your question is not covered by the documents, you will be told that and offered a human.'],
  ],
};

function renderChatEmpty(context) {
  const box = pane('messages');
  const who = PORTAL === 'staff' ? SESSION.display_name.split(' ')[0] : SESSION.display_name;
  const wrap = el('div', 'chat-empty');
  wrap.appendChild(el('h2', null, context && context.ref
    ? `About ${context.ref}` : `Hello, ${who}.`));
  wrap.appendChild(el('p', null, PORTAL === 'staff'
    ? 'Ask about an order, a policy, an SLA or an account. The assistant routes each question through the document corpus and the deterministic policy engine, and shows you every tool call it makes.'
    : (context && context.label
        ? `I have ${context.ref} open \u2014 "${context.label}". Ask me anything about it; you do not need to repeat the reference.`
        : 'Ask about your orders, your agreement or a support ticket. Answers are drawn from ParcelPilot\u2019s current policies and your own contract.')));
  const pts = el('div', 'ce-points');
  EMPTY_POINTS[PORTAL].forEach(([title, body]) => {
    const row = el('div', 'cep');
    row.appendChild(el('i', null, '\u2713'));
    const t = el('div');
    t.appendChild(el('b', null, title + ' '));
    t.appendChild(document.createTextNode(body));
    row.appendChild(t);
    pts.appendChild(row);
  });
  wrap.appendChild(pts);
  wrap.appendChild(el('div', 'ce-hint', 'Pick a question below, or type your own.'));
  box.appendChild(wrap);
}

function clearChatEmpty() {
  const e = pane('messages').querySelector('.chat-empty');
  if (e) e.remove();
}

/* The trace column is empty until the first question, and "Tool calls appear
   here" wastes the space. Showing the four tool CATEGORIES instead tells a
   reviewer how capability is partitioned before anything runs -- and the
   colours match the dots used on the calls themselves. */
function renderTraceEmpty() {
  const t = $('#trace');
  t.innerHTML = '';
  const wrap = el('div', 'trace-empty');
  wrap.appendChild(el('p', null,
    'Every tool call the agent makes appears here as it happens, with its arguments and its raw result. Five categories are available to this session:'));
  const legend = el('div', 'trace-legend');
  [['doc', 'Document retrieval', 'Policies, SOPs, product docs and contracts \u2014 filtered by authority and tenant.'],
   ['data', 'Structured data', 'Accounts, orders and tickets from the workbook, access-checked per row.'],
   ['rule', 'Deterministic policy engine', 'Fees, credits and SLA targets come from code, never the model\u2019s arithmetic.'],
   ['act', 'State-changing action', 'Prepared for human confirmation. Nothing commits without a click.'],
   ['ops', 'Operations intelligence', 'The cross-account signal picture. Internal roles only \u2014 a customer session is never offered it.'],
  ].forEach(([cls, name, desc]) => {
    const row = el('div', `tl ${cls}`);
    row.appendChild(el('i'));
    const d = el('div');
    d.appendChild(el('b', null, name));
    d.appendChild(el('span', null, desc));
    row.appendChild(d);
    legend.appendChild(row);
  });
  wrap.appendChild(legend);
  t.appendChild(wrap);
}

function scroll() { const m = pane('messages'); m.scrollTop = m.scrollHeight; }
function addSystem(t) { pane('messages').appendChild(el('div', 'msg system', t)); scroll(); }
function addUser(t) { pane('messages').appendChild(el('div', 'msg user', t)); scroll(); }

/* Minimal, escaping-first markdown. Deliberately not a full parser: the answer
   text is model output and must never be able to inject markup. */
function md(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .split(/\n\n+/)
    .map(block => {
      const lines = block.split('\n');
      if (lines.every(l => /^\s*[-*•]\s+/.test(l))) {
        return '<ul>' + lines.map(l => `<li>${l.replace(/^\s*[-*•]\s+/, '')}</li>`).join('') + '</ul>';
      }
      return `<p>${lines.join('<br/>')}</p>`;
    }).join('');
}

function botShell() {
  const wrap = el('div', 'msg bot');
  const steps = el('div', 'steps');            // live tool chips, both portals
  const body = el('div', 'body thinking');
  body.innerHTML = '<div class="typing"><i></i><i></i><i></i></div>';
  wrap.appendChild(steps); wrap.appendChild(body);
  pane('messages').appendChild(wrap); scroll();
  return { wrap, steps, body };
}

function stepChip(steps, ev) {
  if (PORTAL === 'customer') return;      // see renderTrust for why
  const chip = el('div', 'step');
  chip.id = `st-${ev.call_id}`;
  chip.appendChild(el('span', 'sspin'));
  chip.appendChild(el('span', 'slabel',
    PORTAL === 'customer' ? (FRIENDLY[ev.tool] || 'Working') : ev.tool));
  steps.appendChild(chip);
  scroll();
}

function stepDone(ev) {
  if (PORTAL === 'customer') return;
  const chip = document.getElementById(`st-${ev.call_id}`);
  if (!chip) return;
  const denied = /denied/i.test(ev.result || '');
  chip.classList.add(denied ? 'denied' : 'done');
  chip.querySelector('.sspin')?.replaceWith(el('span', 'stick', denied ? '✕' : '✓'));
  if (PORTAL === 'staff') chip.title = ev.result || '';
}

/* A customer is not shown the trust strip, the citations or the conflict panel.

   This is not a simplification for its own sake. Those surfaces exist so an
   AGENT can audit the answer; putting "MEDIUM confidence · 65%" in front of the
   customer asks them to do quality control on their own support reply, which is
   our job, not theirs. The layer still runs -- a withheld answer is still
   withheld, and the whole assessment is still recorded for the employee console
   -- the customer simply sees the outcome rather than the machinery. */
function renderTrust(wrap, t) {
  if (PORTAL === 'customer') return;
  const strip = el('div', 'trust');
  if (t.band === 'CLARIFYING') {
    strip.appendChild(el('span', 'pill src', 'awaiting more detail'));
  } else {
    strip.appendChild(el('span', `pill ${t.band}`,
      `${t.band} confidence · ${(t.confidence * 100).toFixed(0)}%`));
  }
  if (t.used_policy_engine) strip.appendChild(el('span', 'pill src', 'deterministic decision'));
  (t.citations || []).slice(0, 4).forEach(c => strip.appendChild(el('span', 'pill src', c)));
  if (t.reasons?.length) strip.appendChild(el('div', 'trust-why', t.reasons.join(' ')));
  wrap.appendChild(strip);

  (t.conflicts || []).forEach(c => {
    const box = el('div', 'conflict');
    box.appendChild(el('h4', null, `⚠ Source conflict — ${c.topic || 'sources disagree'}`));
    const r1 = el('div', 'row');
    r1.innerHTML = `<span class="lbl wins">GOVERNS</span><strong>${escapeHtml(c.authoritative)}</strong>`;
    const r2 = el('div', 'row');
    r2.innerHTML = `<span class="lbl loses">OVERRULED</span>${escapeHtml(c.conflicting)} — “${escapeHtml((c.conflicting_says || '').slice(0, 170))}”`;
    box.appendChild(r1); box.appendChild(r2);
    box.appendChild(el('div', 'res', c.resolution || ''));
    wrap.appendChild(box);
  });
  scroll();
}

function renderProposal(wrap, p) {
  const box = el('div', 'proposal');
  const head = el('div', 'ph');
  head.appendChild(el('span', null, '⏸ Awaiting your confirmation'));
  head.appendChild(el('span', null, p.proposal_id));
  box.appendChild(head);

  const body = el('div', 'pb');
  body.appendChild(el('div', 'psum', p.summary));
  const dl = el('dl', 'kv');
  Object.entries(p.preview || {}).forEach(([k, v]) => {
    if (k === 'reason') return;
    dl.appendChild(el('dt', null, k.replace(/_/g, ' ')));
    dl.appendChild(el('dd', null, String(v)));
  });
  body.appendChild(dl);
  if (p.preview?.reason) body.appendChild(el('div', 'preason', `Reason: ${p.preview.reason}`));
  (p.warnings || []).forEach(w => body.appendChild(el('div', 'warn', `⚠ ${w}`)));
  box.appendChild(body);

  const actions = el('div', 'actions');
  const yes = el('button', 'confirm', 'Confirm & execute');
  const no = el('button', 'decline', 'Decline');
  actions.appendChild(yes); actions.appendChild(no);
  box.appendChild(actions);

  yes.onclick = async () => {
    yes.disabled = no.disabled = true; yes.textContent = 'Executing…';
    const r = await fetch(`/api/proposals/${p.proposal_id}/confirm`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION.session_id }) });
    const data = await r.json();
    actions.remove();
    box.appendChild(el('div', r.ok ? 'committed' : 'declined', r.ok
      ? `✓ Executed — reference ${data.reference}` : `✗ ${data.detail || 'Failed'}`));
    scroll();
  };
  no.onclick = async () => {
    yes.disabled = no.disabled = true;
    await fetch(`/api/proposals/${p.proposal_id}/cancel`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION.session_id }) });
    actions.remove();
    box.appendChild(el('div', 'declined', '✗ Declined — nothing was executed.'));
    scroll();
  };
  wrap.appendChild(box);
  scroll();
}


/* ==================================================================== */
/* Tool trace (employee portal only)                                     */
/* ==================================================================== */

function traceStart(ev) {
  const t = $('#trace');
  if (t.querySelector('.trace-empty')) t.innerHTML = '';
  const box = el('div', 'tcall'); box.id = `tc-${ev.call_id}`;
  const head = el('div', 'th');
  const left = el('div');
  left.appendChild(el('div', 'name', ev.tool));
  left.appendChild(el('div', 'cat', ev.category));
  head.appendChild(left);
  head.appendChild(el('div', 'spin'));
  box.appendChild(head);
  if (ev.args && Object.keys(ev.args).length) {
    box.appendChild(el('div', 'args', JSON.stringify(ev.args)));
  }
  t.appendChild(box);
  t.scrollTop = t.scrollHeight;
}

function traceEnd(ev) {
  const box = document.getElementById(`tc-${ev.call_id}`);
  if (!box) return;
  box.querySelector('.spin')?.replaceWith(el('div', 'tick', '✓'));
  if (/denied/i.test(ev.result || '')) box.classList.add('denied');
  box.appendChild(el('div', 'summary', ev.result));
  const det = el('details');
  det.appendChild(el('summary', null, 'raw result'));
  det.appendChild(el('pre', null, JSON.stringify(ev.raw, null, 2)));
  box.appendChild(det);
  $('#trace').scrollTop = $('#trace').scrollHeight;
}


/* ==================================================================== */
/* Send — reads the SSE stream                                           */
/* ==================================================================== */

async function send() {
  if (BUSY || !SESSION) return;
  const input = pane('input');
  const text = input.value.trim();
  if (!text) return;

  BUSY = true; pane('send').disabled = true;
  input.value = ''; input.style.height = 'auto';
  pane('chips').innerHTML = '';        // starter prompts are for an empty chat
  clearChatEmpty();
  addUser(text);
  if (PORTAL === 'staff') $('#trace').innerHTML = '';

  const { wrap, steps, body } = botShell();
  let draft = '';                 // tokens streamed so far, this step
  let frame = 0;                  // handle of a queued repaint, 0 if none
  let settled = false;            // the verified answer has landed

  /* Repaint on an animation frame rather than per token: a fast stream can
     deliver hundreds of fragments a second, and re-rendering markdown on each
     one makes the text visibly stutter.

     `settled` is the important part. A frame queued by the last token fires
     AFTER the `answer` event has already replaced the body with the verified
     text, and would repaint the raw draft over the top of it. That is not a
     cosmetic race: when the trust layer withholds an answer, the text it
     withheld is exactly what this would put back on screen. Once the final
     answer is in, no draft may overwrite it. */
  const paint = () => {
    frame = 0;
    if (settled || !draft) return;   // a tool call landed first, or we are done
    body.innerHTML = md(draft) + '<span class="caret"></span>';
    scroll();
  };
  const queuePaint = () => { if (!frame) frame = requestAnimationFrame(paint); };
  const settle = () => {
    settled = true;
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
  };

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION.session_id, message: text }) });

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split('\n').find(l => l.startsWith('data: '));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));

        if (ev.type === 'tool_start') {
          // Anything streamed before a tool call was the model thinking aloud,
          // not the answer. Drop it rather than leave a half-sentence on screen.
          draft = '';
          if (frame) { cancelAnimationFrame(frame); frame = 0; }
          body.classList.remove('drafting');
          body.classList.add('thinking');
          body.innerHTML = '<div class="typing"><i></i><i></i><i></i></div>';
          stepChip(steps, ev);
          if (PORTAL === 'staff') traceStart(ev);

        } else if (ev.type === 'tool_end') {
          stepDone(ev);
          if (PORTAL === 'staff') traceEnd(ev);

        } else if (ev.type === 'answer_delta') {
          if (!draft) { body.classList.remove('thinking'); body.classList.add('drafting'); }
          draft += ev.text || '';
          queuePaint();

        } else if (ev.type === 'answer') {
          // The verified text. Replaces whatever streamed, so an answer the
          // trust layer withheld never survives on screen.
          settle();
          body.classList.remove('thinking', 'drafting');
          body.innerHTML = md(ev.text || '');
          scroll();

        } else if (ev.type === 'trust') {
          renderTrust(wrap, ev);

        } else if (ev.type === 'proposals') {
          ev.items.forEach(p => renderProposal(wrap, p));

        } else if (ev.type === 'error') {
          settle();
          body.classList.remove('thinking', 'drafting');
          body.innerHTML = md(`⚠ ${ev.message}`);
        }
      }
    }
  } catch (e) {
    settle();
    body.classList.remove('thinking', 'drafting');
    body.innerHTML = md(`⚠ Request failed: ${e.message}`);
  } finally {
    BUSY = false; pane('send').disabled = false; scroll();
  }
}


/* ==================================================================== */
/* Customer: pick the issue, then talk about it                          */
/* ==================================================================== */

let CUST_VIEW = 'picker';

function showCustView(name) {
  CUST_VIEW = name;
  $('#cust-picker').classList.toggle('active', name === 'picker');
  $('#cust-chat').classList.toggle('active', name === 'chat');
  $('#custBack').style.display = name === 'chat' ? '' : 'none';
  if (name === 'chat') pane('input').focus();
}

async function loadPicker() {
  const box = $('#pickerGroups');
  box.innerHTML = '<div class="empty">Loading your account…</div>';
  $('#pickerHi').textContent =
    `Hello ${SESSION.display_name.split(' ')[0]}. What do you need help with?`;

  const [issues, convos] = await Promise.all([
    fetch(`/api/my-issues?session_id=${SESSION.session_id}`).then(r => r.json()),
    fetch(`/api/conversations?session_id=${SESSION.session_id}`).then(r => r.json()),
  ]);
  box.innerHTML = '';

  const open = [...(issues.tickets || []).filter(t => t.open),
                ...(issues.orders || []).filter(o => o.open)];
  const rest = [...(issues.tickets || []).filter(t => !t.open),
                ...(issues.orders || []).filter(o => !o.open)];

  if (open.length) box.appendChild(issueGroup('Open right now', open));
  if (rest.length) box.appendChild(issueGroup('Earlier', rest));

  // Free-form is always available: a picker that traps you is worse than none.
  const other = el('div', 'pgroup');
  const btn = el('button', 'issue other');
  btn.appendChild(el('span', 'idot'));
  const b = el('div', 'ibody');
  b.appendChild(el('div', 'ilabel', 'Something else'));
  b.appendChild(el('div', 'imeta', 'ask about anything — billing, plans, policy'));
  btn.appendChild(b);
  btn.appendChild(el('span', 'igo', '→'));
  btn.onclick = () => startConversation(null);
  other.appendChild(btn);
  box.appendChild(other);

  const prior = (convos.conversations || []).filter(c => c.turns > 0);
  if (prior.length) {
    const g = el('div', 'pgroup');
    g.appendChild(el('h3', null, 'Pick up where you left off'));
    prior.slice(0, 6).forEach(c => g.appendChild(convoRow(c, () => resumeConversation(c.id))));
    box.appendChild(g);
  }
}

function issueGroup(heading, items) {
  const g = el('div', 'pgroup');
  g.appendChild(el('h3', null, heading));
  items.forEach(it => {
    const btn = el('button', `issue ${it.open ? 'open' : ''}`);
    btn.appendChild(el('span', 'idot'));
    btn.appendChild(el('span', 'iref', it.ref));
    const body = el('div', 'ibody');
    body.appendChild(el('div', 'ilabel', it.label));
    body.appendChild(el('div', 'imeta',
      [it.status, it.created_at].filter(Boolean).join(' · ')));
    btn.appendChild(body);
    btn.appendChild(el('span', 'igo', '→'));
    btn.onclick = () => startConversation(
      { kind: it.kind, ref: it.ref, label: it.label });
    g.appendChild(btn);
  });
  return g;
}

function convoRow(c, onClick, opts = {}) {
  const row = el('button', 'convo' + (opts.active ? ' active' : ''));
  const body = el('div', 'cbody');
  body.appendChild(el('div', 'ctitle', c.title || 'New conversation'));
  const meta = el('div', 'cmeta');
  if (opts.showWho) meta.appendChild(el('span', 'cwho', c.display_name));
  if (c.context_ref) meta.appendChild(el('span', null, c.context_ref));
  meta.appendChild(el('span', null, `${c.turns} turn${c.turns === 1 ? '' : 's'}`));
  meta.appendChild(el('span', null, when(c.last_at)));
  if (c.escalated) meta.appendChild(el('span', 'cesc', 'escalated'));
  if (c.score !== undefined) meta.appendChild(el('span', 'cscore', c.score.toFixed(2)));
  body.appendChild(meta);
  row.appendChild(body);
  row.appendChild(el('span', 'igo', '→'));
  row.onclick = onClick;
  return row;
}

/* Timestamps are stored UTC; a support agent thinks in "yesterday". */
function when(iso) {
  if (!iso) return '';
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
  const mins = (Date.now() - d.getTime()) / 60000;
  if (mins < 1) return 'just now';
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ago`;
  if (mins < 60 * 24 * 7) return `${Math.floor(mins / 1440)}d ago`;
  return d.toLocaleDateString();
}

/* Opening a conversation means a NEW session bound to a NEW (or resumed)
   conversation row, so the model's context and the stored thread cannot drift
   apart. */
async function startConversation(context) {
  await openSession({ context });
  pane('messages').innerHTML = '';
  renderChatEmpty(context);
  renderSuggestions(context);
  showCustView('chat');
}

async function resumeConversation(conversationId) {
  await openSession({ conversation_id: conversationId });
  pane('messages').innerHTML = '';
  (SESSION.history || []).forEach(m => {
    if (m.role === 'user') addUser(m.content);
    else {
      const wrap = el('div', 'msg bot');
      const body = el('div', 'body');
      body.innerHTML = md(m.content);
      wrap.appendChild(body);
      pane('messages').appendChild(wrap);
    }
  });
  pane('chips').innerHTML = '';
  if (PORTAL === 'staff') renderTraceEmpty();   // the old trace is not this turn's
  scroll();
  if (PORTAL === 'customer') showCustView('chat'); else switchView('chat');
}

async function openSession(extra) {
  const r = await fetch('/api/session', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_key: SESSION.user_key, ...extra }) });
  const next = await r.json();
  SESSION = { ...next, user_key: SESSION.user_key };
}


/* ==================================================================== */
/* Employee: conversation review                                         */
/* ==================================================================== */

let CONVO_SCOPE = 'customers';
let CONVO_SELECTED = null;

async function loadConversations(query) {
  const list = $('#convoList');
  list.innerHTML = '<div class="empty">Loading…</div>';
  const url = query
    ? `/api/conversations/search?session_id=${SESSION.session_id}&q=${encodeURIComponent(query)}`
    : `/api/conversations?session_id=${SESSION.session_id}&scope=${CONVO_SCOPE}`;
  const r = await fetch(url);
  if (!r.ok) { list.innerHTML = '<div class="empty">Not available in this session.</div>'; return; }
  const data = await r.json();
  const rows = data.results || data.conversations || [];

  list.innerHTML = '';
  if (query) {
    const h = el('div', 'convo-search');
    list.appendChild(el('div', 'hint',
      data.semantic
        ? `${rows.length} conversation(s) closest in meaning to “${query}”.`
        : 'Semantic search is unavailable; showing nothing rather than a keyword guess.'));
  }
  if (!rows.length) {
    list.innerHTML += '<div class="empty">No conversations yet.</div>';
    return;
  }
  rows.forEach(c => list.appendChild(convoRow(c,
    () => openReview(c.id), { showWho: true, active: c.id === CONVO_SELECTED })));

  // Auto-open the first row only when the reviewer has not already chosen one
  // (e.g. arrived here from an escalation deep link).
  if (!CONVO_SELECTED && rows.length) openReview(rows[0].id);
}

async function openReview(cid) {
  CONVO_SELECTED = cid;
  $$('#convoList .convo').forEach(n => n.classList.remove('active'));
  const box = $('#convoDetail');
  box.innerHTML = '<div class="empty">Loading…</div>';
  const r = await fetch(`/api/conversations/${cid}?session_id=${SESSION.session_id}`);
  if (!r.ok) { box.innerHTML = '<div class="empty">Could not load that conversation.</div>'; return; }
  renderReview(await r.json());
}

/* The point of this pane: not "what did it say" but "was it entitled to say
   it". Each turn shows the question, the answer, and then the working -- the
   tools called with their arguments, the documents cited, the derived
   confidence, and whether the answer was withheld. */
function renderReview(data) {
  const box = $('#convoDetail');
  box.innerHTML = '';
  const c = data.conversation;
  box.className = 'review-detail '
    + (c.role === 'customer' ? 'by-customer' : 'by-staff');

  const head = el('div', 'rv-head');
  head.appendChild(el('h3', null, c.title || 'Conversation'));
  const meta = el('div', 'rvm');
  [c.display_name, c.account_name || c.account_id, c.context_ref,
   `${c.turns} turn(s)`, when(c.last_at)].filter(Boolean)
    .forEach(x => meta.appendChild(el('span', null, x)));
  if (c.escalated) meta.appendChild(el('span', 'cesc', '● escalated'));
  head.appendChild(meta);

  /* Reading your own old thread and being unable to continue it is a dead end.
     Someone else's stays read-only: resuming it would append your questions to
     a customer's conversation. */
  if (c.user_id === SESSION.user_id) {
    const cont = el('button', 'newchat rv-continue', 'Continue this conversation →');
    cont.onclick = async () => {
      await resumeConversation(c.id);
      switchView('chat');
    };
    head.appendChild(cont);
  }
  box.appendChild(head);

  const byTurn = {};
  (data.tool_calls || []).forEach(t => (byTurn[t.turn] ||= []).push(t));

  const msgs = data.messages || [];
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (m.role !== 'user') continue;
    const answer = msgs[i + 1] && msgs[i + 1].role === 'assistant' ? msgs[i + 1] : null;

    const turn = el('div', 'rv-turn');
    turn.appendChild(el('div', 'rv-q', m.content));

    if (answer) {
      const a = el('div', 'rv-a' + (answer.withheld ? ' withheld' : ''));
      a.innerHTML = md(answer.content);
      turn.appendChild(a);

      const tools = byTurn[m.seq] || [];
      if (tools.length) {
        const sec = el('div', 'rv-sec');
        sec.appendChild(el('b', null, `What it checked — ${tools.length} tool call(s)`));
        tools.forEach(t => sec.appendChild(reviewTool(t)));
        turn.appendChild(sec);
      }
      if (answer.citations?.length) {
        const sec = el('div', 'rv-sec');
        sec.appendChild(el('b', null, 'Documents cited'));
        const wrap = el('div', 'rv-cites');
        answer.citations.forEach(x => wrap.appendChild(el('div', 'rv-cite', x)));
        sec.appendChild(wrap);
        turn.appendChild(sec);
      }
      const sec = el('div', 'rv-sec');
      sec.appendChild(el('b', null, 'How this was assessed'));
      const strip = el('div', 'trust');
      if (answer.band) {
        strip.appendChild(el('span', `pill ${answer.band}`,
          `${answer.band} confidence · ${Math.round((answer.confidence || 0) * 100)}%`));
      }
      if (answer.withheld) strip.appendChild(el('span', 'pill LOW', 'answer withheld'));
      if (answer.reasons?.length) {
        strip.appendChild(el('div', 'trust-why', answer.reasons.join(' ')));
      }
      sec.appendChild(strip);
      turn.appendChild(sec);
    }
    box.appendChild(turn);
  }
}

function reviewTool(t) {
  const w = el('div', 'rv-tool');
  const h = el('div', 'rt-h');
  h.appendChild(el('div', 'rt-name', t.tool));
  h.appendChild(el('div', 'rt-cat', t.category || ''));
  w.appendChild(h);
  if (t.args && Object.keys(t.args).length) {
    w.appendChild(el('div', 'rt-args', JSON.stringify(t.args)));
  }
  if (t.summary) w.appendChild(el('div', 'rt-sum', t.summary));
  if (t.result) {
    const d = el('details');
    d.appendChild(el('summary', null, 'raw result'));
    d.appendChild(el('pre', null, JSON.stringify(t.result, null, 2)));
    w.appendChild(d);
  }
  return w;
}


/* ==================================================================== */
/* Employee: escalations                                                 */
/* ==================================================================== */

async function loadEscalations() {
  const box = $('#escList'), stats = $('#escStats');
  box.innerHTML = '<div class="empty">Loading…</div>'; stats.innerHTML = '';
  const r = await fetch(`/api/escalations?session_id=${SESSION.session_id}`);
  if (!r.ok) { box.innerHTML = '<div class="empty">Not available in this session.</div>'; return; }
  const data = await r.json();

  const C = data.counts || {};
  [['open', 'Awaiting a human', 'warn'], ['committed', 'Live escalations', 'crit'],
   ['total', 'Raised in total', '']].forEach(([k, l, cls], i) => {
    const d = el('div', `stat ${cls}`);
    d.style.animationDelay = `${i * 50}ms`;
    d.appendChild(el('div', 'n', C[k] ?? 0));
    d.appendChild(el('div', 'l', l));
    stats.appendChild(d);
  });

  box.innerHTML = '';
  if (!data.escalations?.length) {
    box.innerHTML = '<div class="empty">Nothing has been escalated.</div>';
    return;
  }
  data.escalations.forEach((e, i) => {
    const card = el('div', `esc ${e.status}`);
    card.style.animationDelay = `${i * 40}ms`;
    const left = el('div');
    left.appendChild(el('h4', null,
      `${e.ticket_id || e.account_name || 'Escalation'}${e.severity ? ` · ${e.severity}` : ''}`));
    const m = el('div', 'em');
    [e.account_name || e.account_id, `raised by ${e.raised_by}`, when(e.created_at),
     e.reference].filter(Boolean).forEach(x => m.appendChild(el('span', null, x)));
    left.appendChild(m);
    card.appendChild(left);
    card.appendChild(el('span', `eb ${e.status}`,
      { proposed: 'awaiting confirmation', committed: 'live',
        declined: 'declined', denied: 'blocked' }[e.status] || e.status));
    if (e.details || e.reason) card.appendChild(el('div', 'er', e.details || e.reason));
    if (e.conversation_id) {
      const open = el('div', 'eopen');
      const b = el('button', null, 'Open the conversation that raised this →');
      b.onclick = () => {
        // Land on a clean list; a leftover query would filter out the very
        // conversation we just navigated to.
        $('#convoSearch').value = '';
        CONVO_SCOPE = 'all';
        $$('#convoScope button').forEach(x => x.classList.toggle('on', x.dataset.scope === 'all'));
        switchView('conversations');
        openReview(e.conversation_id);
      };
      open.appendChild(b);
      card.appendChild(open);
    }
    box.appendChild(card);
  });
}


/* ==================================================================== */
/* Signal Board                                                          */
/* ==================================================================== */

/* Evidence rows used to be one flat string per row:
   "account_id: ACCT-002 · created_at: ... · subject: Bulk upload fails · status: open".
   The subject is the only part anyone reads, and it sat in the middle of a
   200-character line. Promote it, and demote the rest to a quieter line beneath
   -- same data, in the order a human actually consumes it. */
const EV_PRIMARY = ['subject', 'title', 'summary', 'detail', 'description'];
const EV_ID = ['ticket_id', 'order_id', 'account_id_ref', 'id'];

function evidenceBlock(evidence) {
  const box = el('div', 'ev');
  evidence.forEach(e => {
    const row = el('div', 'evrow');
    const idKey = EV_ID.find(k => e[k]);
    row.appendChild(el('div', 'eid', idKey ? e[idKey] : ''));

    const subKey = EV_PRIMARY.find(k => e[k]);
    row.appendChild(el('div', 'esub', subKey ? String(e[subKey])
      : Object.entries(e).filter(([k]) => k !== idKey)
          .map(([k, v]) => `${k}: ${v}`).join(' · ')));

    // Everything not already shown, as compact key=value chips.
    const rest = Object.entries(e).filter(([k]) => k !== idKey && k !== subKey);
    if (rest.length) {
      const meta = el('div', 'emeta');
      rest.forEach(([k, v]) => {
        const chip = el('span', k === 'status' && String(v) === 'open' ? 'open' : null,
                        `${k.replace(/_/g, ' ')} ${v}`);
        meta.appendChild(chip);
      });
      row.appendChild(meta);
    }
    box.appendChild(row);
  });
  return box;
}


async function loadSignals() {
  const box = $('#signalList'), stats = $('#signalStats');
  box.innerHTML = '<div class="empty">Loading…</div>'; stats.innerHTML = '';
  const r = await fetch(`/api/signals?session_id=${SESSION.session_id}`);
  if (!r.ok) { box.innerHTML = '<div class="empty">Not available in this session.</div>'; return; }
  const data = await r.json();

  const S = data.stats || {};
  [['open_tickets', 'Open tickets', ''], ['sla_breached', 'SLA breached', 'crit'],
   ['sla_at_risk', 'Approaching SLA', 'warn'], ['total_signals', 'Signals', ''],
   ['accounts_affected', 'Accounts affected', '']].forEach(([k, l, c], i) => {
    const s = el('div', `stat ${c}`);
    s.style.animationDelay = `${i * 50}ms`;
    s.appendChild(el('div', 'n', S[k] ?? 0));
    s.appendChild(el('div', 'l', l));
    stats.appendChild(s);
  });

  box.innerHTML = '';
  if (!data.signals?.length) { box.innerHTML = '<div class="empty">Nothing needs attention.</div>'; return; }
  data.signals.forEach((sig, i) => {
    const card = el('div', `signal ${sig.severity}`);
    card.style.animationDelay = `${i * 45}ms`;
    const head = el('div', 'sh');
    head.appendChild(el('h3', null, sig.title));
    const meta = el('div', 'meta');
    meta.appendChild(el('span', `pill ${sig.severity === 'critical' ? 'LOW' :
      sig.severity === 'high' ? 'MEDIUM' : 'src'}`, sig.severity));
    head.appendChild(meta);
    card.appendChild(head);
    card.appendChild(el('div', 'why', sig.why_it_matters));

    if (sig.evidence?.length) card.appendChild(evidenceBlock(sig.evidence));
    if (sig.sources?.length) card.appendChild(el('div', 'srcs', '↳ ' + sig.sources.join(' · ')));
    if (sig.recommended_action) {
      card.appendChild(el('div', 'rec', `Recommended: ${
        sig.recommended_action.action_type?.replace(/_/g, ' ')} — ${
        sig.recommended_action.details || sig.recommended_action.reason || ''}`));
    }
    box.appendChild(card);
  });
}


/* ==================================================================== */
/* Proactive outreach                                                    */
/* ==================================================================== */

let OUTREACH = null;

async function loadOutreach() {
  const box = $('#draftList'), sup = $('#suppressedList'), stats = $('#outreachStats');
  box.innerHTML = '<div class="empty">Loading…</div>'; sup.innerHTML = ''; stats.innerHTML = '';
  const r = await fetch(`/api/outreach?session_id=${SESSION.session_id}`);
  if (!r.ok) { box.innerHTML = '<div class="empty">Not available in this session.</div>'; return; }
  OUTREACH = await r.json();

  const S = OUTREACH.stats || {};
  [['drafts_ready', 'Ready to send', ''], ['suppressed', 'Held back', 'warn'],
   ['accounts_reached', 'Accounts', ''], ['credits_offered_inr', 'Credits offered (INR)', ''],
   ['needs_manager_approval', 'Needs manager', 'crit']].forEach(([k, l, c], i) => {
    const d = el('div', `stat ${c}`);
    d.style.animationDelay = `${i * 50}ms`;
    d.appendChild(el('div', 'n', S[k] ?? 0));
    d.appendChild(el('div', 'l', l));
    stats.appendChild(d);
  });

  box.innerHTML = '';
  if (!OUTREACH.drafts?.length) {
    box.innerHTML = '<div class="empty">No customers need proactive contact right now.</div>';
  }
  OUTREACH.drafts.forEach((d, i) => {
    const c = draftCard(d); c.style.animationDelay = `${i * 45}ms`; box.appendChild(c);
  });

  $('#supHead').style.display = OUTREACH.suppressed?.length ? 'block' : 'none';
  (OUTREACH.suppressed || []).forEach(s2 => {
    const c = el('div', 'sup');
    const t = el('div', 'st');
    t.appendChild(el('strong', null, `${s2.account_name} — ${s2.kind.replace(/_/g, ' ')}`));
    t.appendChild(el('span', 'sr', s2.reason));
    c.appendChild(t);
    c.appendChild(el('div', 'sd', s2.detail +
      (s2.retry_after ? `  ·  retry after ${s2.retry_after}` : '')));
    sup.appendChild(c);
  });
}

function draftCard(d) {
  const card = el('div', 'draft'); card.id = `draft-${d.candidate_id}`;
  const head = el('div', 'dh');
  const left = el('div');
  left.appendChild(el('h3', null, d.subject));
  left.appendChild(el('div', 'who', `${d.account_name} · ${d.plan} · ${d.kind_label}` +
    (d.csm ? ` · CSM ${d.csm}` : '')));
  head.appendChild(left);
  const meta = el('div', 'meta');
  if (d.entitlement_inr) meta.appendChild(el('span', 'credit', `INR ${d.entitlement_inr}`));
  if (d.body_source === 'llm_verified') meta.appendChild(el('span', 'verified', 'grounding-verified'));
  head.appendChild(meta);
  card.appendChild(head);

  /* The draft and the evidence for it sit SIDE BY SIDE on a wide screen.
     Stacked, the email was 72ch of text against 700px of empty card, and the
     reviewer had to scroll away from the claim to reach its justification --
     which is the one comparison this screen exists to make. */
  const split = el('div', 'dsplit');
  split.appendChild(el('div', 'body', d.body));

  const side = el('div', 'dside');
  if (d.facts?.length) {
    const f = el('div', 'facts');
    f.appendChild(el('b', null, 'Verified facts behind this message'));
    const ul = el('ul');
    d.facts.forEach(x => ul.appendChild(el('li', null, x)));
    f.appendChild(ul); side.appendChild(f);
  }
  if (d.citations?.length) side.appendChild(el('div', 'cite', '↳ ' + d.citations.join(' · ')));
  (d.warnings || []).forEach(w => side.appendChild(el('div', 'facts warn-fact', `⚠ ${w}`)));
  split.appendChild(side);
  card.appendChild(split);

  const act = el('div', 'dactions');
  const prep = el('button', 'confirm', 'Prepare for approval');
  prep.onclick = () => proposeOutreach([d.candidate_id], card);
  act.appendChild(prep);
  card.appendChild(act);
  return card;
}

async function proposeOutreach(ids, cardEl) {
  const r = await fetch('/api/outreach/propose', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: SESSION.session_id, candidate_ids: ids }) });
  const data = await r.json();
  if (!r.ok) { alert(data.detail || 'Failed'); return; }

  // Batching prepares; it does not approve. Each proposal is still confirmed
  // one at a time, through exactly the same gate as a chat proposal.
  data.proposals.forEach(p => {
    const target = cardEl || document.getElementById(`draft-${p.candidate_id}`);
    if (!target) return;
    target.querySelector('.dactions')?.remove();
    if (target.querySelector('.proposal')) return;
    const box = el('div', 'proposal');
    const h = el('div', 'ph');
    h.appendChild(el('span', null, '⏸ Awaiting your confirmation'));
    h.appendChild(el('span', null, p.proposal_id));
    box.appendChild(h);
    const acts = el('div', 'actions');
    const yes = el('button', 'confirm', 'Confirm & send');
    const no = el('button', 'decline', 'Discard');
    acts.appendChild(yes); acts.appendChild(no); box.appendChild(acts);
    yes.onclick = async () => {
      yes.disabled = no.disabled = true; yes.textContent = 'Sending…';
      const rr = await fetch(`/api/proposals/${p.proposal_id}/confirm`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION.session_id }) });
      const dd = await rr.json();
      acts.remove();
      box.appendChild(el('div', rr.ok ? 'committed' : 'declined', rr.ok
        ? `✓ Sent — reference ${dd.reference}` : `✗ ${dd.detail || 'Failed'}`));
    };
    no.onclick = async () => {
      yes.disabled = no.disabled = true;
      await fetch(`/api/proposals/${p.proposal_id}/cancel`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION.session_id }) });
      acts.remove();
      box.appendChild(el('div', 'declined', '✗ Discarded — nothing was sent.'));
    };
    target.appendChild(box);
  });
}


/* ==================================================================== */
/* Audit log                                                             */
/* ==================================================================== */

async function loadAudit() {
  const box = $('#auditList');
  box.innerHTML = '<div class="empty">Loading…</div>';
  const r = await fetch(`/api/audit?session_id=${SESSION.session_id}&limit=60`);
  if (!r.ok) { box.innerHTML = '<div class="empty">Not available in this session.</div>'; return; }
  const { entries } = await r.json();
  box.innerHTML = '';
  entries.slice().reverse().forEach(e => {
    const cls = /denied/.test(e.event) ? 'deny' : /committed/.test(e.event) ? 'commit' : '';
    const row = el('div', `aentry ${cls}`);
    row.appendChild(el('div', 'ts', (e.ts || '').replace('T', ' ').replace('+00:00', 'Z')));
    row.appendChild(el('div', 'ev', e.event));
    row.appendChild(el('div', 'who', `${e.actor}`));
    row.appendChild(el('div', 'dt', Object.entries(e)
      .filter(([k]) => !['ts', 'event', 'actor', 'role', 'actor_account'].includes(k))
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
      .join(' ').slice(0, 220)));
    box.appendChild(row);
  });
}


/* ==================================================================== */
/* Employee tabs                                                         */
/* ==================================================================== */

function switchView(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
  // Scoped to the employee screen: the customer portal uses `.view` for its own
  // picker/chat pair, and a global toggle would blank it.
  $$('#screen-staff .view').forEach(
    v => v.classList.toggle('active', v.id === `view-${name}`));
  if (name === 'signals') loadSignals();
  if (name === 'outreach') loadOutreach();
  if (name === 'audit') loadAudit();
  if (name === 'escalations') loadEscalations();
  if (name === 'conversations') loadConversations($('#convoSearch').value.trim() || undefined);
}


/* ==================================================================== */
/* Wiring                                                                */
/* ==================================================================== */

$$('.portal-card').forEach(c =>
  c.addEventListener('click', () => openIdentityStep(c.dataset.portal)));
$('#backToPortal').addEventListener('click', () => showLoginStep('step-portal'));
$$('[data-signout]').forEach(b => b.addEventListener('click', signOut));
$$('.tab').forEach(t => t.addEventListener('click', () => switchView(t.dataset.view)));
$('#proposeAll').addEventListener('click', () => {
  if (!OUTREACH?.drafts?.length) return;
  proposeOutreach(OUTREACH.drafts.map(d => d.candidate_id), null);
});

['cust', 'staff'].forEach(k => {
  $(`#${k}Send`).addEventListener('click', send);
  const ta = $(`#${k}Input`);
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  ta.addEventListener('input', e => {
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 130) + 'px';
  });
});

$('#custBack').addEventListener('click', () => {
  showCustView('picker');
  loadPicker();                      // a finished thread should show up there
});

$('#convoScope').addEventListener('click', e => {
  const b = e.target.closest('button[data-scope]');
  if (!b) return;
  CONVO_SCOPE = b.dataset.scope;
  $$('#convoScope button').forEach(x => x.classList.toggle('on', x === b));
  $('#convoSearch').value = '';
  CONVO_SELECTED = null;
  loadConversations();
});

/* Search runs on a pause, not per keystroke: each query is an embedding
   forward pass, and firing one per character would be pure waste. */
let searchTimer = 0;
$('#convoSearch').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  searchTimer = setTimeout(() => {
    CONVO_SELECTED = null;
    loadConversations(q.length >= 3 ? q : undefined);
  }, 320);
});

boot();
