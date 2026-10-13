import {
  createCall,
  createFollowup,
  createMeeting,
  cancelAgentTask,
  cancelAgentAction,
  checkSchedulingAvailability,
  chatWithAgent,
  confirmAgentAction,
  createAgentTask,
  generateCustomerSummary,
  generateMeetingBrief,
  getAgentPolicies,
  getAgentTasks,
  getLatestConversation,
  getCurrentUser,
  getCustomerActivity,
  getCustomerOverview,
  getCustomers,
  requireAuth,
  rejectAgentAction,
  resumeAgentTask,
  submitAgentTaskInput,
  updateAgentPolicy,
  updateMeeting,
  updateCall,
  updateFollowup,
} from "./api.js";
import {
  displayValue,
  formatNumber,
  element,
  formatDate,
  humanize,
  initials,
  mountAuthControls,
} from "./common.js";

const searchForm = document.querySelector("#customer-search-form");
const searchInput = document.querySelector("#customer-search");
const resultList = document.querySelector("#customer-results");
const searchState = document.querySelector("#search-state");
const detailPanel = document.querySelector("#customer-detail");
const resultCount = document.querySelector("#result-count");
const pagination = document.querySelector("#pagination");
const previousPage = document.querySelector("#previous-page");
const nextPage = document.querySelector("#next-page");
const pageIndicator = document.querySelector("#page-indicator");
const activityDialog = document.querySelector("#activity-dialog");
import { renderMarkdown } from "./markdown.js";

const activityForm = document.querySelector("#activity-form");
const activityFields = document.querySelector("#activity-fields");
const activityDialogTitle = document.querySelector("#activity-dialog-title");
const activityFormError = document.querySelector("#activity-form-error");
const saveActivityButton = document.querySelector("#save-activity");
const meetingBriefDialog = document.querySelector("#meeting-brief-dialog");
const meetingBriefContent = document.querySelector("#meeting-brief-content");
const hitlTaskList = document.querySelector("#hitl-task-list");
const hitlError = document.querySelector("#hitl-error");
const hitlStatus = document.querySelector("#hitl-status");
const hitlCreateForm = document.querySelector("#create-hitl-task");
const hitlActionType = document.querySelector("#hitl-action-type");
const hitlCustomerContext = document.querySelector("#hitl-customer-context");
const hitlPolicyPanel = document.querySelector("#hitl-policy-panel");
const hitlPolicies = document.querySelector("#hitl-policies");
const hitlContent = document.querySelector("#hitl-content");
const toggleHitlButton = document.querySelector("#toggle-hitl");
const PAGE_SIZE = 20;

let currentPage = 1;
let currentSearch = "";
let totalPages = 1;
let selectedCustomerId = null;
let selectedCustomer = null;
let activityFilter = "";
let activityStartDate = "";
let activityEndDate = "";
let selectedActivityKind = "";
let selectedMeetingForEdit = null;
let selectedCallForEdit = null;
let selectedFollowupForEdit = null;
let selectedMeetingScheduleValue = "";
let filteredActivity = [];
let isSavingActivity = false;
let aiSummary = null;
let aiSummaryError = "";
let aiSummaryLoading = false;
// The generated summary can be folded away so it never crowds the customer record.
let aiSummaryCollapsed = false;
let meetingBriefLoadingId = null;
let agentChatMessages = [];
// Server-issued ID of this user's conversation with the selected customer.
let agentConversationId = null;
// Unsent chat text; the panel is rebuilt on every state change.
let agentChatDraft = "";
let agentChatLoading = false;
let agentActionLoading = false;
let pendingAgentAction = null;
let agentClarificationTask = null;
let agentScheduleAlternatives = [];
let agentScheduleReason = null;
// The scheduling request behind the chat buttons: { taskId, action, values }.
let agentScheduleDraft = null;
let agentActionNotice = "";
let customerListRequest = 0;
let hitlTasks = [];
let hitlBusy = false;
let hitlIsAdmin = false;
let hitlPolicyModes = {};

function resetCustomerAiState({ cancelPending = true } = {}) {
  // Task proposals stay listed under Agent approvals, so only ad-hoc chat proposals are cancelled.
  if (cancelPending && pendingAgentAction?.action_id && !pendingAgentAction.fromTask) {
    const staleActionId = pendingAgentAction.action_id;
    void cancelAgentAction(staleActionId).catch(() => {});
  }
  aiSummary = null;
  aiSummaryError = "";
  aiSummaryLoading = false;
  aiSummaryCollapsed = false;
  meetingBriefLoadingId = null;
  agentChatMessages = [];
  agentConversationId = null;
  agentChatDraft = "";
  agentChatLoading = false;
  agentActionLoading = false;
  pendingAgentAction = null;
  agentClarificationTask = null;
  agentScheduleAlternatives = [];
  agentScheduleReason = null;
  agentScheduleDraft = null;
  agentActionNotice = "";
  if (meetingBriefDialog?.open) {
    meetingBriefDialog.close();
  }
}

function showSearchState(message, error = false) {
  resultList.replaceChildren();
  searchState.hidden = false;
  searchState.replaceChildren(
    element("span", "empty-icon", error ? "!" : "⌕"),
    element("p", error ? "detail-api-error" : "", message),
  );
  pagination.hidden = true;
  resultCount.textContent = "";
}

function setDetailMessage(message, { error = false, loading = false } = {}) {
  detailPanel.replaceChildren();
  const state = element("div", `empty-state detail-empty${error ? " detail-error" : ""}`);
  state.append(
    element("span", "empty-icon", error ? "!" : loading ? "…" : "◎"),
    element(
      "h2",
      "",
      error ? "Unable to load customer data." : loading ? message : "No customer selected",
    ),
  );
  if (error || !loading) {
    state.append(element("p", error ? "detail-api-error" : "", message));
  }
  detailPanel.append(state);
}

function statusClass(value) {
  return `status-badge status-${String(value ?? "").toLowerCase().replace(/[^a-z0-9_-]/g, "")}`;
}

function renderCustomerList(data) {
  resultList.replaceChildren();
  searchState.hidden = true;
  resultCount.textContent = `${data.total} ${data.total === 1 ? "record" : "records"}`;
  totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  pagination.hidden = data.total === 0;
  pageIndicator.textContent = `Page ${data.page} of ${totalPages}`;
  previousPage.disabled = data.page <= 1;
  nextPage.disabled = data.page >= totalPages;

  for (const customer of data.items) {
    const button = element("button", "customer-result");
    button.type = "button";
    button.dataset.customerId = String(customer.customer_id);
    button.setAttribute("aria-current", String(customer.customer_id) === String(selectedCustomerId));
    button.append(
      element("span", "customer-avatar", initials(customer.customer_name)),
    );
    const copy = element("span", "result-copy");
    copy.append(
      element("span", "result-name", displayValue(customer.customer_name)),
      element("span", "result-company", displayValue(customer.company?.company_name)),
    );
    button.append(copy, element("span", "result-arrow", "›"));
    resultList.append(button);
  }

  if (data.total === 0) showSearchState("No customers found.");
}

async function loadCustomers(page = 1) {
  currentPage = page;
  // Only the newest request may render, so overlapping loads never show stale rows.
  const requestId = ++customerListRequest;
  showSearchState("Loading customers...");
  try {
    const data = await getCustomers({
      search: currentSearch,
      page: currentPage,
      page_size: PAGE_SIZE,
    });
    if (requestId === customerListRequest) renderCustomerList(data);
  } catch {
    if (requestId === customerListRequest) showSearchState("Unable to load customer data.", true);
  }
}

function appendInfoGrid(parent, entries, className = "profile-grid") {
  const grid = element("dl", className);
  for (const [label, value] of entries) {
    const item = element("div", "info-item");
    item.append(
      element("dt", "", label),
      element("dd", "", displayValue(value)),
    );
    grid.append(item);
  }
  parent.append(grid);
}

function appendSection(parent, title, count = null) {
  const section = element("section", "detail-section");
  const heading = element("div", "detail-section-heading");
  heading.append(element("h3", "", title));
  if (count !== null) heading.append(element("span", "", String(count)));
  section.append(heading);
  parent.append(section);
  return section;
}

function makeActionButton(label, className, onClick) {
  const button = element("button", className, label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function appendActivityToolbar(section, count) {
  const heading = section.querySelector(".detail-section-heading");
  const controls = element("div", "activity-toolbar");
  controls.append(element("span", "activity-count", `${count} activities`));
  const filter = element("select", "activity-filter");
  filter.setAttribute("aria-label", "Filter activity timeline");
  for (const [value, label] of [
    ["", "All activities"],
    ["enquiry", "Enquiries"],
    ["meeting", "Meetings"],
    ["call", "Calls"],
    ["follow_up", "Follow-ups"],
  ]) {
    const option = element("option", "", label);
    option.value = value;
    filter.append(option);
  }
  filter.value = activityFilter;
  const startDate = element("input", "activity-date-filter");
  startDate.type = "date";
  startDate.value = activityStartDate;
  startDate.setAttribute("aria-label", "Activity start date");
  const endDate = element("input", "activity-date-filter");
  endDate.type = "date";
  endDate.value = activityEndDate;
  endDate.setAttribute("aria-label", "Activity end date");
  const applyButton = makeActionButton(
    "Apply",
    "timeline-apply-button",
    () => loadFilteredActivity(section, controls),
  );
  filter.addEventListener("change", () => loadFilteredActivity(section, controls));
  controls.append(filter, startDate, endDate, applyButton);
  heading.append(controls);
}

async function loadFilteredActivity(section, controls) {
  const filter = controls.querySelector(".activity-filter");
  const startDate = controls.querySelector('[aria-label="Activity start date"]');
  const endDate = controls.querySelector('[aria-label="Activity end date"]');
  const applyButton = controls.querySelector(".timeline-apply-button");
  activityFilter = filter.value;
  activityStartDate = startDate.value;
  activityEndDate = endDate.value;
  filter.disabled = true;
  startDate.disabled = true;
  endDate.disabled = true;
  applyButton.disabled = true;
  try {
    const result = await getCustomerActivity(selectedCustomerId, {
      type: activityFilter,
      start_date: activityStartDate,
      end_date: activityEndDate,
    });
    filteredActivity = result.items ?? [];
    controls.querySelector(".activity-count").textContent =
      `${filteredActivity.length} ${filteredActivity.length === 1 ? "activity" : "activities"}`;
    renderActivityTimeline(section);
  } catch (error) {
    section.querySelector(".timeline-list")?.remove();
    section.querySelector(".section-empty")?.remove();
    section.append(
      element("p", "section-empty detail-api-error", error.message || "Unable to load customer data."),
    );
  } finally {
    filter.disabled = false;
    startDate.disabled = false;
    endDate.disabled = false;
    applyButton.disabled = false;
  }
}

function renderActivityTimeline(section) {
  section.querySelector(".timeline-list")?.remove();
  section.querySelector(".section-empty")?.remove();
  if (filteredActivity.length === 0) {
    section.append(element("p", "section-empty", "No activity yet."));
    return;
  }
  const list = element("ol", "timeline-list");
  for (const activity of filteredActivity) {
    const item = element("li", `timeline-item timeline-${activity.activity_type}`);
    const marker = element("span", "timeline-marker", timelineSymbol(activity.activity_type));
    const body = element("div", "timeline-body");
    const top = element("div", "timeline-top");
    const titleGroup = element("div", "timeline-title-group");
    titleGroup.append(
      element("span", "timeline-type", humanize(activity.activity_type)),
      element("strong", "timeline-title", displayValue(activity.title)),
    );
    top.append(
      titleGroup,
      element("span", statusClass(activity.status), humanize(activity.status)),
    );
    body.append(
      top,
      element(
        "time",
        "timeline-date",
        formatDateTime(activity.activity_date, activity.activity_timezone),
      ),
    );
    if (activity.description) {
      body.append(element("p", "timeline-description", activity.description));
    }
    item.append(marker, body);
    list.append(item);
  }
  section.append(list);
}

function timelineSymbol(type) {
  return { enquiry: "E", meeting: "M", call: "C", follow_up: "F" }[type] ?? "•";
}

function formatDateTime(value, timezoneName = null) {
  if (!value) return "Date not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date not available";
  const options = {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  if (timezoneName) options.timeZone = timezoneName;
  return new Intl.DateTimeFormat(undefined, options).format(date);
}

function zonedDateTimeParts(timestamp, timezoneName) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezoneName,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(timestamp));
  return Object.fromEntries(
    parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]),
  );
}

