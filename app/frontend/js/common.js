import { getAuthenticatedUser, getAuthToken, logoutUser } from "./api.js";

export function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function displayValue(value, fallback = "Not provided") {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "string" && value.trim() === "") return fallback;
  return String(value);
}

export function formatDate(value) {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not available";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(date);
}

// Grouped digits in the viewer's locale ("21,000.00"); non-numbers fall back to displayValue.
export function formatNumber(value, fallback = "Not set") {
  if (value === null || value === undefined || value === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return displayValue(value, fallback);
  const decimals = Number.isInteger(number) ? 0 : 2;
  return new Intl.NumberFormat(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: 2 }).format(number);
}

export function initials(value) {
  const words = displayValue(value, "C").trim().split(/\s+/);
  return words.slice(0, 2).map((word) => word[0]).join("").toUpperCase();
}

export function humanize(value) {
  return displayValue(value, "Not provided")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

// ---------- Display preferences (saved in this browser; applied by theme-init.js) ----------

const PREFERENCES = {
  theme: { key: "salesdesk_theme", values: ["system", "light", "dark"], fallback: "system" },
  density: { key: "salesdesk_density", values: ["comfortable", "compact"], fallback: "comfortable" },
  motion: { key: "salesdesk_motion", values: ["system", "reduce"], fallback: "system" },
  sidebar: { key: "salesdesk_sidebar", values: ["expanded", "collapsed"], fallback: "expanded" },
  startPage: { key: "salesdesk_start_page", values: ["/", "/customer", "/analytics"], fallback: "/" },
};

export function getPreference(name) {
  const { key, values, fallback } = PREFERENCES[name];
  try {
    const stored = window.localStorage.getItem(key);
    return values.includes(stored) ? stored : fallback;
  } catch {
    return fallback;
  }
}

export function setPreference(name, value) {
  const { key, values, fallback } = PREFERENCES[name];
  if (!values.includes(value)) return;
  try {
    if (value === fallback) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Storage can be blocked; the choice still applies to this page.
  }
  applyPreferences();
}

export function applyPreferences() {
  const root = document.documentElement;
  const theme = getPreference("theme");
  root.dataset.theme = theme === "system"
    ? (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : theme;
  if (getPreference("density") === "compact") root.dataset.density = "compact";
  else delete root.dataset.density;
  if (getPreference("motion") === "reduce") root.dataset.motion = "reduce";
  else delete root.dataset.motion;
  if (getPreference("sidebar") === "collapsed") root.dataset.sidebar = "collapsed";
  else delete root.dataset.sidebar;
  // Lets every control that shows a preference (toggle, sidebar, Settings) stay in sync.
  document.dispatchEvent(new Event("preferencechange"));
}

window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
  if (getPreference("theme") === "system") applyPreferences();
});

// ---------- Application shell ----------

const ICONS = {
  dashboard: "M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z",
  customers: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
  newCustomer: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M19 8v6M22 11h-6",
  approvals: "M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11",
  analytics: "M3 3v18h18M7 16v-5M12 16V8M17 16v-8",
  leads: "M22 3H2l8 9.46V19l4 2v-8.54L22 3",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1",
  signOut: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  menu: "M3 6h18M3 12h18M3 18h18",
  collapse: "M3 3h18v18H3zM9 3v18M16 15l-3-3 3-3",
};

const NAVIGATION = [
  {
    label: "Workspace",
    items: [
      { label: "Dashboard", href: "/", icon: "dashboard" },
      { label: "Customers", href: "/customer", icon: "customers" },
      { label: "Sales leads", href: "/leads", icon: "leads" },
      { label: "New customer", href: "/customer-registration", icon: "newCustomer" },
      { label: "Approvals", href: "/customer#approvals", icon: "approvals" },
    ],
  },
  { label: "Insights", items: [{ label: "Analytics", href: "/analytics", icon: "analytics" }] },
];

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("nav-icon");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function isCurrent(href) {
  const [path, hash] = href.split("#");
  if (window.location.pathname !== path) return false;
  // "Customers" and "Approvals" share a page; the hash says which one is in focus.
  return hash ? window.location.hash === `#${hash}` : !NAVIGATION.some((group) =>
    group.items.some((item) => item.href === `${path}#${window.location.hash.slice(1)}`));
}

function navLink(item) {
  const link = element("a", "nav-item");
  link.href = item.href;
  link.title = item.label;
  if (isCurrent(item.href)) link.setAttribute("aria-current", "page");
  link.append(icon(item.icon), element("span", "nav-label", item.label));
  return link;
}

function brand() {
  const link = element("a", "brand");
  link.href = "/";
  link.setAttribute("aria-label", "SalesDesk home");
  const text = element("span", "brand-text", "Sales");
  text.append(element("span", "brand-light", "Desk"));
  link.append(element("span", "brand-mark", "S"), text);
  link.firstChild.setAttribute("aria-hidden", "true");
  return link;
}

