/* The runs that wait for a person, as approval cards, in `box`. `refresh` redraws the page that
   holds the box after a decision. Shared by the Approvals page and Use it. */
async function fillInbox(box, refresh) {
  let data;
  try { data = await api("/api/approvals"); } catch (e) { box.replaceChildren(fail(e)); return; }
  if (!data.approvals.length) { box.replaceChildren(emptyState("inbox", "Nothing waits for you", "Runs that need an OK show up here.")); return; }
  box.replaceChildren(...data.approvals.map((run) => h("section", {class: "inbox-run", "aria-labelledby": "inbox-run-" + run.id},
    h("h3", {id: "inbox-run-" + run.id, text: run.automation + (run.trigger ? " · " + run.trigger : "") + " · " + run.created}),
    run.input ? h("p", {class: "hint", text: run.input}) : "",
    ...run.approvals.map((ask, index) => approvalForm(run, ask, index, refresh)))));
}

function approvalForm(run, ask, index, refresh) {
  const key = run.id + "-" + index;
  const {card, reason} = approvalCard(ask.card, key);
  const args = h("textarea", {id: "inbox-args-" + key, rows: "3", class: "code", spellcheck: "false"});
  const result = h("div", {class: "result", role: "status", "aria-live": "polite"});
  let sent = false;
  const decide = (approve) => async () => {
    if (sent) return;  // a double click must not send the decision twice
    sent = true;
    for (const button of card.parentNode.querySelectorAll("button")) button.disabled = true;
    try {
      const answer = await api("/api/approvals", {run: run.id, interrupt_id: ask.interrupt_id, approve, note: reason.value, args: args.value});
      const open = answer.open ? " " + answer.open + " more decision(s) are still open for this run." : "";
      result.replaceChildren(note(approve ? "ok" : "warning", (approve ? "You approved this." : "You said no.") + " Your answer is saved." + open));
      if (!answer.open) await afterDecision(run.id, result, refresh);
      else await refresh();
    } catch (error) { sent = false; result.replaceChildren(fail(error)); for (const b of card.parentNode.querySelectorAll("button")) b.disabled = false; }
  };
  card.append(
    h("details", {}, h("summary", {text: "Change the values before approving (for experts)"}),
      h("label", {for: "inbox-args-" + key}, "New values as a JSON object ", h("span", {class: "hint", text: "(optional; the tool call then runs with these)"})), args),
    h("div", {class: "actions"},
      h("button", {class: "primary", type: "button", onclick: decide(true), text: "Approve"}),
      h("button", {type: "button", onclick: decide(false), text: "Say no"})), result);
  return card;
}

/* After the last open decision of a run: say who continues it, and continue it here when no
   worker runs. */
async function afterDecision(runId, result, refresh) {
  let health;
  try { health = (await api("/api/runs")).health; } catch (e) { result.append(fail(e)); return; }
  if (health.worker_running) {
    result.append(h("p", {class: "hint", text: "The running awa worker or awa serve continues it."}));
    setTimeout(refresh, 1500);
    return;
  }
  const button = h("button", {type: "button", class: "primary", text: "Continue it now",
    onclick: (e) => busy(e.currentTarget, async () => {
      try {
        const run = await api("/api/runs/continue", {run: runId});
        result.replaceChildren(runNote(run));
        setTimeout(refresh, 1200);
      } catch (error) { result.append(fail(error)); }
    })});
  result.append(h("p", {class: "hint", text: "Nothing is running that would continue it. Press the button to continue it here, or start awa worker (see Use it)."}),
    h("div", {class: "actions"}, button));
}

function loadInbox() { return fillInbox($("#inbox-body"), loadInbox); }
