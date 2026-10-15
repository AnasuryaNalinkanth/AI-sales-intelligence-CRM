import {
  askAnalytics,
  createAgentTask,
  getAgentTasks,
  getAnalytics,
  proposeRecommendationAction,
  requireAuth,
} from "./api.js";
import { displayValue, element, formatNumber, humanize, mountAuthControls } from "./common.js";

const filterForm = document.querySelector("#analytics-filters");
const status = document.querySelector("#analytics-status");
const kpis = document.querySelector("#analytics-kpis");
const errorMessage = document.querySelector("#analytics-error");
const totalValue = document.querySelector("#total-enquiry-value");
const chartTargets = {
  customerStages: document.querySelector("#chart-customer-stages"),
  enquiryStatus: document.querySelector("#chart-enquiry-status"),
  enquiryPriority: document.querySelector("#chart-enquiry-priority"),
  leadQualification: document.querySelector("#chart-lead-qualification"),
  leadOutcome: document.querySelector("#chart-lead-outcome"),
  pipeline: document.querySelector("#chart-pipeline"),
  activity: document.querySelector("#chart-activity"),
  followups: document.querySelector("#chart-followups"),
};

function numberValue(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function renderBars(target, records, valueKey = "count") {
  target.replaceChildren();
  const values = Array.isArray(records)
    ? records.filter((record) => record && record.label !== undefined)
    : [];
  if (values.length === 0) {
    target.append(element("p", "chart-empty", "No data available for this period."));
    return;
  }
  const max = Math.max(...values.map((item) => numberValue(item[valueKey])), 0);
  const list = element("ul", "chart-bars");
  for (const item of values) {
    const row = element("li", "chart-bar-row");
    const label = element("span", "chart-label", humanize(item.label));
    const track = element("span", "chart-track");
    const bar = element("span", "chart-bar");
    const value = numberValue(item[valueKey]);
    bar.style.width = `${max > 0 ? (value / max) * 100 : 0}%`;
    track.append(bar);
    row.append(label, track, element("strong", "chart-value", formatNumber(item[valueKey], "0")));
    list.append(row);
  }
  target.append(list);
}

function renderActivity(target, records) {
  target.replaceChildren();
  const values = Array.isArray(records) ? records : [];
  if (values.length === 0) {
    target.append(element("p", "chart-empty", "No data available for this period."));
    return;
  }
  const max = Math.max(
    ...values.map((item) => numberValue(item.meetings) + numberValue(item.calls) + numberValue(item.followups)),
    0,
  );
  const list = element("ul", "activity-bars");
  for (const item of values) {
    const total = numberValue(item.meetings) + numberValue(item.calls) + numberValue(item.followups);
    const row = element("li", "activity-bar-row");
    row.append(element("span", "chart-label", displayValue(item.period)));
    const segments = element("span", "activity-segments");
    for (const [kind, className] of [
      ["meetings", "segment-meetings"],
      ["calls", "segment-calls"],
      ["followups", "segment-followups"],
    ]) {
      const count = numberValue(item[kind]);
      const segment = element("span", `activity-segment ${className}`);
      segment.style.width = `${max > 0 ? (count / max) * 100 : 0}%`;
      segment.title = `${humanize(kind)}: ${count}`;
      segments.append(segment);
    }
    row.append(segments, element("strong", "chart-value", String(total)));
    list.append(row);
  }
  target.append(list);
}

function setKpis(overview) {
  for (const [name, value] of Object.entries(overview ?? {})) {
    const target = kpis.querySelector(`[data-metric="${name}"]`);
    if (target) target.textContent = String(numberValue(value));
  }
  kpis.hidden = false;
}

function queryParams() {
  const formData = new FormData(filterForm);
  return {
    start_date: formData.get("start_date"),
    end_date: formData.get("end_date"),
    grain: formData.get("grain") || "monthly",
  };
}

async function loadAnalytics() {
  if (!requireAuth()) return;
  const params = queryParams();
  status.hidden = false;
  status.textContent = "Loading analytics...";
  errorMessage.hidden = true;
  kpis.hidden = true;
  for (const target of Object.values(chartTargets)) target.replaceChildren();
  try {
    const [overview, customers, enquiries, activities, pipeline, followups] = await Promise.all([
      getAnalytics("overview", params),
      getAnalytics("customers", params),
      getAnalytics("enquiries", params),
      getAnalytics("activities", params),
      getAnalytics("pipeline", params),
      getAnalytics("followups", params),
    ]);
    setKpis(overview);
    renderBars(chartTargets.customerStages, customers.by_sales_stage);
    renderBars(chartTargets.enquiryStatus, enquiries.by_status);
    renderBars(chartTargets.enquiryPriority, enquiries.by_priority);
    renderBars(chartTargets.leadQualification, enquiries.by_qualification);
    renderBars(chartTargets.leadOutcome, enquiries.by_outcome);
    const winRate = enquiries.win_rate_percent;
    document.querySelector("#win-rate").textContent = winRate == null ? "–" : `${formatNumber(winRate, "0")}%`;
    document.querySelector("#win-rate-note").textContent = winRate == null
      ? displayValue(enquiries.win_rate_unavailable_reason, "")
      : `Win rate: ${formatNumber(winRate, "0")}% of closed opportunities.`;
    renderBars(chartTargets.pipeline, pipeline.by_sales_stage, "value");
    renderActivity(chartTargets.activity, activities.over_time);
    renderBars(chartTargets.followups, followups.by_status);
    totalValue.textContent = formatNumber(enquiries.total_estimated_value, "0");
    document.querySelector("#followup-due-summary").textContent =
      `Due today: ${numberValue(followups.due_today)} · Due in the next 7 days: ${numberValue(followups.due_next_7_days)}`;
    status.hidden = true;
  } catch {
    status.hidden = true;
    kpis.hidden = true;
    errorMessage.hidden = false;
    errorMessage.textContent = "Unable to load analytics.";
  }
}

const INSIGHT_PAGE_SIZE = 10;
const INSIGHT_CATEGORIES = {
  overdue_followup: "Overdue follow-ups",
  stalled_deal: "Stalled deals",
  neglected_lead: "Neglected leads",
  needs_attention: "Needs attention",
};
const EVIDENCE_KINDS = { fact: "Fact", estimate: "Estimate", missing_data: "Missing data" };
const insightFilters = document.querySelector("#insights-filters");
const insightCounts = document.querySelector("#insights-counts");
const insightStatus = document.querySelector("#insights-status");
const insightList = document.querySelector("#insights-list");
const insightPagination = document.querySelector("#insights-pagination");
let insightPage = 1;

// Links always open the record's own customer page; anything else is not rendered.
function safeRecordHref(href) {
  return typeof href === "string" && /^\/customer\?customer_id=\d+(#(enquiry|followup|meeting)-\d+)?$/.test(href)
    ? href
    : null;
}

function renderInsightCounts(counts, selected) {
  insightCounts.replaceChildren();
  for (const [category, label] of Object.entries(INSIGHT_CATEGORIES)) {
    const button = element("button", "insights-count");
    button.type = "button";
    button.setAttribute("aria-pressed", String(selected === category));
    button.append(element("strong", "", String(numberValue(counts?.[category]))), document.createTextNode(label));
    button.addEventListener("click", () => {
      insightFilters.elements.category.value = selected === category ? "" : category;
      insightPage = 1;
      void loadInsights();
    });
    insightCounts.append(button);
  }
}

// ---------- AI proposals and their real task state (existing HITL task API) ----------

const timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const PROPOSAL_ACTIONS = {
  complete_followup: "Mark the overdue follow-up as completed",
  create_followup: "Create a follow-up",
  schedule_call: "Schedule a call",
  create_meeting: "Propose a meeting",
};
const TASK_STATES = {
  collecting_information: "Needs details",
  ready_for_review: "Ready for review",
  awaiting_approval: "Pending approval",
  executing: "Executing",
  completed: "Completed",
  rejected: "Rejected",
  cancelled: "Cancelled",
  failed: "Failed",
  expired: "Expired",
};
const OPEN_TASK_STATES = new Set(["collecting_information", "ready_for_review", "awaiting_approval", "executing"]);
let recommendationTasks = null; // null: task states could not be loaded.

function taskForInsight(item) {
  // Tasks are newest first; the newest one acting on this recommendation's records is its state.
  return (recommendationTasks ?? []).find((task) => (task.operations ?? []).some((operation) => {
    const values = operation.values ?? {};
    if (!(operation.action in PROPOSAL_ACTIONS) || Number(values.customer_id) !== item.customer_id) return false;
    return operation.action === "complete_followup"
      ? Number(values.followup_id) === item.followup_id
      : (values.enquiry_id ?? null) === item.enquiry_id;
  }));
}

function renderTaskState(target, task, customerId) {
  target.replaceChildren();
  if (!task) return;
  target.append(element("strong", "", "Task: "), document.createTextNode(TASK_STATES[task.status] ?? humanize(task.status)));
  if (OPEN_TASK_STATES.has(task.status)) {
    const link = element("a", "", "Review in Approvals");
    link.href = `/customer?customer_id=${encodeURIComponent(customerId)}#approvals`;
    target.append(document.createTextNode(" · "), link);
  }
}

function policyNote(policy) {
  if (policy === "disabled") return "This action is disabled by your organization's policy, so it cannot be submitted.";
  if (policy === "automatic") return "Your organization's policy runs this action automatically once its details are complete.";
  return "Requires your approval in Approvals before anything changes in the CRM.";
}

function renderProposal(box, item, proposal, taskState) {
  box.replaceChildren(
    element("p", "", `AI explanation: ${proposal.explanation}`),
    element("p", "", `AI suggested next step: ${proposal.next_step}`),
  );
  const action = proposal.proposed_action;
  if (!action) {
    box.append(element("p", "insight-ai-note", "No CRM action is proposed for this recommendation."));
    return;
  }
  const text = ["type", "call_type", "agenda", "description", "notes"]
    .filter((field) => action.values[field])
    .map((field) => `${humanize(field)}: ${action.values[field]}`);
  box.append(element("p", "", `Proposed action: ${PROPOSAL_ACTIONS[action.action] ?? humanize(action.action)}${text.length ? ` (${text.join("; ")})` : ""}.`));
  if (action.user_must_provide.length) {
    box.append(element("p", "insight-ai-note", `You provide: ${action.user_must_provide.map((field) => humanize(field).toLowerCase()).join(", ")}.`));
  }
  box.append(element("p", "insight-ai-note", policyNote(action.policy)));
  const existing = taskForInsight(item);
  if (action.policy === "disabled" || (existing && OPEN_TASK_STATES.has(existing.status))) return;
  const status = element("p", "insight-ai-note");
  status.setAttribute("role", "status");
  const submit = element("button", "button button-secondary button-sm", "Prepare task for review");
  submit.type = "button";
  submit.addEventListener("click", async () => {
    submit.disabled = true;
    const values = { ...action.values };
    if (action.timezone_field) values[action.timezone_field] = timezoneName;
    try {
      const task = await createAgentTask([{ action: action.action, values }]);
      recommendationTasks = [task, ...(recommendationTasks ?? [])];
      renderTaskState(taskState, task, item.customer_id);
      submit.remove();
      status.textContent = "";
    } catch (error) {
      submit.disabled = false;
      status.textContent = `The task could not be prepared. ${error?.message || ""}`.trim();
    }
  });
  box.append(submit, status);
}

function renderAiArea(item, taskState) {
  const box = element("div", "insight-ai");
  const button = element("button", "button button-secondary button-sm", "Get AI proposal");
  button.type = "button";
  const status = element("p", "insight-ai-note");
  status.setAttribute("role", "status");
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "Asking the AI… The rule-based recommendation above stays as it is.";
    try {
      renderProposal(box, item, await proposeRecommendationAction(item.recommendation_id), taskState);
    } catch (error) {
      button.disabled = false;
      status.textContent = `AI proposal unavailable: ${error?.message || "please try again"}. The rule-based recommendation is unchanged.`;
    }
  });
  box.append(button, status);
  return box;
}

