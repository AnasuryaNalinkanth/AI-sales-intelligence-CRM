import {
  createAgentTask,
  getAnalytics,
  getAuthToken,
  getAuthenticatedUser,
  getWorkspaceActivities,
  moveWorkspaceActivity,
} from "./api.js";
import { element, humanize, mountAuthControls } from "./common.js";

const publicHome = document.querySelector("#public-home");
const dashboard = document.querySelector("#dashboard");
const weekBoard = document.querySelector("#week-board");
const upcomingEvents = document.querySelector("#upcoming-events");
const dailyHeading = document.querySelector("#daily-heading");
const dailyCount = document.querySelector("#daily-count");
const loadingMessage = document.querySelector("#dashboard-loading");
const errorMessage = document.querySelector("#dashboard-error");
const moveStatus = document.querySelector("#schedule-move-status");
const datePicker = document.querySelector("#schedule-date-picker");
const searchInput = document.querySelector("#schedule-search");
const timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const currentDate = new Date();
const selectedDate = new Date(currentDate.getFullYear(), currentDate.getMonth(), currentDate.getDate());
let activities = [];
let activityLoadFailed = false;
let selectedFilter = "all";
let availableStartKey = "";
let availableEndKey = "";
let movingActivityId = null;

function dayKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function movableActivity(activity) {
  return isActive(activity)
    && (activity.activity_type !== "call" || Boolean(activity.starts_at));
}

function startOfLocalDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function localDateTimeIso(date) {
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const hours = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
  const minutes = String(Math.abs(offset) % 60).padStart(2, "0");
  const local = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
  return `${local}${sign}${hours}:${minutes}`;
}

function setGreeting() {
  const hour = currentDate.getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const user = getAuthenticatedUser();
  const name = typeof user?.full_name === "string" ? user.full_name.trim() : "";
  document.querySelector("#dashboard-greeting").textContent = name
    ? `${greeting}, ${name}`
    : `${greeting}`;
  document.querySelector("#dashboard-date").textContent = new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  }).format(currentDate);
  document.querySelector("#schedule-timezone").textContent = `Times are shown in each activity’s saved timezone; calendar days use ${timezoneName}.`;
}

function activityLocalDay(activity) {
  return dayKey(new Date(activity.starts_at));
}

function matchingActivities() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  return activities.filter((activity) => {
    if (selectedFilter !== "all" && activity.activity_type !== selectedFilter) return false;
    if (!query) return true;
    return [
      activity.title,
      activity.customer_name,
      activity.company_name,
      activity.description,
    ].some((value) => typeof value === "string" && value.toLocaleLowerCase().includes(query));
  });
}

function weekStartDate(date = selectedDate) {
  const start = startOfLocalDay(date);
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  return start;
}

function selectDay(date) {
  selectedDate.setFullYear(date.getFullYear(), date.getMonth(), date.getDate());
  datePicker.value = dayKey(selectedDate);
  renderWeekBoard();
  renderUpcomingEvents();
  const selectedColumn = weekBoard.querySelector('.week-column-heading[aria-pressed="true"]')
    ?.closest(".week-column");
  if (selectedColumn) {
    const boardBounds = weekBoard.getBoundingClientRect();
    const columnBounds = selectedColumn.getBoundingClientRect();
    const offset = columnBounds.left < boardBounds.left
      ? columnBounds.left - boardBounds.left
      : columnBounds.right > boardBounds.right
        ? columnBounds.right - boardBounds.right
        : 0;
    if (offset) weekBoard.scrollBy({ left: offset, behavior: "auto" });
  }
}

function updateWeekNavigation(start) {
  const previousStart = new Date(start);
  previousStart.setDate(previousStart.getDate() - 7);
  const nextWeekLastDay = new Date(start);
  nextWeekLastDay.setDate(nextWeekLastDay.getDate() + 13);
  document.querySelector("#schedule-previous").disabled =
    Boolean(availableStartKey && dayKey(previousStart) < availableStartKey);
  document.querySelector("#schedule-next").disabled =
    Boolean(availableEndKey && dayKey(nextWeekLastDay) > availableEndKey);
}

