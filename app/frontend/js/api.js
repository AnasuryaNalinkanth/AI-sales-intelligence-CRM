const API_BASE_URL = "/api";
const AUTH_TOKEN_KEY = "salesdesk_access_token";
const AUTH_USER_KEY = "salesdesk_user";

export function getAuthToken() {
  try {
    return window.sessionStorage.getItem(AUTH_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function getAuthenticatedUser() {
  try {
    const raw = window.sessionStorage.getItem(AUTH_USER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function setAuthSession(token, user = null) {
  try {
    if (token) {
      window.sessionStorage.setItem(AUTH_TOKEN_KEY, token);
    }
    if (user) {
      window.sessionStorage.setItem(AUTH_USER_KEY, JSON.stringify(user));
    }
  } catch {
    // Ignore storage quota errors in restricted environments.
  }
}

export function clearAuthSession() {
  try {
    window.sessionStorage.removeItem(AUTH_TOKEN_KEY);
    window.sessionStorage.removeItem(AUTH_USER_KEY);
  } catch {
    // Ignore storage errors.
  }
}

export function redirectToLogin() {
  clearAuthSession();
  if (typeof window !== "undefined" && window.location.pathname !== "/login") {
    const nextPath = `${window.location.pathname}${window.location.search || ""}`;
    const target = nextPath && nextPath !== "/"
      ? `/login?next=${encodeURIComponent(nextPath)}`
      : "/login";
    window.location.assign(target);
  }
}

export function requireAuth() {
  const token = getAuthToken();
  if (!token) {
    redirectToLogin();
    return false;
  }
  return true;
}

async function request(path, options = {}) {
  const isPublicAuthPath = path === "/auth/login" || path === "/auth/register";
  const token = getAuthToken();
  if (!isPublicAuthPath && !token) {
    redirectToLogin();
    const authError = new Error("Authentication required. Redirecting to login.");
    authError.status = 401;
    authError.code = "authentication_required";
    throw authError;
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });

  const contentType = response.headers.get("content-type") ?? "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : null;

  if (!response.ok) {
    if (response.status === 401 && !isPublicAuthPath) {
      redirectToLogin();
    }
    const detail = data?.error?.message ?? data?.detail;
    const message = Array.isArray(detail)
      ? detail
          .map((item) => {
            const location = Array.isArray(item.loc)
              ? item.loc.filter((part) => part !== "body").join(" → ")
              : "";
            return location ? `${location}: ${item.msg}` : item.msg;
          })
          .join("; ")
      : typeof detail === "string"
        ? detail
        : `Request failed (${response.status}).`;
    const error = new Error(message || `Request failed (${response.status}).`);
    error.status = response.status;
    error.code = data?.error?.code;
    error.data = data;
    throw error;
  }

  return data;
}

export async function loginUser(payload) {
  const data = await request("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (data?.access_token) {
    setAuthSession(data.access_token, data.user ?? null);
  }
  return data;
}

export function registerUser(payload) {
  return request("/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getCurrentUser() {
  return request("/auth/me");
}

export function logoutUser() {
  clearAuthSession();
  if (typeof window !== "undefined") {
    window.location.assign("/login");
  }
}

export function getCustomers(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return request(`/customers${suffix}`);
}

export function getCustomer(customerId) {
  return request(`/customers/${encodeURIComponent(customerId)}`);
}

export function getCustomerOverview(customerId) {
  return request(`/customers/${encodeURIComponent(customerId)}/overview`);
}

export function getCustomerActivity(customerId, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return request(`/customers/${encodeURIComponent(customerId)}/activity${suffix}`);
}

export function getWorkspaceActivities(startAt, endAt) {
  const query = new URLSearchParams({
    start_at: startAt,
    end_at: endAt,
  });
  return request(`/scheduling/activities?${query.toString()}`);
}

export function moveWorkspaceActivity(activityType, activityId, targetDate, targetTimezone) {
  return request(
    `/scheduling/activities/${encodeURIComponent(activityType)}/${encodeURIComponent(activityId)}/move`,
    {
      method: "POST",
      body: JSON.stringify({ target_date: targetDate, target_timezone: targetTimezone }),
    },
  );
}

export function createMeeting(payload) {
  return request("/meetings", { method: "POST", body: JSON.stringify(payload) });
}

export function updateMeeting(meetingId, payload) {
  return request(`/meetings/${encodeURIComponent(meetingId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function createCall(payload) {
  return request("/calls", { method: "POST", body: JSON.stringify(payload) });
}

export function updateCall(callId, payload) {
  return request(`/calls/${encodeURIComponent(callId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function createFollowup(payload) {
  return request("/followups", { method: "POST", body: JSON.stringify(payload) });
}

export function updateFollowup(followupId, payload) {
  return request(`/followups/${encodeURIComponent(followupId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function checkSchedulingAvailability(payload) {
  return request("/scheduling/availability", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createCustomer(payload) {
  return request("/customers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function generateCustomerSummary(customerId) {
  return request(`/agent/customer-summary/${encodeURIComponent(customerId)}`, {
    method: "POST",
  });
}

export function generateMeetingBrief(meetingId) {
  return request(`/agent/meeting-brief/${encodeURIComponent(meetingId)}`, {
    method: "POST",
  });
}

export function getCalendarStatus() {
  return request("/integrations/google-calendar");
}

export function connectGoogleCalendar() {
  return request("/integrations/google-calendar/connect", { method: "POST" });
}

export function disconnectGoogleCalendar() {
  return request("/integrations/google-calendar", { method: "DELETE" });
}

export function updateCalendarPreferences(reminderMinutes) {
  return request("/integrations/google-calendar/preferences", {
    method: "PUT",
    body: JSON.stringify({ reminder_minutes: reminderMinutes }),
  });
}

export function retryCalendarSync() {
  return request("/integrations/google-calendar/retry", { method: "POST" });
}

export function chatWithAgent(
  message,
  customerId = null,
  conversationId = null,
  taskId = null,
  pendingActionId = null,
) {
  return request("/agent/chat", {
    method: "POST",
    body: JSON.stringify({
      message,
      timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone,
      ...(customerId === null ? {} : { customer_id: customerId }),
      ...(conversationId ? { conversation_id: conversationId } : {}),
      ...(taskId ? { task_id: taskId } : {}),
      ...(pendingActionId ? { pending_action_id: pendingActionId } : {}),
    }),
  });
}

export function getLatestConversation(customerId) {
  return request(`/agent/conversations/latest?customer_id=${encodeURIComponent(customerId)}`);
}

export function confirmAgentAction(actionId) {
  return request(`/agent/actions/${encodeURIComponent(actionId)}/confirm`, {
    method: "POST",
  });
}

export function cancelAgentAction(actionId) {
  return request(`/agent/actions/${encodeURIComponent(actionId)}/cancel`, {
    method: "POST",
  });
}

export function rejectAgentAction(actionId) {
  return request(`/agent/actions/${encodeURIComponent(actionId)}/reject`, {
    method: "POST",
  });
}

export function createAgentTask(operations) {
  return request("/agent/tasks", {
    method: "POST",
    body: JSON.stringify({ operations }),
  });
}

export function getAgentTasks() {
  return request("/agent/tasks");
}

export function resumeAgentTask(taskId) {
  return request(`/agent/tasks/${encodeURIComponent(taskId)}`);
}

export function submitAgentTaskInput(taskId, operationIndex, values) {
  return request(`/agent/tasks/${encodeURIComponent(taskId)}/inputs`, {
    method: "POST",
    body: JSON.stringify({ operation_index: operationIndex, values }),
  });
}

export function cancelAgentTask(taskId) {
  return request(`/agent/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
  });
}

export function getAgentPolicies() {
  return request("/agent/policies");
}

export function updateAgentPolicy(action, mode) {
  return request(`/agent/policies/${encodeURIComponent(action)}`, {
    method: "PUT",
    body: JSON.stringify({ action, mode }),
  });
}

export function getAnalytics(metric, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return request(`/analytics/${encodeURIComponent(metric)}${suffix}`);
}

// Sales leads keep the /enquiries API paths for compatibility.
export function getLeads(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return request(`/enquiries${suffix}`);
}

export function getLeadAssignees() {
  return request("/enquiries/assignees");
}

export function createLead(payload) {
  return request("/enquiries", { method: "POST", body: JSON.stringify(payload) });
}

export function updateLead(enquiryId, payload) {
  return request(`/enquiries/${encodeURIComponent(enquiryId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function updateLeadQualification(enquiryId, payload) {
  return request(`/enquiries/${encodeURIComponent(enquiryId)}/qualification`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function closeOpportunity(enquiryId, payload) {
  return request(`/enquiries/${encodeURIComponent(enquiryId)}/close`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
