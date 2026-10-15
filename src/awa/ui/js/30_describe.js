/* The build in five steps; each event of the build moves them on. */
const STAGES = [
  ["data", "Read your data"], ["tests", "Write eval cases"], ["design", "Design the agents"],
  ["evaluate", "Test and repair"], ["done", "Done"],
];
const build = {stages: {}, cases: {}, rounds: 0};

async function loadDescribe() {
  const state = await api("/api/state").catch(() => ({}));
  markDone("describe", state.has_system);
  markDone("review", state.has_system);
  $("#describe-note").replaceChildren(state.has_system
    ? note("warning", "This folder already has a system. Creating a new one replaces it.") : "");
  if (state.process && !$("#process").value.trim()) $("#process").value = state.process;
  if (state.jobs && "build" in state.jobs && !$("#progress").children.length) {
    resetBuild();
    follow(stream("/api/jobs/build", undefined, onBuild));
  }
  describeFiles();
}

/* The files and systems the design will use, as chips that can be removed here; adding goes to Your data. */
async function describeFiles() {
  const box = $("#describe-files");
  let data, systems;
  try { [data, systems] = await Promise.all([api("/api/data"), api("/api/systems")]); }
  catch (e) { box.replaceChildren(fail(e)); return; }
  const chip = (label, title, remove) => {
    const el = h("span", {class: "chip"}, label,
      h("button", {type: "button", "aria-label": "Remove " + title + " from your data", title: "Remove",
        onclick: () => removeWithUndo(el, title, async () => { try { await remove(); } catch (err) { toast(err.message || "Could not remove " + title, {error: true}); } })}, icon("x")));
    return el;
  };
  const files = data.files.map((f) => chip(f.name, f.name, () => api("/api/data/remove", {name: f.name})));
  const live = systems.systems.map((s) => chip(s.name + " (live)", s.name, () => api("/api/systems/" + encodeURIComponent(s.key) + "/remove", {})));
  const empty = !files.length && !live.length;
  box.replaceChildren(h("div", {class: "card", id: "describe-data"},
    h("h3", {text: "The data the design uses"}),
    empty ? h("p", {class: "hint", text: "No files and no systems: awa writes small sample records (stand-ins) from your description."})
      : h("div", {class: "chips", "aria-label": "Your files and systems"}, files, live),
    h("p", {class: "hint"}, empty ? "" : "Files are copies in your project; systems are read live, read-only. ",
      h("a", {href: "#data", text: empty ? "Add files or systems" : "Add more under Your data"}), ".")));
}

function resetBuild() {
  build.stages = {}; build.cases = {}; build.rounds = 0;
  $("#build-view").hidden = false;
  $("#progress").replaceChildren();
  $("#build-cases").replaceChildren();
  $("#describe-result").replaceChildren();
  drawStages();
}

function setStage(id, status, text) {
  for (const [key] of STAGES) {
    if (key === id) break;
    if (!build.stages[key] || build.stages[key].status === "running") build.stages[key] = {status: "ok", text: (build.stages[key] || {}).text || ""};
  }
  build.stages[id] = {status, text: text || (build.stages[id] || {}).text || ""};
  drawStages();
}

function drawStages() {
  const marks = {ok: "done", running: "in progress", failed: "stopped", waiting: "waiting"};
  $("#stages").replaceChildren(...STAGES.map(([id, label]) => {
    const state = build.stages[id] || {status: "", text: ""};
    return h("li", {class: "step " + state.status, "aria-label": label + ": " + (marks[state.status] || "not started")},
      h("span", {class: "mark", "aria-hidden": "true"}),
      h("span", {}, h("strong", {text: label}), state.text ? h("span", {class: "hint", text: " · " + state.text}) : ""));
  }));
}