function formatWeekRange(start) {
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const sameMonth = start.getMonth() === end.getMonth();
  const sameYear = start.getFullYear() === end.getFullYear();
  const options = { month: "short", day: "numeric" };
  const first = new Intl.DateTimeFormat(undefined, {
    ...options,
    ...(sameYear ? {} : { year: "numeric" }),
  }).format(start);
  const last = new Intl.DateTimeFormat(undefined, {
    ...options,
    year: "numeric",
  }).format(end);
  if (sameMonth) {
    const dayRange = `${start.getDate()}–${end.getDate()}`;
    return `${new Intl.DateTimeFormat(undefined, { month: "long" }).format(start)} ${dayRange}, ${end.getFullYear()}`;
  }
  return `${first} – ${last}`;
}

function renderWeekBoard() {
  weekBoard.replaceChildren();
  const start = weekStartDate();
  const end = new Date(start);
  end.setDate(end.getDate() + 7);
  dailyHeading.textContent = formatWeekRange(start);
  updateWeekNavigation(start);

  if (activityLoadFailed) return;

  const visible = matchingActivities()
    .filter((activity) => {
      const activityDate = activityLocalDay(activity);
      return activityDate >= dayKey(start) && activityDate < dayKey(end);
    })
    .sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at));
  dailyCount.textContent = `${visible.length} ${visible.length === 1 ? "activity" : "activities"}`;

  for (let index = 0; index < 7; index += 1) {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    const key = dayKey(date);
    const dayActivities = visible.filter((activity) => activityLocalDay(activity) === key);
    const column = element("section", "week-column");
    column.setAttribute("aria-label", new Intl.DateTimeFormat(undefined, {
      weekday: "long",
      month: "long",
      day: "numeric",
    }).format(date));
    const heading = element("button", "week-column-heading");
    heading.type = "button";
    heading.setAttribute("aria-pressed", String(key === dayKey(selectedDate)));
    const weekday = element("span", "week-column-weekday", new Intl.DateTimeFormat(undefined, {
      weekday: "short",
    }).format(date));
    const dateLabel = element("span", "week-column-date", String(date.getDate()));
    heading.append(weekday, dateLabel);
    if (key === dayKey(currentDate)) heading.append(element("span", "week-today-label", "Today"));
    heading.addEventListener("click", () => selectDay(date));
    column.append(heading);

    const eventList = element("div", "week-column-events");
    column.addEventListener("dragover", (event) => {
      if (movingActivityId) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      column.classList.add("is-drop-target");
    });
    column.addEventListener("dragleave", (event) => {
      if (!column.contains(event.relatedTarget)) column.classList.remove("is-drop-target");
    });
    column.addEventListener("drop", (event) => {
      event.preventDefault();
      column.classList.remove("is-drop-target");
      const activityId = event.dataTransfer?.getData("text/plain");
      const activity = activities.find((item) => item.activity_id === activityId);
      if (activity && movableActivity(activity)) {
        void moveActivity(activity, key);
      }
    });
    if (dayActivities.length === 0) {
      eventList.append(element("p", "week-column-empty", "Nothing planned"));
    } else {
      for (const activity of dayActivities) {
        eventList.append(renderActivityCard(activity));
      }
    }
    column.append(eventList);
    weekBoard.append(column);
  }
}

function formatActivityTime(activity) {
  const start = new Date(activity.starts_at);
  const end = new Date(start.getTime() + activity.duration_minutes * 60_000);
  const formatter = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: activity.timezone || "UTC",
  });
  return `${formatter.format(start)} – ${formatter.format(end)} ${activity.timezone || "UTC"}`;
}

function formatActivityDate(activity) {
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: activity.timezone || "UTC",
  }).format(new Date(activity.starts_at));
}

function isActive(activity) {
  if (activity.activity_type === "meeting" || activity.activity_type === "call") {
    return activity.status === "scheduled";
  }
  return ["pending", "in_progress", "overdue"].includes(activity.status);
}

