const turn = {live: null, steps: null, done: 0, started: 0, timer: null, controller: null, decided: null};

function clearEmpty() {
  const empty = $("#conversation .empty");
  if (empty) empty.remove();
}

function bubble(who, text, cls) {
  clearEmpty();
  const el = h("div", {class: "bubble " + cls}, h("span", {class: "who", text: who}), h("span", {text}));
  $("#conversation").append(el);
  el.scrollIntoView({block: "nearest"});
  return el.lastChild;
}

function working(on) {
  const box = $("#working");
  box.hidden = !on;
  $("#stop").hidden = !on;
  $("#send").disabled = on;
  clearInterval(turn.timer);
  if (!on) return;
  turn.started = Date.now();
  turn.done = 0;
  const tick = () => {
    const seconds = Math.round((Date.now() - turn.started) / 1000);
    $("#working-text").textContent = "Working… " + seconds + " s" +
      (turn.done ? " · " + turn.done + (turn.done === 1 ? " step" : " steps") + " done" : "");
  };
  tick();
  turn.timer = setInterval(tick, 1000);
}

function addStep(data) {
  clearEmpty();
  // A run continued after an approval reports the paused call again: it is the same step.
  const same = [...document.querySelectorAll("#conversation .step.running, #conversation .step.waiting")]
    .find((li) => li.dataset.tool === data.tool && li.dataset.args === (data.details || ""));
  if (same) { same.dataset.id = data.id; same.classList.replace("waiting", "running"); return; }
  if (!turn.steps || !turn.steps.isConnected) {
    turn.steps = h("ol", {class: "timeline", "aria-label": "What the system did"});
    $("#conversation").append(turn.steps);
  }
  const item = h("li", {class: "step " + (data.done ? "ok" : "running"), "data-id": data.id || "", "data-tool": data.tool || ""},
    h("span", {class: "mark", "aria-hidden": "true"}),
    h("span", {class: "what"},
      h("span", {class: "sentence", text: data.text}),
      data.agent ? h("span", {class: "who", text: data.agent}) : null),
    data.details ? h("details", {}, h("summary", {text: "Details"}), h("pre", {text: data.details})) : null);
  item.dataset.sentence = data.text;
  item.dataset.args = data.details || "";
  turn.steps.append(item);
  item.scrollIntoView({block: "nearest"});
}

function finishStep(data) {
  const open = [...document.querySelectorAll("#conversation .step.running, #conversation .step.waiting")];
  const item = open.find((li) => li.dataset.id === data.id) || open.reverse().find((li) => li.dataset.tool === data.tool);
  if (!item) return;
  item.classList.remove("running", "waiting");
  item.classList.add(data.ok ? "ok" : data.rejected ? "stopped" : "failed");
  item.querySelector(".sentence").textContent = data.ok ? item.dataset.sentence
    : data.rejected ? "Not done, because you said no: " + item.dataset.sentence : "This did not work: " + item.dataset.sentence;
  if (data.result) {
    let details = item.querySelector("details");
    if (!details) { details = h("details", {}, h("summary", {text: "Details"})); item.append(details); }
    details.append(h("pre", {text: "Result:\n" + data.result}));
  }
  turn.done += 1;
  if (turn.decided && turn.decided.tool === data.tool) {
    const text = (data.result || "").trim();
    const short = text && !/^[\[{]/.test(text) ? ": " + text.slice(0, 200) : ".";
    turn.decided.outcome.textContent = (data.ok ? "Done" : "It did not work") + short;
    turn.decided = null;
  }
}

function onRun(event, data) {
  const conversation = $("#conversation");
  if (event === "delta") {
    if (!turn.live) turn.live = bubble("System", "", "system");
    turn.live.textContent += data.text;
  }
  if (event === "activity") addStep(data);
  if (event === "activity_done") finishStep(data);
  if (event === "done") {
    thread = data.thread_id;
    if (data.view) {
      if (turn.live) turn.live.parentNode.remove();
      answerCard(data);
    } else if (turn.live && turn.live.textContent.trim() === data.text.trim()) {
      turn.live.replaceWith(markdown(data.text));
    } else {
      bubble(humanName(data.agent) || "System", "", "system").replaceWith(markdown(data.text));
    }
    turn.live = null;
    turn.steps = null;
    $("#run-state").textContent = "The system has answered.";
  }
  if (event === "approval") {
    thread = data.thread_id;
    turn.live = null;
    turn.steps = null;
    askApproval(data.pending);
  }
  if (event === "error") {
    turn.live = null;
    conversation.append(fail(data));
  }
}

function humanName(name) { return name ? name.replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase()) : ""; }

function answerCard(data) {
  clearEmpty();
  const card = h("div", {class: "bubble system answer"}, h("span", {class: "who", text: humanName(data.agent) || "System"}),
    viewNode(data.view),
    h("details", {}, h("summary", {text: "Details"}), h("pre", {text: data.text})));
  $("#conversation").append(card);
  card.scrollIntoView({block: "nearest"});
}

/* One approval as a card: what will happen, the values with titles, why a person decides, and a
   field for the reason when saying no. `key` makes the element ids unique. Used by Try it and by
   the inbox of automated runs. */
function approvalCard(p, key, heading) {
  const reason = h("input", {id: "note-" + key, type: "text", autocomplete: "off"});
  const rows = p.fields.map((f) => h("tr", {},
    h("th", {scope: "row"}, f.title, f.description ? h("span", {class: "hint block", text: f.description}) : null),
    h("td", {text: f.value})));
  const card = h("div", {class: "card approval", role: "group", "aria-labelledby": "approval-" + key},
    h("h3", {id: "approval-" + key, text: heading || "Please check before this happens"}),
    h("p", {class: "what-happens"}, h("strong", {text: "What will happen: "}), p.summary),
    rows.length ? h("table", {class: "facts-table"}, h("tbody", {}, rows)) : null,
    h("p", {class: "why"}, h("strong", {text: "Why you are asked: "}), p.why.charAt(0).toUpperCase() + p.why.slice(1) + "."),
    h("label", {for: "note-" + key}, "Tell the agent why ", h("span", {class: "hint", text: "(only if you say no; the agent reads it)"})), reason,
    h("details", {}, h("summary", {text: "Details"}), h("pre", {text: p.args})));
  return {card, reason};
}

function askApproval(pending) {
  const box = $("#approvals");
  const cards = pending.map((p, index) => ({p, ...approvalCard(p, index,
    pending.length > 1 ? "Please check before this happens (" + (index + 1) + " of " + pending.length + ")" : null)}));
  let sent = false;
  const decide = (approve) => async () => {
    if (sent) return;  // a double click must not send the decision twice
    sent = true;
    for (const button of box.querySelectorAll("button")) button.disabled = true;
    const decisions = cards.map(({p, reason}) => ({id: p.id, approve, note: reason.value}));
    const outcome = h("p", {class: "hint", text: approve ? "Waiting for the result…" : "The system was told not to go ahead."});
    const record = h("div", {class: "decision " + (approve ? "ok" : "no"), role: "status"}, h("div", {},
      h("p", {}, h("strong", {text: approve ? "You approved: " : "You said no: "}), cards.map(({p}) => p.summary).join("; "),
        " ", h("span", {class: "hint", text: new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})})),
      ...cards.filter(({reason}) => !approve && reason.value.trim()).map(({reason}) => h("p", {class: "hint", text: "Your note: " + reason.value})),
      outcome));
    turn.decided = approve ? {tool: cards[0].p.tool, outcome} : null;
    box.replaceChildren();
    composer(true);
    $("#conversation").append(record);
    record.scrollIntoView({block: "nearest"});
    await runTurn((signal) => stream("/api/run/resume", {thread_id: thread, decisions}, onRun, signal));
  };
  box.replaceChildren(...cards.map((c) => c.card), h("div", {class: "actions"},
    h("button", {class: "primary", type: "button", onclick: decide(true), text: "Approve"}),
    h("button", {type: "button", onclick: decide(false), text: "Say no"})));
  for (const {p} of cards) {
    const step = [...document.querySelectorAll("#conversation .step.running")].reverse().find((li) => li.dataset.tool === p.tool);
    if (step) {
      step.classList.replace("running", "waiting");
      step.querySelector(".sentence").textContent = "Waiting for your OK: " + p.summary;
    }
  }
  $("#run-state").textContent = "The system is waiting for your OK.";
  composer(false);
  box.querySelector("button").focus();
}