function drawCases() {
  const ids = Object.keys(build.cases);
  if (!ids.length) { $("#build-cases").replaceChildren(); return; }
  const label = {passed: "passes", failed: "fails", error: "could not run", flaky: "passed on a second try"};
  $("#build-cases").replaceChildren(h("h3", {text: "Eval cases"}), h("ul", {class: "cases-list"}, ids.map((id) => {
    const status = build.cases[id];
    const cls = status === "passed" || status === "flaky" ? "passed" : status === "pending" ? "skipped" : "failed";
    return h("li", {}, h("span", {text: id.replace(/-/g, " ")}), h("span", {class: "badge " + cls, text: label[status] || "waiting"}));
  })));
}

function onBuild(event, data) {
  if (event === "progress") {
    $("#progress").append(h("li", {text: data.message}));
    const e = data.event || "";
    if (data.stage === "profile" || /^Using your/.test(data.message)) setStage("data", "running", data.message);
    if (e === "data.profile.done") setStage("data", "ok", data.message);
    if (data.stage === "design" && !build.stages.tests) setStage("tests", "running");
    if (e === "design.acceptance") {
      for (const id of data.cases || []) build.cases[id] = "pending";
      setStage("tests", "ok", (data.cases || []).length + " eval cases");
      setStage("design", "running");
      drawCases();
    }
    if (e === "design.architecture") setStage("design", "ok", data.name || "");
    if (e === "eval.round.started") { build.rounds = data.round || build.rounds + 1; setStage("evaluate", "running", "round " + build.rounds); }
    if (e === "eval.case") { build.cases[data.id] = data.flaky ? "flaky" : data.status; drawCases(); }
    if (e === "eval.round.finished") setStage("evaluate", "running", data.passed + " of " + data.total + " cases pass after round " + (data.round || build.rounds));
    if (e === "repair.started") setStage("evaluate", "running", "repairing the design from the failing cases");
    if (e === "build.question") setStage("data", "waiting", "awa needs your answer");
    if (data.stage === "write") setStage("evaluate", "ok");
  }
  if (event === "done") {
    if (!build.stages.data) build.stages.data = {status: "ok", text: "nothing to read"};
    setStage("done", "ok");
    markDone("describe", true);
    markDone("review", true);
    for (const c of data.cases || []) build.cases[c.id] = c.status;
    drawCases();
    showResult(data);
  }
  if (event === "error") {
    const running = STAGES.find(([id]) => build.stages[id] && build.stages[id].status === "running");
    if (running) setStage(running[0], "failed", "stopped");
    $("#describe-result").replaceChildren(fail(data));
  }
}

/* The result in plain words: what was built, how many cases pass, what still needs connecting. */
function showResult(data) {
  const cases = data.cases || [];
  const passed = cases.filter((c) => c.status === "passed").length;
  const standIns = data.data_map ? data.data_map.sources.filter((s) => s.state === "stand-in") : [];
  const lines = [];
  lines.push(data.name ? "awa built " + data.name + "." : "awa designed your system.");
  if (cases.length) lines.push(passed + " of " + cases.length + " eval cases pass" + (data.passed === false ? "; the target was not reached, so check the failing ones on Test." : "."));
  if (standIns.length) lines.push(standIns.map((s) => s.name).join(", ") + (standIns.length === 1 ? " still works" : " still work") + " on a stand-in until you connect it.");
  const next = h("a", {class: "button primary", href: "#review", text: "Review the result"});
  $("#describe-result").replaceChildren(
    note(data.passed === false ? "warning" : "ok", lines.join(" ")),
    data.data_map ? h("div", {}, h("h2", {text: "Where the data comes from"}), dataMapView(data.data_map, {connect: true})) : "",
    h("div", {class: "actions"}, next));
  $("#build-details").append(h("pre", {text: data.summary || ""}));
}

function follow(promise) {
  const button = $("#create");
  return busy(button, () => promise.catch((e) => $("#describe-result").replaceChildren(fail(e))));
}

async function create() {
  await flushRemovals();
  const process = $("#process").value.trim();
  if (!process) {
    $("#describe-result").replaceChildren(fail({message: "The description is empty.", fix: "Describe your process in a few sentences first."}));
    $("#process").focus();
    return;
  }
  resetBuild();
  follow(stream("/api/build", {process, thorough: $("#thorough").checked}, onBuild));
}