function dateTimeLocalValue(value, timezoneName) {
  if (!value) return "";
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return "";
  const offsetMatch = /^([+-])(\d{2}):(\d{2})$/.exec(timezoneName || "");
  if (offsetMatch) {
    const sign = offsetMatch[1] === "+" ? 1 : -1;
    const offset = sign * (Number(offsetMatch[2]) * 60 + Number(offsetMatch[3]));
    return new Date(timestamp + offset * 60000).toISOString().slice(0, 16);
  }
  const parts = zonedDateTimeParts(timestamp, timezoneName || "UTC");
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function dateTimeLocalToISO(value, timezoneName, occurrence = "") {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new RangeError("Enter a valid date and time.");
  const [, year, month, day, hour, minute] = match;
  const requested = Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
  );
  const offsetMatch = /^([+-])(\d{2}):(\d{2})$/.exec(timezoneName || "");
  if (offsetMatch) {
    const sign = offsetMatch[1] === "+" ? 1 : -1;
    const offset = sign * (Number(offsetMatch[2]) * 60 + Number(offsetMatch[3]));
    return new Date(requested - offset * 60000).toISOString();
  }

  const timezone = timezoneName || "UTC";
  const offsets = new Set();
  for (let hours = -36; hours <= 36; hours += 3) {
    const sample = requested + hours * 3600000;
    const parts = zonedDateTimeParts(sample, timezone);
    const rendered = Date.UTC(
      Number(parts.year),
      Number(parts.month) - 1,
      Number(parts.day),
      Number(parts.hour),
      Number(parts.minute),
    );
    offsets.add(rendered - sample);
  }
  const candidates = [...offsets]
    .map((offset) => requested - offset)
    .filter((candidate) => {
      const parts = zonedDateTimeParts(candidate, timezone);
      return (
        parts.year === year
        && parts.month === month
        && parts.day === day
        && parts.hour === hour
        && parts.minute === minute
      );
    })
    .sort((left, right) => left - right);
  if (!candidates.length) {
    throw new RangeError("That local time does not exist because of a daylight-saving change.");
  }
  if (candidates.length > 1 && !occurrence) {
    throw new RangeError(
      "That local time occurs twice. Choose the earlier or later occurrence.",
    );
  }
  const timestamp = candidates[occurrence === "later" ? candidates.length - 1 : 0];
  return new Date(timestamp).toISOString();
}

function appendListBlock(parent, title, items, emptyText, className = "ai-facts") {
  const block = element("div", `ai-block ${className}`);
  block.append(element("h4", "", title));
  const values = Array.isArray(items) ? items.filter((item) => item) : [];
  if (values.length === 0) {
    block.append(element("p", "", emptyText));
  } else {
    const list = element("ul", "ai-list");
    for (const item of values) {
      const listItem = element("li", "");
      renderMarkdown(listItem, displayValue(item));
      list.append(listItem);
    }
    block.append(list);
  }
  parent.append(block);
}

function aiErrorMessage(error) {
  if (error?.code === "llm_not_configured") {
    return error.message || "The AI assistant is not configured.";
  }
  if (error?.status === 404) {
    return error.message || "The requested CRM record was not found.";
  }
  return error?.message || "Unable to generate AI output.";
}

const agentActionLabels = {
  create_meeting: "Create meeting",
  create_followup: "Create follow-up",
  schedule_call: "Schedule call",
  record_call_result: "Record call result",
  complete_followup: "Complete follow-up",
  apply_enrichment: "Apply CRM enrichment",
};

const hitlActions = Object.keys(agentActionLabels);
const hitlModes = ["automatic", "approval_required", "disabled"];

function setHitlError(message = "") {
  hitlError.textContent = message;
  hitlError.hidden = !message;
}

function taskActionButton(label, className, onClick, disabled = false) {
  const button = element("button", className, label);
  button.type = "button";
  button.disabled = disabled;
  button.addEventListener("click", onClick);
  return button;
}

function renderHitlTask(task) {
  const card = element("article", "hitl-task-card");
  const heading = element("div", "hitl-task-heading");
  const title = element("div");
  title.append(
    element("h3", "", "Task"),
    element("code", "hitl-task-id", task.task_id),
  );
  heading.append(title, element("span", statusClass(task.status), humanize(task.status)));
  card.append(heading);
  card.append(
    element("p", "hitl-task-message", task.message),
    element("p", "hitl-task-expiry", `Expires ${formatDateTime(task.expires_at)}`),
  );

  const taskActions = element("div", "agent-action-buttons hitl-task-actions");
  taskActions.append(
    taskActionButton("Resume / revalidate", "button button-secondary", () => {
      void refreshOneHitlTask(task.task_id);
    }, hitlBusy),
  );
  if (["collecting_information", "ready_for_review", "awaiting_approval"].includes(task.status)) {
    taskActions.append(
      taskActionButton("Cancel task", "button button-secondary", () => {
        void cancelHitlTask(task.task_id);
      }, hitlBusy),
    );
  }
  card.append(taskActions);

  for (const [index, operation] of task.operations.entries()) {
    const operationCard = element("section", "hitl-operation-card");
    const operationHeading = element("div", "hitl-operation-heading");
    operationHeading.append(
      element("h4", "", agentActionLabels[operation.action] || humanize(operation.action)),
      element("span", statusClass(operation.status), humanize(operation.status)),
    );
    operationCard.append(operationHeading);

    const exactParameters = element("details", "hitl-parameters");
    exactParameters.append(
      element("summary", "", operation.action_id ? "Review exact proposed parameters" : "Review collected values"),
    );
    const parameterList = element("dl", "agent-action-details");
    for (const [name, value] of Object.entries(operation.values || {})) {
      const row = element("div");
      row.append(
        element("dt", "", name),
        element(
          "dd",
          "",
          value === null || value === undefined
            ? "null"
            : typeof value === "object"
              ? JSON.stringify(value)
              : String(value),
        ),
      );
      parameterList.append(row);
    }
    exactParameters.append(parameterList);
    operationCard.append(exactParameters);

    if (operation.result) {
      const result = operation.result;
      const resultCard = element("div", `hitl-result hitl-result-${result.status}`);
      resultCard.append(
        element("strong", "", humanize(result.status)),
        element("p", "", result.message || "Execution finished."),
      );
      if (result.record_id !== null && result.record_id !== undefined) {
        resultCard.append(element("span", "", `Confirmed CRM record ID: ${result.record_id}`));
      }
      if (result.error_code) {
        resultCard.append(element("span", "hitl-error-code", `Reference: ${result.error_code}`));
      }
      operationCard.append(resultCard);
    }

    if (
      task.status === "collecting_information"
      && !operation.action_id
      && Array.isArray(operation.fields)
      && operation.fields.length
    ) {
      const form = element("form", "hitl-clarification-form");
      const fields = element("div", "hitl-fields");
      for (const field of operation.fields) {
        const wrapper = element("label", "activity-field");
        wrapper.append(element("span", "", field.label));
        let control;
        if (field.input_type === "textarea") {
          control = element("textarea");
          control.rows = 3;
        } else if (field.input_type === "select" || field.input_type === "record") {
          control = element("select");
          const placeholder = element(
            "option",
            "",
            field.input_type === "record" ? "Choose a CRM record by ID" : "Choose an option",
          );
          placeholder.value = "";
          control.append(placeholder);
          for (const choice of field.choices || []) {
            const option = element("option", "", choice.label);
            option.value = String(choice.id);
            option.selected = String(field.value ?? "") === option.value;
            control.append(option);
          }
        } else {
          control = element("input");
            control.type = {
              datetime: "datetime-local",
              date: "date",
              time: "time",
              number: "number",
            }[field.input_type] || "text";
          if (control.type === "number") control.step = "any";
          if (control.type === "datetime-local" && field.value) {
            const timezoneName = field.name === "scheduled_at"
              ? operation.values.scheduled_timezone
                || Intl.DateTimeFormat().resolvedOptions().timeZone
              : field.name === "due_date"
                ? operation.values.due_timezone
                  || Intl.DateTimeFormat().resolvedOptions().timeZone
                : "UTC";
            control.value = dateTimeLocalValue(field.value, timezoneName);
          }
        }
        control.name = field.name;
        control.required = Boolean(field.required);
        if (
          field.input_type === "timezone"
          && !field.value
        ) {
          control.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
        }
        if (control.tagName !== "SELECT" && control.type !== "datetime-local" && field.value !== null && field.value !== undefined) {
          control.value = String(field.value);
        }
        wrapper.append(control);
        if (field.help_text) wrapper.append(element("small", "hitl-field-help", field.help_text));
        fields.append(wrapper);
      }
      form.append(fields);
      const formError = element("p", "hitl-error");
      formError.hidden = true;
      formError.setAttribute("role", "alert");
      form.append(formError);
      const submit = element("button", "button button-primary", "Save details");
      submit.type = "submit";
      submit.disabled = hitlBusy;
      form.append(submit);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (hitlBusy || !form.reportValidity()) return;
        const values = {};
        try {
          const rawValues = Object.fromEntries(
            [...new FormData(form).entries()].map(([name, value]) => [
              name,
              String(value).trim(),
            ]),
          );
          for (const [name, value] of Object.entries(rawValues)) {
            const field = operation.fields.find((candidate) => candidate.name === name);
            if (!value) continue;
            if (field?.input_type === "datetime") {
              const zoneName = name === "scheduled_at"
                ? rawValues.scheduled_timezone
                : name === "due_date"
                  ? rawValues.due_timezone
                  : "UTC";
              const occurrence = name === "scheduled_at"
                ? rawValues.scheduled_time_occurrence
                : rawValues.due_time_occurrence;
              values[name] = dateTimeLocalToISO(value, zoneName || "UTC", occurrence || "");
            } else if (field?.input_type === "number") {
              values[name] = Number(value);
            } else if (field?.input_type === "record" && /^\d+$/.test(value)) {
              values[name] = Number(value);
            } else {
              values[name] = value;
            }
          }
        } catch (error) {
          formError.textContent = error.message || "Enter a valid local date and time.";
          formError.hidden = false;
          return;
        }
        void submitHitlInputs(task.task_id, index, values, formError, operation.action);
      });
      operationCard.append(form);
    }

    if (operation.status === "pending" && operation.action_id) {
      const decisionButtons = element("div", "agent-action-buttons");
      decisionButtons.append(
        taskActionButton("Approve & execute", "button button-primary", () => {
          void decideHitlAction(operation.action_id, "approve");
        }, hitlBusy),
        taskActionButton("Reject", "button button-secondary", () => {
          void decideHitlAction(operation.action_id, "reject");
        }, hitlBusy),
        taskActionButton("Cancel action", "button button-secondary", () => {
          void decideHitlAction(operation.action_id, "cancel");
        }, hitlBusy),
      );
      operationCard.append(decisionButtons);
    }
    card.append(operationCard);
  }
  return card;
}

