const HEALTH = {running: ["Running", "ready"], stopped: ["Not running", "warning"], ondemand: ["On demand", "warning"]};
let sampleResult = null;  // {trigger, nodes}: what the last sample run said, shown again after a redraw

async function loadDeploy() {
  const body = $("#deploy-body");
  let data, runs;
  try { [data, runs] = await Promise.all([api("/api/deploy"), api("/api/runs")]); } catch (e) { body.replaceChildren(fail(e)); return; }
  const pending = data.not_connected.length ? note("warning",
    "Not connected yet: " + data.not_connected.join(", ") + ". Until then the system works on a stand-in " +
    "(sample records awa wrote from your description). Connect them on the Connect step before real use.") : "";
  body.replaceChildren(pending,
    h("p", {class: "hint"}, "Project: ", h("code", {text: data.folder})),
    healthCard(runs.health),
    h("h2", {text: "Waiting for your OK"}),
    h("div", {id: "use-inbox", "aria-live": "polite"}),
    h("h2", {text: "What starts it"}),
    triggerCards(data.automation),
    h("details", {class: "card", id: "add-trigger"}, h("summary", {text: "Add another trigger"}),
      h("p", {class: "hint"}, "Choose what starts a run by itself. awa writes the trigger into blueprint.yaml."),
      triggerForm(data.trigger_kinds)),
    h("h2", {text: "Recent runs"}),
    runsList(runs),
    h("h2", {text: "Keep it running"}),
    keepRunning(data.keep_running),
    h("details", {class: "card", id: "more-ways"}, h("summary", {text: "More ways to use it (for your IT team)"}),
      h("p", {class: "hint"}, "Open a terminal in the project folder ", h("code", {text: data.folder}), " and run one of these commands."),
      ...data.commands.map(commandCard),
      data.triggers.length ? h("h3", {text: "Everything to start and try the triggers"}) : null,
      ...data.triggers.map(commandCard)));
  await fillInbox($("#use-inbox"), loadDeploy);
}

function healthCard(health) {
  const [label, cls] = HEALTH[health.state];
  return h("section", {class: "card", id: "health", "aria-labelledby": "health-title"},
    h("div", {class: "row spread"}, h("h3", {id: "health-title", text: "Is it running?"}), h("span", {class: "badge " + cls, text: label})),
    h("p", {text: health.text}),
    health.problems ? h("div", {class: "alert error", role: "alert"}, h("div", {}, h("p", {text: health.problems}))) : null);
}

function triggerCards(automation) {
  if (!automation.length) {
    return h("div", {class: "card"}, h("p", {text: "This system has no trigger yet, so it starts only when you ask it (on the Try it step). Add a trigger below to let a schedule, a file, an email or another system start it."}));
  }
  return h("div", {}, automation.map((t) => {
    const result = h("div", {class: "result", role: "status", "aria-live": "polite"});
    if (sampleResult && sampleResult.trigger === t.name) result.append(...sampleResult.nodes);
    const inputs = t.sample.map((f) => {
      const id = "sample-" + t.name + "-" + f.name;
      return h("div", {class: "field"}, h("label", {for: id, text: f.label}),
        h("input", {id, type: "text", autocomplete: "off", spellcheck: "false", "data-field": f.name}));
    });
    const run = h("button", {type: "button", class: "primary run-sample", text: "Run it now with this sample",
      onclick: (e) => busy(e.currentTarget, () => runSample(t, inputs, result))});
    return h("section", {class: "card trigger", "aria-labelledby": "trigger-" + t.name + "-title"},
      h("div", {class: "row spread"}, h("h3", {id: "trigger-" + t.name + "-title", text: t.name}),
        h("span", {class: "badge " + (t.enabled ? "ready" : "warning"), text: t.enabled ? t.kind : t.kind + " (off)"})),
      h("p", {text: t.what}),
      h("details", {}, h("summary", {text: "Run it now with a sample"}),
        h("p", {class: "hint", text: t.sample.length ? "Fill in what such an event would carry. The system runs once, for real, on this computer; anything that needs your OK waits above." : "This trigger needs nothing from you: awa supplies the date. The system runs once, for real, on this computer."}),
        inputs, h("div", {class: "actions"}, run), result),
      h("details", {}, h("summary", {text: "What the system is asked (for experts)"}), h("pre", {text: t.input})));
  }));
}

async function runSample(trigger, inputs, result) {
  result.replaceChildren();
  const values = {};
  for (const row of inputs) { const input = row.querySelector("input"); if (input.value.trim()) values[input.dataset.field] = input.value; }
  try {
    const run = await api("/api/trigger/run", {trigger: trigger.name, values});
    sampleResult = {trigger: trigger.name, nodes: [runNote(run)]};
    await loadDeploy();
  } catch (e) { result.append(fail(e)); }
}

function runNote(run) {
  const kind = run.status === "succeeded" ? "ok" : ["waiting", "rejected"].includes(run.status) ? "warning" : "error";
  const waiting = run.status === "waiting" ? " It waits for your OK in the list above." : "";
  const alert = note(kind, run.status_text + (run.summary ? ":" : "") + waiting);
  if (run.summary) alert.firstChild.append(markdown(run.summary));
  return h("div", {}, alert,
    run.fix && run.status !== "succeeded" ? h("p", {class: "hint", text: run.fix}) : null);
}

