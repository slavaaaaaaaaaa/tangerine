// tangerine-ask overlay: an "Ask" button on the archive's document pages that opens
// a chat panel. Lives in a shadow root so the archive's styles and ours don't mix.
(function () {
  "use strict";
  if (window.__tangerineAsk) return;
  window.__tangerineAsk = true;

  const PATH = location.pathname;
  const API = "/_ask/api/";
  let st = null;            // last /state response
  let chat = null;          // the open conversation: {id, path, docName, title, created, updated, messages}
  let busy = false;
  let pollTimer = null;
  let pendingQuestion = null;

  // ---------------------------------------------------------------- storage (best effort)
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} },
  };
  const noun = () => (st && st.noun) || "document";
  const draftKey = () => "tangerine-ask-draft:" + (st && st.doc ? st.doc.path : PATH);

  // ---------------------------------------------------------------- conversations
  // Kept only in this browser (per signed-in user); the server keeps none. Each
  // question sends the earlier turns along so follow-ups have context.
  const MAX_CHATS = 100;
  const HISTORY_MESSAGES = 16;      // the server keeps at most 8 earlier question/answer pairs
  const HISTORY_CHARS = 12000;
  const chatsKey = () => "tangerine-ask-chats:" + (st ? st.user : "");
  const chats = {
    all() {
      try {
        const v = JSON.parse(store.get(chatsKey()) || "[]");
        return Array.isArray(v) ? v : [];
      } catch (e) { return []; }
    },
    save(list) {
      list.sort((a, b) => b.updated - a.updated);
      list = list.slice(0, MAX_CHATS);
      // Out of room: drop the oldest conversations until it fits.
      while (list.length) {
        try { localStorage.setItem(chatsKey(), JSON.stringify(list)); return true; }
        catch (e) { list.pop(); }
      }
      return false;
    },
    put(c) { return this.save(this.all().filter((x) => x.id !== c.id).concat([c])); },
    remove(id) { this.save(this.all().filter((x) => x.id !== id)); },
    latestFor(path) { return this.all().filter((c) => c.path === path).sort((a, b) => b.updated - a.updated)[0] || null; },
  };

  // ---------------------------------------------------------------- api
  class SignedOut extends Error {}

  async function api(method, name, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(API + name, opts);
    if (r.status === 401) throw new SignedOut();
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(data.error || "Request failed (" + r.status + ")");
    return data;
  }

  // ---------------------------------------------------------------- dom helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(kid));
    return el;
  }

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------------------------------------------------------------- markdown (small, escapes first)
  function inline(s, cites) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((\/[^)\s]*)\)/g, '<a href="$2">$1</a>');
    s = s.replace(/\[p:(\d+)\]/g, (_, id) => {
      const c = cites(id);
      return c ? '<a class="cite" href="' + esc(c.url) + '" title="' + esc(c.title) + '">' + c.n + "</a>"
               : '<span class="cite pending">p' + id + "</span>";
    });
    return s;
  }

  // One definition per block type, used both to handle a block and to end a paragraph,
  // so a line can never be refused by both (that loops forever on half-streamed text).
  const FENCE = /^```/;
  const HEADING = /^(#{1,4})\s+(.*)/;
  const TABLE_ROW = /^\s*\|.*\|\s*$/;
  const LIST_ITEM = /^\s*([-*]|\d+[.)])\s+/;
  const startsBlock = (l) => FENCE.test(l) || HEADING.test(l) || TABLE_ROW.test(l) || LIST_ITEM.test(l);

  function markdown(src, cites) {
    const lines = src.replace(/\r/g, "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (FENCE.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !FENCE.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>");
        continue;
      }
      let m;
      if ((m = line.match(HEADING))) {
        const lvl = Math.min(4, m[1].length + 2);
        out.push("<h" + lvl + ">" + inline(m[2], cites) + "</h" + lvl + ">");
        i++;
        continue;
      }
      if (TABLE_ROW.test(line)) {
        const rows = [];
        while (i < lines.length && TABLE_ROW.test(lines[i])) rows.push(lines[i++]);
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const body = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
        if (!body.length) continue;
        const [head, ...rest] = body;
        out.push('<div class="table"><table><thead><tr>' + cells(head).map((c) => "<th>" + inline(c, cites) + "</th>").join("") +
          "</tr></thead><tbody>" + rest.map((r) => "<tr>" + cells(r).map((c) => "<td>" + inline(c, cites) + "</td>").join("") + "</tr>").join("") +
          "</tbody></table></div>");
        continue;
      }
      if (LIST_ITEM.test(line)) {
        const ordered = /^\s*\d/.test(line);
        const items = [];
        while (i < lines.length && LIST_ITEM.test(lines[i])) {
          let item = lines[i++].replace(LIST_ITEM, "");
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !LIST_ITEM.test(lines[i])) item += " " + lines[i++].trim();
          items.push("<li>" + inline(item, cites) + "</li>");
        }
        out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const para = [lines[i++]];
      while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) para.push(lines[i++]);
      out.push("<p>" + inline(para.join(" "), cites) + "</p>");
    }
    return out.join("");
  }

  // Numbered citations in order of first appearance.
  function citer(text, sources) {
    const byId = new Map((sources || []).map((s) => [String(s.id), s]));
    const order = [];
    for (const m of text.matchAll(/\[p:(\d+)\]/g)) if (!order.includes(m[1])) order.push(m[1]);
    return {
      order,
      byId,
      get(id) {
        const s = byId.get(String(id));
        return s ? { url: s.url, title: s.title, n: order.indexOf(String(id)) + 1 } : null;
      },
    };
  }

  // ---------------------------------------------------------------- ui
  const host = h("div", { id: "tangerine-ask" });
  const root = host.attachShadow({ mode: "open" });
  root.append(h("link", { rel: "stylesheet", href: "/_ask/overlay.css" }));

  const fab = h("button", { class: "fab", type: "button", "aria-label": "Ask a question", onclick: () => toggle(true) },
    h("span", { class: "dot", "aria-hidden": "true" }), "Ask");
  const title = h("div", { class: "title" });
  const sub = h("div", { class: "sub" });
  const body = h("div", { class: "body", "aria-live": "polite" });
  const input = h("textarea", { rows: "2", "aria-label": "Your question" });
  const send = h("button", { class: "send", type: "submit" }, "Ask");
  const form = h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); submit(); } },
    input, send);
  const note = h("div", { class: "note" },
    "Questions and the excerpts needed to answer them go to your AI provider. ",
    h("button", { type: "button", class: "link", onclick: () => show("privacy") }, "Privacy"));
  const footer = h("div", { class: "footer" }, form, note);

  const panel = h("section", { class: "panel", role: "dialog", "aria-label": "Ask a question", hidden: true },
    h("header", {},
      h("div", { class: "heading" }, title, sub),
      h("button", { type: "button", class: "icon", title: "New chat", "aria-label": "New chat", onclick: newChat }, "＋"),
      h("button", { type: "button", class: "icon", title: "Conversations", "aria-label": "Conversations", onclick: () => show("history") }, "☰"),
      h("button", { type: "button", class: "icon", title: "Settings", "aria-label": "Settings", onclick: () => show("settings") }, "⚙"),
      h("button", { type: "button", class: "icon", title: "Close", "aria-label": "Close", onclick: () => toggle(false) }, "✕")),
    body, footer);

  // ---------------------------------------------------------------- resizing (drag the left edge)
  const MIN_WIDTH = 340;
  const grip = h("div", { class: "grip", role: "separator", "aria-orientation": "vertical", tabindex: "0",
    "aria-label": "Resize the chat panel (arrow keys)", title: "Drag to resize, double-click to reset" });
  panel.prepend(grip);

  function setWidth(w, save) {
    if (w == null) {
      panel.style.width = "";
    } else {
      w = Math.round(Math.max(MIN_WIDTH, Math.min(w, window.innerWidth - 24)));
      panel.style.width = w + "px";
    }
    if (save) store.set("tangerine-ask-width", w == null ? null : String(w));
  }
  const savedWidth = parseInt(store.get("tangerine-ask-width"), 10);
  if (savedWidth > 0) setWidth(savedWidth, false);
  window.addEventListener("resize", () => { if (panel.style.width) setWidth(parseInt(panel.style.width, 10), false); });

  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    panel.classList.add("resizing");
    const right = window.innerWidth - panel.getBoundingClientRect().right;
    const move = (ev) => setWidth(window.innerWidth - right - ev.clientX, false);
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      grip.removeEventListener("pointercancel", up);
      panel.classList.remove("resizing");
      setWidth(panel.getBoundingClientRect().width, true);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
  });
  grip.addEventListener("dblclick", () => setWidth(null, true));
  grip.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 100 : 30;
    if (e.key === "ArrowLeft") setWidth(panel.getBoundingClientRect().width + step, true);
    else if (e.key === "ArrowRight") setWidth(panel.getBoundingClientRect().width - step, true);
    else return;
    e.preventDefault();
  });

  root.append(fab, panel);

  input.addEventListener("input", () => store.set(draftKey(), input.value || null));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") toggle(false); });

  function toggle(open) {
    panel.hidden = !open;
    fab.hidden = open;
    try { sessionStorage.setItem("tangerine-ask-open", open ? "1" : ""); } catch (e) {}
    if (open) {
      refresh().then(() => {
        chat = chat || chats.latestFor(st.doc.path);
        if (!st.settings.privacy_ack) show("privacy");
        else if (!st.settings.key_hint || !st.settings.model) show("settings");
        else show("chat");
      }).catch(fail);
    }
  }

  function signedOutView() {
    body.replaceChildren(h("div", { class: "card" },
      h("h3", {}, "You've been signed out"),
      h("p", {}, "Sign-ins expire after a day (or two weeks with “Remember me”). Your unsent question is saved."),
      h("a", { class: "button", href: "/_gate/login?next=" + encodeURIComponent(PATH) }, "Sign in again")));
    footer.hidden = true;
  }

  function fail(e) {
    if (e instanceof SignedOut) return signedOutView();
    body.append(h("div", { class: "error" }, e.message || String(e)));
    body.scrollTop = body.scrollHeight;
  }

  async function refresh() {
    st = await api("GET", "state?path=" + encodeURIComponent(PATH));
    if (st.doc) {
      title.textContent = st.doc.name;
      showModel();
    }
    return st;
  }

  // ---------------------------------------------------------------- views
  function show(view) {
    footer.hidden = view !== "chat";
    if (view === "privacy") return privacyView();
    if (view === "settings") return settingsView();
    if (view === "history") return historyView();
    chatView();
  }

  // "$2 in / $10 out per 1M tokens"
  const money = (n) => "$" + (n >= 1 ? n.toFixed(2).replace(/\.00$/, "") : String(+n.toFixed(3)));
  const priceText = (p) => p && p.input != null && p.output != null
    ? money(p.input) + " in / " + money(p.output) + " out per 1M tokens" + (p.note ? " (" + p.note + ")" : "") : "";
  function showModel() {
    const s = st.settings;
    const p = priceText(s.price);
    sub.textContent = s.model ? s.model + (p ? " · " + p : "") : "No model chosen yet";
  }

  function privacyView() {
    const ack = st && st.settings.privacy_ack;
    body.replaceChildren(h("div", { class: "card" },
      h("h3", {}, "Your questions leave this server"),
      h("p", {}, "When you ask a question, it is sent to the AI provider you choose (OpenAI or Anthropic), together with the excerpts of this " + noun() + " needed to answer it."),
      h("p", {}, "It goes through your own API key and falls under that provider's data-use and retention terms."),
      h("p", {}, "Your conversations are kept only in this browser, not on this server. Follow-up questions send the earlier part of the conversation along so the AI has context."),
      h("p", {}, "Nothing is sent until you ask a question."),
      ack ? h("button", { type: "button", class: "button", onclick: () => show("chat") }, "Back")
          : h("button", { type: "button", class: "button", onclick: async () => {
              try {
                await api("POST", "privacy", {});
                await refresh();
                show(st.settings.key_hint && st.settings.model ? "chat" : "settings");
              } catch (e) { fail(e); }
            } }, "I understand")));
  }

  function settingsView() {
    const s = st.settings;
    const provider = h("select", { "aria-label": "Provider" },
      h("option", { value: "anthropic", selected: s.provider === "anthropic" }, "Anthropic (Claude)"),
      h("option", { value: "openai", selected: s.provider === "openai" }, "OpenAI (GPT)"));
    const key = h("input", { type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: s.key_hint ? "Saved key …" + s.key_hint + " (paste to replace)" : "Paste your API key" });
    const model = h("select", { "aria-label": "Model", disabled: !s.key_hint });
    const msg = h("div", { class: "msg" });
    const done = h("button", { type: "button", class: "button", disabled: !(s.key_hint && s.model), onclick: () => show("chat") }, "Done");

    function fillModels(list, current) {
      model.replaceChildren(h("option", { value: "" }, list.length ? "Choose a model…" : "No models available"),
        ...list.map((m) => {
          const p = priceText(m.price);
          return h("option", { value: m.id, selected: m.id === current }, m.id + (p ? " — " + p : " — price unknown"));
        }));
      model.disabled = !list.length;
    }
    if (s.key_hint) {
      fillModels(s.model ? [{ id: s.model, price: s.price }] : [], s.model);
      api("GET", "models").then((r) => fillModels(r.models, s.model)).catch((e) => { msg.textContent = e.message; });
    }

    async function save(extra) {
      msg.textContent = "Saving…";
      try {
        const r = await api("POST", "settings", Object.assign({ provider: provider.value }, extra));
        st.settings = Object.assign(st.settings, r.settings);
        if (r.models) fillModels(r.models, null);
        if (!r.settings.key_hint) fillModels([], null);
        key.value = "";
        key.placeholder = r.settings.key_hint ? "Saved key …" + r.settings.key_hint + " (paste to replace)" : "Paste your API key";
        msg.textContent = r.models ? "Key works. Now pick a model." : r.settings.model ? "Saved." : "";
        done.disabled = !(r.settings.key_hint && r.settings.model);
        showModel();
      } catch (e) {
        if (e instanceof SignedOut) return signedOutView();
        msg.textContent = e.message;
      }
    }

    provider.addEventListener("change", () => {
      if (s.key_hint && !confirm("Switching provider forgets your saved key. Continue?")) {
        provider.value = s.provider;
        return;
      }
      save({});
    });
    model.addEventListener("change", () => model.value && save({ model: model.value }));

    body.replaceChildren(h("div", { class: "card" },
      h("h3", {}, "Your AI provider"),
      h("p", { class: "muted" }, "Answers are billed to your own API key. It's stored encrypted on this server and never shown again."),
      h("label", {}, "Provider", provider),
      h("label", {}, "API key", h("div", { class: "row" }, key,
        h("button", { type: "button", class: "button small", onclick: () => key.value.trim() && save({ key: key.value.trim() }) }, "Save"))),
      h("label", {}, "Model", model),
      h("p", { class: "muted fine" }, "Prices are the providers' list prices per million tokens, as of October 2026; check your provider for current rates. One question usually uses tens of thousands of input tokens (mostly cached, which is cheaper) and a few thousand output tokens."),
      msg, done));
  }

  function chatView() {
    body.replaceChildren();
    const msgs = chat ? chat.messages : [];
    input.placeholder = msgs.length ? "Ask a follow-up…" : "Ask about this " + noun() + "…";
    if (!msgs.length) {
      body.append(h("div", { class: "empty" },
        h("p", {}, "Ask anything about " + st.doc.name + "."),
        (st.examples || []).length ? h("p", { class: "muted" }, "e.g. " + st.examples.map((x) => "“" + x + "”").join(", ")) : null));
    }
    for (const m of msgs) body.append(m.role === "user" ? userBubble(m.content) : answerBubble(m.content, m.sources));
    const draft = store.get(draftKey());
    if (draft && !input.value) input.value = draft;
    if (pendingQuestion) {
      const q = pendingQuestion;
      pendingQuestion = null;
      ask(q);
    }
    body.scrollTop = body.scrollHeight;
    input.focus();
  }

  function userBubble(text) {
    return h("div", { class: "msg-user" }, text);
  }

  function answerBubble(text, sources) {
    const el = h("div", { class: "msg-ai" });
    renderAnswer(el, text, sources);
    return el;
  }

  function renderAnswer(el, text, sources) {
    const c = citer(text, sources);
    const content = h("div", { class: "md" });
    content.innerHTML = markdown(text, (id) => c.get(id));
    el.replaceChildren(content);
    const cited = c.order.map((id) => c.byId.get(id)).filter(Boolean);
    if (!cited.length) return;
    el.append(h("ol", { class: "sources" }, cited.map((s) =>
      h("li", {}, h("a", { href: s.url }, s.title), s.other ? h("span", { class: "tag" }, "other variant") : null,
        h("div", { class: "crumbs" }, s.crumbs)))));
    const figs = [];
    for (const s of cited) for (const f of (s.figures || []).slice(0, 4)) if (figs.length < 8) figs.push([s, f]);
    if (figs.length) {
      el.append(h("div", { class: "figures" }, figs.map(([s, f]) =>
        h("a", { href: s.url, title: s.title }, h("img", { src: f, alt: "Figure from " + s.title, loading: "lazy" })))));
    }
  }

  function newChat() {
    if (busy) return;
    chat = null;
    show("chat");
  }

  function ago(t) {
    const d = (Date.now() - t) / 1000;
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + " min ago";
    if (d < 86400) return Math.floor(d / 3600) + " h ago";
    if (d < 86400 * 30) return Math.floor(d / 86400) + " d ago";
    return new Date(t).toLocaleDateString();
  }

  function historyView() {
    const all = chats.all().sort((a, b) => b.updated - a.updated);
    const here = all.filter((c) => c.path === st.doc.path);
    const elsewhere = all.filter((c) => c.path !== st.doc.path);

    const row = (c, local) => h("li", { class: c === chat || (chat && c.id === chat.id) ? "current" : null },
      h("button", { type: "button", class: "open", onclick: () => {
        if (local) { chat = c; show("chat"); return; }
        // Another document's conversation: go to that document and open it there.
        try { sessionStorage.setItem("tangerine-ask-open-chat", c.id); sessionStorage.setItem("tangerine-ask-open", "1"); } catch (e) {}
        location.href = c.path;
      } },
        h("span", { class: "t" }, c.title),
        h("span", { class: "meta" }, (local ? "" : c.docName + " · ") + ago(c.updated) + " · " +
          Math.ceil(c.messages.length / 2) + (c.messages.length > 2 ? " questions" : " question"))),
      h("button", { type: "button", class: "icon small", title: "Delete", "aria-label": "Delete this conversation", onclick: () => {
        if (!confirm("Delete this conversation?")) return;
        chats.remove(c.id);
        if (chat && chat.id === c.id) chat = null;
        historyView();
      } }, "✕"));

    body.replaceChildren(h("div", { class: "history" },
      h("div", { class: "history-head" },
        h("h3", {}, "Conversations"),
        h("button", { type: "button", class: "button small", onclick: () => show("chat") }, "Back")),
      h("p", { class: "muted" }, "Kept only in this browser. Clearing this site's data, or using another browser, starts fresh."),
      h("h4", {}, "This " + noun()),
      here.length ? h("ul", {}, here.map((c) => row(c, true))) : h("p", { class: "muted" }, "None yet."),
      elsewhere.length ? [h("h4", {}, "Other " + noun() + "s"), h("ul", {}, elsewhere.map((c) => row(c, false)))] : null,
      all.length ? h("button", { type: "button", class: "link danger", onclick: () => {
        if (!confirm("Delete all " + all.length + " conversations in this browser?")) return;
        chats.save([]);
        chat = null;
        historyView();
      } }, "Delete all conversations") : null));
  }

  // ---------------------------------------------------------------- asking
  function submit() {
    const q = input.value.trim();
    if (!q || busy) return;
    if (st.doc.status !== "ready") return prepare(q);
    ask(q);
  }

  function progressCard() {
    const v = st.doc;
    const p = v.progress;
    const text = v.status === "failed" ? "Couldn't read this " + noun() + ": " + (v.error || "unknown error")
      : p ? "Reading pages… " + p.fetched.toLocaleString() + " of " + Math.max(p.discovered, p.fetched).toLocaleString() + " pages"
      : v.status === "queued" ? "Waiting for another " + noun() + " to finish…" : "Starting…";
    return h("div", { class: "card progress" },
      h("h3", {}, v.status === "failed" ? "Something went wrong" : "Getting this " + noun() + " ready"),
      h("p", {}, text),
      v.status === "failed" ? h("button", { type: "button", class: "button", onclick: () => prepare(pendingQuestion) }, "Try again")
        : h("p", { class: "muted" }, "This happens once per " + noun() + " and takes about a minute. Your question will be sent when it's done."));
  }

  async function prepare(q) {
    pendingQuestion = q;
    try {
      const r = await api("POST", "index", { path: PATH });
      st.doc = Object.assign(st.doc, r.doc);
    } catch (e) { return fail(e); }
    const card = progressCard();
    body.append(card);
    body.scrollTop = body.scrollHeight;
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        await refresh();
      } catch (e) {
        clearInterval(pollTimer);
        return fail(e);
      }
      if (st.doc.status === "ready") {
        clearInterval(pollTimer);
        card.remove();
        show("chat");      // sends pendingQuestion
      } else {
        card.replaceWith(progressCard());
        if (st.doc.status === "failed") clearInterval(pollTimer);
      }
    }, 3000);
  }

  async function ask(q) {
    busy = true;
    send.disabled = true;
    input.value = "";
    store.set(draftKey(), null);
    body.querySelector(".empty")?.remove();
    body.append(userBubble(q));
    const el = h("div", { class: "msg-ai streaming" });
    const status = h("div", { class: "status" }, "Thinking…");
    body.append(el, status);
    body.scrollTop = body.scrollHeight;
    const history = (chat ? chat.messages : []).slice(-HISTORY_MESSAGES)
      .map((m) => ({ role: m.role, content: m.content.slice(0, HISTORY_CHARS) }));
    let text = "";
    let sources = null;
    let frame = 0;
    const paint = () => {
      frame = 0;
      renderAnswer(el, text, sources);
      if (body.scrollHeight - body.scrollTop - body.clientHeight < 120) body.scrollTop = body.scrollHeight;
    };
    try {
      const r = await fetch(API + "ask", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: PATH, question: q, history }),
      });
      if (r.status === 401) throw new SignedOut();
      if (!r.ok) {
        let data = {};
        try { data = await r.json(); } catch (e) {}
        if (data.error === "not_ready") { el.remove(); status.remove(); body.lastChild.remove(); busy = false; send.disabled = false; return prepare(q); }
        if (data.error === "privacy") throw new Error("Please read the privacy note first.");
        throw new Error(data.error || "Request failed (" + r.status + ")");
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          if (!chunk.startsWith("data: ")) continue;
          const ev = JSON.parse(chunk.slice(6));
          if (ev.type === "text") { text += ev.data; status.textContent = "Writing…"; frame ||= requestAnimationFrame(paint); }
          else if (ev.type === "status") status.textContent = ev.data;
          else if (ev.type === "sources") sources = ev.data;
          else if (ev.type === "error") el.after(h("div", { class: "error" }, ev.data));
        }
      }
      paint();
      if (text) remember(q, text, sources);
      else { el.remove(); input.value = input.value || q; }
    } catch (e) {
      if (e instanceof SignedOut) {
        store.set(draftKey(), q);
        return signedOutView();
      }
      if (text) remember(q, text, sources);
      else { el.remove(); input.value = input.value || q; }
      fail(e);
    } finally {
      el.classList.remove("streaming");
      status.remove();
      busy = false;
      send.disabled = false;
      input.placeholder = chat ? "Ask a follow-up…" : "Ask about this " + noun() + "…";
      input.focus();
    }
  }

  function remember(q, text, sources) {
    const now = Date.now();
    if (!chat) {
      chat = { id: now.toString(36) + Math.random().toString(36).slice(2, 8), path: st.doc.path,
        docName: st.doc.name, title: q.length > 90 ? q.slice(0, 87) + "…" : q, created: now, messages: [] };
    }
    chat.messages.push({ role: "user", content: q }, { role: "assistant", content: text, sources: sources || [] });
    chat.updated = now;
    if (!chats.put(chat)) {
      body.append(h("div", { class: "status" }, "This browser wouldn't save the conversation (storage blocked or full)."));
    }
  }

  // ---------------------------------------------------------------- start
  async function init() {
    try {
      await refresh();
    } catch (e) {
      return;   // signed out or service down: stay invisible
    }
    if (!st.doc) return;   // not a page of a document this service reads
    document.body.append(host);
    let reopen = false;
    try {
      reopen = sessionStorage.getItem("tangerine-ask-open") === "1";
      const wanted = sessionStorage.getItem("tangerine-ask-open-chat");
      sessionStorage.removeItem("tangerine-ask-open-chat");
      if (wanted) chat = chats.all().find((c) => c.id === wanted && c.path === st.doc.path) || null;
    } catch (e) {}
    if (reopen) toggle(true);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