function renderHitlTasks(tasks) {
  hitlTaskList.replaceChildren();
  if (!tasks.length) {
    hitlTaskList.append(element("p", "hitl-empty", "No saved agent tasks yet."));
    return;
  }
  for (const task of tasks) hitlTaskList.append(renderHitlTask(task));
}

async function refreshHitlTasks() {
  if (!requireAuth() || hitlBusy) return;
  hitlBusy = true;
  setHitlError("");
  hitlStatus.textContent = "Refreshing saved tasks from the server…";
  try {
    hitlTasks = await getAgentTasks();
    if (agentClarificationTask) {
      agentClarificationTask = hitlTasks.find(
        (task) => task.task_id === agentClarificationTask.task_id,
      ) || agentClarificationTask;
    }
    renderHitlTasks(hitlTasks);
    hitlStatus.textContent = `Updated from server · ${hitlTasks.length} saved ${hitlTasks.length === 1 ? "task" : "tasks"}`;
  } catch (error) {
    setHitlError(error.message || "Unable to refresh HITL tasks.");
    hitlStatus.textContent = "";
  } finally {
    hitlBusy = false;
    renderHitlTasks(hitlTasks);
  }
}

async function refreshOneHitlTask(taskId) {
  if (hitlBusy) return;
  hitlBusy = true;
  setHitlError("");
  try {
    await resumeAgentTask(taskId);
    hitlTasks = await getAgentTasks();
    renderHitlTasks(hitlTasks);
    hitlStatus.textContent = "Task revalidated with the server.";
  } catch (error) {
    setHitlError(error.message || "Unable to resume this task.");
  } finally {
    hitlBusy = false;
    renderHitlTasks(hitlTasks);
  }
}

async function submitHitlInputs(taskId, operationIndex, values, formError, action) {
  hitlBusy = true;
  setHitlError("");
  formError.hidden = true;
  let preserveForm = false;
  try {
    const updatedTask = await submitAgentTaskInput(taskId, operationIndex, values);
    if (agentClarificationTask?.task_id === taskId) {
      agentClarificationTask = updatedTask;
    }
    hitlTasks = await getAgentTasks();
    renderHitlTasks(hitlTasks);
    hitlStatus.textContent = "Submitted and revalidated by the server.";
  } catch (error) {
    formError.replaceChildren(element("span", "", error.message || "Unable to save task details."));
    const suggestions = error.data?.error?.details?.suggestions;
    const unavailable = error.code === "schedule_conflict" || error.code === "schedule_in_past";
    preserveForm = unavailable;
    const scheduleField = action === "create_meeting"
      ? "scheduled_at"
      : action === "schedule_call"
        ? "scheduled_at"
        : action === "create_followup"
          ? "due_date"
          : null;
    if (unavailable && scheduleField && Array.isArray(suggestions)) {
      for (const suggestion of suggestions) {
        if (!suggestion?.starts_at || !suggestion?.timezone) continue;
        const button = element(
          "button",
          "button button-secondary schedule-suggestion",
          `Use ${formatDateTime(suggestion.starts_at, suggestion.timezone)} (${suggestion.timezone})`,
        );
        button.type = "button";
        button.addEventListener("click", () => {
          const form = formError.closest("form");
          if (!form) return;
          if (action === "create_meeting") {
            const dateInput = form.querySelector('[name="scheduled_date"]');
            const timeInput = form.querySelector('[name="scheduled_time"]');
            if (dateInput) dateInput.value = suggestion.starts_at.slice(0, 10);
            if (timeInput) timeInput.value = suggestion.starts_at.slice(11, 16);
          } else {
            const timeInput = form.querySelector(`[name="${scheduleField}"]`);
            if (timeInput) timeInput.value = suggestion.starts_at.slice(0, 16);
          }
          const timezoneField = action === "create_followup"
            ? "due_timezone"
            : "scheduled_timezone";
          const timezoneInput = form.querySelector(`[name="${timezoneField}"]`);
          if (timezoneInput) timezoneInput.value = suggestion.timezone;
          const occurrenceField = action === "create_followup"
            ? "due_time_occurrence"
            : "scheduled_time_occurrence";
          const occurrenceInput = form.querySelector(`[name="${occurrenceField}"]`);
          if (occurrenceInput) occurrenceInput.value = "";
          formError.replaceChildren(
            element("span", "", "Alternative selected. Review the time and choose Save details to retry."),
          );
        });
        formError.append(button);
      }
    }
    formError.hidden = false;
  } finally {
    hitlBusy = false;
    if (!preserveForm) renderHitlTasks(hitlTasks);
  }
}

async function decideHitlAction(actionId, decision) {
  if (hitlBusy) return;
  hitlBusy = true;
  setHitlError("");
  try {
    if (decision === "approve") {
      const result = await confirmAgentAction(actionId);
      hitlStatus.textContent = result.message || `Action ${result.status}.`;
    } else if (decision === "reject") {
      const result = await rejectAgentAction(actionId);
      hitlStatus.textContent = result.message || "Action rejected.";
    } else {
      const result = await cancelAgentAction(actionId);
      hitlStatus.textContent = result.message || "Action cancelled.";
    }
    hitlTasks = await getAgentTasks();
    renderHitlTasks(hitlTasks);
    if (selectedCustomer) await refreshSelectedCustomer();
  } catch (error) {
    setHitlError(error.message || "The action decision could not be completed.");
    try {
      hitlTasks = await getAgentTasks();
      renderHitlTasks(hitlTasks);
    } catch (refreshError) {
      setHitlError(`${error.message || "Action failed."} ${refreshError.message || "State refresh also failed."}`);
    }
  } finally {
    hitlBusy = false;
    renderHitlTasks(hitlTasks);
  }
}

async function cancelHitlTask(taskId) {
  if (hitlBusy) return;
  hitlBusy = true;
  setHitlError("");
  try {
    const task = await cancelAgentTask(taskId);
    hitlStatus.textContent = task.message || "Task cancelled.";
    hitlTasks = await getAgentTasks();
    renderHitlTasks(hitlTasks);
  } catch (error) {
    setHitlError(error.message || "Unable to cancel this task.");
  } finally {
    hitlBusy = false;
    renderHitlTasks(hitlTasks);
  }
}

function renderHitlPolicies(policies) {
  hitlPolicyModes = policies;
  hitlPolicies.replaceChildren();
  for (const action of hitlActions) {
    const row = element("div", "hitl-policy-row");
    row.append(element("strong", "", agentActionLabels[action]));
    const select = element("select", "hitl-policy-select");
    select.setAttribute("aria-label", `Policy for ${agentActionLabels[action]}`);
    for (const mode of hitlModes) {
      const option = element("option", "", humanize(mode));
      option.value = mode;
      option.selected = policies[action] === mode;
      select.append(option);
    }
    const save = element("button", "button button-secondary", "Save policy");
    save.type = "button";
    save.addEventListener("click", async () => {
      save.disabled = true;
      setHitlError("");
      try {
        await updateAgentPolicy(action, select.value);
        renderHitlPolicies(await getAgentPolicies());
        hitlStatus.textContent = `Policy updated for ${agentActionLabels[action]}.`;
      } catch (error) {
        setHitlError(error.message || "Unable to update action policy.");
        save.disabled = false;
      }
    });
    row.append(select, save);
    hitlPolicies.append(row);
  }
}

async function initializeHitl() {
  try {
    const user = await getCurrentUser();
    hitlIsAdmin = user.role === "admin";
    if (hitlIsAdmin) {
      hitlPolicyPanel.hidden = false;
      renderHitlPolicies(await getAgentPolicies());
    }
  } catch (error) {
    setHitlError(error.message || "Unable to load the current account.");
    return;
  }
  await refreshHitlTasks();
}

async function startHitlTask(event) {
  event.preventDefault();
  if (hitlBusy) return;
  const values = selectedCustomerId ? { customer_id: Number(selectedCustomerId) } : {};
  if (hitlActionType.value === "create_meeting") {
    values.scheduled_timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  }
  hitlBusy = true;
  setHitlError("");
  hitlStatus.textContent = "Creating a persisted task…";
  try {
    const task = await createAgentTask([
      { action: hitlActionType.value, values },
    ]);
    hitlStatus.textContent = task.message;
    hitlTasks = await getAgentTasks();
    renderHitlTasks(hitlTasks);
  } catch (error) {
    setHitlError(error.message || "Unable to create the task.");
    hitlStatus.textContent = "";
  } finally {
    hitlBusy = false;
    renderHitlTasks(hitlTasks);
  }
}

document.querySelector("#refresh-hitl").addEventListener("click", () => {
  void refreshHitlTasks();
});
toggleHitlButton.addEventListener("click", () => {
  const expanded = toggleHitlButton.getAttribute("aria-expanded") === "true";
  hitlContent.hidden = expanded;
  toggleHitlButton.setAttribute("aria-expanded", String(!expanded));
  toggleHitlButton.querySelector(".hitl-collapse-label").textContent =
    expanded ? "Expand" : "Collapse";
  toggleHitlButton.querySelector(".hitl-collapse-icon").textContent =
    expanded ? "⌄" : "⌃";
});
hitlCreateForm.addEventListener("submit", startHitlTask);