function renderActivityCard(activity, { includeDate = false } = {}) {
  const card = element("article", `dashboard-event dashboard-event-${activity.activity_type}`);
  const movable = isActive(activity)
    && (activity.activity_type === "follow_up" || activity.activity_type === "meeting"
      || (activity.activity_type === "call" && activity.starts_at));
  card.draggable = movable;
  if (movable) {
    card.classList.add("dashboard-event-movable");
    card.addEventListener("dragstart", (event) => {
      if (movingActivityId || !event.dataTransfer) {
        event.preventDefault();
        return;
      }
      event.dataTransfer.setData("text/plain", activity.activity_id);
      event.dataTransfer.effectAllowed = "move";
      card.classList.add("is-dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("is-dragging"));
  }
  const eventHeader = element("div", "dashboard-event-header");
  const kind = activity.activity_type.replace("_", " ");
  eventHeader.append(
    element("span", `activity-kind kind-${activity.activity_type}`, kind),
    element("span", `dashboard-status status-${activity.status}`, activity.status.replaceAll("_", " ")),
  );
  card.append(eventHeader);
  card.append(element("h3", "dashboard-event-title", activity.title));
  const when = includeDate
    ? `${formatActivityDate(activity)} · ${formatActivityTime(activity)}`
    : formatActivityTime(activity);
  card.append(element("p", "dashboard-event-time", when));
  const customerLink = element("a", "dashboard-customer-link", `${activity.customer_name} · ${activity.company_name}`);
  customerLink.href = `/customer?customer_id=${encodeURIComponent(activity.customer_id)}`;
  card.append(customerLink);
  if (activity.description) card.append(element("p", "dashboard-event-description", activity.description));
  if (movable) {
    const moveControl = element("label", "activity-move-control");
    moveControl.append(element("span", "", "Move to"));
    const dateInput = element("input", "activity-move-date");
    dateInput.type = "date";
    dateInput.value = activityLocalDay(activity);
    dateInput.min = availableStartKey;
    dateInput.max = availableEndKey;
    dateInput.setAttribute("aria-label", `Move ${activity.title} to another date`);
    dateInput.addEventListener("change", () => {
      if (dateInput.value) void moveActivity(activity, dateInput.value, dateInput);
    });
    moveControl.append(dateInput);
    card.append(moveControl);
  }
  return card;
}

async function moveActivity(activity, targetDate, control = null) {
  if (movingActivityId) return;
  const currentDateKey = activityLocalDay(activity);
  if (targetDate === currentDateKey) return;
  if (targetDate < availableStartKey || targetDate > availableEndKey) {
    moveStatus.textContent = "Choose a date within the loaded schedule range.";
    moveStatus.classList.add("dashboard-error");
    return;
  }

  const separator = activity.activity_id.lastIndexOf("-");
  const recordId = activity.activity_id.slice(separator + 1);
  if (separator < 1 || !/^\d+$/.test(recordId)) {
    moveStatus.textContent = "This activity cannot be moved because its record id is invalid.";
    moveStatus.classList.add("dashboard-error");
    return;
  }

  movingActivityId = activity.activity_id;
  if (control) control.disabled = true;
  moveStatus.classList.remove("dashboard-error");
  moveStatus.textContent = `Moving “${activity.title}”…`;
  try {
    await moveWorkspaceActivity(activity.activity_type, recordId, targetDate, timezoneName);
    moveStatus.textContent = `Moved “${activity.title}”. The saved schedule is refreshing.`;
    await loadDashboard();
  } catch (error) {
    if (control) control.value = currentDateKey;
    moveStatus.textContent = `Could not move “${activity.title}”: ${error.message || "Please try again."}`;
    moveStatus.classList.add("dashboard-error");
  } finally {
    movingActivityId = null;
    if (control) control.disabled = false;
  }
}

function renderUpcomingEvents() {
  upcomingEvents.replaceChildren();
  if (activityLoadFailed) return;
  const start = weekStartDate();
  const end = new Date(start);
  end.setDate(end.getDate() + 7);
  const weekStartKey = dayKey(start);
  const weekEndKey = dayKey(end);
  const now = Date.now();
  const upcoming = matchingActivities()
    .filter((activity) => isActive(activity))
    .filter((activity) => {
      const date = activityLocalDay(activity);
      return date < weekStartKey || date >= weekEndKey;
    })
    .filter((activity) => {
      const startsAt = new Date(activity.starts_at).getTime();
      return startsAt >= now || activity.activity_type === "follow_up";
    })
    .sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at))
    .slice(0, 5);
  if (upcoming.length === 0) {
    upcomingEvents.append(element("p", "dashboard-empty", "No other upcoming activities."));
    return;
  }
  for (const activity of upcoming) {
    const card = renderActivityCard(activity, { includeDate: true });
    if (new Date(activity.starts_at).getTime() < now && activity.activity_type === "follow_up") {
      card.classList.add("dashboard-event-overdue");
      card.querySelector(".dashboard-status").textContent = "overdue";
    }
    upcomingEvents.append(card);
  }
}

function renderError(error) {
  const message = error?.message || "Your schedule could not be loaded. Please try again.";
  errorMessage.textContent = message;
  errorMessage.hidden = false;
  loadingMessage.hidden = true;
  activityLoadFailed = true;
  weekBoard.replaceChildren();
  upcomingEvents.replaceChildren();
  weekBoard.append(element("p", "dashboard-empty", "Schedule unavailable."));
  upcomingEvents.append(element("p", "dashboard-empty", "Upcoming activities are unavailable."));
}

async function loadDashboard() {
  const rangeStart = startOfLocalDay(currentDate);
  const dayOfWeek = (rangeStart.getDay() + 6) % 7;
  rangeStart.setDate(rangeStart.getDate() - dayOfWeek - 7);
  const rangeEnd = new Date(rangeStart);
  rangeEnd.setDate(rangeStart.getDate() + 105);
  availableStartKey = dayKey(rangeStart);
  const lastAvailableDay = new Date(rangeEnd);
  lastAvailableDay.setDate(lastAvailableDay.getDate() - 1);
  availableEndKey = dayKey(lastAvailableDay);
  datePicker.min = availableStartKey;
  datePicker.max = availableEndKey;
  try {
    const result = await getWorkspaceActivities(
      localDateTimeIso(rangeStart),
      localDateTimeIso(rangeEnd),
    );
    activities = Array.isArray(result?.items) ? result.items : [];
    loadingMessage.hidden = true;
    renderWeekBoard();
    renderUpcomingEvents();
  } catch (error) {
    renderError(error);
  }
}

mountAuthControls();

// Headline counts from the existing analytics overview; a failure leaves the dashes.
async function loadGlance() {
  try {
    const overview = await getAnalytics("overview");
    const format = new Intl.NumberFormat();
    for (const target of document.querySelectorAll("[data-glance]")) {
      const value = Number(overview?.[target.dataset.glance]);
      target.textContent = Number.isFinite(value) ? format.format(value) : "–";
      if (target.dataset.glance === "overdue_followups") {
        target.closest(".glance-item").classList.toggle("has-items", value > 0);
      }
    }
  } catch {
    // Optional context; the schedule below is the primary content.
  }
}

// ---------- Needs attention (GET /api/analytics/attention; rules-based, read-only) ----------

const ATTENTION_LIMIT = 8;
// Labels and symbols carry the meaning; colour only reinforces it.
const ATTENTION_REASONS = {
  overdue_followup: { label: "Overdue follow-up", symbol: "!" },
  no_recent_activity: { label: "No recent activity", symbol: "◷" },
  no_next_step: { label: "No next step", symbol: "→" },
  high_priority: { label: "High priority", symbol: "▲" },
  missing_value: { label: "No estimated value", symbol: "?" },
};
const attentionStatus = document.querySelector("#attention-status");
const attentionError = document.querySelector("#attention-error");
const attentionList = document.querySelector("#attention-list");
const attentionSummary = document.querySelector("#attention-summary");
const attentionRules = document.querySelector("#attention-rules");
const attentionRetry = document.querySelector("#attention-retry");

function renderAttentionItem(item) {
  const card = element("li", "attention-item");
  const top = element("div", "attention-item-top");
  const customer = element("a", "attention-customer", `${item.customer_name} · ${item.company_name}`);
  customer.href = `/customer?customer_id=${encodeURIComponent(item.customer_id)}`;
  top.append(customer, element("span", "attention-priority", `${humanize(item.priority)} priority`));
  const enquiry = element(
    "p",
    "attention-enquiry",
    `${item.product || "Sales lead"} · Lead #${item.enquiry_id} · ${humanize(item.status)}`,
  );
  const reasons = element("ul", "attention-reasons");
  for (const reason of Array.isArray(item.reasons) ? item.reasons : []) {
    const known = ATTENTION_REASONS[reason.code];
    const row = element("li", "attention-reason");
    row.dataset.reason = reason.code;
    const symbol = element("span", "attention-reason-symbol", known?.symbol ?? "•");
    symbol.setAttribute("aria-hidden", "true");
    row.append(
      symbol,
      element("strong", "", known?.label ?? humanize(reason.code)),
      element("span", "attention-reason-detail", reason.detail),
    );
    reasons.append(row);
  }
  card.append(top, enquiry, reasons);
  const suggestions = renderSuggestions(item);
  if (suggestions) card.append(suggestions);
  const context = renderCustomerLevelContext(item);
  if (context) card.append(context);
  return card;
}

// Suggested next steps come from the API's reason codes. Task suggestions only prepare a
// draft in the existing Approvals workflow; review suggestions are plain links.
const TASK_FIELD_LABELS = {
  scheduled_at: "the call date and time",
  due_date: "the due date",
  type: "the follow-up type",
};
let preparingTask = false;

function setSuggestionButtonsDisabled(disabled) {
  for (const button of attentionList.querySelectorAll("button.attention-suggestion-action")) {
    button.disabled = disabled;
  }
}

async function prepareSuggestedTask(action, button, status) {
  if (preparingTask) return;
  preparingTask = true;
  setSuggestionButtonsDisabled(true);
  button.textContent = "Preparing task…";
  status.hidden = true;
  const values = { ...action.prefill };
  if (action.timezone_field) values[action.timezone_field] = timezoneName;
  try {
    await createAgentTask([{ action: action.kind, values }]);
    window.location.assign(`/customer?customer_id=${encodeURIComponent(action.prefill.customer_id)}#approvals`);
  } catch (error) {
    preparingTask = false;
    setSuggestionButtonsDisabled(false);
    button.textContent = button.dataset.label;
    // The shared request helper has already started the redirect to sign in on 401.
    status.textContent = error?.status === 401
      ? "Your session has expired. Redirecting to sign in…"
      : `The task could not be prepared. ${error?.message || ""}`.trim();
    status.hidden = false;
  }
}

function renderSuggestions(item) {
  const actions = Array.isArray(item.suggested_actions) ? item.suggested_actions : [];
  if (!actions.length) return null;
  const box = element("div", "attention-suggestions");
  box.append(element("p", "attention-suggestions-heading", "Suggested next steps"));
  const list = element("ul", "attention-suggestion-list");
  const status = element("p", "attention-suggestion-error");
  status.setAttribute("role", "alert");
  status.hidden = true;
  for (const action of actions) {
    const row = element("li", "attention-suggestion");
    const codes = Array.isArray(action.reason_codes) ? action.reason_codes : [];
    const basis = `Based on: ${codes.map((code) => ATTENTION_REASONS[code]?.label ?? humanize(code)).join(", ")}.`;
    if (action.opens_task) {
      const label = `Prepare task: ${action.label}`;
      const button = element("button", "button button-secondary button-sm attention-suggestion-action", label);
      button.type = "button";
      button.dataset.label = label;
      button.addEventListener("click", () => void prepareSuggestedTask(action, button, status));
      const needs = (action.user_must_provide ?? []).map((field) => TASK_FIELD_LABELS[field] ?? humanize(field).toLowerCase());
      row.append(
        button,
        element(
          "span",
          "attention-suggestion-note",
          `${basis} Opens a draft in Approvals with this customer and sales lead filled in${needs.length ? `; you add ${needs.join(" and ")}` : ""}. Nothing changes in the CRM until you submit it.`,
        ),
      );
    } else {
      const link = element("a", "attention-suggestion-action attention-suggestion-link", action.label);
      link.href = `/customer?customer_id=${encodeURIComponent(action.prefill.customer_id)}`;
      row.append(link, element("span", "attention-suggestion-note", `${basis} Opens the customer record; no task is created.`));
    }
    list.append(row);
  }
  box.append(list, status);
  return box;
}

// Same UTC calendar date the API uses in its reason details.
function utcDay(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString().slice(0, 10);
}

// Records linked to the customer but to no enquiry: context only, never an enquiry reason.
function renderCustomerLevelContext(item) {
  const rows = [];
  const overdue = Number(item.customer_level_overdue_followup_count) || 0;
  if (overdue > 0) {
    const oldest = utcDay(item.customer_level_oldest_overdue_due_at);
    rows.push(["attention-context-overdue", "Overdue customer-level follow-ups", `${overdue}${oldest ? `; the oldest was due ${oldest}` : ""}`]);
  }
  const next = item.customer_level_next_step_at && utcDay(item.customer_level_next_step_at);
  if (next) rows.push(["", "Next customer-level activity", next]);
  const last = item.customer_level_last_activity_at && utcDay(item.customer_level_last_activity_at);
  if (last) rows.push(["", "Last customer-level activity", last]);
  if (!rows.length) return null;

  const box = element("div", "attention-context");
  box.append(element("p", "attention-context-heading", "Customer-level · not linked to this sales lead"));
  const list = element("ul", "attention-context-list");
  for (const [className, label, value] of rows) {
    const row = element("li", className);
    row.append(element("strong", "", `${label}: `), element("span", "", value));
    list.append(row);
  }
  box.append(list);
  return box;
}

function renderAttention(queue) {
  const items = Array.isArray(queue?.items) ? queue.items : [];
  const needing = Number(queue?.needing_attention) || 0;
  const open = Number(queue?.open_enquiries) || 0;
  attentionList.replaceChildren(...items.map(renderAttentionItem));
  attentionSummary.textContent = open
    ? `${needing} of ${open} open sales leads${needing > items.length ? ` · showing the first ${items.length}` : ""}`
    : "";
  attentionStatus.hidden = items.length > 0;
  attentionStatus.textContent = open
    ? "No open sales leads need attention right now."
    : "There are no open sales leads.";
  const ordering = Array.isArray(queue?.ordering) ? queue.ordering : [];
  attentionRules.querySelector("ol").replaceChildren(...ordering.map((rule) => element("li", "", rule)));
  attentionRules.hidden = !items.length || !ordering.length;
}

async function loadAttention() {
  attentionError.hidden = true;
  attentionRetry.hidden = true;
  attentionStatus.hidden = false;
  attentionStatus.textContent = "Loading sales leads that need attention…";
  try {
    renderAttention(await getAnalytics("attention", { limit: ATTENTION_LIMIT }));
  } catch (error) {
    attentionList.replaceChildren();
    attentionRules.hidden = true;
    attentionStatus.hidden = true;
    attentionSummary.textContent = "";
    // The shared request helper has already started the redirect to sign in.
    const expired = error?.status === 401;
    attentionError.textContent = expired
      ? "Your session has expired. Redirecting to sign in…"
      : "Sales leads that need attention could not be loaded.";
    attentionError.hidden = false;
    attentionRetry.hidden = expired;
  }
}

if (getAuthToken()) {
  publicHome.hidden = true;
  dashboard.hidden = false;
  setGreeting();
  void loadGlance();
  void loadAttention();
  attentionRetry.addEventListener("click", () => void loadAttention());
  document.querySelector("#schedule-previous").addEventListener("click", () => {
    const previousWeek = new Date(selectedDate);
    previousWeek.setDate(previousWeek.getDate() - 7);
    if (!availableStartKey || dayKey(previousWeek) >= availableStartKey) selectDay(previousWeek);
  });
  document.querySelector("#schedule-next").addEventListener("click", () => {
    const nextWeek = new Date(selectedDate);
    nextWeek.setDate(nextWeek.getDate() + 7);
    if (!availableEndKey || dayKey(nextWeek) <= availableEndKey) selectDay(nextWeek);
  });
  document.querySelector("#schedule-today").addEventListener("click", () => {
    selectDay(currentDate);
  });
  datePicker.addEventListener("change", () => {
    if (!datePicker.value) return;
    const [year, month, day] = datePicker.value.split("-").map(Number);
    selectDay(new Date(year, month - 1, day));
  });
  document.querySelector("#activity-filters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-activity-filter]");
    if (!button) return;
    selectedFilter = button.dataset.activityFilter;
    for (const filter of document.querySelectorAll("[data-activity-filter]")) {
      const selected = filter === button;
      filter.classList.toggle("is-selected", selected);
      filter.setAttribute("aria-pressed", String(selected));
    }
    renderWeekBoard();
    renderUpcomingEvents();
  });
  searchInput.addEventListener("input", () => {
    renderWeekBoard();
    renderUpcomingEvents();
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const isTyping = target instanceof HTMLElement
      && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
    if (event.key === "/" && !isTyping && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      searchInput.focus();
    } else if (event.key === "Escape" && target === searchInput) {
      searchInput.value = "";
      searchInput.blur();
      renderWeekBoard();
      renderUpcomingEvents();
    } else if (!isTyping && event.key === "ArrowLeft") {
      const previous = new Date(selectedDate);
      previous.setDate(previous.getDate() - 7);
      if (!availableStartKey || dayKey(previous) >= availableStartKey) {
        selectDay(previous);
      }
    } else if (!isTyping && event.key === "ArrowRight") {
      const next = new Date(selectedDate);
      next.setDate(next.getDate() + 7);
      if (!availableEndKey || dayKey(next) <= availableEndKey) {
        selectDay(next);
      }
    }
  });
  selectDay(currentDate);
  void loadDashboard();
} else {
  dashboard.hidden = true;
  publicHome.hidden = false;
}
