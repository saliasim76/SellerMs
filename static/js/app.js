/* Seller Master System – app.js */

document.addEventListener('DOMContentLoaded', function () {

    // ---- Sidebar Toggle (Desktop) ----
    const sidebar = document.getElementById('sidebar');
    const toggleBtn = document.getElementById('sidebarToggle');
    const mainContent = document.querySelector('.main-content');

    if (toggleBtn && sidebar) {
        // Restore state
        if (localStorage.getItem('sidebarCollapsed') === '1') {
            sidebar.classList.add('collapsed');
        }

        toggleBtn.addEventListener('click', function () {
            sidebar.classList.toggle('collapsed');
            localStorage.setItem('sidebarCollapsed', sidebar.classList.contains('collapsed') ? '1' : '0');
        });
    }

    // ---- Sidebar Toggle (Mobile) ----
    const mobileToggle = document.getElementById('sidebarToggleMobile');
    if (mobileToggle && sidebar) {
        mobileToggle.addEventListener('click', function () {
            sidebar.classList.toggle('mobile-open');
        });
        // Close on backdrop click
        document.addEventListener('click', function (e) {
            if (window.innerWidth <= 768 && !sidebar.contains(e.target) && e.target !== mobileToggle) {
                sidebar.classList.remove('mobile-open');
            }
        });
    }

    // ---- Auto-dismiss alerts ----
    document.querySelectorAll('.alert').forEach(function (alert) {
        if (!alert.classList.contains('alert-danger') && !alert.classList.contains('alert-warning')) {
            setTimeout(function () {
                const bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
                bsAlert.close();
            }, 5000);
        }
    });

    // ---- Form validation highlight ----
    document.querySelectorAll('form').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            if (!form.checkValidity()) {
                e.preventDefault();
                e.stopPropagation();
                // Switch to tab with first error
                const firstInvalid = form.querySelector(':invalid');
                if (firstInvalid) {
                    const tabPane = firstInvalid.closest('.tab-pane');
                    if (tabPane) {
                        const tabId = tabPane.id;
                        const tabBtn = document.querySelector(`[data-bs-target="#${tabId}"]`);
                        if (tabBtn) tabBtn.click();
                    }
                    firstInvalid.focus();
                }
            }
            form.classList.add('was-validated');
        });
    });

    // ---- Owner Code formatter (display only) ----
    const codeInput = document.querySelector('[name="owner_code"]');
    if (codeInput) {
        codeInput.setAttribute('readonly', true);
    }

    // ---- Tooltips ----
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
        new bootstrap.Tooltip(el);
    });

    // ---- Table row click to view ----
    document.querySelectorAll('tr[data-href]').forEach(function (row) {
        row.style.cursor = 'pointer';
        row.addEventListener('click', function (e) {
            if (!e.target.closest('button, a, input')) {
                window.location.href = row.dataset.href;
            }
        });
    });

    // ---- Confirm forms ----
    document.querySelectorAll('[data-confirm]').forEach(function (el) {
        el.addEventListener('click', function (e) {
            if (!confirm(el.dataset.confirm)) e.preventDefault();
        });
    });

    // ---- Number inputs: prevent negative ----
    document.querySelectorAll('input[type="number"]').forEach(function (input) {
        input.addEventListener('input', function () {
            if (parseFloat(input.value) < 0) input.value = 0;
        });
    });

    // ---- File input label ----
    document.querySelectorAll('input[type="file"]').forEach(function (input) {
        input.addEventListener('change', function () {
            const label = input.nextElementSibling;
            if (label && label.tagName === 'LABEL') {
                label.textContent = input.files[0]?.name || 'Choose file';
            }
        });
    });

});

/* ════════════════════════════════════════════════════════════════
   PROPER CASE AUTO-CORRECTION — project-wide
   e.g. "ASHIAN COMputer" -> "Ashian Computer". Runs once when a text
   field loses focus (never mid-keystroke, so it can't fight with typing
   the way a browser/extension autocorrect does). Applies to every plain
   text input EXCEPT email/website fields and known code/number/identifier
   fields, where letter casing is fixed by convention (SWIFT/BIC, IBAN,
   VAT, CRN, passport/iqama numbers, phone/mobile/fax, account numbers,
   postal codes, any *_code field, etc.) and must not be touched.
   An individual field can opt out with data-no-propercase.
════════════════════════════════════════════════════════════════ */
const PROPER_CASE_SKIP = [
  'email', 'website', 'url', 'iban', 'swift', 'bic', 'vat', 'crn',
  'password', 'otp', 'captcha', 'account', 'iqama', 'passport',
  'mobile', 'phone', 'fax', 'postal', 'zip', 'code', 'pin', 'csrf', 'token',
];
function properCase(s) {
  return (s || '').trim().replace(/\s+/g, ' ').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}