function appendAgentChat(parent, customer) {
  const chat = element("section", "agent-chat");
  chat.append(
    element("h3", "", "Ask the CRM assistant"),
    element(
      "p",
      "ai-status",
      "Ask about this customer or request a CRM action. Current server policy determines whether explicit approval is required.",
    ),
  );
  const messages = element("div", "agent-chat-messages");
  messages.setAttribute("aria-live", "polite");
  messages.dataset.customerId = String(customer.customer_id);
  messages.dataset.count = String(agentChatMessages.length);
  for (const item of agentChatMessages) {
    const message = element("div", `agent-chat-message agent-chat-${item.role}`);
    if (item.role === "assistant") {
      message.classList.add("ai-markdown");
      renderMarkdown(message, item.text);
    } else {
      message.textContent = item.text;
    }
    messages.append(message);
  }
  chat.append(messages);

  if (agentChatLoading) {
    chat.append(element("p", "ai-status", "Assistant is working…"));
  }
  if (agentActionNotice) {
    chat.append(element("p", "agent-action-notice", agentActionNotice));
  }
  if (pendingAgentAction) {
    appendPendingActionCard(chat, pendingAgentAction, customer);
  }
  // Full task forms live in the Agent approvals panel; chat only links to them.
  if (
    agentClarificationTask?.status === "collecting_information"
    && !agentScheduleDraft
  ) {
    const actions = element("div", "agent-action-buttons");
    const button = element("button", "button button-secondary", "Add the missing details");
    button.type = "button";
    button.addEventListener("click", openHitlPanel);
    actions.append(button);
    chat.append(actions);
  }
  if (agentScheduleDraft && !pendingAgentAction && agentScheduleAlternatives.length) {
    const alternatives = element("div", "agent-action-buttons");
    alternatives.setAttribute("aria-label", "Available alternative times");
    for (const suggestion of agentScheduleAlternatives) {
      if (!suggestion?.starts_at || !suggestion?.timezone) continue;
      const button = element(
        "button",
        "button button-secondary schedule-suggestion",
        `Use ${formatDateTime(suggestion.starts_at, suggestion.timezone)} (${suggestion.timezone})`,
      );
      button.type = "button";
      button.disabled = agentChatLoading;
      button.addEventListener("click", () => {
        void chooseAgentScheduleAlternative(suggestion);
      });
      alternatives.append(button);
    }
    chat.append(alternatives);
  }
  if (
    agentScheduleDraft
    && agentScheduleReason === "past"
    && !pendingAgentAction
    && !agentScheduleAlternatives.length
  ) {
    const label = { create_meeting: "meeting", schedule_call: "call", create_followup: "follow-up" }[agentScheduleDraft.action];
    if (label) {
      const actions = element("div", "agent-action-buttons");
      const button = element("button", "button button-primary", `Schedule a new ${label}`);
      button.type = "button";
      button.disabled = agentChatLoading;
      button.addEventListener("click", () => {
        void requestNewScheduleTimes();
      });
      actions.append(button);
      chat.append(actions);
    }
  }

  const form = element("form", "agent-chat-form");
  const input = element("textarea", "agent-chat-input");
  input.name = "message";
  input.rows = 1;
  input.maxLength = 4000;
  input.placeholder = "Ask a question or request an action… (Shift+Enter for a new line)";
  input.required = true;
  input.value = agentChatDraft;
  input.disabled = agentChatLoading || Boolean(pendingAgentAction);
  input.addEventListener("input", () => {
    agentChatDraft = input.value;
    autosizeChatInput(input);
  });
  input.addEventListener("keydown", (event) => {
    // Enter sends; Shift+Enter adds a line; never interrupt IME composition.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  const submit = element(
    "button",
    "button button-primary",
    agentChatLoading ? "Sending…" : "Send",
  );
  submit.type = "submit";
  submit.disabled = input.disabled;
  form.append(input, submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (message && !agentChatLoading && !pendingAgentAction) {
      agentChatDraft = "";
      void sendAgentChatMessage(message, customer.customer_id);
    }
  });
  chat.append(form);
  parent.append(chat);
}

const CHAT_INPUT_MAX_HEIGHT = 160;

// Grows the prompt with its content up to a cap, then scrolls inside it.
function autosizeChatInput(input) {
  input.style.height = "auto";
  const height = Math.min(input.scrollHeight + 2, CHAT_INPUT_MAX_HEIGHT);
  input.style.height = `${height}px`;
  input.style.overflowY = input.scrollHeight + 2 > CHAT_INPUT_MAX_HEIGHT ? "auto" : "hidden";
}

// Captures the chat log and prompt before renderCustomer() rebuilds them.
function captureChatView(customerId) {
  const log = detailPanel.querySelector(".agent-chat-messages");
  const input = detailPanel.querySelector(".agent-chat-input");
  return {
    log: log && log.dataset.customerId === String(customerId)
      ? {
        top: log.scrollTop,
        count: Number(log.dataset.count),
        atBottom: log.scrollHeight - log.scrollTop - log.clientHeight < 24,
      }
      : null,
    inputFocused: Boolean(input) && document.activeElement === input,
    selection: input ? [input.selectionStart, input.selectionEnd] : null,
  };
}

function restoreChatView(view) {
  const log = detailPanel.querySelector(".agent-chat-messages");
  if (log) {
    const grew = !view.log || agentChatMessages.length > view.log.count;
    const lastIsUser = agentChatMessages.at(-1)?.role === "user";
    // Follow new messages only when the reader was already at the latest one,
    // or just sent it; otherwise keep their place in older history.
    log.scrollTop = grew && (!view.log || view.log.atBottom || lastIsUser)
      ? log.scrollHeight
      : view.log.top;
  }
  const input = detailPanel.querySelector(".agent-chat-input");
  if (input) {
    autosizeChatInput(input);
    if (view.inputFocused && !input.disabled) {
      input.focus({ preventScroll: true });
      if (view.selection) input.setSelectionRange(...view.selection);
    }
  }
}

function appendPendingActionCard(parent, pending, customer) {
  const card = element("section", "agent-pending-action");
  card.setAttribute("aria-label", "Confirm proposed CRM action");
  card.append(element("h4", "", `Confirm: ${agentActionLabels[pending.action] || "CRM action"}`));
  const actions = element("div", "agent-action-buttons");
  const confirm = element(
    "button",
    "button button-primary",
    agentActionLoading ? "Working…" : "Confirm",
  );
  confirm.type = "button";
  confirm.disabled = agentActionLoading;
  confirm.addEventListener("click", () => {
    void confirmPendingAgentAction(customer.customer_id);
  });
  const cancel = element("button", "button button-secondary", "Cancel");
  cancel.type = "button";
  cancel.disabled = agentActionLoading;
  cancel.addEventListener("click", () => {
    void cancelPendingAgentAction();
  });
  actions.append(confirm, cancel);
  card.append(actions);
  parent.append(card);
}

async function sendAgentChatMessage(message, customerId) {
  agentChatMessages.push({ role: "user", text: message });
  agentChatLoading = true;
  agentActionNotice = "";
  renderCustomer(selectedCustomer);
  try {
    const response = await chatWithAgent(message, customerId, agentConversationId);
    if (String(selectedCustomerId) !== String(customerId)) return;
    agentConversationId = response.conversation_id || agentConversationId;
    agentChatMessages.push({ role: "assistant", text: response.response });
    pendingAgentAction = response.pending_action || null;
    agentClarificationTask = response.clarification_task || null;
    agentScheduleAlternatives = response.schedule_alternatives || [];
    agentScheduleReason = response.schedule_reason || null;
    const scheduleOperation = agentClarificationTask?.operations?.[0];
    agentScheduleDraft = agentScheduleReason && scheduleOperation
      ? {
        taskId: agentClarificationTask.task_id,
        action: scheduleOperation.action,
        values: taskInputValues(scheduleOperation.action, scheduleOperation.values),
      }
      : null;
    if (agentClarificationTask) {
      hitlTasks = await getAgentTasks();
      renderHitlTasks(hitlTasks);
    }
  } catch (error) {
    if (String(selectedCustomerId) !== String(customerId)) return;
    if (error?.code === "conversation_not_found") agentConversationId = null;
    agentChatMessages.push({
      role: "assistant",
      text: error?.code === "conversation_not_found"
        ? "This conversation is no longer available. Your next message starts a new one."
        : aiErrorMessage(error),
    });
  } finally {
    agentChatLoading = false;
    if (selectedCustomer) renderCustomer(selectedCustomer);
  }
}

function scheduleValuesFromSuggestion(action, suggestion) {
  // starts_at is local to suggestion.timezone, so its date/time text is wall-clock time.
  if (action === "create_meeting") {
    return {
      scheduled_date: suggestion.starts_at.slice(0, 10),
      scheduled_time: suggestion.starts_at.slice(11, 16),
      scheduled_timezone: suggestion.timezone,
      scheduled_time_occurrence: null,
    };
  }
  if (action === "schedule_call") {
    return {
      scheduled_at: suggestion.starts_at,
      scheduled_timezone: suggestion.timezone,
      scheduled_time_occurrence: null,
    };
  }
  if (action === "create_followup") {
    return {
      due_date: suggestion.starts_at,
      due_timezone: suggestion.timezone,
      due_time_occurrence: null,
    };
  }
  return null;
}

// A complete meeting task reports proposal values (scheduled_at); creating a
// replacement task needs the task form's local scheduled_date/scheduled_time.
function taskInputValues(action, values) {
  if (action !== "create_meeting" || !values?.scheduled_at) return values;
  const { scheduled_at: scheduledAt, ...rest } = values;
  const parts = zonedDateTimeParts(scheduledAt, values.scheduled_timezone || "UTC");
  return {
    ...rest,
    scheduled_date: `${parts.year}-${parts.month}-${parts.day}`,
    scheduled_time: `${parts.hour}:${parts.minute}`,
  };
}

function scrollToElement(target) {
  const reduceMotion = document.documentElement.dataset.motion === "reduce"
    || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}

function openHitlPanel() {
  if (toggleHitlButton.getAttribute("aria-expanded") !== "true") toggleHitlButton.click();
  scrollToElement(hitlContent.closest(".hitl-panel"));
}

// Recommendations link to one record on its customer's page, e.g. #followup-12. The
// page only renders the linked customer's own records, so a missing target is reported
// instead of scrolling somewhere else.
function followRecordLink() {
  const match = /^#((?:enquiry|followup|meeting)-\d+)$/.exec(window.location.hash);
  if (!match || !selectedCustomer) return;
  const target = document.getElementById(match[1]);
  if (!target) {
    const notice = element("p", "detail-api-error record-link-missing", "The linked record was not found for this customer.");
    notice.setAttribute("role", "alert");
    detailPanel.prepend(notice);
    return;
  }
  target.classList.add("is-linked-record");
  target.tabIndex = -1;
  scrollToElement(target);
  target.focus({ preventScroll: true });
}

// A deep link scrolls as soon as the page starts, but the customer list, the linked
// customer and the task list then render above or inside the panel and push it down.
// Scroll once more when those loads settle, unless the user has already moved.
function followApprovalsLinkAfter(loads) {
  let userMoved = false;
  const markMoved = () => {
    userMoved = true;
  };
  const events = ["wheel", "touchmove", "keydown", "pointerdown"];
  for (const name of events) window.addEventListener(name, markMoved, { passive: true });
  void Promise.allSettled(loads).then(() => {
    for (const name of events) window.removeEventListener(name, markMoved);
    if (userMoved || window.location.hash !== "#approvals") return;
    openHitlPanel();
    // Start keyboard navigation from the panel the link pointed to.
    document.querySelector("#hitl-heading")?.focus({ preventScroll: true });
  });
}

const SUBMITTED_TASK_STATUSES = new Set(["awaiting_approval", "executing", "completed"]);

// Sends a scheduling choice through the task lifecycle the task is actually in:
// an open task takes the input; a submitted task is never resubmitted (no duplicate
// proposals); an expired, cancelled, rejected, failed or missing task is replaced by
// a fresh task built from the original request, so the conversation can continue.
async function submitScheduleChoice(values) {
  const draft = agentScheduleDraft;
  let task = await resumeAgentTask(draft.taskId).catch(() => null);
  if (task?.status === "collecting_information") {
    try {
      return await submitAgentTaskInput(task.task_id, 0, values);
    } catch (error) {
      if (error.code !== "invalid_task_transition" && error.code !== "task_expired") throw error;
      task = await resumeAgentTask(draft.taskId).catch(() => null);
    }
  }
  if (task && SUBMITTED_TASK_STATUSES.has(task.status)) return task;
  return createAgentTask([{ action: draft.action, values: { ...draft.values, ...values } }]);
}

