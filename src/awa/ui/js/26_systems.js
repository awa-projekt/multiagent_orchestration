/* The second part of "Your data": databases and APIs the process reads. The kinds come from the
   connector catalog, the fields from the connector's settings; a saved value is never shown. */
let systemCount = 0;
let systemKinds = null;  // {types, databases} from the server
let systemFormFor = "";  // "kind|name" the fields were drawn for

async function loadSystems() {
  const box = $("#system-cards");
  let state;
  try { state = await api("/api/systems"); } catch (e) { box.replaceChildren(fail(e)); return; }
  renderSystems(state);
}

function renderSystems(state) {
  systemCount = state.systems.length;
  if (!systemKinds) {
    systemKinds = {types: state.types, databases: state.databases};
    const select = $("#system-type");
    const groups = {};
    for (const t of state.types) (groups[t.group] = groups[t.group] || []).push(t);
    select.replaceChildren(h("option", {value: "", text: "Choose…"}),
      ...Object.entries(groups).map(([group, members]) => h("optgroup", {label: group},
        members.map((t) => h("option", {value: t.connector, text: t.title})))));
    $("#system-database").replaceChildren(...state.databases.map((d) => h("option", {value: d.id, text: d.title})));
  }
  const box = $("#system-cards");
  box.replaceChildren(...(state.systems.length ? state.systems.map(declaredCard)
    : [emptyState("database", "No systems yet", "Add one below if your process reads a database or an API.")]));
  $("#data-next").textContent = (document.querySelectorAll("#data-cards .file").length || systemCount)
    ? "Continue to Describe" : "Skip: describe your process without files or systems";
}

function declaredCard(sys) {
  const title = "declared-" + sys.key + "-title";
  const result = h("div", {class: "result", role: "status", "aria-live": "polite"});
  const tables = sys.tables.map((t) => h("details", {class: "table-info"},
    h("summary", {}, (t.kind === "view" ? "View " : "Table ") + t.name + (t.rows == null ? "" : ": about " + t.rows.toLocaleString() + " rows")),
    h("p", {class: "hint", text: "Columns: " + t.columns.join(", ")}),
    t.sample ? h("div", {class: "scroll"}, h("table", {class: "sample"},
      h("caption", {class: "hint", text: "Example rows (you allowed them; the design model sees them masked when you chose that above)"}),
      h("thead", {}, h("tr", {}, t.sample.columns.map((c) => h("th", {scope: "col", text: c})))),
      h("tbody", {}, t.sample.rows.map((r) => h("tr", {}, r.map((v) => h("td", {text: v}))))))) : null));
  const operations = sys.operations.length ? h("details", {class: "table-info"},
    h("summary", {}, sys.operations.length + " operation(s) that read data"),
    h("ul", {}, sys.operations.map((op) => h("li", {}, h("code", {text: op.name}), op.path ? " (" + op.method + " " + op.path + ")" : "", op.summary ? ": " + op.summary : "")))) : null;
  const refresh = h("button", {type: "button", class: "small", text: "Read structure again"});
  refresh.addEventListener("click", (e) => busy(e.currentTarget, async () => {
    result.replaceChildren();
    try { await api("/api/systems/" + encodeURIComponent(sys.key) + "/refresh", {}); await loadSystems(); }
    catch (err) { result.append(fail(err)); }
  }));
  const remove = h("button", {type: "button", class: "small ghost"}, icon("trash"), "Remove");
  const card = h("article", {class: "card system", "aria-labelledby": title},
    h("div", {class: "row spread"}, h("h3", {id: title}, sys.name, " ", h("span", {class: "hint", text: "(" + sys.kind + ")"})),
      h("span", {class: "state live", text: "Live, read-only"})),
    h("p", {class: "hint", text: "The design reads it " + (sys.connector === "sql" ? "through the SQL connector with read-only queries" : "through its connector") +
      "; the test runs read it too. Tools: " + sys.tools.join(", ")}),
    sys.tested ? h("p", {class: "hint", text: "Last test: " + sys.tested}) : null,
    sys.warnings.map((w) => note("warning", w)),
    tables, operations,
    sys.left_out ? h("p", {class: "hint", text: sys.left_out + " operation(s) that change data are left out: awa only reads this system."}) : null,
    h("p", {class: "hint"}, "Settings in .env: ", sys.settings.map((s) => h("span", {class: "chip", text: s.label + (s.set ? " (saved)" : " (missing)")}))),
    h("div", {class: "actions"}, refresh, remove), result);
  remove.addEventListener("click", () => removeWithUndo(card, sys.name, async () => {
    try { renderSystems(await api("/api/systems/" + encodeURIComponent(sys.key) + "/remove", {})); }
    catch (err) { toast(err.message || "Could not remove " + sys.name, {error: true}); }
  }));
  return card;
}