function runsList(data) {
  if (!data.runs.length) {
    return emptyState("clock", "No runs yet", "Try “Run it now with a sample” above, or start it with the command under Keep it running.");
  }
  return h("div", {class: "scroll"}, h("table", {class: "runs"},
    h("thead", {}, h("tr", {}, ["Result", "Started", "Started by", "What happened"].map((c) => h("th", {scope: "col", text: c})))),
    h("tbody", {}, data.runs.map((r) => h("tr", {},
      h("td", {}, h("span", {class: "badge " + r.chip, text: r.status_text})),
      h("td", {text: r.created}),
      h("td", {text: r.trigger || "—"}),
      h("td", {}, r.summary ? markdown(r.summary) : r.open_approvals ? "Waiting for your OK" : "—",
        r.fix ? h("span", {class: "hint block", text: r.fix}) : null,
        h("span", {class: "hint block", text: "Run " + r.id + (r.attempts > 1 ? " · " + r.attempts + " attempts" : "")})))))));
}

function keepRunning(panel) {
  return h("section", {class: "card", id: "keep-running", "aria-labelledby": "keep-title"},
    h("h3", {id: "keep-title", text: "To keep it running, start it once and leave it open"}),
    commandCard({title: "Start it", command: panel.command, note: panel.why + ". " + panel.one_each}),
    h("details", {}, h("summary", {text: "Where everything is kept"}),
      h("ul", {}, panel.where.map((w) => h("li", {}, h("code", {text: w.path}), " " + w.text)))),
    h("details", {}, h("summary", {text: "What is kept after a restart"}),
      h("ul", {}, panel.restart.map((line) => h("li", {text: line})))),
    h("details", {}, h("summary", {text: "How to see problems"}),
      ...panel.problems.map((p) => commandCard({title: p.title, command: p.command, note: ""}))));
}

function commandCard(c) {
  const code = h("code", {text: c.command});
  const copy = h("button", {class: "small", type: "button", "aria-label": "Copy the command: " + c.title,
    onclick: async () => {
      try { await navigator.clipboard.writeText(c.command); copy.replaceChildren(tick(true), "Copied"); }
      catch (e) {
        const range = document.createRange(); range.selectNodeContents(code);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        copy.replaceChildren(icon("copy"), "Selected: press Ctrl+C");
      }
    }}, icon("copy"), "Copy");
  return h("div", {class: "card command"}, h("div", {class: "row spread"}, h("h3", {text: c.title}), copy),
    h("pre", {}, code), c.note ? h("p", {class: "hint", text: c.note}) : "");
}

const TRIGGER_FIELDS = {
  webhook: [["secret_env", "Setting that holds the secret (optional)", ""], ["scheme", "Requests prove who sent them by: timestamped or token", "timestamped"]],
  schedule: [["cron", "When (cron, for example 0 7 * * MON-FRI)", "0 7 * * MON-FRI"], ["timezone", "Time zone (optional, for example Europe/Berlin)", ""]],
  "file-drop": [["directory", "Folder to watch", "inbox"], ["pattern", "Which files (for example *.pdf)", "*"]],
  mail: [["host", "IMAP server (for example imap.example.com)", ""], ["folder", "Folder to read", "INBOX"]],
  queue: [["queue", "Queue name (optional)", ""]],
};

function triggerForm(kinds) {
  const kind = h("select", {id: "trigger-kind"}, ...kinds.map((k) => h("option", {value: k, text: k})));
  const name = h("input", {id: "trigger-name", type: "text", autocomplete: "off", spellcheck: "false"});
  const task = h("input", {id: "trigger-input", type: "text", autocomplete: "off"});
  const inputs = h("div", {});
  const result = h("div", {role: "status"});
  const render = () => inputs.replaceChildren(...TRIGGER_FIELDS[kind.value].map(([key, label, value]) => {
    const id = "trigger-" + key;
    return h("div", {}, h("label", {for: id, text: label}), h("input", {id, type: "text", value, autocomplete: "off", spellcheck: "false", "data-setting": key}));
  }));
  kind.addEventListener("change", render);
  render();
  const add = h("button", {class: "primary", type: "button", text: "Add this trigger", onclick: (e) => busy(e.currentTarget, async () => {
    const settings = {input: task.value};
    for (const el of inputs.querySelectorAll("input")) settings[el.dataset.setting] = el.value;
    try { await api("/api/trigger", {kind: kind.value, name: name.value, settings}); await loadDeploy(); }
    catch (e) { result.replaceChildren(fail(e)); }
  })});
  return h("div", {}, h("h3", {text: "Add a trigger"}),
    h("label", {for: "trigger-kind", text: "What starts it"}), kind,
    h("label", {for: "trigger-name"}, "Name ", h("span", {class: "hint", text: "(optional)"})), name,
    inputs,
    h("label", {for: "trigger-input"}, "What the system should do with each one ", h("span", {class: "hint", text: "(optional; {payload.field} fills in the event's values)"})), task,
    h("div", {class: "actions"}, add), result);
}
