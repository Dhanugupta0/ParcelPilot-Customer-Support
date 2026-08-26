'use strict';
/* ParcelPilot Support — front end.

   Two portals from one script. The difference between them is not cosmetic:
   a customer is shown the ANSWER, an agent is shown the answer plus the
   working that produced it. Both come from the same API call; the server
   decides what each role may see, and this file renders what it is given. */

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const el = (t, c, x) => { const e = document.createElement(t);
  if (c) e.className = c; if (x !== undefined) e.textContent = x; return e; };
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

let S = null;          // session
let PORTAL = null;     // 'customer' | 'staff'
let SUBJECT = null;    // the order/ticket the customer picked
let BUSY = false;

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.statusText);
  return r.json();
};
const post = (path, body) => api(path, {method:'POST',
  headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});

/* Answers are plain prose from the model. Rendered with a deliberately tiny
   markdown subset and escaped first — model output must never inject markup. */
function md(t) {
  return esc(t)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .split(/\n\n+/).map(b => {
      const lines = b.split('\n');
      if (lines.every(l => /^\s*[-*•]\s+/.test(l)))
        return '<ul>' + lines.map(l => `<li>${l.replace(/^\s*[-*•]\s+/,'')}</li>`).join('') + '</ul>';
      return `<p>${lines.join('<br/>')}</p>`;
    }).join('');
}
const ago = iso => {
  if (!iso) return '';
  const m = (Date.now() - new Date(/[Z+]/.test(iso) ? iso : iso+'Z')) / 60000;
  return m < 1 ? 'just now' : m < 60 ? `${m|0}m ago`
       : m < 1440 ? `${m/60|0}h ago` : `${m/1440|0}d ago`;
};

const show = id => $$('.screen').forEach(s => s.classList.toggle('on', s.id === id));
const pane = (scope, id) => $$(`#${scope} .pane`).forEach(p => p.classList.toggle('on', p.id === id));


/* ================= sign in ================= */

let USERS = [];
(async function boot() {
  const h = await api('/api/health');
  $('#loginSnap').textContent = `dataset snapshot · ${h.snapshot} · ${h.policy_chunks} policy sections indexed`;
  USERS = (await api('/api/users')).users;
})();

$$('.card').forEach(c => c.onclick = () => {
  PORTAL = c.dataset.portal;
  const list = $('#whoList'); list.innerHTML = '';
  USERS.filter(u => PORTAL === 'customer' ? u.role === 'customer' : u.role !== 'customer')
       .forEach(u => {
    const b = el('button','who-row');
    b.appendChild(el('span','av', u.name[0]));
    const d = el('div');
    d.appendChild(el('b', null, u.name));
    d.appendChild(el('small', null, u.account_id ? `${u.role} · ${u.account_id}` : u.role));
    b.appendChild(d);
    b.onclick = () => signIn(u.key);
    list.appendChild(b);
  });
  $('#step-role').classList.remove('on'); $('#step-who').classList.add('on');
});
$('#backRole').onclick = () => {
  $('#step-who').classList.remove('on'); $('#step-role').classList.add('on');
};

async function signIn(key) {
  S = await post('/api/session', {user_key: key});
  S.user_key = key;
  if (S.role === 'customer') {
    PORTAL = 'customer';
    $('#custWho').textContent = `${S.name} · ${S.account_name}`;
    $('#custHi').textContent = `Hello ${S.name.split(' ')[0]}. What do you need help with?`;
    show('s-cust'); pane('s-cust','cust-pick'); loadIssues();
  } else {
    PORTAL = 'staff';
    $('#staffWho').textContent = `${S.name} · ${S.role}`;
    $('#staffSnap').textContent = S.snapshot;
    show('s-staff'); switchView('board');
  }
}
$$('[data-out]').forEach(b => b.onclick = () => {
  S = null; PORTAL = null; SUBJECT = null;
  $('#step-who').classList.remove('on'); $('#step-role').classList.add('on');
  show('s-login');
});


/* ================= customer: pick an issue ================= */

