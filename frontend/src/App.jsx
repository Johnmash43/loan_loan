import { useCallback, useEffect, useState } from "react";

const SECTION_ROUTES = {
  Overview: "/overview",
  Clients: "/clients",
  Ledgers: "/ledgers",
  Loans: "/loans",
  Payments: "/payments",
  Notifications: "/notifications",
  Staff: "/staff",
};

function sectionForPath(pathname) {
  const path = pathname.replace(/\/+$/, "").toLowerCase() || "/";
  return Object.entries(SECTION_ROUTES).find(([, route]) => route === path)?.[0] || "Overview";
}

const blankClient = {
  name: "",
  phone: "",
  national_id: "",
  business_type: "",
  business_location: "",
  residence_location: "",
  guarantor_name: "",
  guarantor_phone: "",
  guarantor_national_id: "",
  guarantor_business_type: "",
  guarantor_business_location: "",
  guarantor_residence_location: "",
  assigned_staff_id: "",
};

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    credentials: "same-origin",
    headers,
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Request failed.");
  return data;
}

function money(value) {
  return `KES ${Number(value || 0).toLocaleString("en-KE", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function App() {
  const [authenticated, setAuthenticated] = useState(false);
  const [sessionUserId, setSessionUserId] = useState(null);
  const [role, setRole] = useState("");
  const [loading, setLoading] = useState(true);
  const [section, setSection] = useState(() => sectionForPath(window.location.pathname));
  const [clientScope, setClientScope] = useState("mine");
  const [dashboard, setDashboard] = useState(null);
  const [clients, setClients] = useState([]);
  const [loans, setLoans] = useState([]);
  const [payments, setPayments] = useState([]);
  const [notifications, setNotifications] = useState([]);
  const [dailyLedgerReportDate, setDailyLedgerReportDate] = useState("2026-10-01");
  const [dailyLedgerReport, setDailyLedgerReport] = useState(null);
  const [dailyLedgerLoading, setDailyLedgerLoading] = useState(false);
  const [renewalRequests, setRenewalRequests] = useState([]);
  const [ledgerEditRequests, setLedgerEditRequests] = useState([]);
  const [staff, setStaff] = useState([]);
  const [ledgerFiles, setLedgerFiles] = useState([]);
  const [paymentSearch, setPaymentSearch] = useState("");
  const [paymentDate, setPaymentDate] = useState("");
  const [selectedLedger, setSelectedLedger] = useState(null);
  const [ledgerData, setLedgerData] = useState(null);
  const [ledgerLoading, setLedgerLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [clientForm, setClientForm] = useState(blankClient);
  const [formFile, setFormFile] = useState(null);
  const [staffForm, setStaffForm] = useState({ username: "", full_name: "", password: "" });
  const [loanForm, setLoanForm] = useState({
    client_id: "",
    principal: "",
    interest_rate: "25",
    term_days: "25",
  });
  const [renewalForm, setRenewalForm] = useState({
    renewal_request_id: "",
    loan_number: "",
    principal: "",
    total_due: "",
    issued_on: "",
    due_on: "",
  });

  useEffect(() => {
    function syncSectionFromUrl() {
      setSection(sectionForPath(window.location.pathname));
    }
    window.addEventListener("popstate", syncSectionFromUrl);
    return () => window.removeEventListener("popstate", syncSectionFromUrl);
  }, []);

  useEffect(() => {
    if (loading) return;
    const requestedSection = sectionForPath(window.location.pathname);
    const targetSection = requestedSection === "Staff" && role !== "admin"
      ? "Overview"
      : requestedSection;
    const targetPath = SECTION_ROUTES[targetSection];
    if (window.location.pathname !== targetPath) {
      window.history.replaceState({}, "", targetPath);
    }
    if (targetSection !== section) setSection(targetSection);
  }, [loading, role, section]);

  const loadData = useCallback(async () => {
    try {
      const scope = role === "staff" ? clientScope : "company";
      const [stats, clientRows, loanRows, paymentRows, notificationRows, renewals, editRequests] = await Promise.all([
        api(`/api/dashboard?scope=${scope}`),
        api(`/api/clients?scope=${scope}`),
        api("/api/loans"),
        api("/api/payments"),
        api("/api/notifications"),
        api("/api/renewal-requests"),
        api("/api/ledger-edit-requests"),
      ]);
      setDashboard(stats);
      setClients(clientRows);
      setLoans(loanRows);
      setPayments(paymentRows);
      setNotifications(notificationRows);
      setRenewalRequests(renewals);
      setLedgerEditRequests(editRequests);
    } catch (requestError) {
      setError(requestError.message);
    }
  }, [clientScope, role]);

  useEffect(() => {
    if (!authenticated || role !== "admin") return undefined;
    const timer = window.setInterval(() => {
      Promise.all([api("/api/renewal-requests"), api("/api/ledger-edit-requests")])
        .then(([renewals, editRequests]) => {
          setRenewalRequests(renewals);
          setLedgerEditRequests(editRequests);
        })
        .catch((requestError) => setError(requestError.message));
    }, 15000);
    return () => window.clearInterval(timer);
  }, [authenticated, role]);

  useEffect(() => {
    if (!authenticated || role !== "staff") return undefined;
    const timer = window.setInterval(() => {
      api("/api/ledger-edit-requests")
        .then(setLedgerEditRequests)
        .catch((requestError) => setError(requestError.message));
    }, 15000);
    return () => window.clearInterval(timer);
  }, [authenticated, role]);

  useEffect(() => {
    api("/api/session")
      .then(({ authenticated: isAuthenticated, id, role: currentRole }) => {
        setAuthenticated(isAuthenticated);
        setSessionUserId(id || null);
        setRole(currentRole || "");
        if (currentRole === "staff") setClientScope("mine");
        else setClientScope("company");
        return undefined;
      })
      .catch((requestError) => setError(requestError.message))
      .finally(() => setLoading(false));
  }, [loadData]);

  useEffect(() => {
    if (!authenticated || !role) return undefined;
    if (role === "staff") setClientScope((current) => current || "mine");
    else setClientScope("company");
    loadData();
    return undefined;
  }, [authenticated, role, clientScope, loadData]);

  useEffect(() => {
    if (authenticated && role === "admin") {
      api("/api/staff").then(setStaff).catch((requestError) => setError(requestError.message));
    }
  }, [authenticated, role]);

  useEffect(() => {
    if (authenticated && section === "Ledgers") {
      api("/api/client-ledgers").then(setLedgerFiles).catch((requestError) => setError(requestError.message));
    }
  }, [authenticated, section]);

  useEffect(() => {
    if (!authenticated || section !== "Payments") return undefined;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams();
      if (paymentSearch.trim()) params.set("search", paymentSearch.trim());
      if (paymentDate) params.set("date", paymentDate);
      api(`/api/payments?${params}`)
        .then((rows) => { if (!cancelled) setPayments(rows); })
        .catch((requestError) => { if (!cancelled) setError(requestError.message); });
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [authenticated, section, paymentSearch, paymentDate]);

  useEffect(() => {
    if (!authenticated || section !== "Notifications") return undefined;
    setDailyLedgerLoading(true);
    api(`/api/ledger/daily-payments?date=${dailyLedgerReportDate}`)
      .then(setDailyLedgerReport)
      .catch((requestError) => setError(requestError.message))
      .finally(() => setDailyLedgerLoading(false));
    return undefined;
  }, [authenticated, section, dailyLedgerReportDate]);

  async function submit(action, message) {
    setError("");
    setSuccess("");
    try {
      await action();
      await loadData();
      setSuccess(message);
      return true;
    } catch (requestError) {
      setError(requestError.message);
      return false;
    }
  }

  async function login(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const ok = await submit(
      async () => {
        const account = await api("/api/login", {
          method: "POST",
          body: JSON.stringify({
            username: form.get("username"),
            password: form.get("password"),
          }),
        });
        setSessionUserId(account.id || null);
        setRole(account.role);
        setAuthenticated(true);
      },
      "Welcome back.",
    );
    if (ok) await loadData();
  }

  async function logout() {
    await submit(async () => {
      await api("/api/logout", { method: "POST" });
      setAuthenticated(false);
      setSessionUserId(null);
      setRole("");
    }, "You have been signed out.");
  }

  async function saveClient(event) {
    event.preventDefault();
    const body = new FormData();
    Object.entries(clientForm).forEach(([key, value]) => {
      if (value !== "" && value != null) body.append(key, value);
    });
    if (role === "staff" && !body.has("assigned_staff_id")) body.append("assigned_staff_id", String(sessionUserId || ""));
    if (formFile) body.append("form_file", formFile);
    const ok = await submit(
      () => api("/api/clients", { method: "POST", body }),
      "Client registered and added to the Excel ledger.",
    );
    if (ok) {
      setClientForm(blankClient);
      setFormFile(null);
    }
  }

  async function saveLoan(event) {
    event.preventDefault();
    const ok = await submit(
      () => api("/api/loans", { method: "POST", body: JSON.stringify(loanForm) }),
      "Loan issued and added to the ledger.",
    );
    if (ok) setLoanForm({ ...loanForm, client_id: "", principal: "" });
  }

  async function requestRenewal(loan) {
    await submit(
      () => api("/api/renewal-requests", {
        method: "POST",
        body: JSON.stringify({ source_loan_id: loan.id }),
      }),
      `Renewal request sent for ${loan.client_name}.`,
    );
  }

  async function reviewRenewal(renewal, decision) {
    await submit(
      () => api(`/api/renewal-requests/${renewal.id}`, {
        method: "PATCH",
        body: JSON.stringify({ decision }),
      }),
      `Renewal request ${decision}.`,
    );
  }

  async function requestLedgerEditAccess() {
    await submit(
      () => api("/api/ledger-edit-requests", { method: "POST" }),
      "Ledger edit access request sent to the administrator.",
    );
  }

  async function reviewLedgerEditRequest(editRequest, decision) {
    await submit(
      () => api(`/api/ledger-edit-requests/${editRequest.id}`, {
        method: "PATCH",
        body: JSON.stringify({ decision }),
      }),
      decision === "grant"
        ? `Ledger edit access granted to ${editRequest.staff_name}.`
        : decision === "deny"
          ? `Ledger edit access request denied for ${editRequest.staff_name}.`
          : `Ledger edit access revoked for ${editRequest.staff_name}.`,
    );
  }

  async function saveRenewal(event) {
    event.preventDefault();
    const ok = await submit(
      () => api("/api/loans", { method: "POST", body: JSON.stringify(renewalForm) }),
      "Authorized renewal issued and added to the ledger.",
    );
    if (ok) setRenewalForm({
      renewal_request_id: "",
      loan_number: "",
      principal: "",
      total_due: "",
      issued_on: "",
      due_on: "",
    });
  }

  async function setAccount(client, status) {
    await submit(
      () =>
        api(`/api/clients/${client.id}/account`, {
          method: "PATCH",
          body: JSON.stringify({
            status,
            note: status === "paused" ? "Paused by staff." : "",
          }),
        }),
      `Account ${status}.`,
    );
  }

  async function saveStaff(event) {
    event.preventDefault();
    const ok = await submit(async () => {
      await api("/api/staff", { method: "POST", body: JSON.stringify(staffForm) });
      setStaff(await api("/api/staff"));
    }, "Staff account created.");
    if (ok) setStaffForm({ username: "", full_name: "", password: "" });
  }

  async function toggleStaffActive(member) {
    const active = !member.active;
    await submit(
      async () => {
        await api(`/api/staff/${member.id}`, {
          method: "PATCH",
          body: JSON.stringify({ active }),
        });
        setStaff(await api("/api/staff"));
      },
      `${member.full_name}'s account ${active ? "activated" : "deactivated"}.`,
    );
  }

  async function openLedger(ledger) {
    setSelectedLedger(ledger);
    setLedgerData(null);
    setLedgerLoading(true);
    setError("");
    try {
      setLedgerData(await api(`/api/client-ledgers/${ledger.id}`));
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLedgerLoading(false);
    }
  }

  async function saveLedgerEdits(edits, revision) {
    if (!selectedLedger || !edits.length) return false;
    let saveResult;
    const ok = await submit(
      async () => {
        saveResult = await api(`/api/client-ledgers/${selectedLedger.id}`, {
          method: "PUT",
          body: JSON.stringify({ revision, edits }),
        });
      },
      "Client ledger changes saved.",
    );
    if (ok && role === "staff" && saveResult?.edit_access_status === "revoked") {
      setLedgerEditRequests((current) =>
        current.map((editRequest) => ({ ...editRequest, status: "revoked" })),
      );
    }
    if (ok) await openLedger(selectedLedger);
    return ok;
  }

  if (loading) return <div className="loading">Loading Nama Traders…</div>;
  if (!authenticated) {
    return (
      <main className="login-page">
        <form className="login-card" onSubmit={login}>
          <div className="brand-mark">NT</div>
          <p className="eyebrow">STAFF PORTAL</p>
          <h1>Welcome back</h1>
          <p className="muted">Sign in to manage clients, loans, and repayments.</p>
          {error && <div className="alert error">{error}</div>}
          <label>
            Username
            <input name="username" autoComplete="username" required />
          </label>
          <label>
            Password
            <input name="password" type="password" autoComplete="current-password" required />
          </label>
          <button className="primary full" type="submit">Sign in</button>
          <p className="hint">Local demo: admin / admin123</p>
        </form>
      </main>
    );
  }

  const nav = ["Overview", "Clients", "Ledgers", "Loans", "Payments", "Notifications"];
  if (role === "admin") nav.push("Staff");
  const pendingRenewal = role === "admin"
    ? renewalRequests.find((renewal) => renewal.status === "pending")
    : null;
  const pendingLedgerEditRequest = role === "admin"
    ? ledgerEditRequests.find((editRequest) => editRequest.status === "pending")
    : null;
  const currentLedgerEditRequest = ledgerEditRequests[0];
  const canEditLedgers = role === "admin" || currentLedgerEditRequest?.status === "granted";
  const approvedRenewals = renewalRequests.filter((renewal) => renewal.status === "approved");
  const filteredPayments = payments;
  function navigateToSection(targetSection) {
    const targetPath = SECTION_ROUTES[targetSection];
    if (!targetPath || (targetSection === "Staff" && role !== "admin")) return;
    if (window.location.pathname !== targetPath) {
      window.history.pushState({}, "", targetPath);
    }
    setSection(targetSection);
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">NT</span><strong>Nama Traders</strong></div>
        <p className="nav-label">WORKSPACE</p>
        <nav>
          {nav.map((item) => (
            <button
              className={`nav-item ${section === item ? "selected" : ""}`}
              key={item}
              title={item}
              aria-label={item}
              onClick={() => navigateToSection(item)}
            >
              <span className="nav-dot" />{item}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <strong>Paybill only</strong>
          <p>Cash payments are not accepted. Use the client’s National ID as the Paybill account.</p>
        </div>
        <button className="logout" onClick={logout}>Sign out</button>
      </aside>
      <main className="main">
        <header className="topbar">
          <div><p className="eyebrow">LOAN MANAGEMENT</p><h1>{section}</h1></div>
          <div className="topbar-actions"><div className="staff-avatar">{role === "admin" ? "A" : "S"}</div></div>
        </header>
        {error && <div className="alert error" role="alert">{error}<button onClick={() => setError("")}>×</button></div>}
        {success && <div className="alert success" role="status">{success}<button onClick={() => setSuccess("")}>×</button></div>}

        {pendingRenewal && !pendingLedgerEditRequest && (
          <div className="modal-backdrop" role="presentation">
            <section className="renewal-modal" role="dialog" aria-modal="true" aria-labelledby="renewal-title">
              <p className="eyebrow">RENEWAL APPROVAL</p>
              <h2 id="renewal-title">{pendingRenewal.client_name} has cleared the loan</h2>
              <p>
                {pendingRenewal.requester_name} is requesting permission to renew loan{" "}
                {pendingRenewal.source_loan_number} for {pendingRenewal.client_name}.
              </p>
              <div className="renewal-modal-actions">
                <button className="secondary" onClick={() => reviewRenewal(pendingRenewal, "denied")}>Decline</button>
                <button className="primary" onClick={() => reviewRenewal(pendingRenewal, "approved")}>Approve renewal</button>
              </div>
            </section>
          </div>
        )}
        {pendingLedgerEditRequest && (
          <div className="modal-backdrop" role="presentation">
            <section className="renewal-modal" role="dialog" aria-modal="true" aria-labelledby="ledger-edit-approval-title">
              <p className="eyebrow">LEDGER EDIT ACCESS</p>
              <h2 id="ledger-edit-approval-title">{pendingLedgerEditRequest.staff_name} requested ledger editing</h2>
              <p>Granting access lets this staff member edit the client workbook ledgers visible to their assigned locations.</p>
              <div className="renewal-modal-actions">
                <button className="secondary" onClick={() => reviewLedgerEditRequest(pendingLedgerEditRequest, "deny")}>Decline</button>
                <button className="primary" onClick={() => reviewLedgerEditRequest(pendingLedgerEditRequest, "grant")}>Grant access</button>
              </div>
            </section>
          </div>
        )}

        {section === "Overview" && (
          <>
            <section className="stats-grid">
              <Stat label={role === "staff" ? "My clients" : "Registered clients"} value={role === "staff" ? dashboard?.my_clients ?? 0 : dashboard?.clients ?? 0} icon="01" />
              <Stat label={role === "staff" ? "Company clients" : "Active loans"} value={role === "staff" ? dashboard?.company_clients ?? 0 : dashboard?.active_loans ?? 0} icon="02" />
              <Stat label={role === "staff" ? "My payments today" : "Outstanding balance"} value={role === "staff" ? money(dashboard?.my_paid_today) : money(dashboard?.outstanding)} icon="03" />
              <Stat label={role === "staff" ? "Company payments today" : "Overdue loans"} value={role === "staff" ? money(dashboard?.company_paid_today) : dashboard?.overdue_loans ?? 0} icon="04" />
            </section>
            {dashboard?.location_summaries && (
              <section className="panel location-panel">
                <div className="panel-heading">
                  <div>
                    <h2>{role === "admin" ? "Location portfolio" : "Location performance"}</h2>
                    <p>
                      {role === "admin"
                        ? "Outstanding balances and payments recovered under the 25% interest rule"
                        : "Your assigned locations are shown first, followed by company-wide location performance"}
                    </p>
                  </div>
                </div>
                <div className="location-total">
                  <h3>All locations <span>{dashboard.overall_location_summary?.clients ?? 0} clients</span></h3>
                  <div className="location-total-metrics">
                    <div><span>Outstanding</span><strong>{money(dashboard.overall_location_summary?.outstanding)}</strong></div>
                    <div><span>Total recovered</span><strong>{money(dashboard.overall_location_summary?.recovered)}</strong></div>
                    <div><span>Principal recovered</span><strong>{money(dashboard.overall_location_summary?.principal_recovered)}</strong></div>
                    <div><span>25% interest recovered</span><strong>{money(dashboard.overall_location_summary?.interest_recovered)}</strong></div>
                  </div>
                </div>
                <div className="location-summary-grid">
                  {dashboard.location_summaries.map((item) => (
                    <article className="location-summary-card" key={item.location}>
                      <h3>{item.location}{item.assigned_to_staff && <span className="location-assigned-badge">Your location</span>}</h3>
                      <p>{item.clients} {item.clients === 1 ? "client" : "clients"}</p>
                      <div className="location-metric">
                        <span>Outstanding balance</span>
                        <strong>{money(item.outstanding)}</strong>
                      </div>
                      <div className="location-metric">
                        <span>Total recovered</span>
                        <strong>{money(item.recovered)}</strong>
                      </div>
                      <div className="location-recovery-split">
                        <span>Principal: {money(item.principal_recovered)}</span>
                        <span>Interest: {money(item.interest_recovered)}</span>
                      </div>
                    </article>
                  ))}
                </div>
                <p className="location-rule-note">For loans with 25% interest, recovered payments are allocated proportionally between principal and interest.</p>
              </section>
            )}
            {dashboard?.failed_notifications > 0 && (
              <div className="alert warning">{dashboard.failed_notifications} notification delivery/deliveries failed. Check the Notifications tab and webhook configuration.</div>
            )}
            <div className="panel">
              <div className="panel-heading"><div><h2>Recent loans</h2><p>Latest activity across client accounts</p></div><button className="text-button" onClick={() => navigateToSection("Loans")}>View all →</button></div>
              <LoanTable rows={loans.slice(0, 5)} canRequest={role === "staff"} renewalRequests={renewalRequests} onRequest={requestRenewal} />
            </div>
          </>
        )}

        {section === "Clients" && (
          <div className="content-grid">
            {role === "staff" && (
              <div className="panel scope-toggle-row">
                <button className={clientScope === "mine" ? "primary" : "secondary"} onClick={() => setClientScope("mine")}>My clients</button>
                <button className={clientScope === "company" ? "primary" : "secondary"} onClick={() => setClientScope("company")}>Company clients</button>
              </div>
            )}
            <section className="panel">
              <div className="panel-heading"><div><h2>Client register</h2><p>Clients, guarantors and generated forms</p></div></div>
              <ClientTable rows={clients} onToggle={setAccount} />
            </section>
            <form className="panel form-panel" onSubmit={saveClient}>
              <div className="panel-heading"><div><h2>Register a client</h2><p>Enter client and guarantor details</p></div></div>
              <div className="form-section">CLIENT DETAILS</div>
              <div className="form-grid">
                {role === "admin" && (
                  <label>
                    Assign to staff
                    <select
                      value={clientForm.assigned_staff_id}
                      onChange={(event) => setClientForm({ ...clientForm, assigned_staff_id: event.target.value })}
                    >
                      <option value="">Assign automatically by location</option>
                      <option value="unassigned">Leave unassigned</option>
                      {staff.filter((member) => member.active && member.role === "staff").map((member) => (
                        <option key={member.id} value={member.id}>{member.full_name}</option>
                      ))}
                    </select>
                  </label>
                )}
                <Field label="Full name" name="name" value={clientForm.name} onChange={updateClient(setClientForm)} required />
                <Field label="Phone number" name="phone" value={clientForm.phone} onChange={updateClient(setClientForm)} placeholder="+254…" required />
                <Field label="National ID" name="national_id" value={clientForm.national_id} onChange={updateClient(setClientForm)} required />
                <Field label="Business type" name="business_type" value={clientForm.business_type} onChange={updateClient(setClientForm)} required />
                <Field label="Business location" name="business_location" value={clientForm.business_location} onChange={updateClient(setClientForm)} required />
                <Field label="Residence location" name="residence_location" value={clientForm.residence_location} onChange={updateClient(setClientForm)} required />
                <label className="span-two">Registration form (.docx, optional)
                  <input
                    type="file"
                    accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    onChange={(event) => setFormFile(event.target.files?.[0] || null)}
                  />
                </label>
              </div>
              <div className="form-section">GUARANTOR DETAILS</div>
              <div className="form-grid">
                <Field label="Full name" name="guarantor_name" value={clientForm.guarantor_name} onChange={updateClient(setClientForm)} required />
                <Field label="Phone number" name="guarantor_phone" value={clientForm.guarantor_phone} onChange={updateClient(setClientForm)} placeholder="+254…" required />
                <Field label="National ID" name="guarantor_national_id" value={clientForm.guarantor_national_id} onChange={updateClient(setClientForm)} required />
                <Field label="Business type" name="guarantor_business_type" value={clientForm.guarantor_business_type} onChange={updateClient(setClientForm)} required />
                <Field label="Business location" name="guarantor_business_location" value={clientForm.guarantor_business_location} onChange={updateClient(setClientForm)} required />
                <Field className="span-two" label="Residence location" name="guarantor_residence_location" value={clientForm.guarantor_residence_location} onChange={updateClient(setClientForm)} required />
              </div>
              <button className="primary" type="submit">Save client</button>
            </form>
          </div>
        )}

        {section === "Ledgers" && (
          <LedgerBrowser
            ledgers={ledgerFiles}
            role={role}
            editAccessStatus={currentLedgerEditRequest?.status || ""}
            canEdit={canEditLedgers}
            selected={selectedLedger}
            data={ledgerData}
            loading={ledgerLoading}
            onSelect={openLedger}
            onRequestAccess={requestLedgerEditAccess}
            onSave={saveLedgerEdits}
          />
        )}

        {section === "Loans" && (
          <div className="stack">
            {role === "admin" ? <form className="panel loan-form" onSubmit={saveLoan}>
              <div className="panel-heading"><div><h2>Issue a loan</h2><p>Total due = principal + flat interest. Repayments start the day after issue.</p></div></div>
              <div className="loan-fields">
                <label>Client
                  <select value={loanForm.client_id} onChange={(event) => setLoanForm({ ...loanForm, client_id: event.target.value })} required>
                    <option value="">Select an eligible client</option>
                    {clients.filter((client) => client.account_status === "active" && !client.active_loan_id).map((client) => (
                      <option key={client.id} value={client.id}>{client.name} · ID {client.national_id}</option>
                    ))}
                  </select>
                </label>
                <Field label="Principal (KES)" name="principal" type="number" min="0.01" step="0.01" value={loanForm.principal} onChange={(event) => setLoanForm({ ...loanForm, principal: event.target.value })} required />
                <Field label="Flat interest (%)" name="interest_rate" type="number" min="0" max="1000" step="0.01" value={loanForm.interest_rate} onChange={(event) => setLoanForm({ ...loanForm, interest_rate: event.target.value })} required />
                <label>Term
                  <select value={loanForm.term_days} onChange={(event) => setLoanForm({ ...loanForm, term_days: event.target.value })}>
                    {[25, 26, 27, 28, 29, 30].map((days) => <option key={days} value={days}>{days} days</option>)}
                  </select>
                </label>
              </div>
              <button className="primary" type="submit">Issue loan</button>
            </form> : (
              <div className="stack">
                <form className="panel loan-form" onSubmit={saveRenewal}>
                  <div className="panel-heading"><div><h2>Issue an approved renewal</h2><p>Only the loan number, amount issued, total repayment, issue date, and due date can be entered.</p></div></div>
                  {approvedRenewals.length ? (
                    <>
                      <div className="loan-fields renewal-fields">
                        <label>Approved client and request
                          <select
                            value={renewalForm.renewal_request_id}
                            onChange={(event) => setRenewalForm({ ...renewalForm, renewal_request_id: event.target.value })}
                            required
                          >
                            <option value="">Select an approved renewal</option>
                            {approvedRenewals.map((renewal) => (
                              <option key={renewal.id} value={renewal.id}>
                                {renewal.client_name} · request #{renewal.id}
                              </option>
                            ))}
                          </select>
                        </label>
                        <Field label="Loan number" name="loan_number" value={renewalForm.loan_number} onChange={(event) => setRenewalForm({ ...renewalForm, loan_number: event.target.value })} required />
                        <Field label="Amount issued (KES)" name="principal" type="number" min="0.01" step="0.01" value={renewalForm.principal} onChange={(event) => setRenewalForm({ ...renewalForm, principal: event.target.value })} required />
                        <Field label="Total amount to be paid (KES)" name="total_due" type="number" min="0.01" step="0.01" value={renewalForm.total_due} onChange={(event) => setRenewalForm({ ...renewalForm, total_due: event.target.value })} required />
                        <Field label="Date issued" name="issued_on" type="date" value={renewalForm.issued_on} onChange={(event) => setRenewalForm({ ...renewalForm, issued_on: event.target.value })} required />
                        <Field label="Due date" name="due_on" type="date" value={renewalForm.due_on} onChange={(event) => setRenewalForm({ ...renewalForm, due_on: event.target.value })} required />
                      </div>
                      <button className="primary" type="submit">Issue renewal</button>
                    </>
                  ) : <p className="muted">No renewal request has been approved for your account.</p>}
                </form>
              </div>
            )}
            <section className="panel"><div className="panel-heading"><div><h2>Loan ledger</h2><p>Principal, interest, payment balance, and due date</p></div></div><LoanTable rows={loans} canRequest={role === "staff"} renewalRequests={renewalRequests} onRequest={requestRenewal} /></section>
          </div>
        )}

        {section === "Payments" && (
          <div className="stack payments-print-area">
            <section className="panel">
              <div className="panel-heading payments-heading">
                <div><h2>Payment history</h2><p>Search by client name or National ID, and optionally filter by payment date.</p></div>
                <div className="payments-actions">
                  <span className="badge orange">PAYBILL ONLY</span>
                  <button className="secondary no-print" type="button" onClick={() => window.print()} disabled={!filteredPayments.length}>Print list</button>
                </div>
              </div>
              <div className="payment-filters no-print">
                <label>
                  Search client
                  <input
                    type="search"
                    value={paymentSearch}
                    onChange={(event) => setPaymentSearch(event.target.value)}
                    placeholder="Client name or National ID…"
                  />
                </label>
                <label>
                  Payment date
                  <input type="date" value={paymentDate} onChange={(event) => setPaymentDate(event.target.value)} />
                </label>
                <span className="payment-result-count">{filteredPayments.length} payment{filteredPayments.length === 1 ? "" : "s"}</span>
              </div>
              <DataTable
                headers={["Transaction", "Client", "Account ID", "Date", "Time", "Amount Paid", "Balance"]}
                rows={filteredPayments.map((row) => [
                  row.transaction_id, row.client_name, row.national_id,
                  row.payment_date, row.payment_time, money(row.amount), money(row.balance),
                ])}
                empty={payments.length ? "No payments match this client and date." : "No Paybill deposits received yet."}
              />
            </section>
          </div>
        )}

        {section === "Notifications" && (
          <div className="stack">
            <section className="panel">
              <div className="panel-heading">
                <div><h2>Daily ledger payments</h2><p>Payment entries recorded in the client workbooks, grouped by location.</p></div>
                <label>Report date
                  <input
                    type="date"
                    value={dailyLedgerReportDate}
                    onChange={(event) => setDailyLedgerReportDate(event.target.value)}
                  />
                </label>
              </div>
              {dailyLedgerReport?.unreadable_workbooks > 0 && (
                <div className="alert warning">
                  {dailyLedgerReport.unreadable_workbooks} workbook(s) could not be read
                  ({Object.entries(dailyLedgerReport.unreadable_by_location || {}).map(([location, count]) => `${location}: ${count}`).join(", ")}).
                  Their entries may be missing from this report.
                </div>
              )}
              {dailyLedgerLoading && <div className="loading-inline">Refreshing payment rows from the ledgers…</div>}
              {!dailyLedgerLoading && dailyLedgerReport && (
                role === "admin" ? (
                  <DailyPaymentGroups rows={dailyLedgerReport.all_payments} empty="No ledger payments were recorded on this date." />
                ) : (
                  <>
                    <section className="daily-report-section">
                      <div className="panel-heading"><div><h2>My location payments</h2><p>{dailyLedgerReport.my_locations.join(", ") || "No locations assigned"}</p></div></div>
                      <DailyPaymentGroups rows={dailyLedgerReport.my_payments} empty="No payments were recorded for your locations on this date." />
                    </section>
                    <section className="daily-report-section">
                      <div className="panel-heading"><div><h2>Overall staff payments</h2><p>All staff locations, grouped by location</p></div></div>
                      <DailyPaymentGroups rows={dailyLedgerReport.all_payments} empty="No ledger payments were recorded on this date." />
                    </section>
                  </>
                )
              )}
            </section>
            <section className="panel">
              <div className="panel-heading"><div><h2>Notification log</h2><p>Payment receipts, balances, due dates, and late reminders</p></div></div>
              <DataTable headers={["Client", "Message", "Status", "Created"]} rows={notifications.map((row) => [row.client_name, row.message, <span className={`badge ${row.status}`}>{row.status}{row.error ? `: ${row.error}` : ""}</span>, row.created_at])} />
              {!notifications.length && <Empty text="Notifications will appear here when generated." />}
            </section>
          </div>
        )}

        {section === "Staff" && role === "admin" && (
          <div className="content-grid staff-layout">
            <section className="panel">
              <div className="panel-heading"><div><h2>Staff accounts</h2><p>People with access to the loan management system</p></div></div>
              <DataTable
                headers={["Name", "Username", "Access", "Status", "Added", "Action"]}
                rows={staff.map((member) => [
                  member.full_name, member.username, member.role,
                  <span className={`badge ${member.active ? "active" : "paused"}`}>{member.active ? "active" : "inactive"}</span>,
                  member.created_at,
                  <button
                    className="text-button small"
                    onClick={() => toggleStaffActive(member)}
                    disabled={member.role !== "staff"}
                  >
                    {member.active ? "Deactivate" : "Activate"}
                  </button>,
                ])}
                empty="No staff accounts yet."
              />
              <div className="panel-heading renewal-queue-heading"><div><h2>Ledger edit access</h2><p>Staff can edit their assigned client workbooks only after approval. Access remains active until revoked.</p></div></div>
              <DataTable
                headers={["Staff member", "Status", "Requested", "Action"]}
                rows={ledgerEditRequests.map((editRequest) => [
                  editRequest.staff_name,
                  <span className={`badge ${editRequest.status === "granted" ? "active" : editRequest.status === "pending" ? "pending" : ""}`}>{editRequest.status}</span>,
                  editRequest.created_at,
                  editRequest.status === "pending" ? (
                    <>
                      <button className="text-button small" onClick={() => reviewLedgerEditRequest(editRequest, "grant")}>Grant</button>
                      <button className="text-button small" onClick={() => reviewLedgerEditRequest(editRequest, "deny")}>Decline</button>
                    </>
                  ) : editRequest.status === "granted" ? (
                    <button className="text-button small" onClick={() => reviewLedgerEditRequest(editRequest, "revoke")}>Revoke access</button>
                  ) : "—",
                ])}
                empty="No staff ledger edit requests."
              />
              <div className="panel-heading renewal-queue-heading"><div><h2>Renewal requests</h2><p>Requests from staff appear here and as an approval pop-up.</p></div></div>
              <DataTable
                headers={["Client", "Requested by", "Cleared loan", "Status"]}
                rows={renewalRequests.map((renewal) => [
                  renewal.client_name, renewal.requester_name, renewal.source_loan_number,
                  <span className={`badge ${renewal.status}`}>{renewal.status}</span>,
                ])}
                empty="No staff renewal requests yet."
              />
            </section>
            <form className="panel form-panel" onSubmit={saveStaff}>
              <div className="panel-heading"><div><h2>Add staff member</h2><p>New accounts receive standard staff access, not admin rights.</p></div></div>
              <div className="form-grid one-column">
                <Field label="Full name" name="full_name" value={staffForm.full_name} onChange={(event) => setStaffForm({ ...staffForm, full_name: event.target.value })} required />
                <Field label="Username" name="username" value={staffForm.username} onChange={(event) => setStaffForm({ ...staffForm, username: event.target.value })} minLength="3" maxLength="40" required />
                <Field label="Temporary password (10+ characters)" name="password" type="password" value={staffForm.password} onChange={(event) => setStaffForm({ ...staffForm, password: event.target.value })} minLength="10" autoComplete="new-password" required />
              </div>
              <button className="primary" type="submit">Create staff account</button>
            </form>
          </div>
        )}
      </main>
    </div>
  );
}

function updateClient(setter) {
  return (event) => setter((current) => ({ ...current, [event.target.name]: event.target.value }));
}

function Field({ label, className = "", ...props }) {
  return <label className={className}>{label}<input {...props} /></label>;
}

function Stat({ label, value, icon }) {
  return <article className="stat-card"><span className="stat-icon">{icon}</span><p>{label}</p><strong>{value}</strong></article>;
}

function ClientTable({ rows, onToggle }) {
  if (!rows.length) return <Empty text="No clients registered yet." />;
  return (
    <div className="table-wrap"><table><thead><tr><th>Client</th><th>National ID</th><th>Phone</th><th>Assigned to</th><th>Principal Balance</th><th>Total Balance</th><th>Interest Earned</th><th>Form</th><th>Account</th><th>Action</th></tr></thead>
      <tbody>{rows.map((client) => <tr key={client.id}>
        <td><strong>{client.name}</strong><small>{client.business_type}</small></td>
        <td>{client.national_id}</td><td>{client.phone}</td>
        <td>{client.assigned_staff_name || "Unassigned"}</td>
        <td>{client.active_loan_id ? money(client.active_principal_balance) : "—"}</td>
        <td>{client.active_loan_id ? money(client.active_total_balance) : "—"}</td>
        <td>{client.active_loan_id ? money(client.active_interest_earned) : "—"}</td>
        <td>{client.form_filename ? <a href={`/api/clients/${client.id}/form`}>{client.form_original_name || "Download Word form ↗"}</a> : <span className="muted">—</span>}</td>
        <td><span className={`badge ${client.account_status}`}>{client.account_status}</span></td>
        <td><button className="text-button small" onClick={() => onToggle(client, client.account_status === "active" ? "paused" : "active")}>{client.account_status === "active" ? "Pause" : "Reopen"}</button></td>
      </tr>)}</tbody>
    </table></div>
  );
}

function LoanTable({ rows, canRequest = false, renewalRequests = [], onRequest }) {
  const headers = ["Loan number", "Client", "Principal", "Interest", "Total due", "Paid", "Interest earned", "Principal balance", "Total balance", "Due date", "Status"];
  if (canRequest) headers.push("Renewal");
  return <DataTable headers={headers} rows={rows.map((loan) => {
    const request = renewalRequests.find((item) => item.source_loan_id === loan.id);
    const renewable = loan.status === "settled" && Number(loan.balance) === 0;
    const row = [
    loan.loan_number,
    <><strong>{loan.client_name}</strong><small>ID {loan.national_id}</small></>,
    money(loan.principal), money(loan.interest), money(loan.total_due),
    money(loan.paid), money(loan.interest_earned), money(loan.principal_balance),
    money(loan.balance), loan.due_on,
    <span className={`badge ${loan.status}`}>{loan.status}</span>,
    ];
    if (canRequest) {
      row.push(request && request.status !== "denied"
        ? <span className={`badge ${request.status}`}>{request.status}</span>
        : renewable
          ? <button className="text-button small" onClick={() => onRequest(loan)}>Request renewal</button>
          : <span className="muted">—</span>);
    }
    return row;
  })} empty="No loans have been issued." />;
}

function DataTable({ headers, rows, empty }) {
  if (!rows.length) return <Empty text={empty || "Nothing to show yet."} />;
  return <div className="table-wrap"><table><thead><tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr></thead><tbody>
    {rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}
  </tbody></table></div>;
}

function Empty({ text }) {
  return <div className="empty"><span>◌</span><p>{text}</p></div>;
}

function DailyPaymentGroups({ rows, empty }) {
  if (!rows.length) return <Empty text={empty} />;
  const groups = rows.reduce((result, payment) => {
    result[payment.location] = result[payment.location] || [];
    result[payment.location].push(payment);
    return result;
  }, {});
  return Object.entries(groups).map(([location, paymentsInLocation]) => (
    <section className="daily-report-section" key={location}>
      <div className="panel-heading">
        <div>
          <h2>{location}</h2>
          <p>{paymentsInLocation.length} payment{paymentsInLocation.length === 1 ? "" : "s"} · {money(paymentsInLocation.reduce((sum, payment) => sum + Number(payment.amount), 0))} received</p>
        </div>
      </div>
      <DataTable
        headers={["Client", "Amount paid", "Balance"]}
        rows={paymentsInLocation.map((payment) => [
          payment.client_name,
          money(payment.amount),
          payment.balance == null ? "—" : money(payment.balance),
        ])}
      />
    </section>
  ));
}

function LedgerBrowser({
  ledgers,
  role,
  editAccessStatus,
  canEdit,
  selected,
  data,
  loading,
  onSelect,
  onRequestAccess,
  onSave,
}) {
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState({});
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setEditing(false);
    setEdits({});
  }, [selected?.id, data?.revision]);

  const visibleLedgers = ledgers.filter((ledger) =>
    ledger.name.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const locationGroups = [...visibleLedgers.reduce((groups, ledger) => {
    const location = ledger.location || "Main folder";
    const locationLedgers = groups.get(location) || [];
    locationLedgers.push(ledger);
    groups.set(location, locationLedgers);
    return groups;
  }, new Map())].sort(([locationA], [locationB]) =>
    locationA.localeCompare(locationB, undefined, { sensitivity: "base" }),
  );
  const supportsEditing = Boolean(
    typeof data?.revision === "string"
    && Array.isArray(data.sheets)
    && data.sheets.every((sheet) =>
      Array.isArray(sheet.headers)
      && Array.isArray(sheet.rows)
      && Array.isArray(sheet.formula_cells)
      && (sheet.headers.length === 0
        || (Array.isArray(sheet.row_numbers) && sheet.row_numbers.length === sheet.rows.length)),
    ),
  );

  async function saveEdits() {
    const pendingEdits = Object.values(edits);
    if (!pendingEdits.length || !data) {
      setEditing(false);
      setEdits({});
      return;
    }
    setSaving(true);
    const saved = await onSave(pendingEdits, data.revision);
    if (saved) {
      setEditing(false);
      setEdits({});
    }
    setSaving(false);
  }

  return (
    <div className="ledger-browser">
      <section className="panel ledger-list">
        <div className="panel-heading"><div><h2>{role === "admin" ? "Company ledgers" : "Your location ledgers"}</h2><p>{ledgers.length} client workbooks across {new Set(ledgers.map((ledger) => ledger.location || "Main folder")).size} locations</p></div></div>
        {role === "staff" && (
          <div className="ledger-access-control">
            {editAccessStatus === "granted" ? (
              <span className="badge active">Ledger editing approved</span>
            ) : editAccessStatus === "pending" ? (
              <span className="badge pending">Edit access request pending</span>
            ) : (
              <button className="secondary" onClick={onRequestAccess}>Request ledger edit access</button>
            )}
          </div>
        )}
        <label className="ledger-search">
          Search clients by name
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Enter a client name…" />
        </label>
        <div className="ledger-list-items">
          {locationGroups.map(([location, locationLedgers]) => (
            <section className="ledger-location-group" key={location}>
              <div className="ledger-location-heading">
                <strong>{location}</strong>
                <span>{locationLedgers.length} {locationLedgers.length === 1 ? "client" : "clients"}</span>
              </div>
              {locationLedgers.map((ledger) => (
                <button
                  className={`ledger-list-item ${selected?.id === ledger.id ? "selected" : ""}`}
                  key={ledger.id}
                  onClick={() => onSelect(ledger)}
                >
                  <span className="ledger-avatar">{ledger.name.trim().charAt(0).toUpperCase() || "L"}</span>
                  <span><strong>{ledger.name}</strong><small>View client ledger</small></span>
                  <span className="ledger-arrow">›</span>
                </button>
              ))}
            </section>
          ))}
          {!visibleLedgers.length && <Empty text={ledgers.length ? "No clients match that search." : "No client workbooks found."} />}
        </div>
      </section>
      <section className="panel ledger-detail">
        {!selected && <Empty text="Select a client to view their ledger here." />}
        {loading && <div className="loading-inline">Opening {selected?.name}’s ledger…</div>}
        {!loading && data && (
          <>
            <div className="panel-heading ledger-detail-heading">
              <div><p className="eyebrow">CLIENT LEDGER · {data.location || "MAIN FOLDER"}</p><h2>{data.name}</h2></div>
              {canEdit && supportsEditing ? (
                <div className="ledger-edit-actions">
                  {editing ? (
                    <>
                      <button className="secondary" onClick={() => { setEditing(false); setEdits({}); }} disabled={saving}>Cancel</button>
                      <button className="primary" onClick={saveEdits} disabled={saving}>{saving ? "Saving…" : "Save changes"}</button>
                    </>
                  ) : (
                    <button
                      className="secondary"
                      type="button"
                      onClick={() => {
                        setEdits({});
                        setEditing(true);
                      }}
                    >
                      Edit ledger
                    </button>
                  )}
                </div>
              ) : (
                <span className="ledger-view-badge">{canEdit ? "Editing unavailable" : "Read only"}</span>
              )}
            </div>
            {canEdit && !supportsEditing && (
              <div className="alert error" role="alert">
                This ledger response is missing edit metadata. Reload the page; if this persists, restart the backend and try again.
              </div>
            )}
            {data.sheets.map((sheet) => (
              <section className="ledger-sheet" key={sheet.name}>
                {sheet.metadata.length > 0 && (
                  <div className="ledger-metadata">
                    {sheet.metadata.map((item, index) => (
                      <div key={`${item.label}-${index}`}><small>{item.label}</small><strong>{item.value}</strong></div>
                    ))}
                  </div>
                )}
                {sheet.headers.length > 0 && (
                  <div className="table-wrap">
                    <table className="ledger-table">
                      <thead><tr>{sheet.headers.map((header, index) => <th key={`${header}-${index}`}>{header}</th>)}</tr></thead>
                      <tbody>
                        {sheet.rows.map((row, rowIndex) => {
                          const loanHeading = String(row[1] || "").trim().toUpperCase() === "LOANED";
                          return (
                            <tr className={loanHeading ? "ledger-loan-heading" : ""} key={rowIndex}>
                              {row.map((value, cellIndex) => (
                                <td key={cellIndex}>
                                  {editing && !sheet.formula_cells.some(([formulaRow, formulaColumn]) =>
                                    formulaRow === sheet.row_numbers[rowIndex] && formulaColumn === cellIndex + 1,
                                  ) ? (
                                    <input
                                      className="ledger-cell-input"
                                      aria-label={`${sheet.name}, row ${sheet.row_numbers[rowIndex]}, ${sheet.headers[cellIndex]}`}
                                      value={edits[`${sheet.name}:${sheet.row_numbers[rowIndex]}:${cellIndex + 1}`]?.value ?? (value == null ? "" : String(value))}
                                      onChange={(event) => {
                                        const key = `${sheet.name}:${sheet.row_numbers[rowIndex]}:${cellIndex + 1}`;
                                        setEdits((current) => ({
                                          ...current,
                                          [key]: {
                                            sheet: sheet.name,
                                            row: sheet.row_numbers[rowIndex],
                                            column: cellIndex + 1,
                                            value: event.target.value,
                                          },
                                        }));
                                      }}
                                    />
                                  ) : (
                                    value == null || value === "" ? "—" : typeof value === "number" ? value.toLocaleString("en-KE", { maximumFractionDigits: 2 }) : value
                                  )}
                                </td>
                              ))}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            ))}
            <section className="ledger-sheet app-ledger-activity">
              <div className="panel-heading">
                <div><h2>Nama Traders loan activity</h2><p>App-issued loans matched to this client by National ID. Original workbook remains unchanged.</p></div>
                <span className="badge orange">APP RECORDS</span>
              </div>
              <DataTable
                headers={["Loan number", "Amount issued", "Interest", "Total due", "Paid", "Balance", "Date issued", "Due date", "Status"]}
                rows={(data.app_loans || []).map((loan) => [
                  loan.loan_number, money(loan.principal), money(loan.interest),
                  money(loan.total_due), money(loan.paid), money(loan.balance),
                  loan.issued_on, loan.due_on,
                  <span className={`badge ${loan.status}`}>{loan.status}</span>,
                ])}
                empty="No loans have been recorded in Nama Traders for this client."
              />
            </section>
            <section className="ledger-sheet app-ledger-activity">
              <div className="panel-heading">
                <div><h2>Nama Traders payment activity</h2><p>App-recorded payments matched to this client by National ID. Original workbook remains unchanged.</p></div>
                <span className="badge orange">APP RECORDS</span>
              </div>
              <DataTable
                headers={["Transaction", "Amount Paid", "Principal Repaid", "Interest Earned", "Principal Balance", "Total Balance", "Day", "Date", "Month", "Year", "Time"]}
                rows={(data.app_payments || []).map((payment) => [
                  payment.transaction_id, money(payment.amount), money(payment.principal_paid),
                  money(payment.interest_earned), money(payment.principal_balance), money(payment.balance),
                  payment.payment_day, payment.payment_date, payment.payment_month,
                  payment.payment_year, payment.payment_time,
                ])}
                empty="No payments have been recorded in Nama Traders for this client."
              />
            </section>
            {!data.sheets.length && <Empty text="This workbook has no readable ledger table." />}
          </>
        )}
      </section>
    </div>
  );
}

export default App;
