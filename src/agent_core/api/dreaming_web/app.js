"use strict";
const $ = (id) => document.getElementById(id);
let rows = [],
  selected = null,
  state = "proposed",
  cursor = null,
  schedule = null,
  busy = false,
  pending = null,
  generation = 0;
const labels = {
  merge: "Equivalent merge",
  summary: "Related summary",
  hypothesis: "Tentative connection",
  conflict: "Conflict flag",
};
const deferralLabels = {
  legacy_attribution_unavailable: "Legacy deletion metadata needs recovery",
  source_unavailable: "Complete original evidence unavailable",
  source_ineligible: "Sources do not meet admission rules",
  scope_mismatch: "Sources have incompatible visibility",
  egress_unavailable: "Provider privacy check unavailable",
  egress_denied: "Provider privacy check refused export",
  input_budget: "Evidence exceeds the input limit",
  model_unavailable: "Configured model unavailable",
  pricing_unavailable: "Model pricing unavailable",
  cost_budget: "Request exceeds the cost limit",
};
const effects = {
  merge:
    "Group these equivalent memories when answering. Keep every original and its evidence. You can undo this merge here.",
  summary:
    "Make this source-backed summary available to future answers when relevant. Keep every original.",
  hypothesis:
    "Make this tentative connection available to future answers. It remains an inference, not a confirmed fact.",
  conflict:
    "Record this conflict for inspection. Keep both originals; this does not resolve the contradiction.",
};
const date = (value) =>
  value ? new Date(value).toLocaleString() : "Not yet scheduled";
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function error(e) {
  $("error").textContent =
    e.message || "The request failed. Refresh and retry.";
  $("error").hidden = false;
}
function notice(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  setTimeout(() => ($("toast").hidden = true), 5000);
}
function lock(value) {
  busy = value;
  for (const b of document.querySelectorAll("button")) b.disabled = value;
}
async function api(path, body, key, method, bearer) {
  const headers = { Accept: "application/json", "X-Dreaming-Session": "1" };
  if (bearer) headers.Authorization = "Bearer " + bearer;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  if (!response.ok) {
    if (response.status === 401) clearPrivate();
    const e = new Error(
      response.status === 409
        ? "This proposal or schedule changed. Refresh before deciding."
        : response.status === 401
          ? "Please connect again. Your sign-in may have expired."
          : response.status === 404
            ? "This item is no longer available."
            : "Request failed (" +
              response.status +
              "). Your decision may have been saved; retry with the same action.",
    );
    e.status = response.status;
    throw e;
  }
  return response.json();
}
function clearPrivate() {
  generation++;
  rows = [];
  selected = null;
  pending = null;
  schedule = null;
  $("token").value = "";
  $("cards").replaceChildren();
  $("detail").replaceChildren();
  $("runs").replaceChildren();
  $("workspace").hidden = true;
  $("connect").hidden = false;
  $("disconnect").hidden = true;
  $("refresh").hidden = true;
  $("confirm").close();
}
function paint() {
  const cards = $("cards");
  cards.replaceChildren();
  if (!rows.some((r) => r.id === selected)) selected = rows[0]?.id;
  for (const r of rows) {
    const b = node(
      "button",
      undefined,
      "card" + (r.id === selected ? " selected" : ""),
    );
    b.setAttribute("aria-pressed", String(r.id === selected));
    b.append(
      node("span", labels[r.kind], "kind"),
      node("span", r.content?.subject || "Content unavailable", "title"),
      node("span", r.state + " · " + r.sources.length + " sources", "foot"),
    );
    b.onclick = () => {
      selected = r.id;
      paint();
    };
    cards.append(b);
  }
  if (!rows.length)
    cards.append(
      node(
        "p",
        state === "proposed"
          ? "No proposals waiting. Daily runs will appear here when eligible memories produce a validated proposal."
          : "No operations in this view.",
        "empty",
      ),
    );
  $("load-more").hidden = !cursor;
  const r = rows.find((r) => r.id === selected),
    detail = $("detail");
  detail.replaceChildren();
  if (!r) {
    detail.append(
      node(
        "p",
        "Select a proposal to inspect its exact change and supporting memories.",
        "empty",
      ),
    );
    return;
  }
  const inner = node("div", undefined, "detail-inner");
  inner.append(
    node("div", labels[r.kind] + " · " + r.state, "tag"),
    node("h2", r.content?.subject || "Content no longer available"),
    node(
      "p",
      r.content?.statement ||
        "The sources changed, expired or became unavailable. This proposal cannot be applied.",
      "statement",
    ),
  );
  inner.append(node("div", "Original supporting memories", "section-label"));
  for (const s of r.sources) {
    const source = node("div", undefined, "source");
    source.append(
      node("p", s.statement, "statement"),
      node(
        "footer",
        "Revision " +
          s.content_revision +
          (s.omitted ? " · retained as supporting lineage" : ""),
      ),
    );
    const details = node("details");
    details.append(
      node("summary", "Source identifiers"),
      node(
        "p",
        "Memory " +
          s.belief_id +
          " · Session " +
          s.session_id +
          " · Events " +
          s.event_ids.join(", "),
        "statement",
      ),
    );
    source.append(details);
    inner.append(source);
  }
  inner.append(
    node("div", "What changes", "section-label"),
    node("p", effects[r.kind], "change"),
    node(
      "p",
      "Created " + date(r.created_at) + " · Revision " + r.revision,
      "batch",
    ),
  );
  detail.append(inner);
  const actions = node("div", undefined, "feedback actions");
  if (r.state === "proposed" && r.content && r.sources.length) {
    for (const [label, decision] of [
      ["Approve and apply", "approved"],
      ["Reject proposal", "rejected"],
    ]) {
      const b = node(
        "button",
        label,
        "btn" + (decision === "approved" ? " primary" : ""),
      );
      b.onclick = () => ask(r, decision);
      actions.append(b);
    }
  }
  if (r.state === "committed" && r.kind === "merge") {
    const b = node("button", "Undo merge", "btn");
    b.onclick = () => ask(r, "undo");
    actions.append(b);
  }
  if (actions.children.length) detail.append(actions);
}
async function load(append = false) {
  const ticket = generation;
  const [status, page] = await Promise.all([
    api("/v1/dreaming"),
    api(
      "/v1/memory-reconsolidations?ceiling=restricted&limit=30" +
        (state ? "&state=" + state : "") +
        (append && cursor ? "&cursor=" + encodeURIComponent(cursor) : ""),
    ),
  ]);
  if (ticket !== generation) return;
  $("blocker").hidden = !status.blockers?.includes("legacy_attribution_unavailable");
  $("forward-only").hidden = !status.forward_only_since;
  $("forward-only").textContent = status.forward_only_since
    ? "Dreaming uses only new memories and source messages since " + date(status.forward_only_since) + ". Uncertain legacy evidence remains excluded. Proposals still need your approval."
    : "";
  schedule = status.schedule;
  cursor = page.next_cursor;
  rows = append ? [...rows, ...page.items] : page.items;
  paint();
  $("status").textContent = schedule.paused
    ? "Daily proposals paused"
    : "Next daily opportunity: " +
      (schedule.next_run_at
        ? date(schedule.next_run_at)
        : "next maintenance sweep");
  $("schedule-copy").textContent = $("status").textContent;
  $("pause").textContent = schedule.paused
    ? "Resume previews"
    : "Pause previews";
  const runs = $("runs");
  runs.replaceChildren();
  if (!status.runs.length)
    runs.append(
      node(
        "p",
        "No runs yet. The maintenance worker will pick up the first daily opportunity.",
      ),
    );
  for (const r of status.runs) {
    const line = node("div", undefined, "history-row");
    line.append(
      node("span", date(r.started_at)),
      node("span", r.outcome + " · " + (r.deferrals?.length ? r.deferrals.map((reason) => deferralLabels[reason] || reason.replaceAll("_", " ")).join("; ") : r.reason.replaceAll("_", " "))),
      node(
        "span",
        r.proposals +
          " proposals · " +
          r.provider_calls +
          " calls · " +
          (r.charged_usd === null ? "cost pending" : "$" + r.charged_usd),
      ),
    );
    runs.append(line);
  }
}
async function run(task) {
  if (busy) return;
  lock(true);
  $("error").hidden = true;
  try {
    await task();
  } catch (e) {
    error(e);
  } finally {
    lock(false);
  }
}
function ask(r, decision) {
  // Keep the exact retry key and revision if a response was lost.
  if (
    !pending ||
    pending.id !== r.id ||
    pending.decision !== decision ||
    pending.revision !== r.revision
  )
    pending = {
      id: r.id,
      decision,
      revision: r.revision,
      key: crypto.randomUUID(),
    };
  $("confirm-title").textContent =
    decision === "approved"
      ? "Approve and apply?"
      : decision === "rejected"
        ? "Reject this proposal?"
        : "Undo this merge?";
  $("confirm-copy").textContent =
    decision === "approved"
      ? effects[r.kind] +
        " Sources will be checked again before this exact revision is applied."
      : decision === "rejected"
        ? "This proposal will stay out of active memory. Your originals remain unchanged."
        : "Restore separate recall of these originals. The previous decision remains in history.";
  $("confirm-button").textContent =
    decision === "approved"
      ? "Approve and apply"
      : decision === "rejected"
        ? "Reject proposal"
        : "Undo merge";
  $("confirm").showModal();
}
$("confirm-button").onclick = () =>
  run(async () => {
    const p = pending;
    if (!p) return;
    const path =
      p.decision === "undo"
        ? "/v1/memory-reconsolidations/" + p.id + "/undo"
        : "/v1/dreaming/" + p.id + "/decision";
    const body = { expected_revision: p.revision };
    if (p.decision !== "undo") body.decision = p.decision;
    const saved = await api(path + "?ceiling=restricted", body, p.key);
    pending = null;
    $("confirm").close();
    await load();
    notice(
      saved.state === "committed"
        ? "Approved and applied to memory."
        : "Decision saved. Current state: " + saved.state + ".",
    );
  });