async function loadIssues() {
  const box = $('#issues');
  box.innerHTML = '<div class="empty">Loading your account…</div>';
  const [d, chats] = await Promise.all([
    api(`/api/my-issues?session_id=${S.session_id}`),
    api(`/api/chats?session_id=${S.session_id}`),
  ]);
  box.innerHTML = '';
  const open = [...d.tickets.filter(t=>t.open), ...d.orders.filter(o=>o.open)];
  const past = [...d.tickets.filter(t=>!t.open), ...d.orders.filter(o=>!o.open)];
  if (open.length) box.appendChild(group('Open right now', open));
  if (past.length) box.appendChild(group('Earlier', past));

  // Free-form is always available: a picker you cannot escape is worse than none.
  const g = el('div');
  const b = el('button','issue plain');
  const bd = el('div','b');
  bd.appendChild(el('b',null,'Something else'));
  bd.appendChild(el('small',null,'billing, plans, or a general question'));
  b.appendChild(bd); b.appendChild(el('span','go','→'));
  b.onclick = () => openChat(null);
  g.appendChild(b); box.appendChild(g);

  const prior = chats.chats || [];
  if (prior.length) {
    const h = el('div');
    h.appendChild(el('div','grp','Pick up where you left off'));
    prior.slice(0,5).forEach(c => {
      const r = el('button','issue');
      const d2 = el('div','b');
      d2.appendChild(el('b',null,c.title || 'Conversation'));
      d2.appendChild(el('small',null,
        [c.subject_ref, `${c.turns} message${c.turns===1?'':'s'}`, ago(c.last_at)]
          .filter(Boolean).join(' · ')));
      r.appendChild(d2); r.appendChild(el('span','go','→'));
      r.onclick = () => resumeChat(c.id);
      h.appendChild(r);
    });
    box.appendChild(h);
  }
}

function group(label, items) {
  const g = el('div');
  g.appendChild(el('div','grp',label));
  items.forEach(it => {
    const b = el('button','issue');
    b.appendChild(el('span','ref', it.ref));
    const d = el('div','b');
    d.appendChild(el('b', null, it.label));
    d.appendChild(el('small', null,
      [it.status, it.severity, it.created_at].filter(Boolean).join(' · ')));
    b.appendChild(d); b.appendChild(el('span','go','→'));
    b.onclick = () => openChat({kind: it.kind, ref: it.ref, label: it.label});
    g.appendChild(b);
  });
  return g;
}

async function openChat(subject) {
  SUBJECT = subject;
  await post('/api/new-chat', {session_id: S.session_id, subject});
  $('#custMsgs').innerHTML = '';
  if (subject) {
    say('cust','note', `About ${subject.ref} — ${subject.label}`);
    chips('cust', subject.kind === 'order'
      ? ['Can I cancel this without a fee?', 'Am I owed a service credit?',
         'What is happening with it?']
      : ['What is the status?', 'When will someone respond?',
         'What can I do in the meantime?']);
  } else {
    say('cust','note','Ask me anything about your account.');
    chips('cust', ['What does my plan include?', 'How do cancellations work?',
                   'How quickly do you respond to urgent issues?']);
  }
  activityReset('cust', 'When you ask something, the steps I take to answer it '
                + 'will appear here.');
  $('#custHome').hidden = false;
  pane('s-cust','cust-chat'); $('#custIn').focus();
}

async function resumeChat(id) {
  const d = await post(`/api/chats/${id}/resume`, {session_id: S.session_id});
  $('#custMsgs').innerHTML = '';
  d.turns.forEach(t => { say('cust','you', t.question); botBubble('cust', t.answer); });
  chips('cust', []);
  activityReset('cust',
    'Continuing an earlier conversation. New steps will appear here.');
  $('#custHome').hidden = false;
  pane('s-cust','cust-chat'); $('#custIn').focus();
}

$('#custHome').onclick = () => { pane('s-cust','cust-pick'); $('#custHome').hidden = true;
  loadIssues(); };


/* ================= asking ================= */