// Shows the outcome of a scheduling choice: a compact confirmation card for a
// proposal awaiting approval, or a short status message otherwise.
function applyScheduleTask(task) {
  agentClarificationTask = task;
  agentScheduleDraft = { ...agentScheduleDraft, taskId: task.task_id };
  agentScheduleAlternatives = [];
  agentScheduleReason = null;
  const operation = task.operations?.[0];
  if (task.status === "awaiting_approval" && operation?.action_id) {
    pendingAgentAction = {
      action_id: operation.action_id,
      action: operation.action,
      payload: operation.values,
      fromTask: true,
    };
  } else if (task.status === "completed") {
    agentActionNotice = "This request was already completed.";
  } else if (task.status === "collecting_information") {
    agentActionNotice = "Some details are still missing. Add them under Agent approvals.";
  } else {
    agentActionNotice = task.message;
  }
}

async function requestNewScheduleTimes() {
  if (!agentScheduleDraft || agentChatLoading) return;
  const customerId = selectedCustomerId;
  agentChatLoading = true;
  agentActionNotice = "";
  renderCustomer(selectedCustomer);
  try {
    // The unchanged past time is rejected server-side with checked future alternatives.
    const task = await submitScheduleChoice({});
    if (String(selectedCustomerId) !== String(customerId)) return;
    applyScheduleTask(task);
  } catch (error) {
    if (String(selectedCustomerId) !== String(customerId)) return;
    const suggestions = error.data?.error?.details?.suggestions;
    if (error.code === "schedule_in_past" && Array.isArray(suggestions)) {
      agentScheduleReason = null;
      agentScheduleAlternatives = suggestions;
      agentActionNotice = suggestions.length
        ? "Choose an available future time."
        : "No available future times were found. Ask the assistant for a different day.";
    } else {
      agentActionNotice = aiErrorMessage(error);
    }
  } finally {
    agentChatLoading = false;
    if (selectedCustomer) renderCustomer(selectedCustomer);
  }
}

async function chooseAgentScheduleAlternative(suggestion) {
  const values = scheduleValuesFromSuggestion(agentScheduleDraft?.action, suggestion);
  if (!values || agentChatLoading) return;
  const customerId = selectedCustomerId;
  agentChatLoading = true;
  agentActionNotice = "";
  renderCustomer(selectedCustomer);
  try {
    // The server rechecks availability before creating the approval proposal.
    const task = await submitScheduleChoice(values);
    if (String(selectedCustomerId) !== String(customerId)) return;
    agentChatMessages.push({
      role: "assistant",
      text: `Selected ${formatDateTime(suggestion.starts_at, suggestion.timezone)} (${suggestion.timezone}).`,
    });
    applyScheduleTask(task);
    hitlTasks = await getAgentTasks();
    renderHitlTasks(hitlTasks);
  } catch (error) {
    if (String(selectedCustomerId) !== String(customerId)) return;
    const freshSuggestions = error.data?.error?.details?.suggestions;
    const unavailable = error.code === "schedule_conflict" || error.code === "schedule_in_past";
    agentScheduleAlternatives = unavailable && Array.isArray(freshSuggestions)
      ? freshSuggestions
      : [];
    agentActionNotice = unavailable
      ? (agentScheduleAlternatives.length
        ? "That time is no longer available. Choose another alternative."
        : "That time is no longer available, and no alternatives were found. Ask the assistant for a different day.")
      : aiErrorMessage(error);
  } finally {
    agentChatLoading = false;
    if (selectedCustomer) renderCustomer(selectedCustomer);
  }
}

async function confirmPendingAgentAction(customerId) {
  if (!pendingAgentAction || agentActionLoading) return;
  const actionId = pendingAgentAction.action_id;
  agentActionLoading = true;
  agentActionNotice = "";
  renderCustomer(selectedCustomer);
  try {
    const result = await confirmAgentAction(actionId);
    pendingAgentAction = null;
    agentChatMessages.push({
      role: "assistant",
      text: result.message || (result.status === "completed" ? "Action completed." : "Action failed."),
    });
    if (result.status === "completed") {
      await refreshSelectedCustomer();
    }
    await refreshHitlTasks();
  } catch (error) {
    agentActionNotice = aiErrorMessage(error);
    if (error?.status === 404 || error?.status === 410) {
      pendingAgentAction = null;
    }
  } finally {
    agentActionLoading = false;
    if (selectedCustomer && String(selectedCustomerId) === String(customerId)) {
      renderCustomer(selectedCustomer);
    }
  }
}

async function cancelPendingAgentAction() {
  if (!pendingAgentAction || agentActionLoading) return;
  const actionId = pendingAgentAction.action_id;
  agentActionLoading = true;
  agentActionNotice = "";
  renderCustomer(selectedCustomer);
  try {
    const result = await cancelAgentAction(actionId);
    pendingAgentAction = null;
    agentChatMessages.push({ role: "assistant", text: result.message });
    await refreshHitlTasks();
  } catch (error) {
    agentActionNotice = aiErrorMessage(error);
    if (error?.status === 404 || error?.status === 410) {
      pendingAgentAction = null;
    }
  } finally {
    agentActionLoading = false;
    if (selectedCustomer) renderCustomer(selectedCustomer);
  }
}

function appendAiIntelligence(parent, customer) {
  const section = appendSection(parent, "AI customer intelligence");
  section.id = "customer-assistant";
  const panel = element("div", "ai-intelligence");
  const actions = element("div", "ai-actions");
  const summaryButton = makeActionButton(
    aiSummaryLoading ? "Generating…" : "Generate AI Summary",
    "small-action-button",
    () => generateSummary(customer.customer_id),
  );
  summaryButton.disabled = aiSummaryLoading;
  const meetingButton = makeActionButton(
    meetingBriefLoadingId ? "Preparing…" : "Prepare Me for Meeting",
    "small-action-button",
    () => prepareLatestMeeting(customer),
  );
  meetingButton.disabled = Boolean(meetingBriefLoadingId);
  actions.append(summaryButton, meetingButton);
  panel.append(actions);

  if (aiSummaryLoading) {
    panel.append(element("p", "ai-status", "Generating customer summary…"));
  } else if (aiSummaryError) {
    panel.append(element("p", "detail-api-error", aiSummaryError));
  } else if (aiSummary) {
    const disclosure = element("details", "disclosure ai-summary");
    disclosure.open = !aiSummaryCollapsed;
    disclosure.addEventListener("toggle", () => {
      aiSummaryCollapsed = !disclosure.open;
    });
    disclosure.append(element("summary", "ai-summary-toggle", "AI summary"));
    const output = element("div", "ai-output");
    const summaryBlock = element("div", "ai-block ai-facts");
    summaryBlock.append(element("h4", "", "Customer summary"));
    const summary = element("div", "ai-markdown");
    renderMarkdown(summary, displayValue(aiSummary.summary, "Summary unavailable."));
    summaryBlock.append(summary);
    output.append(summaryBlock);
    appendListBlock(output, "Key points", aiSummary.key_points, "No key points available.");
    appendListBlock(output, "Customer concerns", aiSummary.customer_concerns, "No customer concerns recorded.");
    appendListBlock(output, "Open follow-ups", aiSummary.open_followups, "No open follow-ups.");
    const recommendation = element("div", "ai-block ai-recommendation");
    recommendation.append(element("h4", "", "Recommended next action"));
    const recommendationText = element("div", "ai-markdown");
    renderMarkdown(
      recommendationText,
      displayValue(aiSummary.recommended_next_action, "Recommendation unavailable."),
    );
    recommendation.append(recommendationText);
    output.append(recommendation);
    if (aiSummary.generated_at) {
      output.append(element("p", "ai-generated", `Generated ${formatDateTime(aiSummary.generated_at)}`));
    }
    disclosure.append(output);
    panel.append(disclosure);
  } else {
    panel.append(element("p", "ai-status", "Generate a summary from this customer's CRM records."));
  }
  appendAgentChat(panel, customer);
  section.append(panel);
}

async function generateSummary(customerId) {
  aiSummaryLoading = true;
  aiSummaryError = "";
  renderCustomer(selectedCustomer);
  try {
    aiSummary = await generateCustomerSummary(customerId);
  } catch (error) {
    aiSummary = null;
    aiSummaryError = aiErrorMessage(error);
  } finally {
    aiSummaryLoading = false;
    if (selectedCustomer) renderCustomer(selectedCustomer);
  }
}

function latestMeeting(customer) {
  const meetings = Array.isArray(customer?.meetings) ? [...customer.meetings] : [];
  meetings.sort((left, right) => String(right.scheduled_at || "").localeCompare(String(left.scheduled_at || "")));
  return meetings[0] || null;
}

async function prepareLatestMeeting(customer) {
  const meeting = latestMeeting(customer);
  if (!meeting) {
    openMeetingBrief({
      error: "No meetings recorded for this customer.",
    });
    return;
  }
  await prepareMeeting(meeting.meeting_id);
}

function renderMeetingBrief(brief) {
  meetingBriefContent.replaceChildren();
  if (brief.error) {
    meetingBriefContent.append(element("p", "detail-api-error", brief.error));
    return;
  }
  if (brief.loading) {
    meetingBriefContent.append(element("p", "ai-status", "Preparing meeting brief…"));
    return;
  }
  const output = element("div", "ai-output");
  const facts = element("div", "ai-block ai-facts");
  facts.append(element("h4", "", "Facts"));
  const briefText = element("div", "ai-markdown");
  renderMarkdown(briefText, displayValue(brief.brief, "Brief unavailable."));
  facts.append(briefText);
  output.append(facts);
  const overview = element("div", "ai-block ai-facts");
  overview.append(element("h4", "", "Customer overview"));
  const overviewText = element("div", "ai-markdown");
  renderMarkdown(
    overviewText,
    displayValue(brief.customer_overview, "Customer overview unavailable."),
  );
  overview.append(overviewText);
  output.append(overview);
  const requirement = element("div", "ai-block ai-facts");
  requirement.append(element("h4", "", "Current requirement"));
  const requirementText = element("div", "ai-markdown");
  renderMarkdown(
    requirementText,
    displayValue(brief.current_requirement, "Current requirement unavailable."),
  );
  requirement.append(requirementText);
  output.append(requirement);
  appendListBlock(output, "Previous discussions", brief.previous_discussions, "No previous discussions recorded.");
  appendListBlock(output, "Unresolved issues", brief.unresolved_issues, "No unresolved issues recorded.");
  appendListBlock(
    output,
    "Recommended talking points",
    brief.recommended_talking_points,
    "No talking points available.",
    "ai-recommendation",
  );
  const recommendation = element("div", "ai-block ai-recommendation");
  recommendation.append(element("h4", "", "Recommended next action"));
  const recommendationText = element("div", "ai-markdown");
  renderMarkdown(
    recommendationText,
    displayValue(brief.recommended_next_action, "Recommendation unavailable."),
  );
  recommendation.append(recommendationText);
  output.append(recommendation);
  if (brief.generated_at) {
    output.append(element("p", "ai-generated", `Generated ${formatDateTime(brief.generated_at)}`));
  }
  meetingBriefContent.append(output);
}

function openMeetingBrief(brief) {
  renderMeetingBrief(brief);
  if (!meetingBriefDialog.open) meetingBriefDialog.showModal();
}

async function prepareMeeting(meetingId) {
  meetingBriefLoadingId = meetingId;
  if (selectedCustomer) renderCustomer(selectedCustomer);
  openMeetingBrief({ loading: true });
  try {
    const brief = await generateMeetingBrief(meetingId);
    openMeetingBrief(brief);
  } catch (error) {
    openMeetingBrief({ error: aiErrorMessage(error) });
  } finally {
    meetingBriefLoadingId = null;
    if (selectedCustomer) renderCustomer(selectedCustomer);
  }
}

