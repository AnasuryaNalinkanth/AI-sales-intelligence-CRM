import {
  closeOpportunity,
  createLead,
  getCustomer,
  getCustomerOverview,
  getCustomers,
  getLeadAssignees,
  getLeads,
  requireAuth,
  updateLead,
  updateLeadQualification,
} from "./api.js";
import { displayValue, element, formatDate, formatNumber, humanize, mountAuthControls } from "./common.js";

const PAGE_SIZE = 20;
// Qualification (Ready / Not Ready) and outcome (Won / Lost) are separate fields.
const LABELS = { ready: "Ready", not_ready: "Not Ready", won: "Won", lost: "Lost" };
const SERVER_FIELD_ERRORS = {
  contact_not_found: "contact_id",
  assignee_not_found: "assigned_user_id",
  customer_not_found: "customer_id",
};

const filterForm = document.querySelector("#lead-filters");
const list = document.querySelector("#leads-list");
const status = document.querySelector("#leads-status");
const count = document.querySelector("#leads-count");
const notice = document.querySelector("#leads-notice");
const pagination = document.querySelector("#leads-pagination");
const pageLabel = document.querySelector("#leads-page");
const previousButton = document.querySelector("#leads-previous");
const nextButton = document.querySelector("#leads-next");
const scopeNote = document.querySelector("#customer-scope");

const leadDialog = document.querySelector("#lead-dialog");
const leadForm = document.querySelector("#lead-form");
const customerPicker = document.querySelector("#customer-picker");
const customerSearch = document.querySelector("#customer-search");
const customerSelect = document.querySelector("#lead-customer");

const detailDialog = document.querySelector("#detail-dialog");
const detailContent = document.querySelector("#detail-content");
const qualificationForm = document.querySelector("#qualification-form");
const nextFollowup = document.querySelector("#next-followup");
const qualificationSaved = document.querySelector("#qualification-saved");
const editButton = document.querySelector("#edit-lead");
const openCloseButton = document.querySelector("#open-close");
const workspaceLink = document.querySelector("#detail-workspace");

const closeDialog = document.querySelector("#close-dialog");
const closeForm = document.querySelector("#close-form");
const lossReasonField = document.querySelector("#loss-reason-field");

const scopedCustomerId = Number(new URLSearchParams(window.location.search).get("customer_id")) || null;
let page = 1;
let leadsById = new Map();
let currentLead = null;
let editingLead = null;
let assignees = null;
let searchTimer = 0;

const isClosed = (lead) => Boolean(lead?.outcome);
const isActiveOpportunity = (lead) => lead?.qualification_status === "ready" && !isClosed(lead);

function badge(value, label) {
  return element("span", `status-badge status-${value}`, label);
}

function leadBadges(lead) {
  const box = element("div", "lead-badges");
  box.append(badge(lead.qualification_status, LABELS[lead.qualification_status] ?? humanize(lead.qualification_status)));
  if (isClosed(lead)) box.append(badge(lead.outcome, LABELS[lead.outcome]));
  else box.append(badge(lead.status, humanize(lead.status)));
  return box;
}

function leadTitle(lead) {
  return displayValue(lead.product, displayValue(lead.enquiry_text, "Sales lead").slice(0, 80));
}

function showNotice(message, isError = false) {
  notice.textContent = message;
  notice.classList.toggle("is-error", isError);
  notice.hidden = !message;
}

// ---------- List ----------

function filterParams() {
  const data = new FormData(filterForm);
  return {
    search: String(data.get("search") ?? "").trim(),
    qualification_status: data.get("qualification_status"),
    outcome: data.get("outcome"),
    customer_id: scopedCustomerId,
    page,
    page_size: PAGE_SIZE,
  };
}

async function loadLeads() {
  status.hidden = false;
  status.textContent = "Loading sales leads…";
  list.replaceChildren();
  pagination.hidden = true;
  try {
    const data = await getLeads(filterParams());
    leadsById = new Map(data.items.map((lead) => [lead.enquiry_id, lead]));
    renderLeads(data);
  } catch (error) {
    status.textContent = error.message || "Sales leads could not be loaded.";
    count.textContent = "";
  }
}