async function drawSystemForm() {
  const kind = $("#system-type").value, name = $("#system-name").value.trim();
  $("#system-database-row").hidden = kind !== "sql";
  const fields = $("#system-fields");
  if (!kind || !name) {
    fields.replaceChildren(h("p", {class: "hint", text: kind ? "Give it a name, then its settings appear here." : "Choose the kind of system first."}));
    $("#system-setup").textContent = "";
    $("#system-help").hidden = true;
    systemFormFor = "";
    return;
  }
  if (systemFormFor === kind + "|" + name) return;
  const typed = enteredValues(fields);
  let form;
  try { form = await api("/api/systems/form", {connector: kind, name}); }
  catch (e) { fields.replaceChildren(fail(e)); return; }
  systemFormFor = kind + "|" + name;
  $("#system-setup").textContent = form.setup || "";
  $("#system-help").hidden = !form.setup;
  showSystemFields(form.fields, typed);
}

function showSystemFields(list, typed) {
  const fields = $("#system-fields");
  const required = list.filter((f) => f.required), optional = list.filter((f) => !f.required);
  fields.replaceChildren(...required.map(fieldRow),
    optional.length ? h("details", {}, h("summary", {text: "More settings (optional)"}), optional.map(fieldRow)) : "");
  for (const input of fields.querySelectorAll("input[data-env]")) {
    if (typed[input.dataset.env]) input.value = typed[input.dataset.env];
  }
  databaseExample();
}

/* The example of the chosen database goes into the address field's placeholder. */
function databaseExample() {
  if ($("#system-type").value !== "sql" || !systemKinds) return;
  const chosen = systemKinds.databases.find((d) => d.id === $("#system-database").value);
  const input = $("#system-fields input[data-env$='_DATABASE_URL']");
  if (chosen && input && !input.placeholder.startsWith("Saved")) input.placeholder = "for example " + chosen.example;
}

async function systemAction(save) {
  const result = $("#system-result");
  result.replaceChildren();
  const kind = $("#system-type").value, name = $("#system-name").value.trim();
  if (!kind || !name) {
    result.append(fail({message: "Choose the kind of system and give it a name first.", fix: "For example: SQL database, Invoice ledger."}));
    (kind ? $("#system-name") : $("#system-type")).focus();
    return;
  }
  await drawSystemForm();
  const values = enteredValues($("#system-fields"));
  try {
    const answer = await api(save ? "/api/systems" : "/api/systems/test",
      {connector: kind, name, values, samples: $("#system-samples").checked});
    const nodes = [note(answer.ok ? "ok" : "error", (answer.ok ? "" : "The connection test failed: ") + answer.message),
      h("p", {class: "hint", text: answer.advice}), ...answer.warnings.map((w) => note("warning", w))];
    if (answer.fields.length > $("#system-fields").querySelectorAll("input[data-env]").length) {
      nodes.push(note("warning", "The API description asks for more settings; they are shown above."));
      showSystemFields(answer.fields, values);
    }
    if (save && answer.system) {
      clearEntered($("#system-fields"));
      nodes.unshift(note("ok", name + " is saved and its structure was read" +
        (answer.saved.length ? " (settings saved in .env: " + answer.saved.length + ")" : "") + ". The design will use it live, read-only."));
      $("#system-name").value = "";
      systemFormFor = "";
      await loadSystems();
    } else if (save) {
      nodes.push(h("p", {class: "hint", text: "Nothing was saved."}));
    }
    result.append(...nodes);
  } catch (e) { result.append(fail(e)); }
}

function startSystems() {
  $("#system-type").addEventListener("change", drawSystemForm);
  $("#system-name").addEventListener("change", drawSystemForm);
  $("#system-database").addEventListener("change", databaseExample);
  $("#test-system").addEventListener("click", (e) => busy(e.currentTarget, () => systemAction(false)));
  $("#save-system").addEventListener("click", (e) => busy(e.currentTarget, () => systemAction(true)));
}