function shouldSkipProperCase(el) {
  if (el.dataset && el.dataset.noPropercase !== undefined) return true;
  if (el.readOnly || el.disabled) return true;
  const type = (el.type || 'text').toLowerCase();
  if (type !== 'text' && type !== 'search') return true;
  const idlc = (el.id || '').toLowerCase();
  const namelc = (el.name || '').toLowerCase();
  return PROPER_CASE_SKIP.some((k) => idlc.includes(k) || namelc.includes(k));
}
document.addEventListener('focusout', function (e) {
  const el = e.target;
  if (!el || el.tagName !== 'INPUT') return;
  if (shouldSkipProperCase(el)) return;
  const fixed = properCase(el.value);
  if (fixed && fixed !== el.value) el.value = fixed;
}, true);

/* ════════════════════════════════════════════════════════════════
   REQUIRED FIELD VALIDATION — works across ALL forms
   Usage: call validateForm(formEl) before submit
   Marks empty required fields red + shows message below field
════════════════════════════════════════════════════════════════ */

// Add validation styles
(function() {
  const style = document.createElement('style');
  style.textContent = `
    .field-error {
      color: #dc2626;
      font-size: 11px;
      font-weight: 600;
      margin-top: 4px;
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .field-error::before { content: '⚠'; }
    .input-invalid {
      border-color: #dc2626 !important;
      box-shadow: 0 0 0 3px rgba(220,38,38,.1) !important;
    }
    .form-validation-banner {
      background: #fef2f2;
      border: 1.5px solid #fca5a5;
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 16px;
      color: #dc2626;
      font-size: 13px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
    }
  `;
  document.head.appendChild(style);
})();

function validateForm(formEl) {
  if (!formEl) return true;
  let valid = true;
  const IS_AR = document.documentElement.lang === 'ar';

  // Clear previous errors
  formEl.querySelectorAll('.field-error').forEach(el => el.remove());
  formEl.querySelectorAll('.input-invalid').forEach(el => el.classList.remove('input-invalid'));
  formEl.querySelectorAll('.form-validation-banner').forEach(el => el.remove());

  // Check all required inputs, selects, textareas
  const required = formEl.querySelectorAll('input[required], select[required], textarea[required]');
  let firstInvalid = null;

  required.forEach(field => {
    // Skip hidden fields
    if (field.type === 'hidden') return;
    // Skip file inputs
    if (field.type === 'file') return;

    const val = field.value ? field.value.trim() : '';
    if (!val) {
      valid = false;
      field.classList.add('input-invalid');

      // Show error message below field
      const msg = document.createElement('div');
      msg.className = 'field-error';
      const label = field.closest('.field-item, .pl-field-wrap, .mb-3, div')
                        ?.querySelector('label, .field-label, .pl-label-en, .pl-label-ar');
      const fieldName = label ? label.textContent.replace('*','').trim() : (IS_AR ? 'هذا الحقل' : 'This field');
      msg.textContent = IS_AR ? `${fieldName} مطلوب` : `${fieldName} is required`;
      field.insertAdjacentElement('afterend', msg);

      if (!firstInvalid) firstInvalid = field;
    }
  });

  // Also check MCQ hidden inputs that are required
  formEl.querySelectorAll('input[type="hidden"][data-required]').forEach(field => {
    if (!field.value) {
      valid = false;
      const grpId = field.id.replace('f_', '') + 'Grp';
      const grp = document.getElementById(grpId);
      if (grp && !grp.querySelector('.field-error')) {
        const msg = document.createElement('div');
        msg.className = 'field-error';
        msg.textContent = IS_AR ? 'يرجى الاختيار' : 'Please select an option';
        grp.insertAdjacentElement('afterend', msg);
      }
    }
  });

  if (!valid && firstInvalid) {
    // Show banner at top of form
    const banner = document.createElement('div');
    banner.className = 'form-validation-banner';
    banner.innerHTML = IS_AR
      ? '<i class="fas fa-exclamation-circle"></i> يرجى ملء جميع الحقول المطلوبة قبل الحفظ'
      : '<i class="fas fa-exclamation-circle"></i> Please fill all required fields before saving';
    formEl.insertAdjacentElement('afterbegin', banner);

    // Scroll to first invalid field
    firstInvalid.scrollIntoView({ behavior: 'smooth', block: 'center' });
    firstInvalid.focus();

    // Open accordion section if field is inside a hidden one
    const accBody = firstInvalid.closest('.acc-body.hidden');
    if (accBody) {
      const key = accBody.id.replace('body-', '');
      if (typeof togAcc === 'function') togAcc(key);
    }
  }

  return valid;
}

// Auto-clear validation error when user starts typing
document.addEventListener('input', function(e) {
  const field = e.target;
  if (field.classList.contains('input-invalid')) {
    field.classList.remove('input-invalid');
    const err = field.nextElementSibling;
    if (err && err.classList.contains('field-error')) err.remove();
  }
}, true);

document.addEventListener('change', function(e) {
  const field = e.target;
  if (field.classList.contains('input-invalid')) {
    field.classList.remove('input-invalid');
    const err = field.nextElementSibling;
    if (err && err.classList.contains('field-error')) err.remove();
  }
}, true);