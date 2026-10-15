const FILE_STATUS = {
  ok: ["Read", "ready"], partial: ["Partly read", "warning"], error: ["Could not be read", "missing"],
  unsupported: ["Not read by awa", "warning"], new: ["Not analyzed yet", "warning"],
};
const FLAG_LABEL = {info: "Note", warning: "Check", error: "Problem"};
let dataLimits = null;
let dataBusy = false;

async function loadData() {
  const cards = $("#data-cards");
  let state;
  try { state = await api("/api/data"); } catch (e) { cards.replaceChildren(fail(e)); return; }
  renderData(state);
  loadSystems();
  loadExamples();
  const running = await api("/api/state").catch(() => ({}));
  if (running.jobs && running.jobs.data) analyseData(true);  // a reload while files are being read
}

function renderData(state) {
  dataLimits = state.limits;
  $("#data-limits").textContent = "Up to " + state.limits.files + " files, " + state.limits.file + " each and " +
    state.limits.total + " in all (" + state.used_text + " used). Accepted: Excel, CSV, PDF, Word, emails (.eml, .mbox), JSON, text, SQLite databases, and zip archives of these.";
  $("#mask").checked = state.mask !== "none";
  const cards = $("#data-cards");
  $("#analyse-again").hidden = !state.files.length;
  markDone("data", state.files.length || systemCount);
  if (!state.files.length) {
    cards.replaceChildren(emptyState("file", "No files yet", "Drop your documents above, or skip them: without files or systems awa writes small sample records (stand-ins) from your description."));
  } else {
    cards.replaceChildren(...state.files.map((card, index) => fileCard(card, index)));
  }
  $("#data-next").textContent = state.files.length || systemCount ? "Continue to Describe" : "Skip: describe your process without files or systems";
}

function acceptable(file, name) {
  if (dataLimits && file.size > parseSize(dataLimits.file)) {
    return name + " is bigger than " + dataLimits.file + ", the limit for one file.";
  }
  if (file.size === 0) return name + " is empty.";
  return "";
}

function parseSize(text) {
  const [value, unit] = text.split(" ");
  return Number(value) * ({MB: 1e6, KB: 1e3, bytes: 1}[unit] || 1);
}

function sendFile(file, name, row) {
  return new Promise((resolve) => {
    const request = new XMLHttpRequest();
    request.open("PUT", "/api/data/files?name=" + encodeURIComponent(name));
    request.setRequestHeader("Authorization", AUTH.Authorization);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    const bar = row.querySelector(".meter span");
    request.upload.onprogress = (event) => { if (event.lengthComputable) bar.style.width = (100 * event.loaded / event.total) + "%"; };
    request.onload = () => {
      let body = {};
      try { body = JSON.parse(request.responseText); } catch (e) { /* not JSON */ }
      if (request.status < 300) {
        bar.style.width = "100%";
        row.classList.add("done");
        row.querySelector(".state").replaceChildren(tick(true), " Copied into your project.");
        resolve(true);
      } else {
        row.classList.add("failed");
        const error = body.error || {message: "The upload failed (" + request.status + ")"};
        row.querySelector(".state").replaceChildren(fail(error));
        resolve(false);
      }
    };
    request.onerror = () => {
      row.classList.add("failed");
      row.querySelector(".state").replaceChildren(fail({message: "awa is not reachable", fix: "Make sure the awa window is still open, then reload this page."}));
      resolve(false);
    };
    request.send(file);
  });
}

async function uploadFiles(items) {
  if (dataBusy) { $("#upload-list").prepend(note("warning", "Wait until the files before are copied.")); return; }
  dataBusy = true;
  const list = $("#upload-list");
  list.replaceChildren();
  let sent = 0;
  try {
    for (const {file, name} of items) {
      const row = h("li", {class: "upload"},
        h("span", {class: "name", text: name}),
        h("div", {class: "meter", role: "img", "aria-label": "Upload of " + name}, h("span")),
        h("span", {class: "state", role: "status"}));
      list.append(row);
      const problem = acceptable(file, name);
      if (problem) {
        row.classList.add("failed");
        row.querySelector(".state").replaceChildren(fail({message: problem, fix: "Pick a smaller file, or split it."}));
        continue;
      }
      row.querySelector(".state").textContent = "Copying…";
      if (await sendFile(file, name, row)) sent += 1;
    }
  } finally { dataBusy = false; }
  if (sent) await analyseData(false);
  else await loadData();
}

