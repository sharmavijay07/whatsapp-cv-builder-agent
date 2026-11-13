const logEl = document.querySelector("#log");
const textEl = document.querySelector("#text");
const noteEl = document.querySelector("#note");
const dbEl = document.querySelector("#db");
const alertsEl = document.querySelector("#alerts");
const flagEl = document.querySelector("#flag");
const llmEl = document.querySelector("#llm-line");
let sessionId = null;
let busy = false;

function esc(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function linkify(value) {
  return esc(value).replace(
    /https?:\/\/[^\s<]+/g,
    (url) => `<a href="${url}">${url}</a>`,
  );
}

function stamp(value) {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderMessage(message) {
  const side = message.direction === "in" ? "out" : "in";
  let body = linkify(message.text || "");
  if (message.filename && message.token) {
    body += `<a class="filechip" href="/files/${encodeURIComponent(message.token)}">${esc(message.filename)}</a>`;
  } else if (message.filename) {
    body += `<div class="filechip">${esc(message.filename)}</div>`;
  }
  return `<div class="bubble ${side}">${body}<time>${stamp(message.at)}</time></div>`;
}

function render(messages) {
  logEl.innerHTML = messages.map(renderMessage).join("");
  logEl.scrollTop = logEl.scrollHeight;
}

function renderInspector(data) {
  if (!data) return;
  dbEl.textContent = JSON.stringify(data.database_row, null, 2);
  const clean = data.database_contains_cv_text === false;
  flagEl.textContent = clean
    ? "CV text is not in this database row."
    : "CV text was found in the database row.";
  flagEl.className = clean ? "flag" : "flag warn";
  alertsEl.textContent = JSON.stringify(data.alerts || [], null, 2);
  if (data.llm) {
    llmEl.textContent = data.llm.live
      ? `Calling ${data.llm.provider} model ${data.llm.model}.`
      : `Default provider is ${data.llm.provider} (${data.llm.model}). No API key in this session, so rewriting uses the local fact-locked writer.`;
  }
}

function setNote(text) {
  noteEl.textContent = text || "";
}

async function createSession() {
  const response = await fetch("/api/demo/sessions", { method: "POST" });
  if (!response.ok) throw new Error("Could not open a demo session");
  const body = await response.json();
  sessionId = body.session_id;
  render(body.messages);
  renderInspector(body.inspector);
}

async function sendForm(fields, file) {
  if (!sessionId || busy) return;
  busy = true;
  setNote("");
  const form = new FormData();
  Object.entries(fields).forEach(([key, value]) => {
    if (value) form.append(key, value);
  });
  if (file) form.append("file", file);
  logEl.insertAdjacentHTML(
    "beforeend",
    '<div class="bubble in" id="typing">Safu is typing…</div>',
  );
  logEl.scrollTop = logEl.scrollHeight;
  try {
    const response = await fetch(`/api/demo/sessions/${sessionId}/messages`, {
      method: "POST",
      body: form,
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Message failed");
    render(body.messages);
    renderInspector(body.inspector);
  } catch (error) {
    document.querySelector("#typing")?.remove();
    setNote(error.message || "Something went wrong.");
  } finally {
    busy = false;
  }
}

async function sendText(text) {
  textEl.value = "";
  await sendForm({ text });
}

async function sendSample(name) {
  await sendForm({ sample: name });
}

document.querySelector("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = textEl.value.trim();
  if (text) sendText(text);
});

textEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.querySelector("#composer").requestSubmit();
  }
});

document.querySelector("#attach").addEventListener("click", () => {
  document.querySelector("#file").click();
});

document.querySelector("#file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (file) sendForm({}, file);
  event.target.value = "";
});

document.querySelectorAll("[data-sample]").forEach((button) => {
  button.addEventListener("click", () => sendSample(button.dataset.sample));
});

document.querySelector("#play").addEventListener("click", async () => {
  const button = document.querySelector("#play");
  button.disabled = true;
  try {
    await sendText("Amina Wanjiku");
    await sendSample("cv");
    await sendSample("job");
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const last = [...logEl.querySelectorAll(".bubble.in")].pop()?.innerText || "";
      if (/email address/i.test(last)) {
        await sendText("amina.wanjiku@email.com");
        continue;
      }
      if (/phone number/i.test(last)) {
        await sendText("+254 712 345 678");
        continue;
      }
      if (/which city/i.test(last)) {
        await sendText("Nairobi");
        continue;
      }
      break;
    }
    await sendText("YES");
    await sendText("PAY");
  } finally {
    button.disabled = false;
  }
});

async function postAction(path) {
  if (!sessionId) return;
  const response = await fetch(`/api/demo/sessions/${sessionId}/${path}`, { method: "POST" });
  const body = await response.json();
  if (!response.ok) {
    setNote(body.detail || "That action needs a finished step first.");
    return;
  }
  if (body.messages) render(body.messages);
  if (body.inspector) renderInspector(body.inspector);
  if (body.detail) setNote(body.detail);
  if (body.sent && body.sent.length === 0 && body.ok === false) setNote(body.detail);
}

document.querySelector("#fail").addEventListener("click", () => postAction("simulate-failure"));
document.querySelector("#abandon").addEventListener("click", () => postAction("simulate-abandonment"));
document.querySelector("#recover").addEventListener("click", async () => {
  const response = await fetch("/api/demo/recovery/run", { method: "POST" });
  const body = await response.json();
  if (!response.ok) {
    setNote("Recovery could not run.");
    return;
  }
  const session = await fetch(`/api/demo/sessions/${sessionId}`);
  const payload = await session.json();
  render(payload.messages);
  renderInspector(payload.inspector);
  setNote(body.recovered?.length ? "Recovery resent the documents." : "Nothing was waiting for recovery.");
});
document.querySelector("#reset").addEventListener("click", () => createSession().catch((error) => setNote(error.message)));

createSession().catch((error) => setNote(error.message));
setInterval(async () => {
  if (!sessionId || busy) return;
  try {
    const response = await fetch(`/api/demo/sessions/${sessionId}/inspector`);
    if (response.ok) renderInspector(await response.json());
  } catch (_error) {
    /* The next poll retries. */
  }
}, 2000);
