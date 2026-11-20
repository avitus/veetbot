"use strict";
// The owner's recall question set. The page holds no data of its own: every
// question comes from the signed-in API, and the access token lives only in
// this tab's memory. Text reaches the document through textContent alone.
const $ = (id) => document.getElementById(id);
const BASE = "/v1/recall-questions";
const FAMILIES = [
  "attributes",
  "preferences",
  "relationships",
  "goals",
  "decisions",
  "routines",
  "temporal",
  "complex",
];
const SPLIT_LABEL = { development: "Development", holdout: "Held out" };
let token = null;
let questions = [];
let editing = null;

function el(tag, options, children) {
  const node = document.createElement(tag);
  const { className, text, ...attributes } = options || {};
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  for (const child of children || []) node.append(child);
  return node;
}

function button(label, handler, className) {
  const node = el("button", { className: className || "btn", text: label, type: "button" });
  node.addEventListener("click", handler);
  return node;
}

function show(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}

function toast(message) {
  show("toast", message);
  setTimeout(() => ($("toast").hidden = true), 4000);
}

function lock(value) {
  for (const node of document.querySelectorAll("button")) node.disabled = value;
}

function explain(status, message) {
  if (status === 401) return "That access token was not accepted.";
  if (status === 403) return "This access token is not allowed to do that.";
  if (status === 404) return "That question no longer exists. Reload the list.";
  if (status === 409)
    return "Not saved: the question changed since you opened it, or that key is already used. Reload and try again.";
  if (status === 400) return "Not saved: " + (message || "the question is incomplete.");
  return "The server could not do that (" + status + "). Try again.";
}

async function api(path, options) {
  const { method, body } = options || {};
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  if (response.ok) return response.json();
  let message = "";
  try {
    message = (await response.json()).error.message || "";
  } catch (_error) {
    message = "";
  }
  const error = new Error(explain(response.status, message));
  error.status = response.status;
  throw error;
}

function disconnect(message) {
  token = null;
  questions = [];
  $("list").replaceChildren();
  $("workspace").hidden = true;
  $("disconnect").hidden = true;
  $("connect").hidden = false;
  show("error", message || "");
}

async function load(quiet) {
  lock(true);
  try {
    questions = (await api(BASE)).items;
    $("connect").hidden = true;
    $("workspace").hidden = false;
    $("disconnect").hidden = token === null;
    show("error", "");
    render();
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      disconnect(quiet ? "" : error.message);
    } else if (!quiet) {
      show("error", error.message || "The question set could not be loaded.");
    }
  } finally {
    lock(false);
  }
}

function resultOf(question) {
  if (question.passed === true) return "passed";
  if (question.passed === false) return "missed";
  return "none";
}

function visible() {
  const split = $("filter-split").value;
  const status = $("filter-status").value;
  const result = $("filter-result").value;
  const words = $("filter-text").value.trim().toLowerCase();
  return questions.filter(
    (question) =>
      (!split || question.split === split) &&
      (!status || question.status === status) &&
      (!result || resultOf(question) === result) &&
      (!words ||
        (question.key + " " + question.content.question).toLowerCase().includes(words)),
  );
}

function summarize() {
  const active = questions.filter((question) => question.status === "active");
  const count = (test) => active.filter(test).length;
  const parts = [
    active.length + (active.length === 1 ? " active question" : " active questions"),
    count((question) => question.split === "development") + " development",
    count((question) => question.split === "holdout") + " held out",
  ];
  const ran = count((question) => question.passed !== null);
  parts.push(
    ran
      ? "latest run: " +
          count((question) => question.passed === true) +
          " passed, " +
          count((question) => question.passed === false) +
          " missed, " +
          (active.length - ran) +
          " not run"
      : "no run recorded yet",
  );
  return parts.join(" · ");
}

function factLine(item) {
  const line = el("li");
  line.append(item.required ? "Must reach: " : "Also useful: ");
  line.append(
    el("strong", {
      text: item.statement_contains || "memory " + String(item.belief_id).slice(0, 8),
    }),
  );
  return line;
}

function listLine(label, values) {
  const line = el("li");
  line.append(label + ": ");
  line.append(el("strong", { text: values.join(", ") }));
  return line;
}

function pathLine(result) {
  const describe = (path) =>
    (path.path === "automatic" ? "Unasked" : "When it searches") +
    ": " +
    path.required_delivered +
    " of " +
    path.required_total +
    " required facts delivered" +
    (path.stages.length ? " (" + path.stages.join(", ") + ")" : "");
  return (
    result.paths.map(describe).join(" · ") +
    " · recorded " +
    new Date(result.recorded_at).toLocaleString()
  );
}