function say(scope, cls, text) {
  const m = el('div', `m ${cls}`, text);
  $(`#${scope}Msgs`).appendChild(m); scrollDown(scope); return m;
}
function botBubble(scope, text) {
  const wrap = el('div','m bot');
  const body = el('div','body');
  body.innerHTML = md(text);
  wrap.appendChild(body);
  $(`#${scope}Msgs`).appendChild(wrap); scrollDown(scope);
  return wrap;
}
function chips(scope, list) {
  const box = $(`#${scope}Chips`); box.innerHTML = '';
  list.forEach(t => { const c = el('button','chip',t);
    c.onclick = () => { $(`#${scope}In`).value = t; ask(scope); }; box.appendChild(c); });
}
const scrollDown = scope => { const m = $(`#${scope}Msgs`); m.scrollTop = m.scrollHeight; };

async function ask(scope) {
  if (BUSY) return;
  const input = $(`#${scope}In`);
  const q = input.value.trim();
  if (!q) return;
  BUSY = true; input.value = ''; chips(scope, []);
  say(scope, 'you', q);
  // The agent panel shows THIS turn. The chips in the transcript keep the
  // history; a panel that accumulates every turn stops being readable at the
  // point an agent most needs to read it.
  if (scope === 'staff') { activityReset('staff'); activityAsked(q); }

  const wrap = el('div','m bot');
  const steps = el('div','steps');           // live tool trace
  const body = el('div','body');
  body.innerHTML = '<span class="thinking"><i></i><i></i><i></i></span>';
  wrap.appendChild(steps); wrap.appendChild(body);
  $(`#${scope}Msgs`).appendChild(wrap); scrollDown(scope);

  try {
    const resp = await fetch('/api/ask', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: S.session_id, question: q, subject: SUBJECT})});
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', tools = [], proposals = [];

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const part of parts) {
        const line = part.split('\n').find(l => l.startsWith('data: '));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));

        if (ev.type === 'tool_start') {
          steps.appendChild(stepChip(ev));
          activityStart(scope, ev);
        } else if (ev.type === 'tool_end') {
          markDone(ev);
          activityDone(scope, ev);
          if (ev.result) tools.push(ev);
        } else if (ev.type === 'waiting') {
          body.innerHTML = `<span class="thinking"><i></i><i></i><i></i></span>`
            + ` <span class="wait">rate limited upstream — waiting ${ev.seconds}s</span>`;
          activityNote(scope, `rate limited upstream — waiting ${ev.seconds}s`);
        } else if (ev.type === 'answer') {
          body.innerHTML = md(ev.text);
        } else if (ev.type === 'proposals') {
          proposals = ev.items;
        } else if (ev.type === 'done') {
          activityFoot(scope, ev);
        } else if (ev.type === 'verified') {
          if (ev.unverified?.length)
            wrap.appendChild(el('div','badbox',
              `Figures in the prose that no tool produced: ${ev.unverified.join(', ')}. `
              + `Trust the tool trace, not the sentence.`));
        } else if (ev.type === 'error') {
          body.appendChild(el('div','badbox', ev.message));
          activityNote(scope, ev.message, true);
        }
      }
    }
    if (PORTAL === 'staff') tools.forEach(t => renderToolResult(wrap, t));
    proposals.forEach(p => renderProposal(wrap, p));
  } catch (e) {
    body.innerHTML = md(`Something went wrong: ${e.message}`);
  } finally {
    BUSY = false; scrollDown(scope);
  }
}

/* The side panel, for both portals from one set of events.

   A customer sees plain language and no result payload — enough to see that the
   answer was LOOKED UP rather than guessed, which is the thing that earns
   trust, without being handed our internals. An agent sees the same sequence at
   full resolution: the tool name, its category, the arguments, and what came
   back. The server has already decided which of the two it is allowed to send;
   this only renders what arrived. */
const ACT = {cust: '#custActivity', staff: '#staffActivity'};
const actBox = scope => $(ACT[scope]);

