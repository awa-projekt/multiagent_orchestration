const LOADERS = {setup: loadSetup, data: loadData, describe: loadDescribe, review: loadReview, connect: () => { connectResult = null; return loadConnect(); }, test: loadTest, run: loadRun, deploy: loadDeploy, inbox: loadInbox, assistant: loadAssistant};

function route() {
  const page = PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "setup";
  for (const name of PAGES) $("#" + name).hidden = name !== page;
  for (const link of document.querySelectorAll("nav a")) {
    if (link.getAttribute("href") === "#" + page) link.setAttribute("aria-current", "step");
    else link.removeAttribute("aria-current");
  }
  document.title = "awa · " + $("#" + page + " h1").textContent;
  $("#" + page + " h1").focus();
  flushRemovals().then(() => LOADERS[page]());
}

function theme(dark) {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#theme").setAttribute("aria-pressed", String(dark));
  $("#theme").setAttribute("aria-label", dark ? "Light mode" : "Dark mode");
  $("#theme").replaceChildren(icon(dark ? "sun" : "moon"));
}

async function showFolder() {
  const state = await api("/api/state").catch(() => null);
  if (state) $("#folder-chip").textContent = $("#setup-folder").textContent = state.folder;
  if (state) { markDone("describe", state.has_system); markDone("review", state.has_system); }
}

(function start() {
  if (!TOKEN) {
    document.body.replaceChildren(h("main", {id: "main"},
      h("h1", {text: "Open the link from the awa window"}),
      h("p", {text: "For your security this page only opens with the link that awa printed when it started. Go to the window where awa is running and open the link shown there, or start awa ui again."})));
    return;
  }
  let saved = null;
  try { saved = localStorage.getItem("awa-theme"); } catch (e) { /* storage blocked */ }
  theme(saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches);
  $("#theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    theme(dark);
    try { localStorage.setItem("awa-theme", dark ? "dark" : "light"); } catch (e) { /* storage blocked */ }
  });
  watchStates();
  showFolder();
  startData();
  $("#save-setup").addEventListener("click", (e) => busy(e.currentTarget, saveSetup));
  startSystems();
  $("#create").addEventListener("click", create);
  $("#run-tests").addEventListener("click", () => runTests(stream("/api/test", {}, onTest)));
  $("#send").addEventListener("click", () => send());
  $("#stop").addEventListener("click", () => { if (turn.controller) turn.controller.abort(); });
  $("#message").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  $("#new-chat").addEventListener("click", newChat);
  $("#refresh-inbox").addEventListener("click", loadInbox);
  addEventListener("hashchange", route);
  route();
})();
