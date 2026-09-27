const state = {
  cases: [],
  currentCase: null,
  conference: null,
  activeView: "builder",
  activeAgent: "ASCLEPIUS",
  agentAction: "briefing",
  evidenceResults: [],
  savedTopics: [],
  quizSet: null,
  presenterSlides: [],
  presenterIndex: 0,
  library: null,
  libraryCursor: {},
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
  const response = await fetch(path, { headers, ...options });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = payload.detail || "";
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json();
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 2600);
}

function currentAsset() {
  const assets = state.currentCase?.assets || [];
  return assets.find((asset) => asset.asset_id === state.currentCase?.active_asset_id) || assets[0] || null;
}

function isLibraryVolume(asset) {
  return asset?.kind === "library_volume" && asset.library_series_id;
}

function libraryCursor(asset) {
  if (!asset) return { index: 0, preset: "mri" };
  const stored = state.libraryCursor[asset.asset_id];
  return {
    index: stored?.index ?? Number(asset.slice_index || 0),
    preset: stored?.preset || asset.preset || "mri",
  };
}

function setLibraryCursor(asset, patch) {
  const current = libraryCursor(asset);
  state.libraryCursor[asset.asset_id] = { ...current, ...patch };
}

function librarySliceSrc(asset) {
  const cursor = libraryCursor(asset);
  const params = new URLSearchParams({
    index: String(cursor.index),
    preset: cursor.preset,
  });
  return `/api/neuroforge/library/series/${encodeURIComponent(asset.library_series_id)}/slice?${params.toString()}`;
}

function renderLibraryOptions() {
  const select = $("library-select");
  if (!select) return;
  const studies = state.library?.studies || [];
  const options = ['<option value="">No library series</option>'];
  studies.forEach((study) => {
    (study.series || []).forEach((series, index) => {
      const label = `${series.modality} · ${series.slice_count} slices · ${series.rows}×${series.columns} · ${String(study.study_id).slice(0, 8)}/${index + 1}`;
      options.push(`<option value="${escapeHtml(series.series_id)}">${escapeHtml(label)}</option>`);
    });
  });
  select.innerHTML = options.join("");
}

function approvedEvidence() {
  return (state.currentCase?.evidence || []).filter((item) =>
    ["local_reviewed", "doctor_approved"].includes(item.state)
  );
}

async function loadBootstrap() {
  const payload = await api("/api/neuroforge/bootstrap");
  state.cases = payload.cases || [];
  state.conference = payload.conference;
  state.currentCase = payload.active_case;
  state.savedTopics = payload.saved_topics || [];
  state.quizSet = (state.currentCase?.quiz_sets || []).at(-1) || null;
  state.library = payload.library || { studies: [] };

  const urlParams = new URLSearchParams(window.location.search);
  const targetCaseId = urlParams.get("case_id");
  if (targetCaseId) {
    const matching = state.cases.find((c) => c.case_id === targetCaseId);
    if (matching) {
      try {
        state.currentCase = await api(`/api/neuroforge/cases/${encodeURIComponent(targetCaseId)}`);
        state.quizSet = (state.currentCase?.quiz_sets || []).at(-1) || null;
      } catch (err) {
        console.warn("Could not load target case from URL query:", err);
      }
    }
  }
  const targetView = urlParams.get("view");
  if (targetView) {
    setView(targetView);
  }

  renderLibraryOptions();
  renderAll();
}

async function loadCase(caseId) {
  state.currentCase = await api(`/api/neuroforge/cases/${caseId}`);
  state.quizSet = (state.currentCase?.quiz_sets || []).at(-1) || null;
  renderAll();
}

function renderAll() {
  renderCaseList();
  renderCaseHeading();
  renderTimeline();
  renderViewer();
  renderNarrative();
  renderEvidence();
  renderAgenda();
  renderScope();
  renderSavedTopics();
  renderQuiz();
  renderStress();
}

function renderCaseList() {
  const query = $("case-search").value.trim().toLowerCase();
  const visible = state.cases.filter((item) =>
    [item.title, item.clinical_question, item.case_id].join(" ").toLowerCase().includes(query)
  );
  $("case-list").innerHTML = visible.map((item) => {
    const active = item.case_id === state.currentCase?.case_id ? " active" : "";
    const question = item.clinical_question || "No conference question yet.";
    return `
      <button type="button" class="case-row${active}" data-case-id="${escapeHtml(item.case_id)}">
        <img class="case-thumb" src="/static/demo-brain-flair.png" alt="" />
        <span class="case-copy">
          <strong>${escapeHtml(item.title)}</strong>
          <span>${escapeHtml(question.slice(0, 72))}</span>
          <span>${item.study_count || 0} studies · ${item.allotted_minutes || 15} min</span>
        </span>
        <span class="status-tag ${escapeHtml(item.status)}" data-status="${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
      </button>
    `;
  }).join("");
  const total = state.cases.reduce((sum, item) => sum + Number(item.allotted_minutes || 0), 0);
  $("agenda-total").textContent = `${total} min`;
}