function renderInsight(item) {
  const card = element("li", "insight-card");
  card.dataset.urgency = item.urgency;
  const top = element("div", "insight-top");
  const badges = element("div", "insight-badges");
  for (const [label, level] of [[`${humanize(item.urgency)} urgency`, item.urgency], [`${humanize(item.potential_impact)} impact`, item.potential_impact]]) {
    const badge = element("span", "insight-badge", label);
    badge.dataset.level = level;
    badges.append(badge);
  }
  badges.append(
    element("span", "insight-badge", `Urgency ${numberValue(item.urgency_score)}`),
    element("span", "insight-badge", `Impact ${numberValue(item.impact_score)}`),
    element("span", "insight-badge", `Score ${numberValue(item.priority_score)}/100`),
  );
  top.append(element("h3", "insight-title", item.title), badges);
  card.append(
    top,
    element("p", "insight-summary", `${INSIGHT_CATEGORIES[item.category] ?? humanize(item.category)} · ${item.summary}`),
    element("p", "insight-reason", item.reason),
  );
  const action = element("p", "insight-action");
  action.append(element("strong", "", "Suggested next step: "), document.createTextNode(item.recommended_next_action));
  card.append(action);
  const taskState = element("p", "insight-task");
  renderTaskState(taskState, taskForInsight(item), item.customer_id);
  card.append(taskState, renderAiArea(item, taskState));
  const evidence = element("details", "insight-evidence");
  evidence.append(element("summary", "", `Evidence (${item.evidence.length})`));
  const evidenceList = element("ul");
  for (const fact of item.evidence) {
    const entry = element("li");
    entry.dataset.kind = fact.kind;
    entry.append(element("span", "insight-evidence-kind", `${EVIDENCE_KINDS[fact.kind] ?? fact.kind}: `), document.createTextNode(fact.detail));
    evidenceList.append(entry);
  }
  evidence.append(evidenceList);
  card.append(evidence);
  const links = element("nav", "insight-links");
  links.setAttribute("aria-label", `Records for ${item.title}`);
  for (const link of item.links) {
    const href = safeRecordHref(link.href);
    if (!href) continue;
    const anchor = element("a", "", link.label);
    anchor.href = href;
    links.append(anchor);
  }
  card.append(links);
  return card;
}