function renderLeads(data) {
  count.textContent = `${data.total} ${data.total === 1 ? "lead" : "leads"}`;
  const filtered = Object.entries(filterParams()).some(
    ([key, value]) => ["search", "qualification_status", "outcome"].includes(key) && value,
  );
  if (data.items.length === 0) {
    status.textContent = filtered ? "No sales leads match these filters." : "No sales leads yet. Create one to get started.";
    return;
  }
  status.hidden = true;
  for (const lead of data.items) {
    const row = element("li", `lead-row${isClosed(lead) ? " is-closed" : ""}`);
    const button = element("button");
    button.type = "button";
    const identity = element("div");
    identity.append(
      element("span", "lead-title", `#${lead.enquiry_id} · ${leadTitle(lead)}`),
      element("span", "lead-sub", `${lead.customer_name} · ${lead.company_name}`),
    );
    const meta = element("div");
    meta.append(
      element("span", "lead-sub", `Value: ${formatNumber(lead.estimated_value)}`),
      element("span", "lead-sub", `Rep: ${displayValue(lead.assigned_user_name, "Unassigned")} · Updated ${formatDate(lead.updated_at)}`),
    );
    button.append(identity, meta, leadBadges(lead));
    button.addEventListener("click", () => openDetail(lead));
    row.append(button);
    list.append(row);
  }
  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  pagination.hidden = totalPages <= 1;
  pageLabel.textContent = `Page ${data.page} of ${totalPages}`;
  previousButton.disabled = data.page <= 1;
  nextButton.disabled = data.page >= totalPages;
}

// ---------- Form helpers ----------

function clearErrors(form) {
  for (const node of form.querySelectorAll("[aria-invalid]")) node.removeAttribute("aria-invalid");
  for (const node of form.querySelectorAll(".field-error")) node.textContent = "";
  const alert = form.querySelector(".form-alert");
  alert.hidden = true;
  alert.textContent = "";
}

function fieldError(form, name, message) {
  const input = form.elements.namedItem(name);
  if (!input || input instanceof RadioNodeList) return false;
  input.setAttribute("aria-invalid", "true");
  const slot = input.nextElementSibling;
  if (slot?.classList.contains("field-error")) slot.textContent = message;
  return true;
}

function formAlert(form, message) {
  const alert = form.querySelector(".form-alert");
  alert.textContent = message;
  alert.hidden = false;
}

function showServerError(form, error, fieldNames = {}) {
  let marked = false;
  const detail = error?.data?.detail;
  if (Array.isArray(detail)) {
    for (const item of detail) {
      const key = Array.isArray(item.loc) ? item.loc.at(-1) : null;
      if (key && fieldError(form, fieldNames[key] ?? key, item.msg)) marked = true;
    }
  }
  const codeField = SERVER_FIELD_ERRORS[error?.code];
  if (codeField && fieldError(form, codeField, error.message)) marked = true;
  formAlert(form, marked ? `Please correct the highlighted fields. ${error.message}` : error?.message || "The request failed.");
  form.querySelector("[aria-invalid='true']")?.focus();
}

async function submitting(form, task) {
  const button = form.querySelector("button[type='submit']");
  const label = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Saving…";
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = label;
  }
}

function optionalNumber(value) {
  return value === "" || value === null ? null : Number(value);
}

function setOptions(select, options, selected, emptyLabel) {
  select.replaceChildren();
  if (emptyLabel !== undefined) select.append(new Option(emptyLabel, ""));
  for (const [value, label] of options) select.append(new Option(label, String(value)));
  select.value = selected == null ? "" : String(selected);
}

// ---------- Create / edit ----------

async function loadCustomers(search = "") {
  const data = await getCustomers({ search, page_size: 20 });
  const current = customerSelect.value;
  setOptions(
    customerSelect,
    data.items.map((customer) => [customer.customer_id, `${customer.customer_name} · ${customer.company?.company_name ?? ""}`]),
    current || scopedCustomerId,
    data.items.length ? "Choose a customer" : "No matching customers",
  );
}

async function loadContacts(customerId, selected = null) {
  const contactSelect = leadForm.elements.namedItem("contact_id");
  if (!customerId) {
    setOptions(contactSelect, [], null, "Choose a customer first");
    return;
  }
  const customer = await getCustomer(customerId);
  setOptions(contactSelect, customer.contacts.map((contact) => [contact.contact_id, contact.name]), selected, "No contact");
}

async function loadAssignees(selected = null) {
  assignees ??= await getLeadAssignees();
  setOptions(
    leadForm.elements.namedItem("assigned_user_id"),
    assignees.map((user) => [user.user_id, user.full_name]),
    selected,
    "Unassigned",
  );
}