$("cancel").onclick = () => $("confirm").close();
$("connect-button").onclick = () =>
  run(async () => {
    if (
      location.protocol !== "https:" &&
      !["127.0.0.1", "localhost", "[::1]"].includes(location.hostname)
    )
      throw new Error(
        "Use HTTPS before entering an access token on a remote server.",
      );
    const bearer = $("token").value.trim();
    $("token").value = "";
    await api("/v1/dreaming/session", undefined, undefined, "POST", bearer);
    await load();
    showWorkspace();
  });
$("token").onkeydown = (e) => {
  if (e.key === "Enter") $("connect-button").click();
};
$("disconnect").onclick = () => run(async () => {
  await api("/v1/dreaming/session", undefined, undefined, "DELETE");
  clearPrivate();
});
$("refresh").onclick = () => run(() => load());
$("load-more").onclick = () => run(() => load(true));
$("pause").onclick = () =>
  run(async () => {
    await api("/v1/dreaming/schedule", {
      paused: !schedule.paused,
      expected_revision: schedule.revision,
    });
    await load();
  });
for (const b of document.querySelectorAll("[data-state]"))
  b.onclick = () =>
    run(async () => {
      state = b.dataset.state;
      cursor = null;
      for (const t of document.querySelectorAll("[data-state]")) {
        t.classList.toggle("active", t === b);
        t.setAttribute("aria-pressed", String(t === b));
      }
      await load();
    });
for (const b of document.querySelectorAll("[data-view]"))
  b.onclick = () => {
    for (const t of document.querySelectorAll("[data-view]"))
      t.classList.toggle("active", t === b);
    for (const id of ["inbox", "history", "settings"])
      $(id).hidden = id !== b.dataset.view;
  };
window.addEventListener("pagehide", clearPrivate);

function showWorkspace() {
  $("connect").hidden = true;
  $("workspace").hidden = false;
  $("disconnect").hidden = false;
  $("refresh").hidden = false;
}
// Restore only the server cookie; private content is fetched anew on every visit.
run(async () => {
  try {
    await load();
    showWorkspace();
  } catch (e) {
    if (e.status !== 401) throw e;
  }
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) location.reload();
});
