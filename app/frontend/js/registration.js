import { createCustomer, requireAuth } from "./api.js";
import { displayValue, element, mountAuthControls } from "./common.js";

const form = document.querySelector("#registration-form");
const errorBox = document.querySelector("#form-error");
const submitButton = document.querySelector("#submit-button");
const successCard = document.querySelector("#registration-success");
const successSummary = document.querySelector("#success-summary");
const successDetails = document.querySelector("#success-details");
const customerLink = document.querySelector("#open-customer-link");
const emailInput = form.elements.namedItem("email");
const emailError = emailInput.parentElement.querySelector(".field-error");

/*MANUAL EDIT*/
function setEmailError(message) {
  emailInput.setAttribute("aria-invalid", "true");
  emailError.textContent = message;
}

function setError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

function formValue(formData, name) {
  const value = String(formData.get(name) ?? "").trim();
  return value || null;
}

export function buildRegistrationPayload(formData) {
  const estimatedValue = formValue(formData, "estimated_value");
  return {
    customer_name: String(formData.get("customer_name") ?? "").trim(),
    status: formData.get("status"),
    sales_stage: formData.get("sales_stage"),
    company: {
      company_name: String(formData.get("company_name") ?? "").trim(),
      industry: formValue(formData, "industry"),
      website: formValue(formData, "website"),
      address: formValue(formData, "address"),
      city: formValue(formData, "city"),
      country: formValue(formData, "country"),
      company_size: formValue(formData, "company_size"),
      description: formValue(formData, "description"),
    },
    primary_contact: {
      name: String(formData.get("contact_name") ?? "").trim(),
      job_title: formValue(formData, "job_title"),
      email: formValue(formData, "email"),
      phone: formValue(formData, "phone"),
    },
    sales_enquiry: {
      product: formValue(formData, "product"),
      enquiry_text: String(formData.get("enquiry_text") ?? "").trim(),
      priority: formData.get("priority"),
      status: formData.get("enquiry_status"),
      lead_source: formValue(formData, "lead_source"),
      estimated_value: estimatedValue,
    },
  };
}

function renderSuccess(result) {
  successSummary.textContent = `${displayValue(result.customer?.customer_name)} at ${displayValue(result.company?.company_name)} has been added.`;
  successDetails.replaceChildren();
  const details = [
    ["Customer ID", result.customer?.customer_id],
    ["Company", result.company?.company_name],
    ["Sales lead", result.sales_enquiry?.product || result.sales_enquiry?.enquiry_text],
    ["Lead stage", result.sales_enquiry?.status],
  ];
  for (const [label, value] of details) {
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", displayValue(value)));
    successDetails.append(row);
  }
  customerLink.href = `/customer?customer_id=${encodeURIComponent(result.customer.customer_id)}`;
  successCard.hidden = false;
  successCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireAuth()) return;
  setError("");
  emailInput.removeAttribute("aria-invalid");
  emailError.textContent = "";

  /*MANUAL CHANGE 3*/
  const email = emailInput.value.trim();

  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    console.log("INVALID EMAIL - BLOCKING SUBMISSION");
    setEmailError("Email is not valid.");
    emailInput.focus();
    return;
  }


  for (const name of ["customer_name", "company_name", "contact_name", "enquiry_text"]) {
    const input = form.elements.namedItem(name);
    if (!input.value.trim()) {
      input.setCustomValidity("Please enter a value that is not blank.");
      input.reportValidity();
      input.setCustomValidity("");
      return;
    }
  }

  const estimatedValue = form.elements.namedItem("estimated_value");
  if (estimatedValue.value && Number(estimatedValue.value) < 0) {
    setError("Estimated value cannot be negative.");
    estimatedValue.focus();
    return;
  }

  submitButton.disabled = true;
  submitButton.setAttribute("aria-busy", "true");
  submitButton.querySelector("[data-button-label]").textContent = "Registering…";
  emailInput.removeAttribute("aria-invalid");
  emailError.textContent = "";

  try {
    const result = await createCustomer(buildRegistrationPayload(new FormData(form)));
    renderSuccess(result);
    form.reset();
    form.querySelectorAll("[aria-invalid='true']").forEach((input) => input.removeAttribute("aria-invalid"));
    } catch (error) {
    const emailValidationError = error.data?.detail?.some?.(
      (item) =>
        Array.isArray(item.loc) &&
        item.loc.includes("primary_contact") &&
        item.loc.includes("email")
    );

    if (error.code === "invalid_email" || emailValidationError) {
      setEmailError("Email is not valid.");
      emailInput.focus();
      return;
    }

    const message =
      error.status === 409
        ? error.message || "A customer with these details is already registered."
        : error.status === 422
          ? `Please review the registration details: ${error.message}`
          : error.status >= 500
            ? "The server could not complete registration. Please try again."
            : error.message || "Unable to register customer. Check your connection and try again.";

    setError(message);
  } finally {
    submitButton.disabled = false;
    submitButton.removeAttribute("aria-busy");
    submitButton.querySelector("[data-button-label]").textContent = "Register customer";
  }
});

document.querySelector("#register-another").addEventListener("click", () => {
  successCard.hidden = true;
  form.scrollIntoView({ behavior: "smooth", block: "start" });
  form.elements.namedItem("customer_name").focus();
});

mountAuthControls();
requireAuth();