/* While a decision is open, the note on the approval card is the only text field. */
function composer(on) {
  $("#composer").hidden = !on;
  $("#send").hidden = !on;
}

async function runTurn(start) {
  turn.controller = new AbortController();
  working(true);
  try { await start(turn.controller.signal); }
  catch (e) {
    if (e.name === "AbortError") $("#conversation").append(note("warning", "You stopped this. Nothing more will happen; your next message starts from here."));
    else $("#conversation").append(fail(e));
  } finally {
    working(false);
    turn.controller = null;
    turn.steps = null;
    for (const li of document.querySelectorAll("#conversation .step.running")) li.classList.replace("running", "stopped");
  }
}

async function send(text) {
  if (turn.controller) return;  // one turn at a time, whether started by Enter or the button
  const input = $("#message"), message = (text || input.value).trim();
  if (!message) { input.focus(); return; }
  input.value = "";
  $("#approvals").replaceChildren();
  bubble("You", message, "you");
  await runTurn((signal) => stream("/api/run", {message, thread_id: thread}, onRun, signal));
  input.focus();
}

function newChat() {
  if (turn.controller) turn.controller.abort();
  thread = null;
  $("#conversation").replaceChildren(emptyConversation());
  $("#approvals").replaceChildren();
  composer(true);
  $("#message").focus();
}

function emptyConversation() {
  return h("p", {class: "empty", text: "Nothing yet. Write what you would tell a colleague, or pick one of the examples above. The steps the system takes appear here as they happen."});
}

async function loadRun() {
  const notice = $("#run-notice"), examples = $("#examples");
  let data;
  try { data = await api("/api/try"); } catch (e) { notice.replaceChildren(fail(e)); return; }
  notice.replaceChildren();
  $("#run-controls").hidden = !data.exists;
  if (!data.exists) {
    notice.append(emptyState("chat", "Nothing to try yet", "There is no system in this folder yet.",
      h("a", {class: "button primary", href: "#describe", text: "Describe your process"})));
  } else if (!data.model_ready) {
    notice.append(h("div", {class: "alert warning", role: "status"}, h("div", {},
      h("p", {text: "No AI model is set up yet, so the system cannot answer."}),
      h("p", {}, h("a", {class: "button", href: "#setup", text: "Set up your model first"})))));
  }
  examples.replaceChildren();
  if (data.exists && data.examples.length) {
    examples.append(h("h2", {text: "Try one of these"}),
      h("p", {class: "hint", text: "Taken from the eval cases of your system. A click fills the message box; press Send to run it."}),
      h("div", {class: "examples"}, data.examples.map((text) => h("button", {type: "button", class: "example", text,
        onclick: () => { $("#message").value = text; $("#message").focus(); }}))));
  }
  const conversation = $("#conversation");
  if (!conversation.children.length) conversation.append(emptyConversation());
}
