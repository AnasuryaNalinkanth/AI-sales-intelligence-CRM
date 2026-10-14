import { getAnalytics, requireAuth } from "./api.js";
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
  badges.append(element("span", "insight-badge", `Score ${numberValue(item.priority_score)}/100`));
  top.append(element("h3", "insight-title", item.title), badges);
  card.append(
    top,
    element("p", "insight-summary", `${INSIGHT_CATEGORIES[item.category] ?? humanize(item.category)} · ${item.summary}`),
    element("p", "insight-reason", item.reason),
  );
  const action = element("p", "insight-action");
  action.append(element("strong", "", "Suggested next step: "), document.createTextNode(item.recommended_next_action));
  card.append(action);
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
    const result = await getAnalytics("proactive-insights", {
      category,
      urgency: formData.get("urgency"),
      page: insightPage,
      page_size: INSIGHT_PAGE_SIZE,
    });
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