async function openLeadForm(lead = null) {
  editingLead = lead;
  leadForm.reset();
  clearErrors(leadForm);
  document.querySelector("#lead-dialog-title").textContent = lead ? `Edit sales lead #${lead.enquiry_id}` : "Create sales lead";
  for (const node of leadForm.querySelectorAll("[data-create-only]")) node.hidden = Boolean(lead);
  customerSearch.hidden = Boolean(lead);
  customerSearch.previousElementSibling.hidden = Boolean(lead);
  customerSelect.disabled = Boolean(lead);
  const stage = leadForm.elements.namedItem("status");
  for (const option of [...stage.options]) if (!["open", "in_progress"].includes(option.value)) option.remove();
  leadDialog.showModal();
  try {
    if (lead) {
      setOptions(customerSelect, [[lead.customer_id, `${lead.customer_name} · ${lead.company_name}`]], lead.customer_id);
      for (const name of ["product", "enquiry_text", "priority", "lead_source", "expected_close_date"]) {
        leadForm.elements.namedItem(name).value = lead[name] ?? "";
      }
      // A legacy stage stays selectable so an unrelated edit does not change it.
      if (![...stage.options].some((option) => option.value === lead.status)) stage.append(new Option(humanize(lead.status), lead.status));
      stage.value = lead.status;
      leadForm.elements.namedItem("estimated_value").value = lead.estimated_value ?? "";
    } else if (scopedCustomerId) {
      const customer = await getCustomer(scopedCustomerId);
      setOptions(customerSelect, [[customer.customer_id, `${customer.customer_name} · ${customer.company?.company_name ?? ""}`]], customer.customer_id);
    } else {
      customerSelect.replaceChildren();
      await loadCustomers();
    }
    await Promise.all([
      loadContacts(Number(customerSelect.value) || null, lead?.contact_id),
      loadAssignees(lead?.assigned_user_id),
    ]);
  } catch (error) {
    formAlert(leadForm, error.message || "Customer details could not be loaded.");
  }
}

function leadPayload() {
  const data = new FormData(leadForm);
  const text = (name) => String(data.get(name) ?? "").trim();
  return {
    product: text("product") || null,
    enquiry_text: text("enquiry_text"),
    lead_source: text("lead_source") || null,
    priority: text("priority"),
    status: text("status"),
    estimated_value: text("estimated_value") || null,
    contact_id: optionalNumber(text("contact_id")),
    assigned_user_id: optionalNumber(text("assigned_user_id")),
    expected_close_date: text("expected_close_date") || null,
  };
}

leadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireAuth()) return;
  clearErrors(leadForm);
  const payload = leadPayload();
  const problems = [
    [!editingLead && !customerSelect.value, "customer_id", "Choose a customer."],
    [!payload.enquiry_text, "enquiry_text", "Enter the lead's requirements."],
    [payload.estimated_value !== null && !(Number(payload.estimated_value) >= 0), "estimated_value", "Estimated value must be zero or more."],
  ].filter(([failed]) => failed);
  for (const [, name, message] of problems) fieldError(leadForm, name, message);
  if (problems.length) {
    formAlert(leadForm, "Please correct the highlighted fields.");
    leadForm.querySelector("[aria-invalid='true']")?.focus();
    return;
  }
  try {
    const saved = await submitting(leadForm, () => (editingLead
      ? updateLead(editingLead.enquiry_id, payload)
      : createLead({
        ...payload,
        customer_id: Number(customerSelect.value),
        qualification_status: leadForm.elements.namedItem("qualification_status").value,
      })));
    leadDialog.close();
    showNotice(editingLead ? `Sales lead #${saved.enquiry_id} updated.` : `Sales lead #${saved.enquiry_id} created.`);
    await refreshAfterChange(saved, Boolean(editingLead));
  } catch (error) {
    showServerError(leadForm, error);
  }
});

customerSearch.addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    loadCustomers(customerSearch.value.trim()).catch((error) => formAlert(leadForm, error.message));
  }, 250);
});

customerSelect.addEventListener("change", () => {
  loadContacts(Number(customerSelect.value) || null).catch((error) => formAlert(leadForm, error.message));
});

// ---------- Detail, qualification, closure ----------

function fact(label, value) {
  const item = element("div");
  item.append(element("dt", "", label), element("dd", "", value));
  return item;
}

