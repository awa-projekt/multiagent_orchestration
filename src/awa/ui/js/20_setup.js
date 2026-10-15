/* A settings form field, drawn from the metadata the server sends (name, label, description, kind
   secret | link | text). A saved value is never shown: a field is only "saved" or not. */
let fields = 0;
function fieldRow(f) {
  const id = "env-" + f.name + "-" + (fields += 1);
  const secret = f.kind === "secret";
  const advice = {secret: "Hidden while you type and never shown again. ", link: "A secure address, starting with https. ", text: ""}[f.kind];
  return h("div", {class: "field"},
    h("label", {for: id}, f.label,
      f.required ? null : h("span", {class: "hint", text: " (optional)"}),
      f.set ? h("span", {class: "badge ready saved", text: "saved"}) : null),
    h("input", {id, type: secret ? "password" : "text", autocomplete: secret ? "new-password" : "off",
      spellcheck: "false", inputmode: f.kind === "link" ? "url" : null, "data-env": f.name,
      "aria-describedby": id + "-hint",
      placeholder: f.set ? "Saved. Type a new value to replace it." : f.placeholder ? "for example " + f.placeholder : null}),
    h("p", {class: "hint", id: id + "-hint", text: advice + (f.description || "")}));
}

/* The entered values of a form, by variable name (empty fields mean "leave as it is"). */
function enteredValues(root) {
  const values = {};
  for (const input of root.querySelectorAll("input[data-env]")) {
    if (input.value.trim() && !(input.dataset.env in values)) values[input.dataset.env] = input.value;
  }
  return values;
}

function clearEntered(root) {
  for (const input of root.querySelectorAll("input[data-env]")) input.value = "";
}

const CATEGORIES = {crm: "Customers (CRM)", code: "Code", data: "Data and databases", files: "Files", web: "Web",
  generic: "Any other system", payments: "Payments", ticketing: "Tickets and issues", communication: "Messages and email",
  collaboration: "Documents and collaboration"};

function statusBadge(configured) {
  return h("span", {class: "badge " + (configured ? "ready" : "missing"),
    text: configured ? "Ready" : "Not set up"});
}

async function loadSetup() {
  const status = $("#setup-status");
  status.replaceChildren();
  let data;
  try { data = await api("/api/setup"); } catch (e) { status.append(fail(e)); return; }
  const providers = $("#providers");
  providers.replaceChildren();
  markDone("setup", data.providers.some((p) => p.configured));
  const ordered = [...data.providers].sort((a, b) => b.configured - a.configured);
  for (const p of ordered) {
    const partial = !p.configured && p.fields.some((f) => f.set);
    providers.append(h("details", {class: "card provider", open: partial || null},
      h("summary", {}, h("strong", {text: p.title}), " ", statusBadge(p.configured)),
      p.fields.map(fieldRow)));
  }
  $("#model-state").textContent = data.default_model_set
    ? "A model is set. Type a new one to change it."
    : "Not set: awa uses its default model.";
  const needed = $("#project-settings");
  needed.replaceChildren();
  if (data.project.length && data.connect_steps) {
    const open = data.project.filter((item) => !item.set).length;
    needed.append(h("h2", {text: "Settings this project needs"}),
      h("div", {class: "card"},
        h("p", {text: data.connect_steps + (data.connect_steps === 1 ? " system" : " systems") + " of this project " +
          (data.connect_steps === 1 ? "has" : "have") + " settings of their own (" + open + " still to enter)."}),
        h("a", {class: "button", href: "#connect", text: "Go to Connect"})));
  } else if (data.project.length) {
    needed.append(h("h2", {text: "Settings this project needs"}),
      h("p", {class: "hint", text: "The systems your process uses. Until they are set, the system works on a stand-in."}),
      h("div", {class: "card"}, data.project.map((item) => item.set
        ? h("p", {class: "row spread"}, h("code", {text: item.name}), statusBadge(true))
        : fieldRow({name: item.name, label: item.name, kind: "secret", required: true, set: false, description: ""}))));
  }
  const connectors = $("#connectors");
  connectors.replaceChildren();
  if (!data.connectors.length) connectors.append(h("p", {class: "hint", text: "No connectors are available in this installation."}));
  const groups = {};
  for (const c of data.connectors) (groups[c.category] = groups[c.category] || []).push(c);
  for (const [category, members] of Object.entries(groups).sort()) {
    const list = h("div", {class: "grid"});
    connectors.append(h("h3", {class: "category", text: CATEGORIES[category] || category[0].toUpperCase() + category.slice(1)}), list);
    for (const c of members.sort((a, b) => b.configured - a.configured)) {
      const body = c.configured
        ? h("p", {class: "hint", text: "Connected. Tools: " + (c.tools.join(", ") || "none")})
        : h("details", {}, h("summary", {text: "Enter the details"}),
            c.setup ? h("p", {class: "hint", text: c.setup}) : null,
            docLink(c.docs, "Documentation of " + c.title) ? h("p", {class: "hint"}, docLink(c.docs, "Documentation of " + c.title)) : null,
            c.fields.map(fieldRow),
            c.optional.length ? h("details", {}, h("summary", {text: "More settings (optional)"}), c.optional.map(fieldRow)) : null);
      list.append(h("div", {class: "card"},
        h("div", {class: "row spread"}, h("h3", {text: c.title || c.name}), statusBadge(c.configured)),
        body));
    }
  }
  if (data.env_names.length) {
    status.append(note("ok", "Saved in .env: " + data.env_names.join(", ")));
  }
}

async function saveSetup() {
  const values = enteredValues($("#setup"));
  const status = $("#setup-status");
  if (!Object.keys(values).length) { location.hash = "#data"; return; }
  try {
    const result = await api("/api/setup", {values});
    clearEntered($("#setup"));
    toast("Saved: " + result.written.join(", "));
    location.hash = "#data";
  } catch (e) { status.replaceChildren(fail(e)); }
}