function setSidebarOpen(open) {
  if (open) document.documentElement.dataset.sidebarOpen = "true";
  else delete document.documentElement.dataset.sidebarOpen;
  document.querySelector(".sidebar-open")?.setAttribute("aria-expanded", String(open));
}

function mountSidebar(sidebar, user) {
  if (sidebar.childElementCount) return;
  const header = element("div", "sidebar-header");
  const collapse = element("button", "icon-button ghost sidebar-collapse");
  collapse.type = "button";
  const syncCollapse = () => {
    const collapsed = getPreference("sidebar") === "collapsed";
    collapse.setAttribute("aria-label", collapsed ? "Expand navigation" : "Collapse navigation");
    collapse.title = collapse.getAttribute("aria-label");
    collapse.setAttribute("aria-pressed", String(collapsed));
  };
  collapse.append(icon("collapse"));
  collapse.addEventListener("click", () => {
    setPreference("sidebar", getPreference("sidebar") === "collapsed" ? "expanded" : "collapsed");
  });
  document.addEventListener("preferencechange", syncCollapse);
  syncCollapse();
  header.append(brand(), collapse);

  const nav = element("nav", "sidebar-nav");
  nav.setAttribute("aria-label", "Main navigation");
  for (const group of NAVIGATION) {
    const section = element("div", "nav-group");
    const heading = element("p", "nav-group-label", group.label);
    section.append(heading, ...group.items.map(navLink));
    nav.append(section);
  }

  const footer = element("div", "sidebar-footer");
  if (user?.email) {
    const who = element("div", "sidebar-user");
    who.append(element("strong", "", displayValue(user.full_name, user.email)), element("span", "", user.email));
    footer.append(who);
  }
  footer.append(navLink({ label: "Settings", href: "/settings", icon: "settings" }));
  const signOut = element("button", "nav-item nav-button");
  signOut.type = "button";
  signOut.title = "Sign out";
  signOut.append(icon("signOut"), element("span", "nav-label", "Sign out"));
  signOut.addEventListener("click", () => logoutUser());
  footer.append(signOut);

  sidebar.append(header, nav, footer);
  document.querySelector(".sidebar-backdrop")?.addEventListener("click", () => setSidebarOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.documentElement.dataset.sidebarOpen) setSidebarOpen(false);
  });
  window.addEventListener("hashchange", () => {
    for (const link of sidebar.querySelectorAll("a.nav-item")) {
      if (isCurrent(link.getAttribute("href"))) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  });
}

function themeToggle() {
  const button = element("button", "theme-toggle");
  button.type = "button";
  const update = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    button.textContent = dark ? "☀" : "☾";
    button.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} theme`);
    button.title = button.getAttribute("aria-label");
    button.setAttribute("aria-pressed", String(dark));
  };
  button.addEventListener("click", () => {
    setPreference("theme", document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  document.addEventListener("preferencechange", update);
  update();
  return button;
}

/** Builds the shared shell: sidebar navigation (signed in) and the top bar controls. */
export function mountAuthControls() {
  const signedIn = Boolean(getAuthToken());
  if (signedIn) delete document.documentElement.dataset.auth;
  else document.documentElement.dataset.auth = "signed-out";
  const user = getAuthenticatedUser();

  const sidebar = document.querySelector("#app-sidebar");
  if (sidebar && signedIn) mountSidebar(sidebar, user);

  const header = document.querySelector(".site-header");
  if (!header || header.querySelector(".header-auth")) return;

  if (sidebar && signedIn && !header.querySelector(".sidebar-open")) {
    const open = element("button", "icon-button ghost sidebar-open");
    open.type = "button";
    open.setAttribute("aria-label", "Open navigation");
    open.setAttribute("aria-controls", "app-sidebar");
    open.setAttribute("aria-expanded", "false");
    open.append(icon("menu"));
    open.addEventListener("click", () => setSidebarOpen(true));
    header.prepend(open);
  }
  if (!signedIn && !header.querySelector(".brand")) header.prepend(brand());
  if (signedIn && !header.querySelector(".topbar-title")) {
    const current = [...NAVIGATION, { label: "Account", items: [{ label: "Settings", href: "/settings" }] }]
      .flatMap((group) => group.items.map((item) => ({ group: group.label, ...item })))
      .find((item) => isCurrent(item.href));
    if (current) {
      const title = element("p", "topbar-title", `${current.group} / `);
      title.append(element("strong", "", current.label));
      header.append(title);
    }
  }

  const container = element("div", "header-auth");
  if (signedIn && user?.email) container.append(element("span", "header-user-badge", user.email));
  container.append(themeToggle());
  if (!signedIn && window.location.pathname !== "/login") {
    const loginLink = element("a", "header-logout", "Sign in");
    loginLink.href = "/login";
    container.append(loginLink);
  }
  header.append(container);
}