function renderDetail(lead) {
  document.querySelector("#detail-eyebrow").textContent = `Sales lead #${lead.enquiry_id}${isActiveOpportunity(lead) ? " · Active opportunity" : ""}`;
  document.querySelector("#detail-title").textContent = leadTitle(lead);
  detailContent.replaceChildren(leadBadges(lead));
  if (isClosed(lead)) {
    const note = element("p", "leads-closed-note", `Closed as ${LABELS[lead.outcome]} on ${formatDate(lead.closed_at)}. Closed opportunities are read-only.`);
    if (lead.loss_reason) note.append(element("span", "", ` Loss reason: ${lead.loss_reason}`));
    detailContent.append(note);
  }
  const facts = element("dl", "leads-facts");
  facts.append(
    fact("Customer", `${lead.customer_name} · ${lead.company_name}`),
    fact("Contact", displayValue(lead.contact_name, "None")),
    fact("Assigned sales rep", displayValue(lead.assigned_user_name, "Unassigned")),
    fact("Lead source", lead.lead_source ? humanize(lead.lead_source) : "Not specified"),
    fact("Estimated value", formatNumber(lead.estimated_value)),
    fact("Pipeline stage", humanize(lead.status)),
    fact("Priority", humanize(lead.priority)),
    fact("Expected close date", displayValue(lead.expected_close_date, "Not set")),
    fact("Qualification reason", displayValue(lead.qualification_reason, "None recorded")),
    fact("Created / updated", `${formatDate(lead.created_at)} / ${formatDate(lead.updated_at)}`),
  );
  const requirements = element("div");
  requirements.append(element("h3", "leads-section-title", "Requirements and notes"), element("p", "leads-text", displayValue(lead.enquiry_text)));
  const activities = element("div");
  const activityList = element("ul", "leads-activities");
  activities.append(element("h3", "leads-section-title", "Meetings, calls and follow-ups"), element("p", "leads-hint", "Loading activities…"));
  detailContent.append(facts, requirements, activities);
  void loadActivities(lead, activities, activityList);
}

async function loadActivities(lead, container, activityList) {
  try {
    const overview = await getCustomerOverview(lead.customer_id);
    if (currentLead?.enquiry_id !== lead.enquiry_id) return;
    const ofLead = (item) => item.enquiry_id === lead.enquiry_id;
    const linked = [
      ...overview.meetings.filter(ofLead).map((item) => ["Meeting", item.scheduled_at, item.status, item.agenda]),
      ...overview.calls.filter(ofLead).map((item) => ["Call", item.actual_time ?? item.scheduled_at, item.status, item.outcome ?? item.call_type]),
      ...overview.followups.filter(ofLead).map((item) => ["Follow-up", item.due_date, item.status, item.description ?? item.type]),
    ];
    container.querySelector(".leads-hint").remove();
    if (linked.length === 0) {
      container.append(element("p", "leads-hint", "No activities are linked to this lead. Schedule them from the customer workspace."));
      return;
    }
    for (const [type, when, state, label] of linked) {
      const item = element("li");
      item.append(element("span", "", `${type} · ${formatDate(when)}${label ? ` · ${label}` : ""}`), badge(state, humanize(state)));
      activityList.append(item);
    }
    container.append(activityList);
  } catch (error) {
    container.querySelector(".leads-hint").textContent = error.message || "Activities could not be loaded.";
  }
}

function openDetail(lead) {
  currentLead = lead;
  renderDetail(lead);
  clearErrors(qualificationForm);
  qualificationForm.reset();
  qualificationSaved.textContent = "";
  qualificationForm.hidden = isClosed(lead);
  for (const radio of qualificationForm.elements.namedItem("qualification_status")) radio.checked = radio.value === lead.qualification_status;
  qualificationForm.elements.namedItem("qualification_reason").value = lead.qualification_reason ?? "";
  nextFollowup.hidden = lead.qualification_status !== "not_ready";
  editButton.disabled = isClosed(lead);
  openCloseButton.disabled = !isActiveOpportunity(lead);
  openCloseButton.title = isClosed(lead)
    ? "This opportunity is already closed."
    : isActiveOpportunity(lead) ? "" : "Mark the lead Ready before closing it as Won or Lost.";
  workspaceLink.href = `/customer?customer_id=${encodeURIComponent(lead.customer_id)}#enquiry-${lead.enquiry_id}`;
  if (!detailDialog.open) detailDialog.showModal();
}

qualificationForm.addEventListener("change", (event) => {
  qualificationSaved.textContent = "";
  if (event.target.name === "qualification_status") nextFollowup.hidden = event.target.value !== "not_ready";
});

qualificationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireAuth() || !currentLead) return;
  clearErrors(qualificationForm);
  const data = new FormData(qualificationForm);
  const qualification = data.get("qualification_status");
  if (!qualification) {
    formAlert(qualificationForm, "Choose Ready or Not Ready.");
    return;
  }
  const payload = {
    qualification_status: qualification,
    qualification_reason: String(data.get("qualification_reason") ?? "").trim() || null,
  };
  const type = String(data.get("followup_type") ?? "").trim();
  const due = String(data.get("followup_due") ?? "");
  if (qualification === "not_ready" && (type || due)) {
    if (!type) fieldError(qualificationForm, "followup_type", "Enter the follow-up type.");
    if (!due) fieldError(qualificationForm, "followup_due", "Choose when the follow-up is due.");
    if (!type || !due) {
      formAlert(qualificationForm, "Complete the follow-up, or clear both fields to skip it.");
      return;
    }
    payload.next_followup = {
      type,
      due_date: new Date(due).toISOString(),
      due_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      description: String(data.get("followup_description") ?? "").trim() || null,
    };
  }
  try {
    const saved = await submitting(qualificationForm, () => updateLeadQualification(currentLead.enquiry_id, payload));
    const message = `Sales lead #${saved.enquiry_id} is now ${LABELS[saved.qualification_status]}${payload.next_followup ? " and the next follow-up is scheduled" : ""}.`;
    showNotice(message);
    await refreshAfterChange(saved, true);
    // The page notice sits behind the modal, so confirm inside the dialog too.
    qualificationSaved.textContent = message;
  } catch (error) {
    showServerError(qualificationForm, error, { type: "followup_type", due_date: "followup_due" });
  }
});

editButton.addEventListener("click", () => {
  if (currentLead && !isClosed(currentLead)) void openLeadForm(currentLead);
});

openCloseButton.addEventListener("click", () => {
  if (!isActiveOpportunity(currentLead)) return;
  closeForm.reset();
  clearErrors(closeForm);
  lossReasonField.hidden = true;
  document.querySelector("#close-title").textContent = `Close opportunity #${currentLead.enquiry_id}`;
  closeDialog.showModal();
});

closeForm.addEventListener("change", () => {
  lossReasonField.hidden = closeForm.elements.namedItem("outcome").value !== "lost";
});

closeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireAuth() || !isActiveOpportunity(currentLead)) return;
  clearErrors(closeForm);
  const outcome = closeForm.elements.namedItem("outcome").value;
  const lossReason = closeForm.elements.namedItem("loss_reason").value.trim();
  if (!outcome) {
    formAlert(closeForm, "Choose Won or Lost.");
    return;
  }
  if (outcome === "lost" && !lossReason) {
    fieldError(closeForm, "loss_reason", "Enter why the opportunity was lost.");
    formAlert(closeForm, "A loss reason is required.");
    closeForm.elements.namedItem("loss_reason").focus();
    return;
  }
  try {
    const saved = await submitting(closeForm, () => closeOpportunity(currentLead.enquiry_id, {
      outcome,
      loss_reason: outcome === "lost" ? lossReason : null,
    }));
    closeDialog.close();
    showNotice(`Opportunity #${saved.enquiry_id} closed as ${LABELS[saved.outcome]}.`);
    await refreshAfterChange(saved, true);
  } catch (error) {
    showServerError(closeForm, error);
  }
});

async function refreshAfterChange(saved, reopenDetail) {
  await loadLeads();
  if (reopenDetail && detailDialog.open) {
    // The lead may now be outside the current filter, so fall back to the saved record.
    openDetail(leadsById.get(saved.enquiry_id) ?? { ...currentLead, ...saved });
  }
}

// ---------- Wiring ----------

// Clear a field's error as soon as the user edits it.
for (const form of [leadForm, qualificationForm, closeForm]) {
  form.addEventListener("input", (event) => {
    if (event.target.getAttribute?.("aria-invalid") !== "true") return;
    event.target.removeAttribute("aria-invalid");
    const slot = event.target.nextElementSibling;
    if (slot?.classList.contains("field-error")) slot.textContent = "";
  });
}

for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}

document.querySelector("#create-lead").addEventListener("click", () => void openLeadForm());

filterForm.addEventListener("submit", (event) => {
  event.preventDefault();
  page = 1;
  void loadLeads();
});

// Dropdown filters apply immediately; search still waits for Apply or Enter.
filterForm.addEventListener("change", (event) => {
  if (event.target instanceof HTMLSelectElement) filterForm.requestSubmit();
});

filterForm.addEventListener("reset", () => {
  // The form clears after this event; reload with the defaults on the next tick.
  window.setTimeout(() => {
    page = 1;
    void loadLeads();
  }, 0);
});

previousButton.addEventListener("click", () => {
  page = Math.max(1, page - 1);
  void loadLeads();
});

nextButton.addEventListener("click", () => {
  page += 1;
  void loadLeads();
});

if (scopedCustomerId) {
  scopeNote.hidden = false;
  scopeNote.append(`Showing leads for customer #${scopedCustomerId}. `);
  const all = element("a", "text-button", "Show all leads");
  all.href = "/leads";
  scopeNote.append(all);
}

mountAuthControls();
if (requireAuth()) void loadLeads();