function activityStart(scope, ev) {
  const box = actBox(scope);
  if (!box) return;
  if (box.querySelector('.aidle')) box.innerHTML = '';
  const row = el('div','astep');
  row.id = `act-${scope}-${ev.call_id}`;
  row.appendChild(el('span','dot'));
  const d = el('div','abody');

  if (scope === 'staff') {
    d.appendChild(el('code','tname', ev.tool || 'tool'));
    if (ev.category) d.appendChild(el('span','acat', ev.category));
    const a = argLine(ev.args);
    if (a) d.appendChild(el('div','aargs', a));
  } else {
    d.appendChild(document.createTextNode(ev.label || 'Working'));
  }
  d.appendChild(el('span','when', scope === 'staff' ? 'running…' : 'checking…'));
  row.appendChild(d);
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
}

function activityDone(scope, ev) {
  const row = document.getElementById(`act-${scope}-${ev.call_id}`);
  if (!row) return;
  // The same test the inline chip uses, so the panel and the transcript never
  // disagree about whether a step succeeded.
  const bad = /denied|not visible|error/i.test(ev.summary || '');
  row.classList.add(bad ? 'bad' : 'done');
  const w = row.querySelector('.when');
  if (w) w.textContent = (scope === 'staff' && ev.summary) ? ev.summary : 'done';
  actBox(scope).scrollTop = actBox(scope).scrollHeight;
}

function activityNote(scope, msg, bad) {
  const box = actBox(scope);
  if (!box || scope !== 'staff') return;
  box.appendChild(el('div', 'anote' + (bad ? ' bad' : ''), msg));
  box.scrollTop = box.scrollHeight;
}

/* The question the workflow belongs to, so a panel read on its own still says
   what it was answering. */
function activityAsked(q) {
  const box = actBox('staff');
  if (!box) return;
  box.innerHTML = '';
  box.appendChild(el('div','aq', q));
}

function activityFoot(scope, ev) {
  const foot = $('#staffWorkFoot');
  if (!foot || scope !== 'staff') return;
  foot.innerHTML = '';
  const n = (ev.tools || []).length;
  const bits = [`${n} tool call${n === 1 ? '' : 's'}`, `${ev.steps} model step(s)`];
  if (ev.truncated) bits.push('step budget reached');
  if (ev.failed) bits.push('model unreachable');
  foot.appendChild(el('p', null, bits.join(' · ')));
  // Which categories the answer actually rests on. "Deterministic calculation"
  // present means a figure in the prose came from the engine.
  const cats = [...new Set((ev.tools || []).map(t => t.category).filter(Boolean))];
  if (cats.length) {
    const row = el('div','acats');
    cats.forEach(c => row.appendChild(el('span','acat', c)));
    foot.appendChild(row);
  }
}