function card(question) {
  const content = question.content;
  const badges = el("div", { className: "badges" }, [
    el("span", {
      className: "pill " + question.split,
      text: SPLIT_LABEL[question.split],
    }),
  ]);
  const result = resultOf(question);
  badges.append(
    el("span", {
      className: "pill " + (result === "none" ? "" : result),
      text: result === "passed" ? "Passed" : result === "missed" ? "Missed" : "Not run yet",
    }),
  );
  if (question.status === "retired") badges.append(el("span", { className: "pill", text: "Retired" }));

  const facts = el("ul", { className: "facts" }, content.expected.map(factLine));
  if (content.delivered_must_contain.length)
    facts.append(listLine("Recalled memories must show", content.delivered_must_contain));
  if (content.answer_must_contain.length)
    facts.append(listLine("Answer must contain", content.answer_must_contain));
  if (content.answer_must_not_contain.length)
    facts.append(listLine("Answer must not contain", content.answer_must_not_contain));
  if (content.forbidden.length) facts.append(listLine("Must not recall", content.forbidden));

  const node = el(
    "article",
    { className: "card" + (question.status === "retired" ? " retired" : "") },
    [
      el("div", { className: "card-head" }, [
        el("div", {}, [
          el("p", { className: "question", text: content.question }),
          el("div", { className: "key", text: question.key }),
        ]),
        badges,
      ]),
      facts,
    ],
  );
  if (question.latest_result)
    node.append(el("p", { className: "paths", text: pathLine(question.latest_result) }));
  node.append(
    el("div", { className: "actions" }, [
      button("Edit", () => openEditor(question)),
      button("History", () => openHistory(question)),
      question.status === "active"
        ? button("Retire", () => setStatus(question, "retired"))
        : button("Restore", () => setStatus(question, "active")),
    ]),
  );
  return node;
}

function render() {
  $("summary").textContent = summarize();
  const shown = visible();
  const sections = [];
  for (const family of FAMILIES) {
    const members = shown.filter((question) => question.content.family === family);
    if (!members.length) continue;
    const heading = el("h2", { text: family }, []);
    heading.append(el("span", { text: String(members.length) }));
    sections.push(el("section", { className: "family" }, [heading, ...members.map(card)]));
  }
  $("list").replaceChildren(...sections);
  $("empty").hidden = shown.length > 0;
}

async function setStatus(question, status) {
  lock(true);
  try {
    await api(BASE + "/" + question.id + "/status", {
      body: { expected_revision: question.revision, status },
    });
    toast(status === "retired" ? "Retired. It will not run until restored." : "Restored.");
  } catch (error) {
    show("error", error.message);
  } finally {
    lock(false);
  }
  await load(true);
}

function addFact(item) {
  const fact = item || { statement_contains: "", required: true };
  const kind = el("select", { "aria-label": "How the fact is named" }, [
    el("option", { value: "text", text: "Text a memory contains" }),
    el("option", { value: "memory", text: "One exact memory" }),
  ]);
  kind.value = fact.belief_id ? "memory" : "text";
  const value = el("input", { type: "text", "aria-label": "The text or memory identifier" });
  value.value = fact.belief_id || fact.statement_contains || "";
  const required = el("input", { type: "checkbox" });
  required.checked = fact.required !== false;
  const row = el("div", { className: "fact" }, [
    kind,
    value,
    el("label", {}, [required, "Required"]),
  ]);
  row.append(button("Remove", () => row.remove()));
  $("facts").append(row);
}

