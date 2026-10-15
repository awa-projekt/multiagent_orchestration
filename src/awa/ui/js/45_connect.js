const SYSTEM_STATUS = {
  connected: ["Live", "ready"],
  attention: ["Needs attention", "warning"],
  not_set_up: ["Stand-in", "missing"],
};
let connectResult = null;  // {key, nodes}: what the last Save said, shown again after the cards are redrawn

async function loadConnect() {
  const body = $("#connect-body");
  body.replaceChildren(skeleton(3));
  let data;
  try { data = await api("/api/connections"); } catch (e) { body.replaceChildren(fail(e)); return; }
  if (!data.exists) {
    body.replaceChildren(emptyState("plug", "Nothing to connect yet", "There is no system in this folder yet.",
      h("a", {class: "button primary", href: "#describe", text: "Describe your process"})));
    return;
  }
  if (!data.systems.length) {
    markDone("connect", true);
    body.replaceChildren(note("ok", "This system uses no outside systems, so there is nothing to connect."),
      h("div", {class: "actions"}, h("a", {class: "button primary", href: "#test", text: "Continue to Test"})));
    return;
  }
  const c = data.counts;
  markDone("connect", c.connected === data.systems.length);
  const map = await api("/api/datamap").catch(() => null);
  body.replaceChildren(
    h("p", {id: "connect-summary", role: "status"}, h("strong", {text: c.connected + " of " + data.systems.length + " live"}),
      " · " + c.attention + " need attention · " + c.not_set_up + " on a stand-in"),
    ...data.systems.map(systemCard),
    map && map.exists ? h("details", {class: "card"}, h("summary", {text: "Where the data comes from (all agents)"}), dataMapView(map)) : "",
    h("div", {class: "actions"}, h("a", {class: "button primary", href: "#test", text: "Continue to Test"})));
}

function systemCard(sys) {
  const [label, cls] = SYSTEM_STATUS[sys.status];
  const title = "system-" + sys.key + "-title";
  const result = h("div", {class: "result", role: "status", "aria-live": "polite"});
  if (connectResult && connectResult.key === sys.key) result.append(...connectResult.nodes);
  const about = sys.setup || sys.notes.length || docLink(sys.docs, "x");
  const form = h("div", {class: "connect-form"},
    sys.fields.map(fieldRow),
    sys.optional.length || about ? h("details", {}, h("summary", {text: "More settings and help"}),
      sys.setup ? h("p", {class: "hint", text: sys.setup}) : null,
      docLink(sys.docs, "Documentation of " + sys.connector_title) ? h("p", {class: "hint"}, docLink(sys.docs, "Documentation of " + sys.connector_title)) : null,
      sys.notes.map((text) => h("p", {class: "hint", text})),
      sys.optional.map(fieldRow)) : null,
    h("div", {class: "actions"},
      h("button", {type: "button", class: "test-connection", text: "Test connection",
        onclick: (e) => busy(e.currentTarget, () => connectAction(sys, form, result, false))}),
      h("button", {type: "button", class: "primary save-connection", text: sys.mode === "real" ? "Save changes" : "Save and connect",
        onclick: (e) => busy(e.currentTarget, () => connectAction(sys, form, result, true))})));
  let chooser = null;
  if (sys.sample) {
    const real = sys.mode === "real" || sys.status !== "not_set_up";
    const back = h("button", {type: "button", class: "small", text: "Go back to the stand-in",
      onclick: (e) => busy(e.currentTarget, async () => {
        result.replaceChildren();
        try { await api("/api/connections/" + encodeURIComponent(sys.key) + "/stand-in", {}); connectResult = {key: sys.key, nodes: [note("ok", sys.system + " works on its stand-in again; the settings stay in .env.")]}; await loadConnect(); }
        catch (err) { result.append(fail(err)); }
      })});
    const pick = (value, text, checked, disabled) => h("label", {class: "check"},
      h("input", {type: "radio", name: "mode-" + sys.key, value, checked: checked || null, disabled: disabled || null,
        onchange: () => { form.hidden = value === "sample"; }}), text);
    chooser = h("fieldset", {class: "mode"}, h("legend", {text: "Where should awa get this data?"}),
      pick("sample", "Use the stand-in (sample records awa wrote; nothing to enter, good for trying things out)", !real, sys.mode === "real"),
      pick("real", "Connect the live system", real, false),
      sys.mode === "real" ? h("p", {class: "hint"}, "This system is live. ", back) : null);
    form.hidden = !real;
  }
  return h("section", {class: "card system", id: "system-" + sys.key, "aria-labelledby": title},
    h("div", {class: "row spread"},
      h("h3", {id: title}, sys.system, " ", sys.connector_title === sys.system ? null : h("span", {class: "hint", text: "(" + sys.connector_title + ")"})),
      h("span", {class: "badge " + cls, text: label})),
    h("p", {class: "status-text", text: sys.status_text}),
    sys.summary ? h("p", {class: "hint", text: sys.summary}) : null,
    chooser, form, result);
}

async function connectAction(sys, root, result, save) {
  result.replaceChildren();
  try {
    const answer = await api("/api/connections/" + encodeURIComponent(sys.key) + (save ? "" : "/test"), {values: enteredValues(root)});
    const test = save ? answer.test : answer;
    const nodes = [note(test.ok ? "ok" : "error", (test.ok ? "" : "The connection test failed: ") + test.message),
      h("p", {class: "hint", text: test.advice})];
    if (!save) { result.append(...nodes); return; }
    nodes.unshift(note(answer.connected ? "ok" : "warning", answer.saved.length
      ? "Saved: " + answer.saved.join(", ") + (answer.connected ? ". awa now uses the live system." : ". Not connected yet.")
      : answer.connected ? "awa now uses the live system." : "Nothing new to save."));
    if (answer.removed.length) {
      nodes.push(note("warning", "These tools of the stand-in have no counterpart in the live system and were removed: " +
        answer.removed.join(", ") + ". Choose the operation that changes this system in blueprint.yaml (Review, Edit the design)."));
    }
    connectResult = {key: sys.key, nodes};
    await loadConnect();
  } catch (e) { result.append(fail(e)); }
}
