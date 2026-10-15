"use strict";
const TOKEN = signIn();
const AUTH = {"Authorization": "Bearer " + TOKEN};
const PAGES = ["setup", "data", "describe", "review", "connect", "test", "run", "deploy", "inbox", "assistant"];
const $ = (selector) => document.querySelector(selector);
let thread = null;

function signIn() {
  const supplied = new URLSearchParams(location.search).get("token");
  if (supplied) {
    try { sessionStorage.setItem("awa-token", supplied); } catch (e) { /* storage blocked */ }
    history.replaceState(null, "", "/" + location.hash);
    return supplied;
  }
  try { return sessionStorage.getItem("awa-token") || ""; } catch (e) { return ""; }
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

/* The HTML parser puts <svg> markup into the SVG namespace, so no namespace address is needed. */
function svgFrom(markup) {
  const holder = document.createElement("template");
  holder.innerHTML = markup;
  return holder.content.firstElementChild;
}

function icon(name, cls) {
  const svg = svgFrom('<svg aria-hidden="true"><use></use></svg>');
  svg.setAttribute("class", "ic" + (cls ? " " + cls : ""));
  svg.firstElementChild.setAttribute("href", "#i-" + name);
  return svg;
}

/* The success mark: a green check that draws itself once when it appears. */
function tick(pop) {
  const svg = svgFrom(
    '<svg aria-hidden="true" viewBox="0 0 24 24"><circle class="ring" cx="12" cy="12" r="8.5" pathLength="1"></circle>' +
    '<path class="mark" d="M8.5 12.5l2.5 2.5 4.5-5" pathLength="1"></path></svg>'
  );
  svg.setAttribute("class", "tick" + (pop ? " pop" : ""));
  return svg;
}

/* Every state element gets its symbol from its class, so a word is never the only signal. */
const STATE_ICONS = [
  [".badge.ready, .badge.passed, .alert.ok, .decision.ok", "tick"],
  [".state.live", "plug"],
  [".badge.missing, .badge.failed, .badge.error, .alert.error", "x-circle"],
  [".badge.warning, .alert.warning, .decision.no", "alert"],
  [".badge.skipped", "clock"],
  [".state.copy, .state.output, .state.stand-in", "database"],
  [".approval > h3", "shield-check"],
  [".step.ok > .mark", "check"],
  [".step.failed > .mark, .step.stopped > .mark", "x"],
  [".step.waiting > .mark", "shield-check"],
  ["a.button.primary[href^='#']", "arrow-right:end"],
  [".upload .name, .card.file > .row > h3", "file"],
];

function decorate(root) {
  for (const [selector, spec] of STATE_ICONS) {
    const [name, where] = spec.split(":");
    for (const el of root.querySelectorAll(selector)) {
      if (el.dataset.ic === name) continue;
      el.querySelector(":scope > .ic.auto, :scope > .tick.auto")?.remove();
      const node = name === "tick" ? tick() : icon(name);
      node.classList.add("auto");
      if (where === "end") el.append(node); else el.prepend(node);
      el.dataset.ic = name;
    }
  }
}

function watchStates() {
  let queued = false;
  const run = () => { queued = false; decorate(document.body); };
  new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(run); } })
    .observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ["class"]});
  decorate(document.body);
}

function fail(error) {
  return h("div", {class: "alert error", role: "alert"}, h("div", {},
    h("p", {}, h("strong", {text: "Something went wrong: "}), error.message || String(error)),
    error.fix ? h("p", {}, h("strong", {text: "What to do: "}), error.fix) : null,
    error.link ? h("p", {}, h("a", {class: "button", href: error.link, text: error.link_text || "Fix it"})) : null,
    error.detail ? h("details", {}, h("summary", {text: "Details"}), h("pre", {text: error.detail})) : null));
}

function emptyState(iconName, title, text, action) {
  return h("div", {class: "empty-state"}, icon(iconName, "big"), h("strong", {text: title}), text ? h("p", {text}) : null, action || null);
}

function skeleton(lines) {
  return h("div", {class: "loading", "aria-busy": "true", "aria-label": "Loading"},
    h("span", {class: "skeleton block"}), Array.from({length: lines || 2}, () => h("span", {class: "skeleton"})));
}

/* A short message in the corner; `undo` adds an Undo button and keeps the message for 8 s. */
function toast(text, options) {
  const opts = options || {};
  const box = $("#toasts");
  const el = h("div", {class: "toast", role: opts.error ? "alert" : "status"},
    opts.error ? icon("x-circle") : icon("check-circle"), h("span", {text}));
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  };
  if (opts.undo) {
    el.append(h("button", {type: "button", class: "small", text: "Undo", onclick: () => { close(); opts.undo(); }}));
  }
  el.append(h("button", {type: "button", class: "small ghost", "aria-label": "Dismiss", onclick: close}, icon("x")));
  box.append(el);
  if (!opts.error || opts.undo) setTimeout(close, opts.undo ? 8000 : 5000);
  return close;
}

/* Delete quietly: the row collapses at once and a toast offers Undo for 8 s; the server is only
   asked when the time is up (or when the page changes), so Undo needs nothing from it. */
const pendingRemovals = new Set();