async function loadInsights() {
  if (!requireAuth()) return;
  const formData = new FormData(insightFilters);
  const category = formData.get("category") || "";
  insightStatus.hidden = false;
  insightStatus.classList.remove("analytics-error");
  insightStatus.textContent = "Loading recommendations...";
  insightList.replaceChildren();
  insightPagination.hidden = true;
  try {
    // Task states are optional: the recommendations render without them.
    const tasks = getAgentTasks().catch(() => null);
    const result = await getAnalytics("proactive-insights", {
      category,
      urgency: formData.get("urgency"),
      page: insightPage,
      page_size: INSIGHT_PAGE_SIZE,
    });
    recommendationTasks = await tasks;
    renderInsightCounts(result.counts_by_category, category);
    for (const item of result.items) insightList.append(renderInsight(item));
    if (result.total === 0) {
      insightStatus.textContent = category || formData.get("urgency")
        ? "No recommendations match these filters."
        : "Nothing needs attention right now: no overdue follow-ups, neglected leads, stalled deals or upcoming meetings to prepare.";
    } else {
      insightStatus.hidden = true;
    }
    const pages = Math.ceil(result.total / INSIGHT_PAGE_SIZE);
    insightPagination.hidden = pages <= 1;
    document.querySelector("#insights-page").textContent = `Page ${insightPage} of ${pages}`;
    document.querySelector("#insights-previous").disabled = insightPage <= 1;
    document.querySelector("#insights-next").disabled = insightPage >= pages;
  } catch {
    // Never show stale or partial recommendations as if they were current.
    insightCounts.replaceChildren();
    insightStatus.classList.add("analytics-error");
    insightStatus.textContent = "Unable to load recommendations. Try Refresh.";
  }
}