function appendActivities(parent, customer) {
  const meetings = Array.isArray(customer.meetings) ? customer.meetings : [];
  const meetingSection = appendSection(parent, "Meetings", meetings.length);
  meetingSection.id = "customer-activity";
  meetingSection.querySelector(".detail-section-heading").append(
    makeActionButton("Schedule meeting", "small-action-button", () => openActivityDialog("meeting")),
  );
  if (meetings.length === 0) {
    meetingSection.append(element("p", "section-empty", "No meetings recorded."));
  } else {
    const list = element("div", "activity-record-list");
    for (const meeting of meetings) {
      const card = element("article", "activity-record-card");
      card.id = `meeting-${meeting.meeting_id}`;
      const top = element("div", "activity-record-top");
      top.append(
        element(
          "strong",
          "",
          formatDateTime(meeting.scheduled_at, meeting.scheduled_timezone),
        ),
        element("span", statusClass(meeting.status), humanize(meeting.status)),
      );
      card.append(top);
      appendOptionalText(card, "Agenda", meeting.agenda);
      const contact = customer.contacts?.find((item) => item.contact_id === meeting.contact_id);
      appendOptionalText(card, "Contact", contact?.name);
      appendOptionalText(card, "Notes", meeting.notes);
      appendOptionalText(card, "Summary", meeting.summary);
      const prepareButton = makeActionButton(
        meetingBriefLoadingId === meeting.meeting_id ? "Preparing…" : "Prepare Me",
        "small-action-button",
        () => prepareMeeting(meeting.meeting_id),
      );
      prepareButton.disabled = meetingBriefLoadingId === meeting.meeting_id;
      card.append(
        makeActionButton(
          "Edit",
          "small-action-button",
          () => openActivityDialog("meeting", meeting),
        ),
        prepareButton,
      );
      list.append(card);
    }
    meetingSection.append(list);
  }

  const calls = Array.isArray(customer.calls) ? customer.calls : [];
  const callSection = appendSection(parent, "Calls", calls.length);
  callSection.querySelector(".detail-section-heading").append(
    makeActionButton("Log call", "small-action-button", () => openActivityDialog("call")),
  );
  if (calls.length === 0) {
    callSection.append(element("p", "section-empty", "No calls recorded."));
  } else {
    const list = element("div", "activity-record-list");
    for (const call of calls) {
      const card = element("article", "activity-record-card");
      const top = element("div", "activity-record-top");
      top.append(
        element("strong", "", displayValue(call.call_type, "Sales call")),
        element("span", statusClass(call.status), humanize(call.status)),
      );
      card.append(top);
      if (call.scheduled_at) {
        appendOptionalText(
          card,
          "Scheduled time",
          formatDateTime(call.scheduled_at, call.scheduled_timezone),
        );
      }
      if (call.actual_time) {
        appendOptionalText(card, "Actual time", formatDateTime(call.actual_time));
      }
      appendOptionalText(card, "Outcome", call.outcome);
      appendOptionalText(card, "Notes", call.notes);
      if (call.next_followup_date) {
        appendOptionalText(card, "Next follow-up", formatDateTime(call.next_followup_date));
      }
      card.append(
        makeActionButton(
          "Edit",
          "small-action-button",
          () => openActivityDialog("call", call),
        ),
      );
      list.append(card);
    }
    callSection.append(list);
  }

  const followups = Array.isArray(customer.followups) ? customer.followups : [];
  const followupSection = appendSection(parent, "Follow-ups", followups.length);
  followupSection.querySelector(".detail-section-heading").append(
    makeActionButton("Add follow-up", "small-action-button", () => openActivityDialog("followup")),
  );
  if (followups.length === 0) {
    followupSection.append(element("p", "section-empty", "No follow-ups recorded."));
  } else {
    const list = element("div", "activity-record-list");
    for (const followup of followups) {
      const card = element("article", "activity-record-card");
      card.id = `followup-${followup.followup_id}`;
      const top = element("div", "activity-record-top");
      top.append(
        element("strong", "", displayValue(followup.description, humanize(followup.type))),
        element("span", statusClass(followup.status), humanize(followup.status)),
      );
      card.append(top);
      appendOptionalText(card, "Type", humanize(followup.type));
      appendOptionalText(
        card,
        "Due",
        formatDateTime(followup.due_date, followup.due_timezone),
      );
      appendOptionalText(card, "Related enquiry", labelForId(customer.sales_enquiries, "enquiry_id", followup.enquiry_id, "product"));
      appendOptionalText(card, "Related meeting", labelForId(meetings, "meeting_id", followup.meeting_id, "agenda"));
      appendOptionalText(card, "Related call", labelForId(calls, "call_id", followup.call_id, "call_type"));
      if (followup.status !== "completed") {
        const completeButton = makeActionButton(
          "Mark completed",
          "text-action-button",
          () => completeFollowup(followup.followup_id),
        );
        completeButton.dataset.followupId = String(followup.followup_id);
        card.append(completeButton);
      }
      card.append(
        makeActionButton(
          "Edit",
          "small-action-button",
          () => openActivityDialog("followup", followup),
        ),
      );
      list.append(card);
    }
    followupSection.append(list);
  }

  const timelineSection = appendSection(parent, "Activity timeline");
  appendActivityToolbar(timelineSection, filteredActivity.length);
  renderActivityTimeline(timelineSection);
}

function appendOptionalText(parent, label, value) {
  if (!value) return;
  const row = element("p", "record-detail");
  row.append(element("span", "record-label", `${label}: `), element("span", "", value));
  parent.append(row);
}

function labelForId(records, key, id, labelKey) {
  if (id === null || id === undefined) return null;
  const record = records?.find((item) => item[key] === id);
  return record ? record[labelKey] || `${humanize(labelKey)} #${id}` : `#${id}`;
}

async function refreshSelectedCustomer() {
  if (!selectedCustomerId) return;
  const customer = await getCustomerOverview(selectedCustomerId);
  selectedCustomer = customer;
  const timeline = await getCustomerActivity(selectedCustomerId, {
    type: activityFilter,
    start_date: activityStartDate,
    end_date: activityEndDate,
  });
  filteredActivity = timeline.items ?? [];
  renderCustomer(customer);
}

function renderCustomer(customer) {
  const chatView = captureChatView(customer.customer_id);
  selectedCustomer = customer;
  detailPanel.replaceChildren();
  const content = element("div", "detail-content");
  const hero = element("div", "detail-hero");
  const headingGroup = element("div", "detail-heading-group");
  headingGroup.append(element("span", "detail-avatar", initials(customer.customer_name)));
  const heading = element("div", "detail-title");
  heading.append(
    element("p", "eyebrow", "CUSTOMER PROFILE"),
    element("h2", "", displayValue(customer.customer_name)),
    element("p", "", displayValue(customer.company?.company_name)),
  );
  headingGroup.append(heading);
  hero.append(headingGroup, element("span", statusClass(customer.status), humanize(customer.status)));
  content.append(hero);

  const jump = element("nav", "detail-jump");
  jump.setAttribute("aria-label", "Customer sections");
  for (const [label, id] of [["Overview", "customer-overview"], ["Assistant", "customer-assistant"], ["Activity", "customer-activity"]]) {
    jump.append(makeActionButton(label, "detail-jump-link", () => {
      const target = document.getElementById(id);
      if (target) scrollToElement(target);
    }));
  }
  content.append(jump);

  const profile = appendSection(content, "Customer profile");
  profile.id = "customer-overview";
  appendInfoGrid(profile, [
    ["Status", humanize(customer.status)],
    ["Sales stage", humanize(customer.sales_stage)],
    ["Customer ID", customer.customer_id],
    ["Created", formatDate(customer.created_at)],
    ["Last updated", formatDate(customer.updated_at)],
  ]);

  const company = customer.company ?? {};
  const companySection = appendSection(content, "Company information");
  const companyCard = element("div", "company-card");
  appendInfoGrid(companyCard, [
    ["Company name", company.company_name],
    ["Industry", company.industry],
    ["Website", company.website],
    ["Address", company.address],
    ["City", company.city],
    ["Country", company.country],
    ["Company size", company.company_size],
    ["Description", company.description],
  ]);
  companySection.append(companyCard);

  const contacts = Array.isArray(customer.contacts) ? customer.contacts : [];
  const contactSection = appendSection(content, "Contacts", contacts.length);
  if (contacts.length === 0) {
    contactSection.append(element("p", "section-empty", "No contacts recorded."));
  } else {
    const list = element("div", "contact-list");
    for (const contact of contacts) {
      const card = element("article", "contact-card");
      const top = element("div", "contact-card-top");
      const identity = element("div");
      identity.append(
        element("span", "contact-name", displayValue(contact.name)),
        element("span", "contact-title", displayValue(contact.job_title)),
      );
      top.append(identity);
      if (contact.is_primary) top.append(element("span", "primary-badge", "Primary contact"));
      card.append(top);
      const meta = element("div", "contact-meta");
      if (contact.email) {
        const email = element("a", "", contact.email);
        email.href = `mailto:${contact.email}`;
        meta.append(email);
      }
      if (contact.phone) {
        const phone = element("a", "", contact.phone);
        phone.href = `tel:${contact.phone}`;
        meta.append(phone);
      }
      if (!contact.email && !contact.phone) meta.append(element("span", "", "No contact details"));
      card.append(meta);
      list.append(card);
    }
    contactSection.append(list);
  }

  const enquiries = Array.isArray(customer.sales_enquiries) ? customer.sales_enquiries : [];
  const enquirySection = appendSection(content, "Sales enquiries", enquiries.length);
  if (enquiries.length === 0) {
    enquirySection.append(element("p", "section-empty", "No sales enquiries recorded."));
  } else {
    const list = element("div", "enquiry-list");
    for (const enquiry of enquiries) {
      const card = element("article", "enquiry-card");
      card.id = `enquiry-${enquiry.enquiry_id}`;
      const top = element("div", "enquiry-card-top");
      const identity = element("div");
      identity.append(
        element("span", "enquiry-product", displayValue(enquiry.product, "Sales enquiry")),
        element("span", "enquiry-date", `Created ${formatDate(enquiry.created_at)}`),
      );
      top.append(identity, element("span", statusClass(enquiry.status), humanize(enquiry.status)));
      card.append(
        top,
        element("p", "enquiry-description", displayValue(enquiry.enquiry_text)),
      );
      const meta = element("div", "enquiry-meta");
      meta.append(
        element("span", "meta-pill", `${humanize(enquiry.priority)} priority`),
        element("span", "meta-pill", `Estimated value: ${formatNumber(enquiry.estimated_value)}`),
      );
      card.append(meta);
      list.append(card);
    }
    enquirySection.append(list);
  }
  appendAiIntelligence(content, customer);
  appendActivities(content, customer);
  detailPanel.append(content);
  restoreChatView(chatView);
}

async function selectCustomer(customerId) {
  if (!requireAuth()) return;
  resetCustomerAiState({ cancelPending: true });
  selectedCustomerId = customerId;
  hitlCustomerContext.textContent = `New tasks will start with customer #${customerId}; confirm the record in the form.`;
  selectedCustomer = null;
  resultList.querySelectorAll(".customer-result").forEach((button) => {
    button.setAttribute("aria-current", String(button.dataset.customerId) === String(customerId));
  });
  setDetailMessage("Loading customer...", { loading: true });
  try {
    selectedCustomerId = customerId;
    await refreshSelectedCustomer();
  } catch {
    setDetailMessage("Unable to load customer data.", { error: true });
    return;
  }
  await restoreAgentConversation(customerId);
}

