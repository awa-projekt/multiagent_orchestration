/* The brief for a coding assistant, written from the project folder. */
let briefText = "";
let briefExists = false;

async function loadAssistant() {
  $("#assistant-note").replaceChildren();
  $("#assistant-body").hidden = true;
  $("#assistant-empty").replaceChildren(skeleton(3));
  $("#assistant-empty").hidden = false;
  try {
    const data = await api("/api/brief");
    if (data.brief === null) throw new Error("no system");
    briefText = data.brief;
    briefExists = data.exists;
    $("#brief-text").textContent = briefText;
    $("#assistant-body").hidden = false;
    $("#assistant-empty").hidden = true;
  } catch (e) {
    briefText = "";
    $("#assistant-body").hidden = true;
    $("#assistant-empty").replaceChildren(emptyState("sparkle", "No system in this folder yet", "Describe your process first; the brief is written from what exists.",
      h("a", {class: "button primary", href: "#describe", text: "Describe your process"})));
    $("#assistant-empty").hidden = false;
  }
}

async function copyBrief() {
  try { await navigator.clipboard.writeText(briefText); $("#assistant-note").replaceChildren(note("ok", "Copied.")); toast("Copied the brief."); }
  catch (e) { $("#assistant-note").replaceChildren(fail({message: "The browser did not allow copying.", fix: "Select the text and copy it, or use Download."})); }
}

function downloadBrief() {
  const url = URL.createObjectURL(new Blob([briefText], {type: "text/markdown"}));
  const link = h("a", {href: url, download: "BRIEF.md"});
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  toast("Downloaded BRIEF.md.");
}

async function saveBrief(force) {
  const replace = () => h("button", {type: "button", id: "replace-brief", onclick: () => saveBrief(true)}, icon("refresh"), "Replace it");
  if (!force && briefExists) {
    $("#assistant-note").replaceChildren(note("warning", "BRIEF.md already exists in your project folder."), replace());
    return;
  }
  try {
    const data = await api("/api/brief", {force});
    briefExists = true;
    $("#assistant-note").replaceChildren(note("ok", "Saved as " + data.written + " in your project folder."));
    toast("Saved " + data.written + ".");
  } catch (e) {
    $("#assistant-note").replaceChildren(fail(e));
  }
}

$("#copy-brief").addEventListener("click", copyBrief);
$("#download-brief").addEventListener("click", downloadBrief);
$("#save-brief").addEventListener("click", () => saveBrief(false));
