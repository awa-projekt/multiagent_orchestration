/* A tool reference as the agent calls the tool: connector:ledger.query is ledger_query. */
function toolName(reference) {
  return reference.startsWith("connector:") ? reference.slice(10).replace(".", "_") : reference.replace(/^(builtin|tool):/, "");
}

function issueList(issues) {
  if (!issues.length) return note("ok", "awa's structural checks found no problems (names, tools, approvals, variables). They do not test answer quality: try it and run the eval cases.");
  return h("ul", {class: "log"}, issues.map((i) => h("li", {},
    h("span", {class: "badge " + i.severity, text: i.severity === "error" ? "Must fix" : "Note"}), " ",
    h("code", {text: i.path}), " ", i.message)));
}

async function loadReview() {
  const body = $("#review-body");
  body.replaceChildren(skeleton(3));
  let data;
  try { data = await api("/api/project"); } catch (e) { body.replaceChildren(fail(e)); return; }
  if (!data.exists) {
    body.replaceChildren(emptyState("flow", "No system yet", "Describe your process and awa designs it for you.",
      h("a", {class: "button primary", href: "#describe", text: "Describe your process"})));
    return;
  }
  $("#review-lead").textContent = data.name + ": " + (data.description || "What awa designed for your process.");
  const figure = h("figure", {class: "card diagram"});
  figure.innerHTML = data.diagram;  // server-rendered SVG, every label escaped
  figure.append(h("figcaption", {class: "hint", text: "Arrows show who hands work to whom; dashed lines are agents that talk to each other."}));
  const agents = h("div", {class: "grid"}, data.agents.map((a) => h("div", {class: "card"},
    h("h3", {text: a.name}), h("p", {class: "hint", text: a.description}),
    h("div", {class: "chips"}, a.tools.map((t) => h("span", {class: "chip" + (a.approvals.includes(t) ? " gate" : ""),
      text: toolName(t) + (a.approvals.includes(t) ? " (asks you first)" : "")}))))));
  const standIns = data.data_map.sources.filter((s) => s.state === "stand-in");
  const connections = h("div", {},
    h("p", {class: "hint", text: "The data stays in your project folder and your external systems; the parts an agent reads are sent to your AI model provider."}),
    dataMapView(data.data_map, {connect: true}),
    data.connectors.filter((c) => !data.steps.some((step) => step.connector === c.name)).map((c) => h("div", {class: "card row spread"}, h("span", {},
      h("strong", {text: c.title || c.name}), c.missing && c.missing.length ? h("span", {class: "hint", text: " needs " + c.missing.join(", ")}) : ""),
      statusBadge(c.configured))),
    data.connect ? h("details", {class: "card"}, h("summary", {text: "Step-by-step instructions (CONNECT.md)"}), markdown(data.connect)) : "");
  const cases = data.cases.length ? h("div", {class: "scroll"}, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {text: "Case"}), h("th", {text: "Input"}), h("th", {text: "Expected"}))),
    h("tbody", {}, data.cases.map((c) => h("tr", {}, h("td", {text: c.id}), h("td", {text: c.input}), h("td", {text: c.expected || c.checks.join(", ")}))))))
    : h("p", {class: "hint", text: "No eval cases yet. Create the system with testing switched on to get them."});
  const yaml = h("textarea", {id: "yaml", class: "code", rows: "18", spellcheck: "false"});
  yaml.value = data.yaml;
  const checks = h("div", {id: "checks", role: "status", "aria-live": "polite"}, issueList(data.issues));
  const submit = (save) => async (event) => {
    const button = event.currentTarget;
    await busy(button, async () => {
      try {
        const result = await api("/api/project", {yaml: yaml.value, save});
        checks.replaceChildren(issueList(result.issues));
        if (save) checks.prepend(result.saved ? note("ok", "Saved.") : note("warning", "Not saved: fix the problems marked Must fix first."));
      } catch (e) { checks.replaceChildren(fail(e)); }
    });
  };
  body.replaceChildren(
    h("h2", {text: "How the work flows"}), figure,
    h("h2", {text: "Agents and their tools"}), agents,
    h("h2", {text: "Where the data comes from"}), connections,
    h("h2", {text: "Eval cases"}), cases,
    h("h2", {text: "Checks"}), checks,
    h("details", {class: "card"}, h("summary", {}, icon("pencil"), "Edit the design (for advanced users)"),
      h("label", {for: "yaml", text: "Blueprint (YAML)"}), yaml,
      h("div", {class: "actions"}, h("button", {type: "button", onclick: submit(false), text: "Check"}),
        h("button", {type: "button", onclick: submit(true), text: "Check and save"}))),
    h("div", {class: "actions"}, h("a", {class: "button primary", href: standIns.length ? "#connect" : "#test",
      text: standIns.length ? "Continue to Connect" : "Continue to Test"})));
}