function lines(id) {
  return $(id)
    .value.split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function openEditor(question) {
  editing = question || null;
  show("editor-error", "");
  $("editor-title").textContent = question ? "Edit question" : "Add question";
  $("once-fields").hidden = Boolean(question);
  $("once-summary").hidden = !question;
  if (question)
    $("once-summary").textContent =
      "Key " + question.key + " · " + SPLIT_LABEL[question.split] + " half. Neither can change.";
  const content = question ? question.content : null;
  $("field-key").value = "";
  for (const radio of document.querySelectorAll('input[name="split"]')) radio.checked = false;
  $("field-question").value = content ? content.question : "";
  $("field-family").value = content ? content.family : "attributes";
  $("field-delivered").value = content ? content.delivered_must_contain.join("\n") : "";
  $("field-answer").value = content ? content.answer_must_contain.join("\n") : "";
  $("field-not-answer").value = content ? content.answer_must_not_contain.join("\n") : "";
  $("field-forbidden").value = content ? content.forbidden.join("\n") : "";
  $("field-search").value = content && content.search_text ? content.search_text : "";
  $("field-notes").value = content ? content.notes : "";
  $("facts").replaceChildren();
  for (const item of content ? content.expected : [undefined]) addFact(item);
  $("memory-search").value = "";
  $("memory-results").replaceChildren();
  show("memory-status", "");
  $("editor").showModal();
}

function collect() {
  const expected = [];
  for (const row of $("facts").children) {
    const [kind, value] = row.querySelectorAll("select, input[type=text]");
    const text = value.value.trim();
    if (!text) continue;
    const required = row.querySelector("input[type=checkbox]").checked;
    expected.push(
      kind.value === "memory"
        ? { belief_id: text, required }
        : { statement_contains: text, required },
    );
  }
  const content = {
    question: $("field-question").value.trim(),
    family: $("field-family").value,
    expected,
    forbidden: lines("field-forbidden"),
    delivered_must_contain: lines("field-delivered"),
    answer_must_contain: lines("field-answer"),
    answer_must_not_contain: lines("field-not-answer"),
    notes: $("field-notes").value.trim(),
  };
  const search = $("field-search").value.trim();
  if (search) content.search_text = search;
  return content;
}

async function save() {
  const content = collect();
  if (!content.question) return show("editor-error", "Write the question first.");
  if (
    !content.expected.length &&
    !content.delivered_must_contain.length &&
    !content.answer_must_contain.length
  )
    return show(
      "editor-error",
      "Name at least one fact the answer should reach, or a name or word it must show.",
    );
  let request;
  if (editing) {
    request = api(BASE + "/" + editing.id, {
      method: "PUT",
      body: { expected_revision: editing.revision, content },
    });
  } else {
    const key = $("field-key").value.trim();
    const split = document.querySelector('input[name="split"]:checked');
    if (!/^[a-z0-9][a-z0-9-]*$/.test(key))
      return show("editor-error", "Give the question a key of lowercase letters, digits and hyphens.");
    if (!split) return show("editor-error", "Choose the half this question joins.");
    request = api(BASE, { body: { key, split: split.value, content } });
  }
  lock(true);
  try {
    await request;
    $("editor").close();
    toast("Saved.");
  } catch (error) {
    lock(false);
    return show("editor-error", error.message);
  }
  lock(false);
  await load(true);
}

async function searchMemories() {
  const text = $("memory-search").value.trim();
  if (!text) return;
  show("memory-status", "Searching…");
  $("memory-results").replaceChildren();
  let found;
  try {
    found = await api(
      "/v1/memories?ceiling=restricted&limit=8&text=" + encodeURIComponent(text),
    );
  } catch (error) {
    return show(
      "memory-status",
      error.status === 404
        ? "Memory search is not available on this server. Type the text a memory contains instead."
        : error.message,
    );
  }
  show("memory-status", found.items.length ? "" : "No stored memory matches those words.");
  for (const memory of found.items) {
    $("memory-results").append(
      el("div", { className: "memory" }, [
        el("p", { text: memory.statement }),
        el("div", { className: "actions" }, [
          button("Reach this text", () =>
            addFact({ statement_contains: memory.statement, required: true }),
          ),
          button("Reach this exact memory", () =>
            addFact({ belief_id: memory.id, required: true }),
          ),
        ]),
      ]),
    );
  }
}

async function openHistory(question) {
  lock(true);
  try {
    const history = await api(BASE + "/" + question.id + "/history");
    $("history-title").textContent = "History of " + question.key;
    $("history-list").replaceChildren(
      ...history.items
        .slice()
        .reverse()
        .map((revision) =>
          el("div", { className: "revision" }, [
            el("p", {
              className: "key",
              text:
                "Version " +
                revision.revision +
                " · " +
                new Date(revision.changed_at).toLocaleString() +
                " · " +
                revision.status,
            }),
            el("p", { className: "question", text: revision.content.question }),
            el("ul", { className: "facts" }, revision.content.expected.map(factLine)),
          ]),
        ),
    );
    $("history").showModal();
  } catch (error) {
    show("error", error.message);
  } finally {
    lock(false);
  }
}

$("connect-button").addEventListener("click", () => {
  token = $("token").value.trim() || null;
  $("token").value = "";
  load(false);
});
$("token").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("connect-button").click();
});
$("disconnect").addEventListener("click", () => disconnect(""));
$("refresh").addEventListener("click", () => load(false));
$("add").addEventListener("click", () => openEditor(null));
$("add-fact").addEventListener("click", () => addFact());
$("editor-save").addEventListener("click", save);
$("editor-cancel").addEventListener("click", () => $("editor").close());
$("history-close").addEventListener("click", () => $("history").close());
$("memory-search-button").addEventListener("click", searchMemories);
$("memory-search").addEventListener("keydown", (event) => {
  if (event.key === "Enter") searchMemories();
});
for (const id of ["filter-split", "filter-status", "filter-result"])
  $(id).addEventListener("change", render);
$("filter-text").addEventListener("input", render);

// Local development needs no token, so try once before asking for one.
load(true);
