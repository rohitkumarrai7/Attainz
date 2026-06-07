const API = "";
let pollTimer = null;
let currentJobId = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function showPanel(name) {
  $$(".panel").forEach((p) => p.classList.remove("active"));
  $$(".nav button").forEach((b) => b.classList.remove("active"));
  $(`#panel-${name}`)?.classList.add("active");
  $(`.nav button[data-panel="${name}"]`)?.classList.add("active");
}

async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function updateStats(stats = {}) {
  $("#stat-companies").textContent = stats.companies_found ?? "—";
  $("#stat-contacts").textContent = stats.decision_makers_found ?? "—";
  $("#stat-emails").textContent = stats.emails_resolved ?? "—";
  $("#stat-send").textContent =
    stats.would_send || stats.emails_ready_to_send || stats.emails_sent || "—";
}

function updateStages(stage) {
  $$(".stage").forEach((el, i) => {
    el.classList.remove("active", "done");
    if (i < stage) el.classList.add("done");
    else if (i === stage) el.classList.add("active");
  });
  const pct = Math.min(100, ((stage + 1) / 4) * 100);
  $("#progress-fill").style.width = `${pct}%`;
}

function setStatus(text, type = "ok") {
  const pill = $("#status-pill");
  pill.innerHTML = `<span class="dot" style="background:var(--${type === "ok" ? "success" : type === "warn" ? "warning" : "danger"})"></span>${text}`;
}

async function loadValidation() {
  const data = await api("/api/validate");
  const grid = $("#validation-grid");
  grid.innerHTML = data.checks
    .map(
      (c) => `
    <div class="validation-item">
      <div>
        <strong>${c.service}</strong>
        <div style="color:var(--muted);font-size:0.8rem">${c.detail}</div>
      </div>
      <span class="badge ${c.ok ? "badge-ok" : "badge-fail"}">${c.ok ? "OK" : "FAIL"}</span>
    </div>`
    )
    .join("");
  setStatus(data.all_ok ? "All APIs connected" : "Some APIs need attention", data.all_ok ? "ok" : "warn");
}

async function loadRunHistory() {
  const data = await api("/api/runs?limit=15");
  const list = $("#run-history");
  list.innerHTML = data.runs.length
    ? data.runs
        .map(
          (r) => `
      <div class="run-item" data-run-id="${r.id}">
        <strong>${r.seed_domain}</strong>
        <small>${r.mode} · Run #${r.id} · ${new Date(r.started_at).toLocaleString()}</small>
      </div>`
        )
        .join("")
    : "<p style='color:var(--muted)'>No runs yet</p>";

  list.querySelectorAll(".run-item").forEach((el) => {
    el.addEventListener("click", () => loadRunDetail(el.dataset.runId));
  });
}

async function loadRunDetail(runId) {
  showPanel("results");
  const { run, report } = await api(`/api/runs/${runId}`);
  if (run.stats) updateStats(run.stats);
  if (report) {
    $("#report-meta").innerHTML = `
      <p>Run #${report.run_id} · ${report.seed_domain} · ${report.mode}</p>
      <p>Deliverability: ${report.deliverability_rate}% · Est. cost/lead: $${report.estimated_cost_per_lead}</p>`;
  }
  const data = await api(`/api/runs/${runId}/data`);
  renderTable("#companies-table", data.companies, ["domain", "name", "company_size", "country"]);
  renderTable("#contacts-table", data.contacts, ["full_name", "job_title", "company_domain", "linkedin_url"]);
  renderTable("#emails-table", data.emails, ["email", "full_name", "company_domain", "provider"]);
}