function argLine(args) {
  return Object.entries(args || {})
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${v}`).join('  ');
}

function activityReset(scope, msg) {
  const box = actBox(scope);
  if (!box) return;
  box.innerHTML = '';
  if (msg) box.appendChild(el('div','aidle', msg));
  if (scope === 'staff') $('#staffWorkFoot').innerHTML = '';
}

/* Requirement 6 of the brief: the interface should show which tool is being
   used. A customer sees plain language ("Checking your order"); an agent sees
   the tool name and its category, because they are the one auditing it. */
function stepChip(ev) {
  const c = el('div','step');
  c.id = `st-${ev.call_id}`;
  c.appendChild(el('span','spin'));
  c.appendChild(el('span','lbl', ev.label || ev.tool));
  if (ev.category) c.title = ev.category;
  return c;
}
function markDone(ev) {
  const c = document.getElementById(`st-${ev.call_id}`);
  if (!c) return;
  const bad = /denied|not visible|error/i.test(ev.summary || '');
  c.classList.add(bad ? 'bad' : 'done');
  c.querySelector('.spin')?.replaceWith(el('span','tick', bad ? '✕' : '✓'));
  if (ev.summary) c.title = ev.summary;
}

/* The working, for staff only. A calculation shows its rule chain; everything
   else shows its arguments and raw result. */
function renderToolResult(wrap, ev) {
  const d = ev.result?.decision;
  const box = el('details','work');
  box.appendChild(el('summary', null,
    `${ev.tool} — ${ev.summary}`));
  const inner = el('div','in');
  if (d) {
    const ol = el('ol','chain');
    d.rule_chain.forEach(s => {
      const li = el('li', null, s.statement);
      li.appendChild(el('span','src', s.source));
      ol.appendChild(li);
    });
    inner.appendChild(ol);
    (d.caveats || []).forEach(c => inner.appendChild(el('div','warnbox', c)));
  } else {
    const pre = el('pre', null, JSON.stringify(ev.result, null, 1).slice(0, 3000));
    inner.appendChild(pre);
  }
  box.appendChild(inner); wrap.appendChild(box);
}

/* Requirement 4: nothing changes state until a person presses this. */
function renderProposal(wrap, p) {
  const card = el('div','proposal');
  card.appendChild(el('div','ph',
    `${p.summary} — prepared, not executed`));
  const b = el('div','pb');
  Object.entries(p.preview || {}).forEach(([k,v]) => {
    if (k === 'action') return;
    const r = el('div','kv');
    r.appendChild(el('span','k', k.replace(/_/g,' ')));
    r.appendChild(el('span','v', String(v)));
    b.appendChild(r);
  });
  if (p.requires_manager)
    b.appendChild(el('div','warnbox',
      'Above the SOP approval threshold — a support manager must confirm this.'));
  card.appendChild(b);

  const act = el('div','pact');
  const yes = el('button','confirm','Confirm');
  const no = el('button','decline','Not now');
  yes.onclick = async () => {
    yes.disabled = no.disabled = true;
    try {
      const r = await post(`/api/proposals/${p.proposal_id}/confirm`,
                           {session_id: S.session_id});
      act.replaceWith(el('div','committed', `Executed — reference ${r.reference}`));
    } catch (e) {
      act.replaceWith(el('div','badbox', e.message));
    }
  };
  no.onclick = async () => {
    yes.disabled = no.disabled = true;
    await post(`/api/proposals/${p.proposal_id}/decline`, {session_id: S.session_id});
    act.replaceWith(el('div','declined','Declined — nothing was created.'));
  };
  act.appendChild(yes); act.appendChild(no);
  card.appendChild(act);
  wrap.appendChild(card);
}

$('#custForm').onsubmit = e => { e.preventDefault(); ask('cust'); };

/* Requirement 1: a request needing human judgement gets escalated rather than
   guessed at. The customer can also ask for a person directly. */
$('#custEscalate').onclick = () => {
  $('#custIn').value = 'This has not answered my question — please escalate '
    + 'this to a support agent.';
  ask('cust');
};
$('#staffForm').onsubmit = e => { e.preventDefault(); ask('staff'); };

/* The working, for staff. This is the difference between reading an answer and
   being able to defend it to a customer. */
function renderWorking(wrap, d) {
  (d.decisions || []).forEach(dec => {
    const box = el('details','work');
    box.appendChild(el('summary', null,
      `How this was determined — ${dec.rule_chain.length} step(s), via ${dec.authority_used}`));
    const inner = el('div','in');
    const ol = el('ol','chain');
    dec.rule_chain.forEach(s => {
      const li = el('li', null, s.statement);
      li.appendChild(el('span','src', s.source));
      ol.appendChild(li);
    });
    inner.appendChild(ol);
    if (dec.citations?.length) {
      const c = el('div','cites');
      dec.citations.forEach(x => c.appendChild(el('span','cite', x)));
      inner.appendChild(c);
    }
    box.appendChild(inner);
    wrap.appendChild(box);
    (dec.caveats || []).forEach(cv => wrap.appendChild(el('div','warnbox', cv)));
    if (dec.needs_manager)
      wrap.appendChild(el('div','warnbox',
        'This amount is above the SOP approval threshold — a manager must sign it off.'));
  });
  if (d.excluded_sources?.length) {
    const x = el('details','work');
    x.appendChild(el('summary', null,
      `${d.excluded_sources.length} source(s) matched but were not used`));
    const i = el('div','in');
    d.excluded_sources.forEach(s =>
      i.appendChild(el('div','cite', `${s.citation} — ${s.reason}`)));
    x.appendChild(i); wrap.appendChild(x);
  }
  if (d.unverified?.length)
    wrap.appendChild(el('div','badbox',
      `Figures in the prose that no computation produced: ${d.unverified.join(', ')}. `
      + `Trust the rule chain above, not the sentence.`));
}


/* ================= employee: views ================= */

function switchView(name) {
  $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.view === name));
  pane('s-staff', `v-${name}`);
  if (name === 'board') loadBoard();
  if (name === 'review') loadChats();
  if (name === 'ask' && !$('#staffMsgs').children.length) {
    activityReset('staff', 'Ask something and every tool the model chooses will '
                  + 'appear here as it runs — name, arguments and result.');
    say('staff','note','Every number below is computed before the model writes a word.');
    chips('staff', ['Can Northstar cancel ORD-1001 without a fee?',
                    'Is TKT-505 within its SLA?',
                    'Is LumenWorks owed a credit for ORD-2002?',
                    'What does the bulk upload limit actually say?']);
  }
}
$$('.tab').forEach(t => t.onclick = () => switchView(t.dataset.view));

async function loadBoard() {
  const d = await api(`/api/dashboard?session_id=${S.session_id}`);
  const st = $('#boardStats'); st.innerHTML = '';
  [['open_tickets','Open tickets',''],['breached','Breaching SLA','red'],
   ['credits_due','Credits due','green'],['accounts','Accounts','']]
    .forEach(([k,l,c]) => {
      const s = el('div',`stat ${c}`);
      s.appendChild(el('div','n', d.stats[k]));
      s.appendChild(el('div','l', l));
      st.appendChild(s);
    });

  fill('#breached', d.breached, r => slaRow(r, 'red'));
  fill('#within', d.within_target, r => slaRow(r, 'green'));
  fill('#credits', d.credits, creditRow);
}

function fill(sel, items, render) {
  const box = $(sel); box.innerHTML = '';
  if (!items.length) { box.appendChild(el('div','empty','Nothing here.')); return; }
  items.forEach(x => box.appendChild(render(x)));
}

function slaRow(r, tone) {
  const row = el('div', `row ${tone}`);
  const h = el('div','h');
  const l = el('div');
  l.appendChild(el('b', null, `${r.ticket_id} · ${r.subject}`));
  const m = el('div','meta');
  [r.account_name, `${r.plan} plan`, `target ${r.target}`, `due ${r.due_at}`,
   `via ${r.authority}`].forEach(x => m.appendChild(el('span',null,x)));
  l.appendChild(m); h.appendChild(l);
  h.appendChild(el('span', `tag ${tone}`, r.breached
    ? `overdue ${r.overdue_minutes} min` : 'within target'));
  row.appendChild(h);
  (r.caveats || []).forEach(c => row.appendChild(el('div','warnbox', c)));
  return row;
}

function creditRow(c) {
  const eligible = c.outcome === 'ELIGIBLE';
  const row = el('div', `row ${eligible ? 'green' : 'amber'}`);
  const h = el('div','h');
  const l = el('div');
  l.appendChild(el('b', null, `${c.order_id} · ${c.headline}`));
  const m = el('div','meta');
  [c.account_name, `via ${c.authority}`].forEach(x => m.appendChild(el('span',null,x)));
  l.appendChild(m); h.appendChild(l);
  h.appendChild(el('span', `tag ${eligible ? 'green' : 'amber'}`,
    eligible ? `INR ${c.amount_inr}` : 'needs verification'));
  row.appendChild(h);
  (c.caveats || []).forEach(x => row.appendChild(el('div','warnbox', x)));
  if (c.needs_manager) row.appendChild(el('div','warnbox','Manager approval required.'));
  return row;
}


/* ================= employee: review ================= */

let SCOPE = 'customers', PICKED = null;
$('#reviewScope').onclick = e => {
  const b = e.target.closest('button[data-scope]'); if (!b) return;
  SCOPE = b.dataset.scope; PICKED = null;
  $$('#reviewScope button').forEach(x => x.classList.toggle('on', x === b));
  loadChats();
};

async function loadChats() {
  const list = $('#chatList');
  list.innerHTML = '<div class="empty">Loading…</div>';
  const d = await api(`/api/chats?session_id=${S.session_id}&scope=${SCOPE}`);
  list.innerHTML = '';
  if (!d.chats.length) {
    list.appendChild(el('div','empty','No conversations yet.'));
    $('#chatDetail').innerHTML = '<div class="empty">Nothing to review.</div>';
    return;
  }
  d.chats.forEach(c => {
    const b = el('button','crow' + (c.id === PICKED ? ' on' : ''));
    b.appendChild(el('b', null, c.title || 'Conversation'));
    const s = el('small');
    [c.user_name, c.subject_ref, `${c.turns} turn(s)`, ago(c.last_at)]
      .filter(Boolean).forEach(x => s.appendChild(el('span',null,x)));
    b.appendChild(s);
    b.onclick = () => openReview(c.id);
    list.appendChild(b);
  });
  if (!PICKED) openReview(d.chats[0].id);
}

async function openReview(id) {
  PICKED = id;
  $$('#chatList .crow').forEach(n => n.classList.remove('on'));
  const box = $('#chatDetail');
  box.innerHTML = '<div class="empty">Loading…</div>';
  const d = await api(`/api/chats/${id}?session_id=${S.session_id}`);
  box.innerHTML = '';
  const head = el('div');
  head.appendChild(el('b', null, d.chat.title || 'Conversation'));
  const m = el('small','');
  m.style.cssText = 'display:block;color:var(--dim);font:11px var(--mono);margin:5px 0 14px';
  m.textContent = [d.chat.user_name, d.chat.role, d.chat.subject_ref,
                   ago(d.chat.last_at)].filter(Boolean).join(' · ');
  head.appendChild(m); box.appendChild(head);

  d.turns.forEach(t => {
    const w = el('div','turn');
    w.appendChild(el('div','q', t.question));
    const a = el('div','a'); a.innerHTML = md(t.answer); w.appendChild(a);
    const p = t.payload || {};
    (p.decisions || []).forEach(dec => {
      const det = el('details','work');
      det.appendChild(el('summary', null,
        `${dec.kind} → ${dec.outcome}${dec.amount_inr != null ? ` · INR ${dec.amount_inr}` : ''} · via ${dec.authority_used}`));
      const inner = el('div','in');
      const ol = el('ol','chain');
      dec.rule_chain.forEach(s => {
        const li = el('li', null, s.statement);
        li.appendChild(el('span','src', s.source)); ol.appendChild(li);
      });
      inner.appendChild(ol);
      det.appendChild(inner); w.appendChild(det);
    });
    if (p.unverified?.length)
      w.appendChild(el('div','badbox',
        `Unverified figure(s) in the prose: ${p.unverified.join(', ')}`));
    box.appendChild(w);
  });
}


/* ================= employee: policy search ================= */

$('#polForm').onsubmit = async e => {
  e.preventDefault();
  const q = $('#polIn').value.trim(); if (!q) return;
  const box = $('#polResults');
  box.innerHTML = '<div class="empty">Searching…</div>';
  const d = await api(`/api/policy-search?session_id=${S.session_id}&q=${encodeURIComponent(q)}`);
  box.innerHTML = '';
  d.passages.forEach(p => {
    const c = el('div','psg');
    const h = el('div','h');
    h.appendChild(el('div','t', p.citation));
    h.appendChild(el('span','tag blue', p.authority));
    c.appendChild(h);
    c.appendChild(el('div','x', p.text));
    box.appendChild(c);
  });
  if (d.excluded.length) {
    box.appendChild(el('div','grp','Matched but not used'));
    d.excluded.forEach(x => box.appendChild(
      el('div','warnbox', `${x.citation} — ${x.reason}`)));
  }
};
