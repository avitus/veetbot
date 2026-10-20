/* Offline owner findings. No defaults, network calls, or automatic acceptance. */
"use strict";
const meta = JSON.parse(document.getElementById("review-data").textContent);
const draftKey = "recall-owner-review-submission@2:" + meta.scope_proposal_sha256;
const fields = [...document.querySelectorAll("[data-finding]")];
const byId = (id) => document.getElementById(id);
const valueIds = ["reviewer", "decision", "findings", "risk"];
const checkIds = ["human", "independent"];

function draft() {
  return {
    version: "recall-owner-review-draft@2",
    scope_proposal_sha256: meta.scope_proposal_sha256,
    values: Object.fromEntries(valueIds.map((id) => [id, byId(id).value])),
    checks: Object.fromEntries(checkIds.map((id) => [id, byId(id).checked])),
    assessments: Object.fromEntries(fields.map((el) => [el.dataset.finding, {
      verdict: el.querySelector(".verdict").value,
      notes: el.querySelector(".notes").value,
    }])),
  };
}

function restore(d) {
  if (d.version !== "recall-owner-review-draft@2" ||
      d.scope_proposal_sha256 !== meta.scope_proposal_sha256 ||
      !d.values || !d.checks || !d.assessments) {
    throw new Error("This draft belongs to a different review.");
  }
  for (const id of valueIds) byId(id).value = String(d.values[id] || "");
  for (const id of checkIds) byId(id).checked = d.checks[id] === true;
  for (const el of fields) {
    const item = d.assessments[el.dataset.finding] || {};
    el.querySelector(".verdict").value = item.verdict || "";
    el.querySelector(".notes").value = String(item.notes || "");
  }
}

function update() {
  const d = draft();
  const completed = Object.values(d.assessments).filter((f) =>
    f.verdict && (f.verdict === "acceptable" || f.notes.trim().length >= 3)).length;
  byId("progress").textContent = `${completed} of 42 assessments recorded`;
  try {
    localStorage.setItem(draftKey, JSON.stringify(d));
    byId("storage").textContent = "Draft saved in this browser. Download a draft to keep a separate copy.";
  } catch {
    byId("storage").textContent = "Browser storage is unavailable. Use Download draft to preserve your work.";
  }
}

function download(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + "\n"], {
    type: "application/json",
  }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function submission() {
  const d = draft();
  for (const [key, f] of Object.entries(d.assessments)) {
    if (!["acceptable", "concern", "unacceptable"].includes(f.verdict) ||
        (f.verdict !== "acceptable" && f.notes.trim().length < 3)) {
      throw new Error(`Complete the assessment and any required notes for ${key}.`);
    }
  }
  if (d.values.reviewer.trim().length < 2 || !d.values.decision ||
      d.values.findings.trim().length < 20 || d.values.risk.trim().length < 20 ||
      !d.checks.human || !d.checks.independent) {
    throw new Error("Complete your name, decision, findings, residual-risk finding and both attestations.");
  }
  if (d.values.decision === "accept_bounded_rollout" &&
      Object.values(d.assessments).some((f) => f.verdict === "unacceptable")) {
    throw new Error("An unacceptable finding requires changes or rejection, not acceptance.");
  }
  return {
    version: "recall-owner-review-submission@2",
    reviewer: d.values.reviewer.trim(),
    reviewer_kind: "human",
    independent_of_implementation: true,
    reviewed_at: new Date().toISOString(),
    decision: d.values.decision,
    findings: d.values.findings.trim(),
    observations_sha256: meta.observations_sha256,
    review_items_sha256: meta.review_items_sha256,
    scope_proposal_sha256: meta.scope_proposal_sha256,
    reviewed_scenarios: meta.selected,
    omitted_scenarios: meta.omitted,
    scenario_findings: Object.fromEntries(meta.selected.map((id) => [id, d.assessments["case:" + id]])),
    summary_findings: Object.fromEntries(["cost", ...Object.keys(meta.groups)].map((id) =>
      [id, d.assessments["summary:" + id]])),
    reviewed_item_groups: meta.groups,
    residual_risk_findings: d.values.risk.trim(),
  };
}

byId("save").addEventListener("click", () => download("recall-owner-review-draft.json", draft()));
byId("export").addEventListener("click", () => {
  try {
    const data = submission();
    download("recall-owner-review.json", data);
    byId("error").textContent = "Review exported. Return the saved JSON in this chat; recall remains disabled.";
  } catch (error) {
    byId("error").textContent = error.message;
  }
});
byId("restore").addEventListener("change", async (event) => {
  try {
    const file = event.target.files[0];
    if (!file) return;
    restore(JSON.parse(await file.text()));
    update();
    byId("error").textContent = "";
  } catch (error) {
    byId("error").textContent = error.message;
  }
});
document.addEventListener("input", update);
document.addEventListener("change", update);
try {
  const saved = localStorage.getItem(draftKey);
  if (saved) restore(JSON.parse(saved));
} catch {
  byId("storage").textContent = "No readable saved draft. Download a draft to preserve your work.";
}
update();