// Restores this user's latest chat with the customer; a failure leaves an empty chat.
async function restoreAgentConversation(customerId) {
  try {
    const conversation = await getLatestConversation(customerId);
    if (String(selectedCustomerId) !== String(customerId) || !conversation) return;
    if (agentChatMessages.length || agentChatLoading) return; // Never overwrite a chat already in progress.
    agentConversationId = conversation.conversation_id;
    agentChatMessages = conversation.messages.map((item) => ({ role: item.role, text: item.content }));
    if (selectedCustomer) renderCustomer(selectedCustomer);
  } catch {
    // History is a convenience; the chat still works without it.
  }
}

function addFormField({
  name,
  label,
  type = "text",
  required = false,
  options = [],
  min,
  defaultValue,
}) {
  const wrapper = element("label", "activity-field");
  wrapper.append(element("span", "", label));
  let control;
  if (type === "textarea") {
    control = element("textarea");
    control.rows = 3;
  } else if (type === "select") {
    control = element("select");
    const blank = element("option", "", "None");
    blank.value = "";
    control.append(blank);
    for (const optionData of options) {
      const option = element("option", "", optionData.label);
      option.value = String(optionData.value);
      control.append(option);
    }
  } else {
    control = element("input");
    control.type = type;
  }
  control.name = name;
  if (required) control.required = true;
  if (min !== undefined) control.min = String(min);
  if (type === "datetime-local") control.step = "60";
  if (defaultValue !== undefined) control.value = defaultValue;
  wrapper.append(control);
  activityFields.append(wrapper);
}

function relationshipOptions(records, idKey, nameSelector) {
  return (records ?? []).map((record) => ({
    value: record[idKey],
    label: nameSelector(record) || `Record #${record[idKey]}`,
  }));
}

function configureActivityForm(kind, record = null) {
  activityFields.replaceChildren();
  const contacts = relationshipOptions(
    selectedCustomer?.contacts,
    "contact_id",
    (contact) => contact.name,
  );
  const enquiries = relationshipOptions(
    selectedCustomer?.sales_enquiries,
    "enquiry_id",
    (enquiry) => enquiry.product || enquiry.enquiry_text,
  );
  const meetings = relationshipOptions(
    selectedCustomer?.meetings,
    "meeting_id",
    (meeting) => meeting.agenda || formatDateTime(meeting.scheduled_at, meeting.scheduled_timezone),
  );
  const calls = relationshipOptions(
    selectedCustomer?.calls,
    "call_id",
    (call) => call.call_type || formatDateTime(call.actual_time || call.scheduled_at),
  );

  if (kind === "meeting") {
    activityDialogTitle.textContent = record ? "Edit meeting" : "Schedule meeting";
    addFormField({ name: "scheduled_at", label: "Date and time", type: "datetime-local", required: true });
    addFormField({
      name: "scheduled_time_occurrence",
      label: "Repeated local time",
      type: "select",
      options: [
        { value: "earlier", label: "Earlier occurrence" },
        { value: "later", label: "Later occurrence" },
      ],
    });
    addFormField({ name: "duration", label: "Duration (minutes)", type: "number", min: 1 });
    addFormField({
      name: "contact_id",
      label: "Contact",
      type: "select",
      options: contacts,
      defaultValue: record?.contact_id ?? "",
    });
    addFormField({
      name: "enquiry_id",
      label: "Related enquiry",
      type: "select",
      options: enquiries,
      defaultValue: record?.enquiry_id ?? "",
    });
    addFormField({
      name: "status",
      label: "Status",
      type: "select",
      options: [
        { value: "scheduled", label: "Scheduled" },
        { value: "completed", label: "Completed" },
        { value: "cancelled", label: "Cancelled" },
        { value: "no_show", label: "No show" },
      ],
      defaultValue: record?.status ?? "scheduled",
    });
    addFormField({ name: "agenda", label: "Agenda", type: "textarea" });
    addFormField({ name: "notes", label: "Notes", type: "textarea" });
  } else if (kind === "call") {
    activityDialogTitle.textContent = record ? "Edit call" : "Log call";
    addFormField({ name: "call_type", label: "Call type" });
    addFormField({ name: "scheduled_at", label: "Scheduled time", type: "datetime-local" });
    addFormField({
      name: "scheduled_timezone",
      label: "Scheduling timezone (IANA)",
      defaultValue: record?.scheduled_timezone
        || Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    addFormField({
      name: "scheduled_time_occurrence",
      label: "Repeated local time",
      type: "select",
      options: [
        { value: "earlier", label: "Earlier occurrence" },
        { value: "later", label: "Later occurrence" },
      ],
    });
    addFormField({ name: "duration", label: "Duration (minutes)", type: "number", min: 1 });
    addFormField({ name: "actual_time", label: "Actual time", type: "datetime-local" });
    addFormField({
      name: "contact_id",
      label: "Contact",
      type: "select",
      options: contacts,
      defaultValue: record?.contact_id ?? "",
    });
    addFormField({
      name: "enquiry_id",
      label: "Related enquiry",
      type: "select",
      options: enquiries,
      defaultValue: record?.enquiry_id ?? "",
    });
    addFormField({
      name: "status",
      label: "Status",
      type: "select",
      options: [
        { value: "scheduled", label: "Scheduled" },
        { value: "attempted", label: "Attempted" },
        { value: "completed", label: "Completed" },
        { value: "failed", label: "Failed" },
        { value: "cancelled", label: "Cancelled" },
      ],
      defaultValue: record?.status ?? "scheduled",
    });
    addFormField({ name: "outcome", label: "Outcome", defaultValue: record?.outcome ?? "" });
    addFormField({ name: "notes", label: "Notes", type: "textarea", defaultValue: record?.notes ?? "" });
    addFormField({ name: "next_followup_date", label: "Next follow-up", type: "datetime-local" });
    const callType = activityFields.querySelector('[name="call_type"]');
    if (callType) callType.value = record?.call_type ?? "";
  } else {
    activityDialogTitle.textContent = record ? "Edit follow-up" : "Add follow-up";
    addFormField({
      name: "type",
      label: "Type",
      type: "select",
      defaultValue: record?.type ?? "",
      required: true,
      options: [
        { value: "call", label: "Call" },
        { value: "email", label: "Email" },
        { value: "meeting", label: "Meeting" },
        { value: "task", label: "Task" },
        { value: "other", label: "Other" },
      ],
    });
    addFormField({ name: "due_date", label: "Due date and time", type: "datetime-local", required: true });
    addFormField({
      name: "status",
      label: "Status",
      type: "select",
      options: [
        { value: "pending", label: "Pending" },
        { value: "in_progress", label: "In progress" },
        { value: "completed", label: "Completed" },
        { value: "cancelled", label: "Cancelled" },
        { value: "overdue", label: "Overdue" },
      ],
      defaultValue: record?.status ?? "pending",
    });
    addFormField({
      name: "due_timezone",
      label: "Due-time timezone (IANA)",
      defaultValue: record?.due_timezone
        || Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    addFormField({
      name: "due_time_occurrence",
      label: "Repeated local time",
      type: "select",
      options: [
        { value: "earlier", label: "Earlier occurrence" },
        { value: "later", label: "Later occurrence" },
      ],
    });
    addFormField({ name: "duration", label: "Reserved duration (minutes)", type: "number", min: 1 });
    addFormField({
      name: "assigned_to",
      label: "Assigned to",
      defaultValue: record?.assigned_to ?? "",
    });
    addFormField({
      name: "enquiry_id",
      label: "Related enquiry",
      type: "select",
      options: enquiries,
      defaultValue: record?.enquiry_id ?? "",
    });
    addFormField({
      name: "meeting_id",
      label: "Related meeting",
      type: "select",
      options: meetings,
      defaultValue: record?.meeting_id ?? "",
    });
    addFormField({
      name: "call_id",
      label: "Related call",
      type: "select",
      options: calls,
      defaultValue: record?.call_id ?? "",
    });
    addFormField({
      name: "description",
      label: "Description",
      type: "textarea",
      defaultValue: record?.description ?? "",
    });
  }

  if (kind === "meeting" && record) {
    const scheduledAt = activityFields.querySelector('[name="scheduled_at"]');
    scheduledAt.value = dateTimeLocalValue(
      record.scheduled_at,
      record.scheduled_timezone || "UTC",
    );
    selectedMeetingScheduleValue = scheduledAt.value;
    const duration = activityFields.querySelector('[name="duration"]');
    if (duration) duration.value = record.duration ?? "";
    const agenda = activityFields.querySelector('[name="agenda"]');
    if (agenda) agenda.value = record.agenda ?? "";
    const notes = activityFields.querySelector('[name="notes"]');
    if (notes) notes.value = record.notes ?? "";
  } else if (kind === "call" && record) {
    for (const field of ["scheduled_at", "actual_time", "next_followup_date"]) {
      const input = activityFields.querySelector(`[name="${field}"]`);
      const zone = field === "scheduled_at"
        ? record.scheduled_timezone || "UTC"
        : Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (input) input.value = dateTimeLocalValue(record[field], zone);
    }
    const duration = activityFields.querySelector('[name="duration"]');
    if (duration) duration.value = record.duration ?? "";
  } else if (kind === "followup" && record) {
    const dueDate = activityFields.querySelector('[name="due_date"]');
    if (dueDate) {
      dueDate.value = dateTimeLocalValue(
        record.due_date,
        record.due_timezone || "UTC",
      );
    }
    const duration = activityFields.querySelector('[name="duration"]');
    if (duration) duration.value = record.duration ?? "";
  } else {
    selectedMeetingScheduleValue = "";
  }
}

function openActivityDialog(kind, meeting = null) {
  if (!selectedCustomerId || !selectedCustomer) return;
  selectedActivityKind = kind;
  selectedMeetingForEdit = kind === "meeting" ? meeting : null;
  selectedCallForEdit = kind === "call" ? meeting : null;
  selectedFollowupForEdit = kind === "followup" ? meeting : null;
  activityForm.reset();
  activityFormError.hidden = true;
  activityFormError.replaceChildren();
  configureActivityForm(kind, meeting);
  saveActivityButton.textContent = meeting
    ? `Save ${kind === "followup" ? "follow-up" : kind}`
    : "Save activity";
  activityDialog.showModal();
  activityFields.querySelector("input, select, textarea")?.focus();
}

function optionalFormValue(formData, field) {
  const value = String(formData.get(field) ?? "").trim();
  return value || null;
}

function activityFormPayload(kind, formData) {
  const payload = { customer_id: Number(selectedCustomerId) };
  const stringFields = {
    meeting: ["status", "agenda", "notes"],
    call: ["call_type", "status", "outcome", "notes"],
    followup: ["type", "status", "assigned_to", "description"],
  }[kind];
  const relationFields = {
    meeting: ["contact_id", "enquiry_id"],
    call: ["contact_id", "enquiry_id"],
    followup: ["enquiry_id", "meeting_id", "call_id"],
  }[kind];
  for (const field of stringFields) {
    payload[field] = optionalFormValue(formData, field);
  }
  for (const field of relationFields) {
    const value = optionalFormValue(formData, field);
    payload[field] = value ? Number(value) : null;
  }
  if (kind === "meeting") {
    const localSchedule = optionalFormValue(formData, "scheduled_at");
    const timezoneName = selectedMeetingForEdit?.scheduled_timezone
      || Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (
      !selectedMeetingForEdit
      || localSchedule !== selectedMeetingScheduleValue
    ) {
      payload.scheduled_at = dateTimeLocalToISO(
        localSchedule,
        timezoneName,
        optionalFormValue(formData, "scheduled_time_occurrence"),
      );
      payload.scheduled_timezone = timezoneName;
    }
    const duration = optionalFormValue(formData, "duration");
    if (duration) payload.duration = Number(duration);
  } else if (kind === "call") {
    const timezoneName = optionalFormValue(formData, "scheduled_timezone")
      || Intl.DateTimeFormat().resolvedOptions().timeZone;
    const localSchedule = optionalFormValue(formData, "scheduled_at");
    const oldSchedule = selectedCallForEdit?.scheduled_at
      ? dateTimeLocalValue(
        selectedCallForEdit.scheduled_at,
        selectedCallForEdit.scheduled_timezone || "UTC",
      )
      : "";
    const timezoneChanged = selectedCallForEdit
      && timezoneName !== (selectedCallForEdit.scheduled_timezone || "UTC");
    if (!selectedCallForEdit || localSchedule !== oldSchedule || timezoneChanged) {
      payload.scheduled_at = localSchedule
        ? dateTimeLocalToISO(
          localSchedule,
          timezoneName,
          optionalFormValue(formData, "scheduled_time_occurrence"),
        )
        : null;
      payload.scheduled_timezone = timezoneName;
    }
    for (const field of ["actual_time", "next_followup_date"]) {
      const value = optionalFormValue(formData, field);
      const oldValue = selectedCallForEdit?.[field]
        ? dateTimeLocalValue(
          selectedCallForEdit[field],
          Intl.DateTimeFormat().resolvedOptions().timeZone,
        )
        : "";
      if (!selectedCallForEdit || value !== oldValue) {
        payload[field] = value
          ? dateTimeLocalToISO(value, Intl.DateTimeFormat().resolvedOptions().timeZone)
          : null;
      }
    }
    const duration = optionalFormValue(formData, "duration");
    if (duration) payload.duration = Number(duration);
  } else {
    const timezoneName = optionalFormValue(formData, "due_timezone")
      || Intl.DateTimeFormat().resolvedOptions().timeZone;
    const localDueDate = optionalFormValue(formData, "due_date");
    const oldDueDate = selectedFollowupForEdit
      ? dateTimeLocalValue(
        selectedFollowupForEdit.due_date,
        selectedFollowupForEdit.due_timezone || "UTC",
      )
      : "";
    const timezoneChanged = selectedFollowupForEdit
      && timezoneName !== (selectedFollowupForEdit.due_timezone || "UTC");
    if (!selectedFollowupForEdit || localDueDate !== oldDueDate || timezoneChanged) {
      payload.due_date = localDueDate
        ? dateTimeLocalToISO(
          localDueDate,
          timezoneName,
          optionalFormValue(formData, "due_time_occurrence"),
        )
        : null;
      payload.due_timezone = timezoneName;
    }
    const duration = optionalFormValue(formData, "duration");
    if (duration) payload.duration = Number(duration);
  }
  return payload;
}

function schedulingPreviewRequest(kind, payload) {
  const record = kind === "meeting"
    ? selectedMeetingForEdit
    : kind === "call"
      ? selectedCallForEdit
      : selectedFollowupForEdit;
  const status = payload.status ?? record?.status
    ?? (kind === "meeting" ? "scheduled" : kind === "call" ? "scheduled" : "pending");
  if (
    (kind === "meeting" && status !== "scheduled")
    || (kind === "call" && status !== "scheduled")
    || (kind === "followup" && !["pending", "in_progress", "overdue"].includes(status))
  ) {
    return null;
  }
  const startField = kind === "followup" ? "due_date" : "scheduled_at";
  const startsAt = payload[startField] !== undefined
    ? payload[startField]
    : record?.[startField];
  if (!startsAt) return null;
  const timezoneField = kind === "followup"
    ? "due_timezone"
    : "scheduled_timezone";
  const timezoneName = payload[timezoneField]
    ?? record?.[timezoneField]
    ?? Intl.DateTimeFormat().resolvedOptions().timeZone
    ?? "UTC";
  const activityType = kind === "followup" ? "followup" : kind;
  return {
    activity_type: activityType,
    starts_at: startsAt,
    duration_minutes: payload.duration ?? record?.duration ?? undefined,
    timezone_name: timezoneName,
    exclude_id: record
      ? record[kind === "meeting" ? "meeting_id" : kind === "call" ? "call_id" : "followup_id"]
      : undefined,
  };
}

function showScheduleConflict(error) {
  activityFormError.replaceChildren();
  const message = error.status >= 500
    ? "Unable to save activity. Please try again."
    : error.message || "Unable to save activity.";
  activityFormError.append(element("p", "", message));
  const suggestions = error.data?.error?.details?.suggestions;
  const timeField = selectedActivityKind === "followup" ? "due_date" : "scheduled_at";
  if (error.code === "schedule_conflict" && Array.isArray(suggestions)) {
    for (const suggestion of suggestions) {
      if (!suggestion?.starts_at || !suggestion?.timezone) continue;
      const button = element(
        "button",
        "button button-secondary schedule-suggestion",
        `Use ${formatDateTime(suggestion.starts_at, suggestion.timezone)} (${suggestion.timezone})`,
      );
      button.type = "button";
      button.addEventListener("click", () => {
        const input = activityFields.querySelector(`[name="${timeField}"]`);
        if (input) input.value = suggestion.starts_at.slice(0, 16);
        const zoneField = selectedActivityKind === "followup"
          ? "due_timezone"
          : selectedActivityKind === "call"
            ? "scheduled_timezone"
            : null;
        if (zoneField) {
          const zoneInput = activityFields.querySelector(`[name="${zoneField}"]`);
          if (zoneInput) zoneInput.value = suggestion.timezone;
        }
        activityFormError.hidden = true;
        activityFormError.replaceChildren();
      });
      activityFormError.append(button);
    }
  }
  activityFormError.hidden = false;
}

activityForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  activityFormError.hidden = true;
  if (!activityForm.reportValidity() || isSavingActivity) return;
  isSavingActivity = true;
  saveActivityButton.disabled = true;
  saveActivityButton.textContent = "Saving…";
  try {
    try {
      const payload = activityFormPayload(selectedActivityKind, new FormData(activityForm));
      const previewRequest = schedulingPreviewRequest(selectedActivityKind, payload);
      if (previewRequest) {
        const availability = await checkSchedulingAvailability(previewRequest);
        if (!availability.available) {
          showScheduleConflict({
            status: 409,
            code: "schedule_conflict",
            message: "The requested time overlaps another scheduled CRM activity.",
            data: { error: { details: availability } },
          });
          return;
        }
      }
      if (selectedActivityKind === "meeting" && selectedMeetingForEdit) {
        await updateMeeting(selectedMeetingForEdit.meeting_id, payload);
      } else if (selectedActivityKind === "meeting") await createMeeting(payload);
      else if (selectedActivityKind === "call" && selectedCallForEdit) {
        await updateCall(selectedCallForEdit.call_id, payload);
      } else if (selectedActivityKind === "call") await createCall(payload);
      else if (selectedFollowupForEdit) {
        await updateFollowup(selectedFollowupForEdit.followup_id, payload);
      } else await createFollowup(payload);
    } catch (error) {
      showScheduleConflict(error);
      return;
    }
    activityDialog.close();
    selectedMeetingForEdit = null;
    selectedCallForEdit = null;
    selectedFollowupForEdit = null;
    try {
      await refreshSelectedCustomer();
    } catch {
      setDetailMessage("Unable to load customer data.", { error: true });
    }
  } finally {
    isSavingActivity = false;
    saveActivityButton.disabled = false;
    saveActivityButton.textContent = selectedMeetingForEdit
      ? "Save meeting"
      : selectedCallForEdit
        ? "Save call"
        : selectedFollowupForEdit
          ? "Save follow-up"
          : "Save activity";
  }
});

async function completeFollowup(followupId) {
  try {
    await updateFollowup(followupId, {
      status: "completed",
      completed_at: new Date().toISOString(),
    });
  } catch (error) {
    const card = [...detailPanel.querySelectorAll(".activity-record-card")].find(
      (item) => item.querySelector(".text-action-button")?.dataset.followupId === String(followupId),
    );
    card?.append(element("p", "detail-api-error", error.message || "Unable to update follow-up."));
    return;
  }
  try {
    await refreshSelectedCustomer();
  } catch {
    setDetailMessage("Unable to load customer data.", { error: true });
  }
}

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!requireAuth()) return;
  resetCustomerAiState({ cancelPending: true });
  currentSearch = searchInput.value.trim();
  currentPage = 1;
  selectedCustomerId = null;
  selectedCustomer = null;
  setDetailMessage("Select a customer from the search results to view their information.");
  void loadCustomers(1);
});

