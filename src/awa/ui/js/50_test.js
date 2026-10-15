/* Every eval case as a card: what it sends (and so which files it uses), what it runs against,
   and its last result. A changed case is saved in evals/cases.yaml and can be run alone. */
let caseCards = {};

/* A check of a case in plain words. */
function plainCheck(spec) {
  const [kind, ...rest] = spec.split(":");
  const value = rest.join(":");
  const words = {contains: "The answer mentions " + value, tool: "The agents use " + value,
    no_tool: "The agents do not use " + value, approval_requested: "A person is asked before " + value,
    no_approval: "Nobody is asked before " + value, judge: "A reviewer model checks: " + value,
    regex: "The answer contains a value of the expected form"};
  return words[kind] || spec;
}

function caseResult(data) {
  const label = {passed: "Passed", failed: "Failed", error: "Could not run", skipped: "Skipped"}[data.status];
  return h("div", {class: "result"},
    h("p", {}, h("span", {class: "badge " + data.status, text: label})),
    data.output ? h("div", {class: "answer"}, h("strong", {text: "Answer:"}), markdown(data.output)) : "",
    data.failed.length ? h("ul", {}, data.failed.map((f) => h("li", {}, h("code", {text: f.check}), " " + f.reason))) : "",
    data.error ? fail({message: data.error, fix: "Check the connection steps on Review, or the settings on Set up."}) : "");
}

/* The result of one case on its card (or a new card for a case the page did not list). */
function showCase(data) {
  const card = caseCards[data.id];
  if (!card) {
    $("#test-results").append(h("div", {class: "card"}, h("h3", {text: data.id}), caseResult(data)));
    return;
  }
  card.querySelector(".case-result").replaceChildren(caseResult(data));
  card.classList.remove("pass", "fail");
  card.classList.add(data.status === "passed" ? "pass" : "fail");
}

/* Events of a test run; a single case (`one`: its card's status line) never touches the run's
   summary or the Test step. */
function onTest(event, data, one = null) {
  if (one) {
    if (event === "case") showCase(data);
    if (event === "error") one.replaceChildren(fail(data));
    return;
  }
  const summary = $("#test-summary");
  if (event === "started") {
    summary.replaceChildren(h("p", {text: "Running " + data.total + " eval case(s)…"}));
    summary.dataset.total = data.total;
    summary.dataset.done = 0;
  }
  if (event === "case") {
    summary.dataset.done = Number(summary.dataset.done) + 1;
    summary.replaceChildren(h("p", {text: summary.dataset.done + " of " + summary.dataset.total + " done…"}));
    showCase(data);
  }
  if (event === "done") {
    const meter = h("div", {class: "meter", role: "img", "aria-label": data.passed + " of " + data.total + " passed"}, h("span"));
    meter.firstChild.style.width = (data.total ? 100 * data.passed / data.total : 0) + "%";
    markDone("test", data.total && data.passed === data.total);
    if (data.total && data.passed === data.total) meter.classList.add("ok");
    summary.replaceChildren(
      note(data.passed === data.total ? "ok" : "warning", data.passed + " of " + data.total + " eval cases passed."), meter,
      h("div", {class: "actions"}, h("a", {class: "button", href: "#run", text: "Next: try it yourself"})));
  }
  if (event === "error") summary.replaceChildren(fail(data));
}

async function loadTest() {
  const state = await api("/api/state").catch(() => ({}));
  await loadCases();
  if (state.jobs && "test" in state.jobs) runTests(stream("/api/jobs/test", undefined, onTest));
}

async function loadCases() {
  const box = $("#test-results");
  let data;
  try { data = await api("/api/cases"); } catch (e) { box.replaceChildren(fail(e)); return; }
  caseCards = {};
  $("#run-tests").hidden = !data.exists;
  if (!data.exists) {
    box.replaceChildren(emptyState("flask", "No eval cases yet", "Create the system on Describe with testing switched on.",
      h("a", {class: "button primary", href: "#describe", text: "Describe your process"})));
    return;
  }
  box.replaceChildren(...data.cases.map((c, index) => caseCard(c, index, data)));
}

function caseCard(c, index, data) {
  const id = "case-" + index;
  const text = h("textarea", {id: id + "-input", rows: "3", maxlength: "20000"});
  text.value = c.input;
  const used = h("div", {class: "chips", id: id + "-files", "aria-live": "polite"});
  const showFiles = () => {
    const lowered = text.value.toLowerCase();
    const names = data.files.filter((f) => lowered.includes(f.toLowerCase()) || lowered.includes(f.split("/").pop().toLowerCase()));
    used.replaceChildren(...(names.length ? names.map((f) => h("span", {class: "chip", text: f})) : [h("span", {class: "hint", text: "none of your files"})]));
  };
  text.addEventListener("input", showFiles);
  showFiles();
  const insert = data.files.length ? h("div", {class: "row"},
    h("label", {for: id + "-add", class: "hint", text: "Add a file to the request:"}),
    h("select", {id: id + "-add", onchange: (e) => {
      const name = e.currentTarget.value;
      if (name) { text.value = text.value.trimEnd() + " " + name; showFiles(); e.currentTarget.value = ""; }
    }}, h("option", {value: "", text: "Choose a file…"}), data.files.map((f) => h("option", {value: f, text: f})))) : null;
  const systems = data.systems.map((s) => h("fieldset", {class: "mode"}, h("legend", {text: s.name + ": run this case against"}),
    ["live", "stand-in"].map((mode) => h("label", {class: "check"},
      h("input", {type: "radio", name: id + "-" + s.key, value: mode, "data-system": s.key, checked: c.systems[s.key] === mode || null}),
      mode === "live" ? "the live system" : "its stand-in (sample records)"))));
  const state = h("div", {role: "status", "aria-live": "polite"});
  const save = async () => {
    const chosen = {};
    for (const input of card.querySelectorAll("input[data-system]:checked")) chosen[input.dataset.system] = input.value;
    await api("/api/cases/" + encodeURIComponent(c.id), {input: text.value, systems: chosen});
    state.replaceChildren(note("ok", "Saved in evals/cases.yaml. The expected results did not change: check them if the case now uses other files."));
  };
  const card = h("article", {class: "card case-edit", "aria-labelledby": id + "-title"},
    h("h3", {id: id + "-title", text: "Eval case " + (index + 1) + ": " + c.id.replace(/-/g, " ")}),
    h("label", {for: id + "-input", text: "What the case sends"}), text,
    h("p", {class: "hint"}, "Files this case uses: "), used, insert,
    systems,
    h("details", {}, h("summary", {text: "What is checked"}), h("ul", {}, c.checks.map((check) => h("li", {text: plainCheck(check)})))),
    h("div", {class: "actions"},
      h("button", {type: "button", text: "Save changes", onclick: (e) => busy(e.currentTarget, () => save().catch((err) => state.replaceChildren(fail(err))))}),
      h("button", {type: "button", text: "Save and run this case", onclick: (e) => busy(e.currentTarget, async () => {
        try { await save(); await stream("/api/test/case", {id: c.id}, (event, data) => onTest(event, data, state)); } catch (err) { state.replaceChildren(fail(err)); }
      })})),
    state, h("div", {class: "case-result"}));
  caseCards[c.id] = card;
  return card;
}

function runTests(promise) {
  return busy($("#run-tests"), () => promise.catch((e) => $("#test-summary").replaceChildren(fail(e))));
}