insightFilters.addEventListener("change", () => {
  insightPage = 1;
  void loadInsights();
});
document.querySelector("#insights-refresh").addEventListener("click", () => void loadInsights());
document.querySelector("#insights-previous").addEventListener("click", () => {
  insightPage = Math.max(1, insightPage - 1);
  void loadInsights();
});
document.querySelector("#insights-next").addEventListener("click", () => {
  insightPage += 1;
  void loadInsights();
});

// ---------- Ask a question (POST /analytics/ask); independent of the date filters ----------

const askForm = document.querySelector("#ask-form");
const askInput = document.querySelector("#ask-question");
const askButton = document.querySelector("#ask-submit");
const askStatus = document.querySelector("#ask-status");
const askError = document.querySelector("#ask-error");
const askResult = document.querySelector("#ask-result");
const askChart = document.querySelector("#ask-chart");
const GRAIN_LABELS = { daily: "day", weekly: "week", monthly: "month" };
let askPending = false;

function askPeriod(plan, allTime) {
  if (allTime) return "All time (dates do not apply)";
  const { start_date: start, end_date: end } = plan;
  const range = start && end ? `${start} to ${end}` : start ? `since ${start}` : end ? `up to ${end}` : "All records";
  if (plan.month) {
    const month = new Date(Date.UTC(2000, plan.month - 1, 1)).toLocaleString(undefined, { month: "long", timeZone: "UTC" });
    return `${month}: ${range}`;
  }
  if (plan.last_week) return `Last week: ${range}`;
  if (plan.last_n_days) return `Last ${plan.last_n_days} ${plan.last_n_days === 1 ? "day" : "days"}: ${range}`;
  return range;
}

