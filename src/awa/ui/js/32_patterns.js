/* Suggest how the agents could work together, test the proposals on the same cases, and use one. */
const patterns = {gate: false};

function patternCard(p) {
  const diagram = h("div", {class: "diagram"});
  diagram.innerHTML = p.diagram;
  return h("div", {class: "card pattern", "data-pattern": p.id},
    h("h3", {text: p.title}),
    h("p", {text: p.why}),
    diagram,
    h("p", {class: "hint"}, h("strong", {text: "Fits when: "}), p.when_to_use),
    h("p", {class: "hint"}, h("strong", {text: "Roles: "}), p.agents),
    h("p", {class: "hint"}, h("strong", {text: "Watch out: "}), p.risks || p.cost),
    p.approval_gate ? h("div", {class: "chips"}, h("span", {class: "chip gate", text: "approval gate"})) : null,
    h("label", {class: "check"}, h("input", {type: "checkbox", value: p.id, checked: true, "data-test-pattern": ""}), " Test this one"));
}

function patternControls() {
  return h("div", {class: "card"},
    h("h3", {text: "Test the proposals"}),
    h("p", {class: "hint", text: "awa drafts a system for each ticked pattern, runs the same eval cases through each and shows the results side by side. Nothing is saved until you use one."}),
    h("label", {class: "check"}, h("input", {id: "patterns-allow", type: "checkbox"}), " Use my model for this test (costs tokens)"),
    h("label", {for: "patterns-cap", text: "Most model calls for the whole test"}),
    h("input", {id: "patterns-cap", type: "number", min: "1", max: "200", value: "60"}),
    h("div", {class: "actions"}, h("button", {id: "test-patterns", class: "primary", type: "button", onclick: (e) => busy(e.currentTarget, testPatterns)}, icon("flask"), "Test the proposals")),
    h("div", {id: "patterns-test-result", role: "status", "aria-live": "polite"}));
}

function onSuggest(event, data) {
  const box = $("#patterns-result");
  if (event === "error") { box.replaceChildren(fail(data)); return; }
  if (event !== "done") return;
  patterns.gate = data.proposals.some((p) => p.approval_gate);
  box.replaceChildren(
    note("ok", data.ranked_by === "model" ? "Ranked by your model." : "Ranked from the words of your description."),
    h("div", {class: "cards"}, data.proposals.map(patternCard)),
    patternControls());
}

async function suggestPatterns() {
  const process = $("#process").value.trim();
  if (!process) { $("#patterns-result").replaceChildren(fail({message: "The description is empty.", fix: "Describe your process in a few sentences first."})); return; }
  $("#patterns-result").replaceChildren(skeleton(3));
  try { await stream("/api/patterns/suggest", {process, allow_model: $("#patterns-model").checked}, onSuggest); }
  catch (e) { $("#patterns-result").replaceChildren(fail(e)); }
}

function drawTrials(data) {
  const label = {scored: "tested", failed: "no valid system", stopped: "stopped"};
  const rows = data.trials.map((t) => h("tr", {},
    h("td", {text: t.title}),
    h("td", {}, h("span", {class: "badge " + (t.status === "scored" ? "ready" : t.status === "failed" ? "failed" : "skipped"),
      text: t.status === "scored" ? t.passed + " of " + t.total + " cases" : label[t.status]})),
    h("td", {text: String(t.requests)}),
    h("td", {text: String(t.tokens)}),
    h("td", {text: t.verdict + (t.pattern_id === data.best ? " Best result of this test." : "")}),
    h("td", {}, t.status === "scored" ? h("button", {type: "button", class: "small", "data-use": t.pattern_id,
      onclick: (e) => busy(e.currentTarget, () => usePattern(t.pattern_id))}, icon("check"), "Use this pattern") : "")));
  return h("div", {},
    note(data.capped ? "warning" : "ok", data.capped
      ? "The cap of " + data.cap + " model calls was reached; the patterns marked stopped were not finished."
      : data.requests + " of " + data.cap + " model calls used."),
    h("div", {class: "scroll"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Pattern", "Result", "Model calls", "Tokens", "What happened", ""].map((c) => h("th", {scope: "col", text: c})))),
      h("tbody", {}, rows))),
    h("p", {class: "hint", text: "The result compares this one run on " + data.cases.length + " cases; it is not a general ranking."}),
    h("div", {id: "patterns-use-result", role: "status"}));
}

async function testPatterns() {
  const ids = [...document.querySelectorAll("[data-test-pattern]:checked")].map((i) => i.value);
  const out = $("#patterns-test-result");
  if (!ids.length) { out.replaceChildren(fail({message: "No pattern is ticked.", fix: "Tick at least one pattern to test."})); return; }
  out.replaceChildren(h("div", {id: "patterns-progress", class: "alert", role: "status"}, h("p", {text: "Designing the eval cases…"})));
  const body = {process: $("#process").value.trim(), ids, approval_gate: patterns.gate,
    allow_model: $("#patterns-allow").checked, max_requests: Number($("#patterns-cap").value) || 60};
  try {
    await stream("/api/patterns/test", body, (event, data) => {
      if (event === "pattern.started") $("#patterns-progress p").textContent = "Trying " + data.title + "…";
      if (event === "done") out.replaceChildren(drawTrials(data));
      if (event === "error") out.replaceChildren(fail(data));
    });
  } catch (e) { out.replaceChildren(fail(e)); }
}

async function usePattern(id) {
  const out = $("#patterns-use-result");
  try {
    const data = await api("/api/patterns/use", {id});
    toast("Saved " + data.name + " as your system.");
    out.replaceChildren(note("ok", "Saved " + data.name + " as your system."), h("a", {class: "button primary", href: "#review", text: "Review the result"}));
  } catch (e) { out.replaceChildren(fail(e)); }
}

$("#suggest-patterns").addEventListener("click", (e) => busy(e.currentTarget, suggestPatterns));