function removeWithUndo(row, label, commit) {
  const wrap = h("div", {class: "collapse"});
  row.replaceWith(wrap);
  const inner = h("div", {}, row);
  wrap.append(inner);
  requestAnimationFrame(() => wrap.classList.add("leave"));
  let done = false;
  const entry = {commit: async () => {
    if (done) return;
    done = true;
    pendingRemovals.delete(entry);
    clearTimeout(timer);
    try { await commit(); } catch (e) { toast(e.message || "Could not remove " + label, {error: true}); }
  }};
  const timer = setTimeout(entry.commit, 8000);
  pendingRemovals.add(entry);
  toast("Removed " + label + ".", {undo: () => {
    if (done) return;
    done = true;
    pendingRemovals.delete(entry);
    clearTimeout(timer);
    wrap.classList.remove("leave");
    setTimeout(() => wrap.replaceWith(row), 220);
  }});
}

async function flushRemovals() {
  await Promise.all([...pendingRemovals].map((entry) => entry.commit()));
}

/* A step is shown with a check once its work exists. */
function markDone(page, done) {
  const item = document.querySelector('nav a[href="#' + page + '"]');
  if (item) item.parentElement.classList.toggle("is-done", Boolean(done));
}

/* Structured output from the server: a string, or {type: facts | table | list}. */
function viewNode(view) {
  if (typeof view === "string") return document.createTextNode(view);
  if (view.type === "facts") {
    return h("dl", {class: "facts"}, view.items.flatMap((item) => [h("dt", {text: item.title}), h("dd", {}, viewNode(item.value))]));
  }
  if (view.type === "table") {
    return h("div", {class: "scroll"},
      h("table", {}, h("thead", {}, h("tr", {}, view.columns.map((c) => h("th", {scope: "col", text: c})))),
        h("tbody", {}, view.rows.map((row) => h("tr", {}, row.map((cell) => h("td", {text: cell})))))),
      view.more ? h("p", {class: "hint", text: "… and " + view.more + " more rows"}) : null);
  }
  return h("ul", {}, view.items.map((item) => h("li", {}, viewNode(item))));
}

/* A link to documentation: only https addresses become links. */
function docLink(url, text) {
  return /^https:\/\//.test(url || "") ? h("a", {href: url, target: "_blank", rel: "noopener noreferrer", text}) : null;
}

/* Markdown (an agent's answer, a guide) as elements: paragraphs, headings, lists, code blocks,
   bold, italic, inline code and http(s) links. Text only ever becomes text nodes, never markup,
   so nothing in an answer can inject HTML. */
const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s](?:[^*]*[^*\s])?\*|\[[^\]]+\]\([^)\s]+\))/;

function inlineMarkdown(text) {
  return text.split(INLINE).filter(Boolean).map((part) => {
    if (part.length > 1 && part.startsWith("`") && part.endsWith("`")) return h("code", {text: part.slice(1, -1)});
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) return h("strong", {}, inlineMarkdown(part.slice(2, -2)));
    if (part.length > 2 && part.startsWith("*") && part.endsWith("*")) return h("em", {}, inlineMarkdown(part.slice(1, -1)));
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
    if (link) {
      return /^https?:\/\//.test(link[2])
        ? h("a", {href: link[2], target: "_blank", rel: "noopener noreferrer"}, inlineMarkdown(link[1]))
        : h("span", {}, inlineMarkdown(link[1]));
    }
    return part;
  });
}

function markdown(text) {
  const root = h("div", {class: "doc"});
  let list = null, para = null, code = null;
  for (const raw of String(text).split("\n")) {
    const line = raw.trimEnd();
    if (code) {
      if (/^\s*```/.test(line)) { code = null; continue; }
      code.lines.push(raw);
      code.el.textContent = code.lines.join("\n");
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const item = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (/^\s*```/.test(line)) {
      list = para = null;
      code = {lines: [], el: h("code")};
      root.append(h("pre", {}, code.el));
    } else if (item) {
      para = null;
      const tag = /\d/.test(item[1]) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) { list = h(tag); root.append(list); }
      list.append(h("li", {}, inlineMarkdown(item[2])));
    } else if (heading) {
      list = para = null;
      root.append(h(heading[1].length <= 2 ? "h3" : "h4", {}, inlineMarkdown(heading[2])));
    } else if (!line.trim() || /^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      list = para = null;
    } else if (para) {
      para.append(h("br"), ...inlineMarkdown(line.trim()));
    } else {
      list = null;
      para = h("p", {}, inlineMarkdown(line.trim()));
      root.append(para);
    }
  }
  return root;
}

function note(kind, text) { return h("div", {class: "alert " + kind, role: "status"}, h("div", {}, h("p", {text}))); }

async function api(path, body) {
  const options = body === undefined ? {headers: AUTH} : {
    method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", ...AUTH}};
  let response;
  try { response = await fetch(path, options); }
  catch (e) { throw {message: "awa is not reachable", fix: "Make sure the awa window is still open, then reload this page."}; }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw data.error || {message: "Request failed (" + response.status + ")"};
  return data;
}

async function stream(path, body, onEvent, signal) {
  const options = body === undefined ? {headers: AUTH} : {
    method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", ...AUTH}};
  if (signal) options.signal = signal;
  let response;
  try { response = await fetch(path, options); }
  catch (e) {
    if (e.name === "AbortError") throw e;
    throw {message: "awa is not reachable", fix: "Make sure the awa window is still open, then reload this page."};
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw data.error || {message: "Request failed (" + response.status + ")"};
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message", data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

async function busy(button, work) {
  const kept = [...button.childNodes];
  button.disabled = true;
  button.replaceChildren(h("span", {class: "spinner", "aria-hidden": "true"}), "Working…");
  try { await work(); } finally { button.disabled = false; button.replaceChildren(...kept); }
}