function renderAskChart(result, grain, target = askChart) {
  target.replaceChildren();
  if (result.kind === "overview") {
    // One block per approved metric; each summary states its own dates and calculation.
    for (const section of result.sections ?? []) {
      const block = element("section", "ask-section");
      const chart = element("div");
      renderAskChart(section.result, section.result.kind.endsWith("series") ? "monthly" : null, chart);
      block.append(element("h3", null, humanize(section.operation)), element("p", "ask-summary", section.summary), chart);
      target.append(block);
    }
    return;
  }
  const items = Array.isArray(result.items) ? result.items : [];
  if (["number", "amount", "percent"].includes(result.kind)) {
    if (result.value === null || result.value === undefined) {
      target.append(element("p", "chart-empty", "Not available for this period."));
    } else {
      const suffix = result.kind === "percent" ? "%" : "";
      target.append(element("strong", "ask-figure", `${formatNumber(result.value, "0")}${suffix}`));
    }
  } else if (result.kind === "activity_series") {
    const legend = element("div", "chart-legend");
    legend.append(element("span", null, "Meetings"), element("span", null, "Calls"), element("span", null, "Follow-ups"));
    const chart = element("div");
    renderActivity(chart, items);
    target.append(legend, chart);
  } else if (result.kind === "time_series") {
    renderBars(target, items.map((item) => ({ label: item.period, count: item.count })));
  } else {
    // Value-bearing distributions (estimated value by stage/status) chart the value.
    renderBars(target, items, items.some((item) => item.value != null) ? "value" : "count");
  }
  if (grain) target.append(element("p", "chart-footnote", `Grouped by ${GRAIN_LABELS[grain] ?? grain}.`));
}

function renderAskAnswer(answer) {
  document.querySelector("#ask-summary").textContent = displayValue(answer.summary, "");
  const meta = document.querySelector("#ask-interpretation");
  meta.replaceChildren();
  askChart.replaceChildren();
  const plan = answer.interpretation ?? {};
  if (answer.status === "answered" && answer.result) {
    meta.append(
      element("span", null, `Report: ${humanize(plan.operation)}`),
      element("span", null, `Period: ${askPeriod(plan, answer.result.all_time)}`),
    );
    if (answer.result.all_time) meta.append(element("span", "insight-badge", "All time"));
    renderAskChart(answer.result, plan.grain);
  }
  askResult.hidden = false;
}

askForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (askPending || !requireAuth()) return;
  const question = askInput.value.trim();
  askError.hidden = true;
  askInput.removeAttribute("aria-invalid");
  if (question.length < 3) {
    askInput.setAttribute("aria-invalid", "true");
    askError.textContent = "Enter a question of at least 3 characters.";
    askError.hidden = false;
    askInput.focus();
    return;
  }
  askPending = true;
  askButton.disabled = true;
  askForm.setAttribute("aria-busy", "true");
  askResult.hidden = true;
  askStatus.textContent = "Working on your question… This can take a while, and a slow model may time out.";
  askStatus.hidden = false;
  try {
    renderAskAnswer(await askAnalytics(question));
  } catch (error) {
    // Server messages are written for users (timeouts, rate limits, invalid periods, AI errors).
    const message = displayValue(error?.message, "Unable to answer this question.");
    askError.textContent = !error?.status
      ? "Unable to reach the server. Check your connection and try again."
      : /again/i.test(message)
        ? message
        : `${message.replace(/\.?$/, ".")} You can ask again.`;
    askError.hidden = false;
  } finally {
    askPending = false;
    askButton.disabled = false;
    askForm.removeAttribute("aria-busy");
    askStatus.hidden = true;
  }
});

filterForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!requireAuth()) return;
  const params = queryParams();
  if (params.start_date && params.end_date && params.start_date > params.end_date) {
    errorMessage.hidden = false;
    errorMessage.textContent = "Start date must be on or before end date.";
    return;
  }
  void loadAnalytics();
});

filterForm.addEventListener("reset", () => {
  // The form clears after this event; reload with the defaults on the next tick.
  window.setTimeout(() => {
    errorMessage.hidden = true;
    void loadAnalytics();
  }, 0);
});

mountAuthControls();
if (requireAuth()) {
  void loadAnalytics();
  void loadInsights();
}