resultList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-customer-id]");
  if (!button) return;
  // Stacked layout: the details render below the list, so bring them into view once loaded.
  void selectCustomer(button.dataset.customerId).then(() => {
    if (window.matchMedia("(max-width: 780px)").matches) scrollToElement(detailPanel);
  });
});

previousPage.addEventListener("click", () => {
  if (currentPage > 1) loadCustomers(currentPage - 1);
});
nextPage.addEventListener("click", () => {
  if (currentPage < totalPages) loadCustomers(currentPage + 1);
});
document.querySelector("#close-activity-dialog").addEventListener("click", () => activityDialog.close());
document.querySelector("#cancel-activity").addEventListener("click", () => activityDialog.close());
activityDialog.addEventListener("click", (event) => {
  if (event.target === activityDialog && !isSavingActivity) activityDialog.close();
});
document.querySelector("#close-meeting-brief").addEventListener("click", () => meetingBriefDialog.close());
document.querySelector("#close-meeting-brief-action").addEventListener("click", () => meetingBriefDialog.close());
meetingBriefDialog.addEventListener("click", (event) => {
  if (event.target === meetingBriefDialog) meetingBriefDialog.close();
});

mountAuthControls();
if (requireAuth()) {
  const hitlReady = initializeHitl();
  // The sidebar's "Approvals" item and dashboard task suggestions link here.
  const linkedToApprovals = window.location.hash === "#approvals";
  if (linkedToApprovals) openHitlPanel();
  window.addEventListener("hashchange", () => {
    if (window.location.hash === "#approvals") openHitlPanel();
  });
  const initialParams = new URLSearchParams(window.location.search);
  const linkedCustomerId = initialParams.get("customer_id");
  const initialLoad = loadCustomers(1);
  let customerReady = initialLoad;
  if (linkedCustomerId && /^\d+$/.test(linkedCustomerId)) {
    setDetailMessage("Loading customer...", { loading: true });
    customerReady = initialLoad.then(() => selectCustomer(linkedCustomerId));
    void customerReady.then(followRecordLink);
  } else {
    setDetailMessage("Select a customer from the list or search to view their information.");
  }
  if (linkedToApprovals) followApprovalsLinkAfter([hitlReady, customerReady]);
}