/* Files and folders from a drop: a folder is walked, its structure kept. */
async function droppedFiles(dataTransfer) {
  const items = [];
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      items.push({file, name: prefix + entry.name});
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await walk(child, prefix + entry.name + "/");
      }
    }
  };
  const entries = [...dataTransfer.items].map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
  if (entries.length) for (const entry of entries) await walk(entry, "");
  else for (const file of dataTransfer.files) items.push({file, name: file.name});
  return items;
}

function pickedFiles(input) {
  return [...input.files].map((file) => ({file, name: file.webkitRelativePath || file.name}));
}

async function analyseData(follow) {
  const status = $("#data-status");
  status.replaceChildren(h("p", {class: "working"}, h("span", {class: "spinner", "aria-hidden": "true"}), h("span", {id: "analyse-text", text: "Reading your files…"})));
  for (const button of document.querySelectorAll("#data button")) button.disabled = true;
  const onEvent = (event, data) => {
    if (event === "progress") $("#analyse-text").textContent = data.message + "…";
    if (event === "done") { status.replaceChildren(); renderData(data); }
    if (event === "error") { status.replaceChildren(fail(data)); loadData(); }
  };
  try {
    if (follow === true) await stream("/api/jobs/data", undefined, onEvent);
    else await stream("/api/data/analyse", {}, onEvent);
  } catch (e) { status.replaceChildren(fail(e)); }
  for (const button of document.querySelectorAll("#data button")) button.disabled = false;
}

function fileCard(card, index) {
  const [label, cls] = FILE_STATUS[card.status] || [card.status, "warning"];
  const title = "file-" + index + "-title", noteId = "file-" + index + "-note";
  const flags = card.flags.length ? h("ul", {class: "flags"}, card.flags.map((f) => h("li", {class: "flag " + f.severity},
    h("span", {class: "badge " + (f.severity === "error" ? "missing" : f.severity === "warning" ? "warning" : "ready"), text: FLAG_LABEL[f.severity] || f.severity}), " ", f.text))) : null;
  const personal = card.personal.length ? h("p", {class: "personal"}, h("strong", {text: "Contains personal data: "}), card.personal.join(", "),
    ". Switch on “Hide personal data from the AI” above to keep it away from the model.") : null;
  const tables = card.tables.map((t) => h("details", {class: "table-info"},
    h("summary", {}, t.title + ": " + t.summary),
    t.totals_rows.length ? h("p", {class: "hint", text: "Totals row at row " + t.totals_rows.join(", ") + " (not counted as data)."}) : null,
    h("div", {class: "scroll"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Column", "What it holds", "Examples", "Notes"].map((c) => h("th", {scope: "col", text: c})))),
      h("tbody", {}, t.columns.map((c) => h("tr", {}, h("td", {text: c.name}), h("td", {text: c.type}), h("td", {text: c.examples.join(" · ")}), h("td", {text: c.notes.join("; ")})))))),
    t.more_columns ? h("p", {class: "hint", text: "… and " + t.more_columns + " more columns"}) : null,
    t.sample ? h("div", {class: "scroll"}, h("table", {class: "sample"},
      h("caption", {class: "hint", text: "The first rows, as written in the file"}),
      h("thead", {}, h("tr", {}, t.sample.columns.map((c) => h("th", {scope: "col", text: c})))),
      h("tbody", {}, t.sample.rows.map((r) => h("tr", {}, r.map((v) => h("td", {text: v}))))))) : null));
  const noteBox = h("textarea", {id: noteId, rows: "2", maxlength: "1000", placeholder: "For example: match every line to an open invoice by amount and reference."});
  noteBox.value = card.note;
  const noteState = h("span", {class: "hint", role: "status"});
  noteBox.addEventListener("change", async () => {
    try { await api("/api/data/note", {name: card.name, text: noteBox.value}); noteState.replaceChildren(tick(), " Saved."); }
    catch (e) { noteState.replaceChildren(fail(e)); }
  });
  const remove = h("button", {type: "button", class: "small ghost"}, icon("trash"), "Remove");
  const done = h("article", {class: "card file", "aria-labelledby": title},
    h("div", {class: "row spread"}, h("h3", {id: title, text: card.name}), h("span", {class: "badge " + cls, text: label})),
    h("p", {class: "hint", text: card.kind_label + " · " + card.size_text}),
    h("p", {class: "headline", text: card.headline}),
    personal, flags,
    card.excerpt ? h("p", {class: "excerpt hint"}, h("strong", {text: "Starts with: "}), card.excerpt) : null,
    card.attachments.length ? h("p", {class: "hint", text: "Attachments: " + card.attachments.join(", ")}) : null,
    tables,
    h("label", {for: noteId}, "What should happen with this data? ", h("span", {class: "hint", text: "(optional; awa reads it when it designs the process)"})),
    noteBox, noteState,
    h("div", {class: "actions"},
      h("button", {type: "button", class: "small analyse-file", text: "Analyze again", onclick: () => analyseData(false)}), remove));
  remove.addEventListener("click", () => removeWithUndo(done, card.name,
    async () => { try { renderData(await api("/api/data/remove", {name: card.name})); } catch (e) { $("#data-status").replaceChildren(fail(e)); } }));
  return done;
}