function renderCaseHeading() {
  const caseItem = state.currentCase;
  if (!caseItem) return;
  $("case-code").textContent = caseItem.case_id;
  $("case-title-heading").textContent = caseItem.title;
  $("case-status").textContent = caseItem.status || "draft";
  $("case-status").className = `status-tag ${caseItem.status || "draft"}`;
  $("export-pdf-btn").disabled = !caseItem;

  const syncBtn = $("sync-chart-btn");
  if (syncBtn) {
    if (caseItem.source_patient_id) {
      syncBtn.hidden = false;
      syncBtn.textContent = `Sync to Chart (${caseItem.source_patient_id})`;
    } else {
      syncBtn.hidden = true;
    }
  }
}

async function syncToPatientChart() {
  if (!state.currentCase) return;
  const targetPid = state.currentCase.source_patient_id;
  const button = $("sync-chart-btn");
  if (button) button.disabled = true;
  try {
    const res = await api(`/api/neuroforge/cases/${encodeURIComponent(state.currentCase.case_id)}/sync-to-chart`, {
      method: "POST",
      body: JSON.stringify({ patient_id: targetPid }),
    });
    toast(`Synced conference summary to patient ${res.patient_id} chart!`);
  } catch (err) {
    toast(`Sync failed: ${err.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

const TIMELINE_VIEW_LABELS = {
  mri: "MRI",
  brain: "Brain",
  blood: "Blood",
  bone: "Bone",
};

function timelineDimension(shape) {
  if (!Array.isArray(shape) || shape.length === 0 || shape.some((value) => !Number.isFinite(Number(value)))) {
    return "";
  }
  return shape.map((value) => Math.max(0, Math.trunc(Number(value)))).join(" × ");
}

function renderVolumetricSummary(summary) {
  if (!summary || typeof summary !== "object") return "";
  const series = Array.isArray(summary.manifest?.series) ? summary.manifest.series : [];
  const sliceCount = series.reduce((total, item) => {
    const count = Number(item?.slice_count);
    return total + (Number.isFinite(count) && count >= 0 ? Math.trunc(count) : 0);
  }, 0);
  const dimensions = Object.entries(TIMELINE_VIEW_LABELS)
    .map(([key, label]) => {
      const dimension = timelineDimension(summary.view_shapes?.[key]);
      return dimension ? `${escapeHtml(label)} ${escapeHtml(dimension)}` : "";
    })
    .filter(Boolean)
    .join(" · ");
  const preprocessingVersion = summary.preprocessing && typeof summary.preprocessing === "object"
    ? summary.preprocessing.preprocessing_version
    : "";
  const state = summary.state === "local_only" || summary.state === "local_only_skipped"
    ? summary.state
    : "local_only_skipped";
  const stateLabel = state === "local_only" ? "Local only" : "Local only · skipped";
  const skipReason = state === "local_only_skipped" && ["unsupported_modality", "voxel_limit_exceeded"].includes(summary.skip_reason)
    ? summary.skip_reason === "unsupported_modality" ? "unsupported modality" : "voxel limit exceeded"
    : "";

  return `
    <div class="volumetric-summary" aria-label="Privacy-safe volumetric preparation">
      <span>${escapeHtml(summary.modality || "Unknown modality")}</span>
      <span>${escapeHtml(`${sliceCount} slices`)}</span>
      ${dimensions ? `<span>${dimensions}</span>` : ""}
      ${preprocessingVersion ? `<span>Prep ${escapeHtml(preprocessingVersion)}</span>` : ""}
      <span class="volumetric-state ${state === "local_only_skipped" ? "skipped" : ""}">${escapeHtml(stateLabel)}${skipReason ? ` · ${escapeHtml(skipReason)}` : ""}</span>
    </div>
  `;
}

function renderTimeline() {
  const studies = state.currentCase?.studies || [];
  $("timeline-count").textContent = `${studies.length} timepoint${studies.length === 1 ? "" : "s"}`;
  $("study-timeline").innerHTML = studies.length
    ? studies.map((study) => `
        <div class="timepoint">
          <strong>${escapeHtml(study.label || "Study")}</strong>
          <span>${escapeHtml(study.date_label || "")}</span>
          ${(Array.isArray(study.volumetric_summary) ? study.volumetric_summary : []).map(renderVolumetricSummary).join("")}
        </div>
      `).join("")
    : '<div class="muted">No longitudinal studies imported.</div>';
}

function renderViewer() {
  const asset = currentAsset();
  const stage = document.querySelector(".image-stage");
  const image = $("active-image");
  const presets = $("volume-presets");
  if (!asset?.preview_url && !isLibraryVolume(asset)) {
    stage.classList.remove("has-image");
    image.removeAttribute("src");
    $("image-series").textContent = "No series";
    $("image-meta").textContent = "Local preview";
    presets.hidden = true;
  } else if (isLibraryVolume(asset)) {
    const cursor = libraryCursor(asset);
    stage.classList.add("has-image");
    image.src = librarySliceSrc(asset);
    $("image-series").textContent = asset.series_label || asset.name;
    const meta = asset.metadata || {};
    $("image-meta").textContent = [
      meta.modality || "volume",
      `${cursor.index + 1} / ${asset.slice_count}`,
      `${meta.columns || asset.metadata?.columns || ""}×${meta.rows || ""}`.replace(/^×$/, ""),
      `preset ${cursor.preset}`,
      "live volume · not current care",
    ].filter(Boolean).join(" · ");
    presets.hidden = false;
    presets.querySelectorAll("[data-preset]").forEach((button) => {
      button.classList.toggle("active", button.dataset.preset === cursor.preset);
    });
  } else {
    stage.classList.add("has-image");
    image.src = asset.preview_url;
    $("image-series").textContent = asset.series_label || asset.name;
    const meta = asset.metadata || {};
    $("image-meta").textContent = [
      meta.modality || asset.kind,
      meta.rows && meta.columns ? `${meta.columns} x ${meta.rows}` : "",
      asset.kind === "dicom" ? "DICOM preview" : "local image",
    ].filter(Boolean).join(" · ");
    presets.hidden = true;
  }
  $("key-image-btn").disabled = !asset;
  $("key-image-btn").classList.toggle("active", Boolean(asset?.key_image));
  $("key-image-btn").textContent = asset?.key_image ? "Key Image Set" : "Mark Key Image";

  const assets = state.currentCase?.assets || [];
  $("key-count").textContent = `${assets.filter((item) => item.key_image).length} / ${assets.length}`;
  $("asset-strip").innerHTML = assets.length
    ? assets.map((item) => `
        <button type="button" class="asset-button${item.asset_id === asset?.asset_id ? " active" : ""}${item.key_image ? " key" : ""}" data-asset-id="${escapeHtml(item.asset_id)}">
          <img src="${escapeHtml(isLibraryVolume(item) ? librarySliceSrc(item) : (item.preview_url || ""))}" alt="" />
          <span>${escapeHtml(item.series_label || item.name)}</span>
        </button>
      `).join("")
    : '<div class="muted">Imported series and images will appear here.</div>';
}

function renderNarrative() {
  const caseItem = state.currentCase;
  if (!caseItem) return;
  $("clinical-question").value = caseItem.clinical_question || "";
  $("narrative").value = caseItem.narrative || "";
  $("limitations").value = caseItem.limitations || "";
  $("takeaways").value = caseItem.takeaways || "";
  $("save-state").textContent = `Saved ${new Date(caseItem.updated_at || Date.now()).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

function renderEvidence() {
  const items = approvedEvidence();
  $("attached-evidence-list").innerHTML = items.length
    ? items.map((item) => `
        <article class="evidence-card">
          <strong>${escapeHtml(item.title || "Untitled source")}</strong>
          <span>${escapeHtml(item.citation || item.source_id || item.source || "")}</span>
        </article>
      `).join("")
    : '<p class="muted">No reviewed evidence attached yet.</p>';
}

function renderScope() {
  const count = [
    $("attach-narrative").checked,
    $("attach-key-images").checked,
    $("attach-evidence").checked,
  ].filter(Boolean).length;
  $("scope-count").textContent = String(count);
}

function renderAgenda() {
  $("agenda-list").innerHTML = state.cases.map((item, index) => `
    <div class="agenda-item">
      <span>${index === 0 ? "12:00 PM" : `+${state.cases.slice(0, index).reduce((sum, row) => sum + Number(row.allotted_minutes || 0), 0)} min`}</span>
      <div><strong>${escapeHtml(item.title)}</strong><br/><span>${escapeHtml(item.clinical_question || "Question not entered")}</span></div>
      <span class="status-tag ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
    </div>
  `).join("");
}

function setView(view) {
  if (view === "presenter") {
    openPresenter();
    return;
  }
  state.activeView = view;
  document.querySelectorAll(".primary-nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.querySelectorAll(".view-panel").forEach((panel) => panel.classList.remove("active"));
  $(`${view}-view`)?.classList.add("active");
}

async function saveCurrentCase() {
  if (!state.currentCase) return;
  $("save-case-btn").disabled = true;
  try {
    state.currentCase = await api(`/api/neuroforge/cases/${state.currentCase.case_id}`, {
      method: "PUT",
      body: JSON.stringify({
        clinical_question: $("clinical-question").value,
        narrative: $("narrative").value,
        limitations: $("limitations").value,
        takeaways: $("takeaways").value,
      }),
    });
    const row = state.cases.find((item) => item.case_id === state.currentCase.case_id);
    if (row) {
      row.title = state.currentCase.title;
      row.clinical_question = state.currentCase.clinical_question;
      row.updated_at = state.currentCase.updated_at;
    }
    renderAll();
    toast("Conference case saved locally.");
  } catch (error) {
    toast(`Save failed: ${error.message}`);
  } finally {
    $("save-case-btn").disabled = false;
  }
}

async function selectAsset(assetId) {
  state.currentCase.active_asset_id = assetId;
  renderViewer();
  try {
    await api(`/api/neuroforge/cases/${state.currentCase.case_id}`, {
      method: "PUT",
      body: JSON.stringify({ active_asset_id: assetId }),
    });
  } catch (error) {
    toast(`Could not persist selected image: ${error.message}`);
  }
}

async function moveAsset(delta) {
  const assets = state.currentCase?.assets || [];
  if (!assets.length) return;
  const active = currentAsset();
  if (isLibraryVolume(active)) {
    const cursor = libraryCursor(active);
    const count = Number(active.slice_count || 1);
    setLibraryCursor(active, { index: (cursor.index + delta + count) % count });
    renderViewer();
    return;
  }
  const index = Math.max(0, assets.findIndex((item) => item.asset_id === active?.asset_id));
  await selectAsset(assets[(index + delta + assets.length) % assets.length].asset_id);
}

async function attachLibrarySeries() {
  const seriesId = $("library-select").value;
  if (!seriesId || !state.currentCase) {
    toast("Choose a library series first.");
    return;
  }
  $("attach-library-btn").disabled = true;
  try {
    const result = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/library-series`, {
      method: "POST",
      body: JSON.stringify({
        series_id: seriesId,
        timepoint_label: $("timepoint-label").value || "TCIA library volume",
      }),
    });
    state.currentCase = result.case;
    const row = state.cases.find((item) => item.case_id === result.case.case_id);
    if (row) row.study_count = (result.case.studies || []).length;
    renderAll();
    toast("Library volume attached. Scroll to read the stack.");
  } catch (error) {
    toast(`Library attach failed: ${error.message}`);
  } finally {
    $("attach-library-btn").disabled = false;
  }
}

async function toggleKeyImage() {
  const asset = currentAsset();
  if (!asset) return;
  try {
    state.currentCase = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/key-image`, {
      method: "POST",
      body: JSON.stringify({ asset_id: asset.asset_id, key_image: !asset.key_image, note: asset.note || "" }),
    });
    renderViewer();
    renderScope();
    toast(asset.key_image ? "Removed from key images." : "Marked as a conference key image.");
  } catch (error) {
    toast(`Key image update failed: ${error.message}`);
  }
}

async function createCase(event) {
  event.preventDefault();
  try {
    const created = await api("/api/neuroforge/cases", {
      method: "POST",
      body: JSON.stringify({
        title: $("new-case-title").value,
        clinical_question: $("new-case-question").value,
        allotted_minutes: Number($("new-case-minutes").value || 15),
      }),
    });
    state.cases.push({
      case_id: created.case_id,
      title: created.title,
      status: created.status,
      clinical_question: created.clinical_question,
      study_count: 0,
      key_image_count: 0,
      allotted_minutes: created.allotted_minutes,
      updated_at: created.updated_at,
    });
    state.currentCase = created;
    $("new-case-dialog").close();
    $("new-case-form").reset();
    renderAll();
    setView("builder");
    toast("New conference case created.");
  } catch (error) {
    toast(`Case creation failed: ${error.message}`);
  }
}

async function importStudy() {
  const files = Array.from($("study-files").files || []);
  if (!files.length) {
    toast("Choose one or more MRI, CT, DICOM, or image files.");
    return;
  }
  if (!$("deid-attestation").checked) {
    toast("Confirm the files are de-identified before import.");
    return;
  }
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  body.append("timepoint_label", $("timepoint-label").value || "Current");
  body.append("deidentified_attestation", "true");
  $("import-study-btn").disabled = true;
  $("import-status").textContent = `Importing ${files.length} file(s) locally...`;
  try {
    const result = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/studies`, { method: "POST", body });
    state.currentCase = result.case;
    const row = state.cases.find((item) => item.case_id === state.currentCase.case_id);
    if (row) row.study_count = state.currentCase.studies.length;
    renderAll();
    $("study-files").value = "";
    $("deid-attestation").checked = false;
    $("import-status").textContent = `${result.imported} imported · ${result.rejected.length} rejected`;
    toast(`Imported ${result.imported} de-identified file(s).`);
  } catch (error) {
    $("import-status").textContent = "Import stopped.";
    toast(`Import failed: ${error.message}`);
  } finally {
    $("import-study-btn").disabled = false;
  }
}

async function runAgent() {
  const button = $("run-agent-btn");
  button.disabled = true;
  $("agent-status").textContent = `${state.activeAgent} is preparing ${state.agentAction}...`;
  try {
    const result = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/agent`, {
      method: "POST",
      body: JSON.stringify({
        action: state.agentAction,
        question: `${state.activeAgent}: ${$("agent-question").value}`,
        include_active_image: $("attach-key-images").checked,
        max_tokens: Number($("agent-tokens").value || 1800),
      }),
    });
    $("agent-output").textContent = result.response;
    $("agent-status").textContent = `${result.receipt.validation} · ${result.receipt.model_status} · local receipt saved`;
  } catch (error) {
    $("agent-status").textContent = "Agent run failed.";
    toast(`Agent error: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function searchEvidence() {
  const query = $("evidence-query-main").value.trim();
  if (!query) {
    toast("Enter a literature or imaging question.");
    return;
  }
  const button = $("evidence-search-main-btn");
  const mode = $("evidence-mode-main").value;
  button.disabled = true;
  button.textContent = mode === "online" ? "Searching PubMed" : "Searching locally";
  try {
    const result = await api("/api/neuroforge/evidence/search", {
      method: "POST",
      body: JSON.stringify({ query, mode, limit: 10 }),
    });
    state.evidenceResults = result.results || [];
    renderEvidenceResults();
    toast(mode === "online"
      ? "PubMed results staged for doctor review. Only the typed query was transmitted."
      : "Local reviewed vault search complete.");
  } catch (error) {
    $("evidence-results-main").innerHTML = `<p class="muted">Search unavailable: ${escapeHtml(error.message)}</p>`;
  } finally {
    button.disabled = false;
    button.textContent = "Search";
  }
}

function renderEvidenceResults() {
  const node = $("evidence-results-main");
  node.innerHTML = state.evidenceResults.length
    ? state.evidenceResults.map((item, index) => `
      <article class="evidence-result">
        <div>
          <h3>${escapeHtml(item.title || "Untitled source")}</h3>
          <p>${escapeHtml(item.citation || item.source_id || item.source || "")}</p>
          <p>${escapeHtml(item.snippet || item.url || "")}</p>
        </div>
        <button class="secondary-button" type="button" data-approve-evidence="${index}">
          ${item.state === "online_unreviewed" ? "Review & Attach" : "Attach"}
        </button>
      </article>
    `).join("")
    : '<p class="muted">No results found. Refine the query or use the alternate source.</p>';
}

function renderSavedTopics() {
  const node = $("saved-topic-list");
  if (!node) return;
  node.innerHTML = state.savedTopics.length
    ? state.savedTopics.map((topic) => `
      <div class="saved-topic-row">
        <div>
          <strong>${escapeHtml(topic.query)}</strong>
          <span>${escapeHtml(topic.cadence)} · ${topic.last_run_at ? "last refreshed " + new Date(topic.last_run_at).toLocaleDateString() : "not refreshed yet"} · ${topic.candidate_count || 0} candidates</span>
        </div>
        <button type="button" class="secondary-button" data-refresh-topic="${escapeHtml(topic.topic_id)}">Refresh now</button>
      </div>
    `).join("")
    : '<p class="muted">Save a reusable topic query for optional weekly review.</p>';
}

function renderQuiz() {
  const quiz = state.quizSet;
  const hasQuiz = Boolean(quiz?.questions?.length);
  $("quiz-title").disabled = !hasQuiz;
  $("quiz-instructions").disabled = !hasQuiz;
  $("save-quiz-btn").disabled = !hasQuiz;
  $("approve-quiz-btn").disabled = !hasQuiz;
  $("export-quiz-btn").disabled = !hasQuiz || !quiz.questions.every((question) => question.status === "approved");
  $("quiz-title").value = quiz?.title || "";
  $("quiz-instructions").value = quiz?.instructions || "";
  if (!hasQuiz) {
    $("quiz-question-list").innerHTML = "";
    $("quiz-status").className = "quiz-status";
    $("quiz-status").textContent = "No quiz generated for this case.";
    return;
  }
  const approvedCount = quiz.questions.filter((question) => question.status === "approved").length;
  $("quiz-status").className = `quiz-status${approvedCount === quiz.questions.length ? " ready" : ""}`;
  $("quiz-status").textContent = approvedCount === quiz.questions.length
    ? `${quiz.questions.length} questions approved · Word export ready · ${quiz.generation_source || "doctor authored"}`
    : `${approvedCount} of ${quiz.questions.length} approved · physician review required before Word export · ${quiz.generation_source || "draft"}`;
  $("quiz-question-list").innerHTML = quiz.questions.map((question, index) => {
    const optionLabels = ["A", "B", "C", "D"];
    const options = question.options.map((option, optionIndex) => `
      <label>Option ${optionLabels[optionIndex]}
        <input data-option-index="${optionIndex}" value="${escapeHtml(option)}" />
      </label>
    `).join("");
    const answerOptions = optionLabels.map((label, optionIndex) =>
      `<option value="${optionIndex}"${question.correct_index === optionIndex ? " selected" : ""}>${label}</option>`
    ).join("");
    return `
      <article class="quiz-question${question.status === "approved" ? " approved" : ""}" data-question-index="${index}" data-question-id="${escapeHtml(question.question_id)}">
        <div class="quiz-question-header">
          <strong>Question ${index + 1}</strong>
          <span>${escapeHtml(question.status)} · ${escapeHtml(question.difficulty)}</span>
        </div>
        <div class="quiz-question-body">
          <label>Question stem<textarea data-field="prompt" rows="3">${escapeHtml(question.prompt)}</textarea></label>
          <div class="quiz-options">${options}</div>
          <div class="quiz-review-grid">
            <label>Correct answer<select data-field="correct_index">${answerOptions}</select></label>
            <label>Difficulty
              <select data-field="difficulty">
                <option value="introductory"${question.difficulty === "introductory" ? " selected" : ""}>Introductory</option>
                <option value="moderate"${question.difficulty === "moderate" ? " selected" : ""}>Moderate</option>
                <option value="advanced"${question.difficulty === "advanced" ? " selected" : ""}>Advanced</option>
              </select>
            </label>
            <label>Review state
              <select data-field="status">
                <option value="draft"${question.status === "draft" ? " selected" : ""}>Draft</option>
                <option value="approved"${question.status === "approved" ? " selected" : ""}>Approved</option>
              </select>
            </label>
          </div>
          <label>Answer explanation<textarea data-field="explanation" rows="3">${escapeHtml(question.explanation)}</textarea></label>
          <label>Teaching point<textarea data-field="teaching_point" rows="2">${escapeHtml(question.teaching_point || "")}</textarea></label>
        </div>
      </article>
    `;
  }).join("");
}

function renderStress() {
  const stress = state.currentCase?.stress_test || {};
  const observations = stress.observations || [];
  $("stress-gate").textContent = observations.length ? `${observations.length} note${observations.length === 1 ? "" : "s"}` : "Optional notes";
  $("stress-gate").className = `status-tag ${observations.length ? "ready" : "draft"}`;
  $("stress-obs-count").textContent = `${observations.length} recorded`;
  $("stress-obs-list").innerHTML = observations.map((item) => `
    <div class="stress-obs-row">
      <div>
        <strong>${escapeHtml(item.finding)}</strong>
        <div class="muted">${escapeHtml(item.location || "Location not specified")}</div>
      </div>
      <button type="button" class="text-button" data-remove-observation="${escapeHtml(item.observation_id)}">Remove</button>
    </div>
  `).join("") || `<p class="muted">No observations recorded. You can still commit a conference move.</p>`;
  if (stress.hypothesis) $("stress-hypothesis").value = stress.hypothesis;
  if (stress.conference_move) $("stress-move").value = stress.conference_move;
  if (Number.isInteger(stress.confidence)) $("stress-confidence").value = String(stress.confidence);
  $("stress-commit-btn").disabled = false;
  if (!stress.committed_at) {
    $("stress-status").textContent = observations.length
      ? "Notes on the record. Commit when the conference move is ready."
      : "No commit yet. Commit whenever the conference move is ready.";
  } else {
    $("stress-status").textContent = "Committed locally. Challenge can press the write-up; it does not invent findings.";
  }
  const debrief = stress.last_debrief;
  const box = $("stress-debrief");
  if (!debrief) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("stress-compare").innerHTML = `
    <li>${debrief.matched ? "First try matched the conference path." : `You pinned ${escapeHtml(debrief.first_try_choice)}; conference path: ${escapeHtml(debrief.expert_hypothesis)}`}</li>
  `;
  $("stress-modes").innerHTML = (debrief.error_modes || []).map((item) =>
    `<span class="error-mode-tag">${escapeHtml(item.error_mode_label)}</span>`
  ).join("") || `<p class="muted">No named miss on this commit.</p>`;
  $("stress-expert").textContent = `Conference move: ${debrief.expert_move}${debrief.overconfident ? " High-confidence miss recorded." : ""}`;
}

async function addStressObservation() {
  const finding = $("stress-finding").value.trim();
  if (!finding || !state.currentCase) {
    toast("Record what the image shows before adding it.");
    return;
  }
  try {
    const result = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/stress/observations`, {
      method: "POST",
      body: JSON.stringify({ finding, location: $("stress-location").value.trim() }),
    });
    state.currentCase = result.case;
    $("stress-finding").value = "";
    $("stress-location").value = "";
    renderAll();
    toast("Observation recorded locally.");
  } catch (error) {
    toast(`Observation failed: ${error.message}`);
  }
}

async function removeStressObservation(observationId) {
  if (!state.currentCase || !observationId) return;
  try {
    state.currentCase = await api(
      `/api/neuroforge/cases/${state.currentCase.case_id}/stress/observations/${encodeURIComponent(observationId)}`,
      { method: "DELETE" },
    );
    renderAll();
  } catch (error) {
    toast(`Could not remove observation: ${error.message}`);
  }
}

async function commitStressTest() {
  if (!state.currentCase) return;
  try {
    const result = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/stress/commit`, {
      method: "POST",
      body: JSON.stringify({
        hypothesis: $("stress-hypothesis").value.trim(),
        conference_move: $("stress-move").value.trim(),
        confidence: Number($("stress-confidence").value || 0),
      }),
    });
    state.currentCase = result.case;
    renderAll();
    toast(result.debrief?.matched ? "Commit matched the conference path." : "Commit recorded with named misses.");
  } catch (error) {
    toast(`Commit failed: ${error.message}`);
  }
}

function collectQuizFromEditor() {
  if (!state.quizSet) return null;
  const questions = Array.from(document.querySelectorAll(".quiz-question")).map((node) => ({
    question_id: node.dataset.questionId,
    prompt: node.querySelector('[data-field="prompt"]').value.trim(),
    options: Array.from(node.querySelectorAll("[data-option-index]")).map((input) => input.value.trim()),
    correct_index: Number(node.querySelector('[data-field="correct_index"]').value),
    explanation: node.querySelector('[data-field="explanation"]').value.trim(),
    teaching_point: node.querySelector('[data-field="teaching_point"]').value.trim(),
    difficulty: node.querySelector('[data-field="difficulty"]').value,
    asset_id: state.quizSet.questions[Number(node.dataset.questionIndex)]?.asset_id || "",
    status: node.querySelector('[data-field="status"]').value,
  }));
  return {
    quiz_id: state.quizSet.quiz_id,
    title: $("quiz-title").value.trim(),
    audience: state.quizSet.audience,
    topic: state.quizSet.topic,
    instructions: $("quiz-instructions").value.trim(),
    questions,
    generation_source: state.quizSet.generation_source || "doctor_authored",
  };
}

async function generateQuiz() {
  if (!state.currentCase) return;
  const button = $("generate-quiz-btn");
  button.disabled = true;
  $("quiz-status").className = "quiz-status";
  $("quiz-status").textContent = "Local agents are drafting questions. Physician review will still be required.";
  try {
    const result = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/quiz/generate`, {
      method: "POST",
      body: JSON.stringify({
        audience: $("quiz-audience").value,
        topic: $("quiz-topic").value.trim(),
        count: Number($("quiz-count").value || 5),
        include_key_image: $("quiz-include-image").checked,
        max_tokens: 2400,
      }),
    });
    state.quizSet = result.quiz;
    state.currentCase.quiz_sets = [...(state.currentCase.quiz_sets || []), result.quiz];
    renderQuiz();
    toast("Quiz draft generated locally. Review each answer and explanation.");
  } catch (error) {
    $("quiz-status").textContent = `Quiz generation failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function saveQuiz({ approveAll = false } = {}) {
  const payload = collectQuizFromEditor();
  if (!payload) return false;
  if (approveAll) payload.questions.forEach((question) => { question.status = "approved"; });
  try {
    const result = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/quiz/${state.quizSet.quiz_id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    state.quizSet = result.quiz;
    const quizzes = state.currentCase.quiz_sets || [];
    const index = quizzes.findIndex((item) => item.quiz_id === result.quiz.quiz_id);
    if (index >= 0) quizzes[index] = result.quiz;
    renderQuiz();
    toast(approveAll ? "All questions approved. Word export is ready." : "Quiz draft saved locally.");
    return true;
  } catch (error) {
    toast(`Quiz save failed: ${error.message}`);
    return false;
  }
}

async function exportQuizWord() {
  const saved = await saveQuiz();
  if (!saved || !state.quizSet.questions.every((question) => question.status === "approved")) {
    toast("Approve every question before exporting the finished Word copy.");
    return;
  }
  const link = document.createElement("a");
  link.href = `/api/neuroforge/cases/${state.currentCase.case_id}/quiz/${state.quizSet.quiz_id}/export.docx`;
  link.download = "";
  document.body.append(link);
  link.click();
  link.remove();
  toast("Finished Word quiz and answer key generated.");
}

async function saveCurrentTopic() {
  const query = $("evidence-query-main").value.trim();
  if (!query) {
    toast("Enter a topic before saving it.");
    return;
  }
  try {
    const topic = await api("/api/neuroforge/topics", {
      method: "POST",
      body: JSON.stringify({ query, cadence: "weekly" }),
    });
    state.savedTopics.push(topic);
    renderSavedTopics();
    toast("Weekly topic saved. It refreshes only when requested or during an enabled weekly review.");
  } catch (error) {
    toast(`Could not save topic: ${error.message}`);
  }
}

async function refreshSavedTopic(topicId) {
  const button = document.querySelector(`[data-refresh-topic="${CSS.escape(topicId)}"]`);
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/neuroforge/topics/${topicId}/refresh`, { method: "POST" });
    const index = state.savedTopics.findIndex((item) => item.topic_id === topicId);
    if (index >= 0) state.savedTopics[index] = result.topic;
    state.evidenceResults = result.results || [];
    renderSavedTopics();
    renderEvidenceResults();
    toast("Saved topic refreshed. Results remain unreviewed until attached.");
  } catch (error) {
    toast(`Topic refresh failed: ${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

async function approveEvidence(index) {
  const item = state.evidenceResults[index];
  if (!item) return;
  try {
    state.currentCase = await api(`/api/neuroforge/cases/${state.currentCase.case_id}/evidence`, {
      method: "POST",
      body: JSON.stringify({ evidence: item, decision: "doctor_approved" }),
    });
    renderEvidence();
    renderScope();
    toast("Evidence attached as doctor approved.");
  } catch (error) {
    toast(`Evidence approval failed: ${error.message}`);
  }
}

function buildPresenterSlides() {
  const caseItem = state.currentCase;
  const keyImages = (caseItem?.assets || []).filter((item) => item.key_image);
  const evidence = approvedEvidence();
  const slides = [
    { type: "copy", heading: caseItem.title, subheading: "Clinical question", body: caseItem.clinical_question || "Not entered." },
    { type: "copy", heading: "Longitudinal Studies", subheading: `${(caseItem.studies || []).length} timepoints`, body: (caseItem.studies || []).map((item) => `${item.label}: ${item.date_label || ""}`).join("\n") || "No timepoints imported." },
    ...keyImages.map((item) => ({ type: "image", heading: item.series_label || item.name, body: item.note || "Selected de-identified key image.", image: isLibraryVolume(item) ? librarySliceSrc(item) : item.preview_url })),
    { type: "copy", heading: "Limitations", subheading: "State these before narrowing the discussion", body: caseItem.limitations || "No limitations documented." },
    { type: "copy", heading: "Reviewed Evidence", subheading: `${evidence.length} approved source${evidence.length === 1 ? "" : "s"}`, body: evidence.map((item) => `${item.title}\n${item.citation || item.source_id || ""}`).join("\n\n") || "No reviewed evidence attached." },
    { type: "copy", heading: "Conference Narrative", subheading: "Discussion", body: caseItem.narrative || "No narrative entered." },
    { type: "copy", heading: "Takeaways", subheading: "Close with what is known, uncertain, and next", body: caseItem.takeaways || "No takeaways entered." },
  ];
  state.presenterSlides = slides;
  state.presenterIndex = 0;
}

function openPresenter() {
  if (!state.currentCase) return;
  buildPresenterSlides();
  $("presenter-overlay").hidden = false;
  $("presenter-pdf-link").href = `/api/neuroforge/cases/${state.currentCase.case_id}/export.pdf`;
  renderPresenterSlide();
}

function renderPresenterSlide() {
  const slide = state.presenterSlides[state.presenterIndex];
  if (!slide) return;
  $("presenter-position").textContent = `${state.presenterIndex + 1} / ${state.presenterSlides.length}`;
  $("presenter-slide").innerHTML = slide.type === "image"
    ? `
      <div class="presentation-image">
        <img src="${escapeHtml(slide.image)}" alt="" />
        <aside><h2>${escapeHtml(slide.heading)}</h2><p>${escapeHtml(slide.body)}</p></aside>
      </div>
    `
    : `
      <div class="presentation-copy">
        <h2>${escapeHtml(slide.subheading || "")}</h2>
        <h1>${escapeHtml(slide.heading)}</h1>
        <p>${escapeHtml(slide.body)}</p>
      </div>
    `;
}

function movePresenter(delta) {
  state.presenterIndex = Math.max(0, Math.min(state.presenterSlides.length - 1, state.presenterIndex + delta));
  renderPresenterSlide();
}

document.addEventListener("click", (event) => {
  const caseButton = event.target.closest("[data-case-id]");
  if (caseButton) loadCase(caseButton.dataset.caseId);
  const assetButton = event.target.closest("[data-asset-id]");
  if (assetButton) selectAsset(assetButton.dataset.assetId);
  const evidenceButton = event.target.closest("[data-approve-evidence]");
  if (evidenceButton) approveEvidence(Number(evidenceButton.dataset.approveEvidence));
  const topicButton = event.target.closest("[data-refresh-topic]");
  if (topicButton) refreshSavedTopic(topicButton.dataset.refreshTopic);
  const removeObservation = event.target.closest("[data-remove-observation]");
  if (removeObservation) removeStressObservation(removeObservation.dataset.removeObservation);
});

document.querySelectorAll(".primary-nav button").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});
$("case-search").addEventListener("input", renderCaseList);
$("new-case-btn").addEventListener("click", () => $("new-case-dialog").showModal());
$("close-dialog-btn").addEventListener("click", () => $("new-case-dialog").close());
$("new-case-form").addEventListener("submit", createCase);
$("save-case-btn").addEventListener("click", saveCurrentCase);
$("sync-chart-btn")?.addEventListener("click", syncToPatientChart);
$("import-study-btn").addEventListener("click", importStudy);
$("attach-library-btn").addEventListener("click", attachLibrarySeries);
$("previous-image-btn").addEventListener("click", () => moveAsset(-1));
$("next-image-btn").addEventListener("click", () => moveAsset(1));
$("volume-presets").addEventListener("click", (event) => {
  const button = event.target.closest("[data-preset]");
  const asset = currentAsset();
  if (!button || !isLibraryVolume(asset)) return;
  setLibraryCursor(asset, { preset: button.dataset.preset });
  renderViewer();
});
document.querySelector(".image-stage").addEventListener("wheel", (event) => {
  if (!isLibraryVolume(currentAsset())) return;
  event.preventDefault();
  moveAsset(event.deltaY > 0 ? 1 : -1);
}, { passive: false });
$("key-image-btn").addEventListener("click", toggleKeyImage);
$("present-btn").addEventListener("click", openPresenter);
$("export-pdf-btn").addEventListener("click", () => {
  if (state.currentCase) window.open(`/api/neuroforge/cases/${state.currentCase.case_id}/export.pdf`, "_blank");
});
$("open-evidence-btn").addEventListener("click", () => setView("evidence"));
$("evidence-search-main-btn").addEventListener("click", searchEvidence);
$("save-topic-btn").addEventListener("click", saveCurrentTopic);
$("generate-quiz-btn").addEventListener("click", generateQuiz);
$("stress-add-obs-btn").addEventListener("click", addStressObservation);
$("stress-commit-btn").addEventListener("click", commitStressTest);
$("save-quiz-btn").addEventListener("click", () => saveQuiz());
$("approve-quiz-btn").addEventListener("click", () => saveQuiz({ approveAll: true }));
$("export-quiz-btn").addEventListener("click", exportQuizWord);
$("evidence-query-main").addEventListener("keydown", (event) => {
  if (event.key === "Enter") searchEvidence();
});
$("run-agent-btn").addEventListener("click", runAgent);
document.querySelectorAll(".agent-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    state.activeAgent = button.dataset.agent;
    document.querySelectorAll(".agent-tabs button").forEach((item) => item.classList.toggle("active", item === button));
  });
});
document.querySelectorAll(".agent-action").forEach((button) => {
  button.addEventListener("click", () => {
    state.agentAction = button.dataset.action;
    document.querySelectorAll(".agent-action").forEach((item) => item.classList.toggle("active", item === button));
  });
});
["attach-narrative", "attach-key-images", "attach-evidence"].forEach((id) => $(id).addEventListener("change", renderScope));
$("close-presenter-btn").addEventListener("click", () => { $("presenter-overlay").hidden = true; });
$("presenter-prev-btn").addEventListener("click", () => movePresenter(-1));
$("presenter-next-btn").addEventListener("click", () => movePresenter(1));
document.addEventListener("keydown", (event) => {
  if (!$("presenter-overlay").hidden) {
    if (event.key === "ArrowRight") movePresenter(1);
    if (event.key === "ArrowLeft") movePresenter(-1);
    if (event.key === "Escape") $("presenter-overlay").hidden = true;
    return;
  }
  if (event.key === "ArrowRight" || event.key === "ArrowDown") moveAsset(1);
  if (event.key === "ArrowLeft" || event.key === "ArrowUp") moveAsset(-1);
  if (event.key.toLowerCase() === "k") toggleKeyImage();
  if (event.key.toLowerCase() === "p") openPresenter();
});

loadBootstrap().catch((error) => {
  toast(`NeuroForge failed to load: ${error.message}`);
});