function renderTable(sel, rows, cols) {
  const el = $(sel);
  if (!rows?.length) {
    el.innerHTML = "<p style='color:var(--muted);padding:1rem'>No data</p>";
    return;
  }
  el.innerHTML = `<table><thead><tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${r[c] ?? "—"}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

async function startPipeline(mode, confirmSend = false) {
  const domain = $("#seed-domain").value.trim();
  if (!domain) return alert("Enter a seed domain");

  $("#btn-dry-run").disabled = true;
  $("#btn-run").disabled = true;
  showPanel("pipeline");
  setStatus("Pipeline running...", "warn");
  updateStages(0);
  updateStats({});

  const { job_id } = await api("/api/runs", {
    method: "POST",
    body: JSON.stringify({ domain, mode, confirm_send: confirmSend }),
  });

  currentJobId = job_id;
  pollJob(job_id);
}

function pollJob(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      updateStages(job.stage ?? 0);
      updateStats(job.stats);
      $("#stage-label").textContent = job.stage_label || "";

      if (job.status === "awaiting_confirmation") {
        clearInterval(pollTimer);
        showConfirmModal(job);
        setStatus("Awaiting send confirmation", "warn");
        enableButtons();
      } else if (job.status === "completed") {
        clearInterval(pollTimer);
        setStatus("Pipeline completed", "ok");
        enableButtons();
        if (job.run_id) {
          await loadRunDetail(job.run_id);
          await loadPreviews(job.run_id);
        }
        loadRunHistory();
      } else if (job.status === "failed") {
        clearInterval(pollTimer);
        setStatus(`Failed: ${job.error}`, "danger");
        enableButtons();
        alert(`Pipeline failed: ${job.error}`);
      } else {
        setStatus(job.stage_label || "Running...", "warn");
      }
    } catch (e) {
      clearInterval(pollTimer);
      enableButtons();
    }
  }, 1500);
}

function enableButtons() {
  $("#btn-dry-run").disabled = false;
  $("#btn-run").disabled = false;
}

function showConfirmModal(job) {
  const stats = job.stats || {};
  $("#confirm-body").innerHTML = `
    <p><strong>${stats.companies_found}</strong> companies ·
    <strong>${stats.decision_makers_found}</strong> contacts ·
    <strong>${stats.emails_ready_to_send}</strong> emails ready</p>
    <p style="color:var(--muted);margin-top:0.5rem">No emails have been sent yet. Confirm to deliver.</p>`;
  $("#confirm-modal").classList.remove("hidden");
  $("#confirm-modal").dataset.jobId = currentJobId;
  $("#confirm-modal").dataset.runId = job.run_id;
}

async function loadPreviews(runId) {
  const { previews } = await api(`/api/runs/${runId}/previews?limit=3`);
  const el = $("#email-previews");
  el.innerHTML = previews.length
    ? previews
        .map(
          (p) => `
      <div class="card" style="margin-bottom:0.75rem">
        <div><strong>To:</strong> ${p.to} (${p.name || "—"})</div>
        <div><strong>Subject:</strong> ${p.subject}</div>
        <div class="preview-frame">${p.body_html}</div>
      </div>`
        )
        .join("")
    : "";
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".nav button").forEach((btn) => {
    btn.addEventListener("click", () => showPanel(btn.dataset.panel));
  });

  $("#btn-validate").addEventListener("click", loadValidation);
  $("#btn-dry-run").addEventListener("click", () => startPipeline("dry_run"));
  $("#btn-run").addEventListener("click", () => startPipeline("run", false));

  $("#btn-cancel-send").addEventListener("click", () => {
    $("#confirm-modal").classList.add("hidden");
  });

  $("#btn-confirm-send").addEventListener("click", async () => {
    const modal = $("#confirm-modal");
    const jobId = modal.dataset.jobId;
    const runId = modal.dataset.runId;
    modal.classList.add("hidden");
    setStatus("Sending emails...", "warn");

    const { job_id } = await api(`/api/runs/${runId}/confirm-send`, { method: "POST" });
    currentJobId = job_id;
    pollJob(job_id);
  });

  loadValidation();
  loadRunHistory();
  showPanel("dashboard");
});
