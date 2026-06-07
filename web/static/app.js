const API = "";
let pollTimer = null;
let currentJobId = null;
let currentRunId = null;

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
    stats.emails_sent || stats.would_send || stats.emails_ready_to_send || "—";
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
  const color = type === "ok" ? "success" : type === "warn" ? "warning" : "danger";
  pill.innerHTML = `<span class="dot" style="background:var(--${color})"></span>${text}`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderTable(sel, rows, cols, formatters = {}) {
  const el = $(sel);
  if (!rows?.length) {
    el.innerHTML = "<p class='empty-state'>Waiting for data...</p>";
    return;
  }
  const fmt = (col, val, row) => {
    if (formatters[col]) return formatters[col](val, row);
    if (col === "linkedin_url" && val)
      return `<a href="${escapeHtml(val)}" target="_blank" rel="noopener">LinkedIn</a>`;
    if (col === "verified") return val ? '<span class="badge badge-ok">Yes</span>' : '<span class="badge badge-warn">No</span>';
    if (col === "status") {
      const cls = val === "sent" ? "badge-ok" : val === "failed" ? "badge-fail" : "badge-warn";
      return `<span class="badge ${cls}">${escapeHtml(val || "—")}</span>`;
    }
    return escapeHtml(val ?? "—");
  };
  el.innerHTML = `<table><thead><tr>${cols.map((c) => `<th>${c.replace(/_/g, " ")}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${fmt(c, r[c], r)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function renderEmailPreviews(container, previews, compact = false) {
  const el = $(container);
  if (!previews?.length) {
    el.innerHTML = "<p class='empty-state'>No email previews yet</p>";
    return;
  }
  el.innerHTML = previews
    .map(
      (p, i) => `
    <div class="email-card ${compact ? "compact" : ""}">
      <div class="email-card-header">
        <span class="email-num">#${i + 1}</span>
        <div>
          <div class="email-to"><strong>To:</strong> ${escapeHtml(p.to)}</div>
          <div class="email-meta">${escapeHtml(p.name || "—")} · ${escapeHtml(p.company || "—")}</div>
        </div>
      </div>
      <div class="email-subject"><strong>Subject:</strong> ${escapeHtml(p.subject)}</div>
      <div class="preview-frame">${p.body_html}</div>
    </div>`
    )
    .join("");
}

function setFlowCount(id, count) {
  const el = $(id);
  if (el) el.textContent = count;
}

function clearFlow() {
  ["#flow-companies-table", "#flow-contacts-table", "#flow-emails-table", "#email-previews", "#flow-sent-table"].forEach(
    (sel) => {
      const el = $(sel);
      if (el) el.innerHTML = "<p class='empty-state'>Waiting for data...</p>";
    }
  );
  ["#flow-companies-count", "#flow-contacts-count", "#flow-emails-count", "#flow-outreach-count", "#flow-sent-count"].forEach(
    (sel) => setFlowCount(sel, 0)
  );
  $("#flow-sent")?.classList.add("hidden");
  $("#flow-run-badge")?.classList.add("hidden");
}

async function refreshFlowData(runId, stage = 4) {
  if (!runId) return;
  currentRunId = runId;

  const badge = $("#flow-run-badge");
  if (badge) {
    badge.textContent = `Run #${runId}`;
    badge.classList.remove("hidden");
  }

  const data = await api(`/api/runs/${runId}/data`);

  if (stage >= 1 && data.companies?.length) {
    setFlowCount("#flow-companies-count", data.companies.length);
    renderTable("#flow-companies-table", data.companies, ["domain", "name", "company_size", "country"]);
  }
  if (stage >= 2 && data.contacts?.length) {
    setFlowCount("#flow-contacts-count", data.contacts.length);
    renderTable("#flow-contacts-table", data.contacts, ["full_name", "job_title", "company_domain", "linkedin_url"]);
  }
  if (stage >= 3 && data.emails?.length) {
    setFlowCount("#flow-emails-count", data.emails.length);
    renderTable("#flow-emails-table", data.emails, ["email", "full_name", "job_title", "company_domain", "provider"]);
  }
  if (stage >= 3) {
    const { previews, total } = await api(`/api/runs/${runId}/previews?limit=100`);
    setFlowCount("#flow-outreach-count", total || previews.length);
    renderEmailPreviews("#email-previews", previews);
  }
  if (data.sent_emails?.length) {
    $("#flow-sent")?.classList.remove("hidden");
    setFlowCount("#flow-sent-count", data.sent_emails.length);
    renderTable("#flow-sent-table", data.sent_emails, ["email", "full_name", "company_domain", "subject", "status", "sent_at"]);
  }
}

async function loadValidation() {
  const data = await api("/api/validate");
  $("#validation-grid").innerHTML = data.checks
    .map(
      (c) => `
    <div class="validation-item">
      <div>
        <strong>${c.service}</strong>
        <div class="validation-detail">${escapeHtml(c.detail)}</div>
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
        <strong>${escapeHtml(r.seed_domain)}</strong>
        <small>${r.mode} · Run #${r.id} · ${new Date(r.started_at).toLocaleString()}</small>
      </div>`
        )
        .join("")
    : "<p class='empty-state'>No runs yet</p>";

  list.querySelectorAll(".run-item").forEach((el) => {
    el.addEventListener("click", () => loadRunDetail(el.dataset.runId));
  });
}

async function loadRunDetail(runId) {
  showPanel("results");
  currentRunId = runId;
  const { run, report } = await api(`/api/runs/${runId}`);
  if (run?.stats) updateStats(run.stats);

  if (report) {
    $("#report-meta").innerHTML = `
      <div class="report-grid">
        <div><span class="label">Run</span><strong>#${report.run_id}</strong></div>
        <div><span class="label">Seed</span><strong>${escapeHtml(report.seed_domain)}</strong></div>
        <div><span class="label">Mode</span><strong>${report.mode}</strong></div>
        <div><span class="label">Companies</span><strong>${report.companies_discovered}</strong></div>
        <div><span class="label">Contacts</span><strong>${report.contacts_enriched}</strong></div>
        <div><span class="label">Emails</span><strong>${report.emails_resolved}</strong></div>
        <div><span class="label">Sent</span><strong>${report.emails_sent}</strong></div>
        <div><span class="label">Deliverability</span><strong>${report.deliverability_rate}%</strong></div>
      </div>`;
  } else {
    $("#report-meta").innerHTML = `<p>Run #${runId}</p>`;
  }

  const data = await api(`/api/runs/${runId}/data`);
  renderTable("#companies-table", data.companies, ["domain", "name", "company_size", "country"]);
  renderTable("#contacts-table", data.contacts, ["full_name", "job_title", "company_domain", "linkedin_url"]);
  renderTable("#emails-table", data.emails, ["email", "full_name", "job_title", "company_domain", "provider"]);
  renderTable("#sent-table", data.sent_emails, ["email", "full_name", "company_domain", "subject", "status", "sent_at"]);

  const { previews } = await api(`/api/runs/${runId}/previews?limit=100`);
  renderEmailPreviews("#results-previews", previews);
}

async function startPipeline(mode, confirmSend = false) {
  const domain = $("#seed-domain").value.trim();
  if (!domain) return alert("Enter a seed domain");

  $("#btn-dry-run").disabled = true;
  $("#btn-run").disabled = true;
  showPanel("pipeline");
  clearFlow();
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
      const stage = job.stage ?? 0;
      updateStages(stage);
      updateStats(job.stats);
      $("#stage-label").textContent = job.stage_label || "";

      if (job.run_id) {
        const refreshStage = job.status === "sending" ? 4 : Math.max(stage, 1);
        await refreshFlowData(job.run_id, refreshStage);
      }

      if (job.status === "awaiting_confirmation") {
        clearInterval(pollTimer);
        await showConfirmModal(job);
        setStatus("Awaiting send confirmation", "warn");
        enableButtons();
      } else if (job.status === "completed") {
        clearInterval(pollTimer);
        setStatus("Pipeline completed", "ok");
        enableButtons();
        if (job.run_id) {
          await refreshFlowData(job.run_id, 4);
          const { report } = await api(`/api/runs/${job.run_id}`);
          if (report?.stats) updateStats(report.stats);
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

async function showConfirmModal(job) {
  const stats = job.stats || {};
  const runId = job.run_id;
  $("#confirm-body").innerHTML = `
    <div class="confirm-stats">
      <div class="confirm-stat"><span>${stats.companies_found}</span><small>Companies</small></div>
      <div class="confirm-stat"><span>${stats.decision_makers_found}</span><small>Contacts</small></div>
      <div class="confirm-stat"><span>${stats.emails_ready_to_send}</span><small>Emails to send</small></div>
    </div>
    <p class="hint">Review the outreach emails below. Nothing has been sent yet.</p>`;

  if (runId) {
    const { previews } = await api(`/api/runs/${runId}/previews?limit=100`);
    renderEmailPreviews("#confirm-previews", previews, true);
  }

  $("#confirm-modal").classList.remove("hidden");
  $("#confirm-modal").dataset.jobId = currentJobId;
  $("#confirm-modal").dataset.runId = runId;
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".nav button").forEach((btn) => {
    btn.addEventListener("click", () => showPanel(btn.dataset.panel));
  });

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $(`#tab-${tab.dataset.tab}`)?.classList.add("active");
    });
  });

  $("#btn-validate").addEventListener("click", loadValidation);
  $("#btn-dry-run").addEventListener("click", () => startPipeline("dry_run"));
  $("#btn-run").addEventListener("click", () => startPipeline("run", false));

  $("#btn-cancel-send").addEventListener("click", () => {
    $("#confirm-modal").classList.add("hidden");
  });

  $("#btn-confirm-send").addEventListener("click", async () => {
    const modal = $("#confirm-modal");
    const runId = modal.dataset.runId;
    modal.classList.add("hidden");
    showPanel("pipeline");
    setStatus("Sending emails...", "warn");
    updateStages(3);

    const { job_id } = await api(`/api/runs/${runId}/confirm-send`, { method: "POST" });
    currentJobId = job_id;
    pollJob(job_id);
  });

  loadValidation();
  loadRunHistory();
  clearFlow();
  showPanel("dashboard");
});