let examples = null;

async function loadExamples() {
  if (examples) return;
  const pick = $("#example-pick");
  try { examples = (await api("/api/examples")).examples; }
  catch (e) { $("#example-status").replaceChildren(fail(e)); return; }
  if (!examples.length) {
    $("#example-box").hidden = true;
    return;
  }
  pick.replaceChildren(...examples.map((e) => h("option", {value: e.id, text: e.title + " (" + e.kind + ")"})));
  describeExample();
}

function describeExample() {
  const chosen = (examples || []).find((e) => e.id === $("#example-pick").value);
  $("#example-about").textContent = chosen ? chosen.description + " " +
    (chosen.files ? chosen.files + (chosen.files === 1 ? " data file." : " data files.") : "No data files: awa writes stand-ins from the description.") : "";
}

async function openExample() {
  const id = $("#example-pick").value;
  const status = $("#example-status");
  if (!id) return;
  let opened;
  try { opened = await api("/api/examples/open", {id}); }
  catch (e) { status.replaceChildren(fail(e)); return; }
  $("#setup-folder").textContent = opened.folder;
  $("#process").value = opened.process;
  newChat();
  showFolder();
  status.replaceChildren(note("ok", "Opened " + opened.title + " in " + opened.folder + ". Check the files, then continue to Describe."));
  $("#example-box").open = false;
  await loadData();
  if (opened.files.length) await analyseData(false);
}

function startData() {
  $("#example-pick").addEventListener("change", describeExample);
  $("#open-example").addEventListener("click", (e) => busy(e.currentTarget, openExample));
  const zone = $("#dropzone");
  for (const name of ["dragenter", "dragover"]) zone.addEventListener(name, (e) => { e.preventDefault(); zone.classList.add("over"); });
  for (const name of ["dragleave", "drop"]) zone.addEventListener(name, (e) => { e.preventDefault(); zone.classList.remove("over"); });
  zone.addEventListener("drop", async (e) => uploadFiles(await droppedFiles(e.dataTransfer)));
  $("#choose-files").addEventListener("click", () => $("#pick-files").click());
  $("#choose-folder").addEventListener("click", () => $("#pick-folder").click());
  for (const input of [$("#pick-files"), $("#pick-folder")]) {
    input.addEventListener("change", () => { const items = pickedFiles(input); input.value = ""; if (items.length) uploadFiles(items); });
  }
  $("#analyse-again").addEventListener("click", () => analyseData(false));
  $("#mask").addEventListener("change", async (e) => {
    const hide = e.currentTarget.checked;
    try {
      await api("/api/data/mask", {mask: hide ? "placeholders" : "none"});
      $("#mask-state").textContent = hide ? "Personal data is hidden from the design model; agents that read your data at run time see it unmasked." : "The design model sees your data as it is.";
    } catch (error) { $("#data-status").replaceChildren(fail(error)); }
  });
}
