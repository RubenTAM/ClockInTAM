(() => {
  const menuButton = document.querySelector("[data-menu-toggle]");
  const sidebar = document.querySelector("#sidebar");
  if (menuButton && sidebar) {
    menuButton.addEventListener("click", () => {
      sidebar.classList.toggle("open");
    });
  }

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const accessRole = document.querySelector("[data-user-access-role]");
  const supervisedAreaField = document.querySelector(
    "[data-supervised-area-field]"
  );
  if (accessRole && supervisedAreaField) {
    const supervisedAreaSelect = supervisedAreaField.querySelector("select");
    const updateSupervisedAreaVisibility = () => {
      const workerSelected = accessRole.value === "worker";
      supervisedAreaField.hidden = workerSelected;
      if (supervisedAreaSelect) supervisedAreaSelect.disabled = workerSelected;
    };
    accessRole.addEventListener("change", updateSupervisedAreaVisibility);
    updateSupervisedAreaVisibility();
  }

  const payrollDate = document.querySelector("[data-payroll-period-date]");
  const payrollLabel = document.querySelector("[data-payroll-period-label]");
  if (payrollDate && payrollLabel) {
    const weekdays = ["Domingo", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado"];
    const months = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
    const formatDate = (value) =>
      `${weekdays[value.getDay()]} ${value.getDate()} de ${months[value.getMonth()]}`;
    const updatePayrollPeriod = () => {
      if (!payrollDate.value) {
        payrollLabel.textContent = "Selecciona una fecha";
        return;
      }
      const selected = new Date(`${payrollDate.value}T12:00:00`);
      const daysSinceThursday = (selected.getDay() - 4 + 7) % 7;
      const start = new Date(selected);
      start.setDate(selected.getDate() - daysSinceThursday);
      const end = new Date(start);
      end.setDate(start.getDate() + 6);
      payrollLabel.textContent = `${formatDate(start)} — ${formatDate(end)}`;
    };
    payrollDate.addEventListener("change", updatePayrollPeriod);
    updatePayrollPeriod();
  }

  const payrollForm = document.querySelector("[data-payroll-period-form]");
  const payrollStatus = document.querySelector("[data-payroll-generation-status]");
  if (payrollForm && payrollStatus) {
    const submitButton = payrollForm.querySelector('button[type="submit"]');
    const readyPeriod = payrollStatus.querySelector("[data-payroll-ready-period]");
    const errorMessage = payrollStatus.querySelector("[data-payroll-error-message]");
    const downloadLink = payrollStatus.querySelector("[data-payroll-download-link]");
    const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
    const readError = async (response) => {
      try {
        const payload = await response.json();
        return payload.error || "No fue posible generar el recibo.";
      } catch (_error) {
        return "No fue posible generar el recibo.";
      }
    };
    const showError = (message) => {
      payrollStatus.hidden = false;
      payrollStatus.dataset.state = "error";
      errorMessage.textContent = message;
      submitButton.disabled = false;
      submitButton.textContent = "Intentar nuevamente";
    };

    payrollForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      submitButton.disabled = true;
      submitButton.textContent = "Generando…";
      payrollStatus.hidden = false;
      payrollStatus.dataset.state = "loading";
      downloadLink.removeAttribute("href");

      try {
        const created = await fetch(payrollForm.dataset.createUrl, {
          method: "POST",
          body: new FormData(payrollForm),
          headers: { Accept: "application/json" },
        });
        if (!created.ok) throw new Error(await readError(created));
        const job = await created.json();
        const statusUrl = `/api/recibos-nomina/solicitudes/${encodeURIComponent(job.jobId)}`;

        while (true) {
          await delay(1400);
          const response = await fetch(statusUrl, {
            headers: { Accept: "application/json" },
            cache: "no-store",
          });
          if (!response.ok) throw new Error(await readError(response));
          const result = await response.json();
          if (result.status === "failed") {
            throw new Error(result.error || "CONTPAQi no pudo generar el recibo.");
          }
          if (result.status === "ready") {
            readyPeriod.textContent = payrollLabel?.textContent || "Periodo seleccionado";
            downloadLink.href = `/recibos-nomina/solicitudes/${encodeURIComponent(job.jobId)}/descargar`;
            payrollStatus.dataset.state = "ready";
            submitButton.disabled = false;
            submitButton.textContent = "Generar otro recibo";
            break;
          }
        }
      } catch (error) {
        showError(error.message || "No fue posible generar el recibo.");
      }
    });
  }

  const workerList = document.querySelector("#worker-list");
  if (!workerList) return;

  const loadButton = document.querySelector("#load-workers");
  const referenceDate = document.querySelector("#reference-date");
  const workerState = document.querySelector("#worker-state");
  const search = document.querySelector("#worker-search");
  const selectAll = document.querySelector("#select-all");
  const weekStart = document.querySelector("#week-start");
  const dayGrid = document.querySelector("#day-grid");
  let workers = [];

  const escapeHtml = (value) => {
    const element = document.createElement("div");
    element.textContent = value;
    return element.innerHTML;
  };
  const escapeAttribute = (value) =>
    escapeHtml(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");

  const renderWorkers = () => {
    const query = search.value.trim().toLocaleLowerCase("es");
    const filtered = workers.filter((worker) =>
      `${worker.name} ${worker.area}`.toLocaleLowerCase("es").includes(query)
    );
    workerList.innerHTML = filtered.map((worker) => `
      <label class="worker-option">
        <input type="checkbox" name="employee_names"
               value="${escapeAttribute(worker.name)}">
        <span class="worker-avatar">${escapeHtml(worker.name.charAt(0).toUpperCase())}</span>
        <span>
          <strong>${escapeHtml(worker.name)}</strong>
          <small>${escapeHtml(worker.area || "Sin área")}</small>
        </span>
      </label>
    `).join("");
    workerState.hidden = filtered.length > 0;
    if (!filtered.length) {
      workerState.innerHTML = "<span>No hay trabajadores que coincidan.</span>";
    }
  };

  loadButton.addEventListener("click", async () => {
    loadButton.disabled = true;
    workerState.hidden = false;
    workerState.innerHTML = "<span class=\"spinner\"></span> Consultando Hik-Connect…";
    workerList.innerHTML = "";
    try {
      const response = await fetch(
        `/api/trabajadores?fecha=${encodeURIComponent(referenceDate.value)}&actualizar=1`,
        { headers: { Accept: "application/json" } }
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "No fue posible consultar.");
      workers = payload.workers;
      search.disabled = false;
      selectAll.disabled = false;
      renderWorkers();
      if (!workers.length) {
        workerState.hidden = false;
        workerState.innerHTML = "<span>No se encontraron trabajadores en esa fecha.</span>";
      }
    } catch (error) {
      workerState.hidden = false;
      workerState.innerHTML = `<span class="danger-text">${escapeHtml(error.message)}</span>`;
    } finally {
      loadButton.disabled = false;
    }
  });

  search.addEventListener("input", renderWorkers);
  selectAll.addEventListener("click", () => {
    workerList.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      input.checked = true;
    });
  });

  const toIsoLocal = (day) => {
    const year = day.getFullYear();
    const month = String(day.getMonth() + 1).padStart(2, "0");
    const date = String(day.getDate()).padStart(2, "0");
    return `${year}-${month}-${date}`;
  };

  const dayNames = [
    "domingo", "lunes", "martes", "miércoles",
    "jueves", "viernes", "sábado"
  ];
  const monthNames = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic"
  ];

  const renderDays = () => {
    if (!weekStart.value) return;
    const [year, month, day] = weekStart.value.split("-").map(Number);
    const selected = new Set(
      [...dayGrid.querySelectorAll("input:checked")].map((input) => input.value)
    );
    const start = new Date(year, month - 1, day);
    const daysSinceThursday = (start.getDay() - 4 + 7) % 7;
    start.setDate(start.getDate() - daysSinceThursday);
    weekStart.value = toIsoLocal(start);
    dayGrid.innerHTML = "";
    for (let index = 0; index < 7; index += 1) {
      const current = new Date(start);
      current.setDate(start.getDate() + index);
      const iso = toIsoLocal(current);
      const label = document.createElement("label");
      label.className = "day-option";
      label.innerHTML = `
        <input type="checkbox" name="work_dates" value="${iso}"
               ${selected.has(iso) ? "checked" : ""}>
        <span>
          <small>${dayNames[current.getDay()]} · ${monthNames[current.getMonth()]}</small>
          <strong>${current.getDate()}</strong>
        </span>
      `;
      dayGrid.appendChild(label);
    }
  };
  weekStart.addEventListener("change", renderDays);
})();

(() => {
  const form = document.querySelector("[data-vacation-request-form]");
  if (!form) return;

  const startInput = form.querySelector("[data-vacation-start]");
  const endInput = form.querySelector("[data-vacation-end]");
  const daysOutput = form.querySelector("[data-vacation-days]");
  const ledger = document.querySelector("[data-vacation-ledger]");
  const previewRow = ledger?.querySelector("[data-vacation-ledger-preview]");
  const previewStart = ledger?.querySelector("[data-vacation-preview-start]");
  const previewEnd = ledger?.querySelector("[data-vacation-preview-end]");
  const previewTaken = ledger?.querySelector("[data-vacation-preview-taken]");
  const previewBalance = ledger?.querySelector("[data-vacation-preview-balance]");
  const projectedBalance = form.querySelector(
    "[data-vacation-projected-balance]"
  );
  const projectedCopy = projectedBalance?.closest(".vacation-projected-copy");

  const formatDate = (value) => {
    const [year, month, day] = value.split("-");
    return `${day}/${month}/${year}`;
  };
  const formatDays = (value) => new Intl.NumberFormat("es-MX", {
    maximumFractionDigits: 2,
  }).format(value);

  const updateProjection = (days) => {
    const datesAreValid = startInput.value && endInput.value
      && endInput.value >= startInput.value && days > 0;
    if (!datesAreValid) {
      if (previewRow) previewRow.hidden = true;
      if (projectedBalance) projectedBalance.textContent = "—";
      projectedCopy?.classList.remove("projected-negative");
      return;
    }

    const availableValue = ledger?.dataset.availableDays || "";
    const hasAvailableBalance = availableValue !== "";
    const balance = hasAvailableBalance
      ? Number(availableValue) - days
      : null;
    if (previewStart) previewStart.textContent = formatDate(startInput.value);
    if (previewEnd) previewEnd.textContent = formatDate(endInput.value);
    if (previewTaken) previewTaken.textContent = formatDays(days);
    if (previewBalance) {
      previewBalance.textContent = balance === null ? "—" : formatDays(balance);
    }
    if (previewRow) {
      previewRow.hidden = false;
      previewRow.classList.toggle("projected-negative", balance !== null && balance < 0);
    }
    if (projectedBalance) {
      projectedBalance.textContent = balance === null
        ? "Pendiente de sincronizar"
        : `${formatDays(balance)} días`;
    }
    projectedCopy?.classList.toggle(
      "projected-negative", balance !== null && balance < 0
    );
  };

  const calculateDays = () => {
    if (!startInput.value || !endInput.value) {
      daysOutput.textContent = "—";
      updateProjection(0);
      return;
    }
    endInput.min = startInput.value;
    const start = new Date(`${startInput.value}T12:00:00`);
    const end = new Date(`${endInput.value}T12:00:00`);
    if (end < start) {
      daysOutput.textContent = "—";
      updateProjection(0);
      return;
    }
    let days = 0;
    const current = new Date(start);
    while (current <= end) {
      if (current.getDay() !== 0) days += 1;
      current.setDate(current.getDate() + 1);
    }
    daysOutput.textContent = String(days);
    updateProjection(days);
  };

  startInput.addEventListener("change", () => {
    if (!endInput.value || endInput.value < startInput.value) {
      endInput.value = startInput.value;
    }
    calculateDays();
  });
  endInput.addEventListener("change", calculateDays);
  calculateDays();
})();

(() => {
  const page = document.querySelector("[data-vacations-page]");
  if (!page) return;

  const search = page.querySelector("[data-vacation-search]");
  const workers = [...page.querySelectorAll("[data-vacation-worker]")];
  const empty = page.querySelector("[data-vacation-empty]");
  const dialog = document.querySelector("#vacation-dialog");
  const employeeKey = dialog.querySelector("#vacation-employee-key");
  const employeeName = dialog.querySelector("#vacation-employee-name");
  const start = dialog.querySelector("#vacation-start");
  const end = dialog.querySelector("#vacation-end");

  search.addEventListener("input", () => {
    const query = search.value.trim().toLocaleLowerCase("es");
    let visible = 0;
    workers.forEach((worker) => {
      const matches = worker.dataset.workerName.includes(query);
      worker.hidden = !matches;
      if (matches) visible += 1;
    });
    empty.hidden = visible !== 0;
  });

  const historySearch = page.querySelector("[data-vacation-history-search]");
  const historyRows = [...page.querySelectorAll("[data-vacation-history-row]")];
  const historyEmpty = page.querySelector("[data-vacation-history-empty]");
  const historyPrompt = page.querySelector("[data-vacation-history-prompt]");
  if (historySearch) {
    historySearch.addEventListener("input", () => {
      const normalizeName = (value) => value
        .trim()
        .toLocaleLowerCase("es")
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "");
      const query = normalizeName(historySearch.value);
      const hasQuery = query.length > 0;
      let visible = 0;
      historyRows.forEach((row) => {
        const matches = hasQuery
          && normalizeName(row.dataset.historyName).includes(query);
        row.hidden = !matches;
        if (matches) visible += 1;
      });
      if (historyPrompt) historyPrompt.hidden = hasQuery;
      if (historyEmpty) historyEmpty.hidden = !hasQuery || visible !== 0;
    });
  }

  workers.forEach((worker) => {
    worker.addEventListener("click", () => {
      employeeKey.value = worker.dataset.employeeKey;
      employeeName.textContent = worker.dataset.employeeName;
      start.value = "";
      end.value = "";
      end.min = "";
      dialog.showModal();
    });
  });

  start.addEventListener("change", () => {
    end.min = start.value;
    if (!end.value || end.value < start.value) end.value = start.value;
  });

  dialog.querySelectorAll("[data-vacation-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  // Yearly calendar: clicking a circled day shows who was, is, or will be
  // on vacation that date.
  const dayButtons = [...page.querySelectorAll("[data-vacation-day]")];
  const dayDialog = document.querySelector("#vacation-day-dialog");
  if (dayDialog && dayButtons.length) {
    const MONTHS_ES = [
      "enero", "febrero", "marzo", "abril", "mayo", "junio",
      "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ];
    const WEEKDAYS_ES = [
      "domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado",
    ];

    const parseIsoDate = (value) => {
      const [year, month, day] = value.split("-").map(Number);
      return new Date(year, month - 1, day);
    };
    const formatLongDate = (value) => {
      const parsed = parseIsoDate(value);
      return `${WEEKDAYS_ES[parsed.getDay()]} ${parsed.getDate()} de `
        + `${MONTHS_ES[parsed.getMonth()]} de ${parsed.getFullYear()}`;
    };
    const formatRange = (startValue, endValue) => {
      const rangeStart = parseIsoDate(startValue);
      const rangeEnd = parseIsoDate(endValue);
      if (startValue === endValue) {
        return `${rangeStart.getDate()} de ${MONTHS_ES[rangeStart.getMonth()]} `
          + `de ${rangeStart.getFullYear()}`;
      }
      if (
        rangeStart.getFullYear() === rangeEnd.getFullYear()
        && rangeStart.getMonth() === rangeEnd.getMonth()
      ) {
        return `Del ${rangeStart.getDate()} al ${rangeEnd.getDate()} de `
          + `${MONTHS_ES[rangeStart.getMonth()]} de ${rangeStart.getFullYear()}`;
      }
      return `Del ${rangeStart.getDate()} de ${MONTHS_ES[rangeStart.getMonth()]} `
        + `al ${rangeEnd.getDate()} de ${MONTHS_ES[rangeEnd.getMonth()]} `
        + `de ${rangeEnd.getFullYear()}`;
    };

    const dayTitle = dayDialog.querySelector("[data-vacation-day-title]");
    const dayStatus = dayDialog.querySelector("[data-vacation-day-status]");
    const dayList = dayDialog.querySelector("[data-vacation-day-list]");
    const csrfToken = page.dataset.csrf || "";
    const todayValue = page.dataset.today || "";
    const yearValue = page.dataset.year || "";
    const deleteUrlTemplate = page.dataset.deleteUrlTemplate || "";

    dayButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const dateValue = button.dataset.date;
        let vacations = [];
        try {
          vacations = JSON.parse(button.dataset.vacations || "[]");
        } catch (err) {
          vacations = [];
        }

        dayTitle.textContent = formatLongDate(dateValue);
        if (dateValue < todayValue) {
          dayStatus.textContent = "Vacaciones pasadas";
        } else if (dateValue > todayValue) {
          dayStatus.textContent = "Vacaciones próximas";
        } else {
          dayStatus.textContent = "Vacaciones en curso";
        }

        dayList.innerHTML = "";
        vacations.forEach((vacation) => {
          const item = document.createElement("li");
          item.className = "vacation-day-list-item";

          const avatar = document.createElement("span");
          avatar.className = "worker-avatar";
          avatar.textContent = (vacation.employee_name || "?")
            .trim().charAt(0).toUpperCase();

          const copy = document.createElement("span");
          copy.className = "vacation-day-list-copy";
          const strong = document.createElement("strong");
          strong.textContent = vacation.employee_name;
          const small = document.createElement("small");
          small.textContent = formatRange(vacation.start_date, vacation.end_date);
          copy.append(strong, small);

          item.append(avatar, copy);

          if (csrfToken && deleteUrlTemplate) {
            const form = document.createElement("form");
            form.method = "post";
            form.action = deleteUrlTemplate.replace(
              "/0/eliminar", `/${vacation.id}/eliminar`
            );
            form.className = "vacation-day-list-delete";
            form.addEventListener("submit", (event) => {
              const confirmed = window.confirm(
                `¿Eliminar las vacaciones de ${vacation.employee_name}?`
              );
              if (!confirmed) event.preventDefault();
            });

            const csrfInput = document.createElement("input");
            csrfInput.type = "hidden";
            csrfInput.name = "csrf_token";
            csrfInput.value = csrfToken;

            const yearInput = document.createElement("input");
            yearInput.type = "hidden";
            yearInput.name = "year";
            yearInput.value = yearValue;

            const deleteButton = document.createElement("button");
            deleteButton.type = "submit";
            deleteButton.setAttribute(
              "aria-label", `Eliminar vacaciones de ${vacation.employee_name}`
            );
            deleteButton.textContent = "×";

            form.append(csrfInput, yearInput, deleteButton);
            item.append(form);
          }

          dayList.append(item);
        });

        if (!vacations.length) {
          const emptyState = document.createElement("li");
          emptyState.className = "vacation-day-empty";
          emptyState.textContent = "No hay vacaciones registradas ese día.";
          dayList.append(emptyState);
        }

        dayDialog.showModal();
      });
    });

    dayDialog.querySelectorAll("[data-vacation-day-close]").forEach((button) => {
      button.addEventListener("click", () => dayDialog.close());
    });
    dayDialog.addEventListener("click", (event) => {
      if (event.target === dayDialog) dayDialog.close();
    });
  }

  const todayMonth = page.querySelector("[data-today-month]");
  if (todayMonth) {
    todayMonth.scrollIntoView({ behavior: "auto", block: "start" });
  }
})();

(() => {
  const dialog = document.querySelector("#report-detail-dialog");
  if (!dialog) return;

  const employeeName = dialog.querySelector("#report-detail-name");
  const detailBody = dialog.querySelector("#report-detail-body");

  document.querySelectorAll("[data-report-worker]").forEach((button) => {
    button.addEventListener("click", () => {
      const template = document.querySelector(
        `#${CSS.escape(button.dataset.template)}`
      );
      if (!template) return;
      employeeName.textContent = button.dataset.employee;
      detailBody.replaceChildren(template.content.cloneNode(true));
      dialog.showModal();
    });
  });

  dialog.querySelectorAll("[data-report-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
})();

(() => {
  const normalizeSearch = (value) => value
    .trim()
    .toLocaleLowerCase("es")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");

  const browser = document.querySelector("[data-employees-browser]");
  if (!browser) return;

  const search = browser.querySelector("[data-employees-search]");
  const areaFilter = browser.querySelector("[data-employees-area-filter]");
  const empty = browser.querySelector("[data-employees-empty]");
  const rows = [...browser.querySelectorAll("[data-employee-row]")];
  if (!search || !areaFilter) return;

  const filterRows = () => {
    const query = normalizeSearch(search.value);
    const area = normalizeSearch(areaFilter.value);
    let visible = 0;
    rows.forEach((row) => {
      const matchesSearch = normalizeSearch(row.dataset.employeeName).includes(query)
        || normalizeSearch(row.dataset.employeeCode || "").includes(query);
      const matches = matchesSearch
        && (!area || normalizeSearch(row.dataset.employeeArea) === area);
      row.hidden = !matches;
      row.classList.toggle("is-filtered-out", !matches);
      if (matches) visible += 1;
    });
    if (empty) empty.hidden = visible > 0;
  };

  search.addEventListener("input", filterRows);
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    filterRows();
  });
  areaFilter.addEventListener("change", filterRows);
})();

(() => {
  const normalizeSearch = (value) => value
    .trim()
    .toLocaleLowerCase("es")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");

  const browser = document.querySelector("[data-home-browser]");
  if (!browser) return;

  const search = browser.querySelector("[data-home-search]");
  const areaFilter = browser.querySelector("[data-home-area-filter]");
  const profile = browser.querySelector("[data-home-profile]");
  const empty = browser.querySelector("[data-home-empty]");
  const preview = browser.querySelector("[data-home-preview]");
  const workers = [...browser.querySelectorAll("[data-home-worker]")];
  const choices = preview ? [preview, ...workers] : workers;

  const setupSupervisorPreview = () => {
    const supervisor = profile.querySelector("[data-supervisor-home]");
    if (!supervisor) return;
    const previewFilter = supervisor.querySelector(
      "[data-supervisor-preview-filter]"
    );
    const previewRows = [...supervisor.querySelectorAll(
      "[data-supervisor-worker-row]"
    )];
    const previewEmpty = supervisor.querySelector(
      "[data-supervisor-preview-empty]"
    );
    if (!previewFilter) return;

    const filterPreviewRows = () => {
      const selectedArea = normalizeSearch(areaFilter.value);
      const showAll = previewFilter.value === "all";
      let visible = 0;
      previewRows.forEach((row) => {
        const matchesArea = !selectedArea
          || normalizeSearch(row.dataset.previewWorkerArea) === selectedArea;
        const matchesIncidents = showAll
          || row.dataset.previewHasIncidents === "1";
        const matches = matchesArea && matchesIncidents;
        row.hidden = !matches;
        if (matches) visible += 1;
      });
      if (previewEmpty) previewEmpty.hidden = visible > 0;
    };

    previewFilter.addEventListener("change", filterPreviewRows);
    filterPreviewRows();
  };

  const showWorker = (button) => {
    const template = document.getElementById(button.dataset.template);
    if (!template) return;
    choices.forEach((choice) => choice.classList.toggle("active", choice === button));
    profile.replaceChildren(template.content.cloneNode(true));
    setupSupervisorPreview();
  };

  choices.forEach((button) => {
    button.addEventListener("click", () => showWorker(button));
  });

  const filterWorkers = () => {
    const query = normalizeSearch(search.value);
    const area = normalizeSearch(areaFilter.value);
    const matchingWorkers = [];
    if (preview) preview.hidden = Boolean(query);
    workers.forEach((button) => {
      const matchesSearch = normalizeSearch(button.dataset.workerName).includes(query)
        || normalizeSearch(button.dataset.workerCode || "").includes(query);
      const matches = matchesSearch
        && (!area || normalizeSearch(button.dataset.workerArea) === area);
      button.hidden = !matches;
      button.classList.toggle("is-filtered-out", !matches);
      if (matches) matchingWorkers.push(button);
    });
    empty.hidden = matchingWorkers.length > 0;
    if (preview && !preview.hidden) {
      showWorker(preview);
    } else if (matchingWorkers.length) {
      showWorker(matchingWorkers[0]);
    } else {
      choices.forEach((choice) => choice.classList.remove("active"));
      profile.replaceChildren();
    }
  };

  search.addEventListener("input", filterWorkers);
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    filterWorkers();
  });
  areaFilter.addEventListener("change", filterWorkers);

  const selectedWorker = browser.dataset.selectedWorker;
  const initialWorker = workers.find(
    (worker) => worker.dataset.workerKey === selectedWorker
  );
  if (initialWorker) {
    showWorker(initialWorker);
  } else if (preview) {
    showWorker(preview);
  } else if (workers.length) {
    showWorker(workers[0]);
  }
})();

(() => {
  const dialog = document.querySelector("#home-enable-overtime-dialog");
  if (!dialog) return;

  const employeeKey = dialog.querySelector("#home-overtime-employee-key");
  const workDate = dialog.querySelector("#home-overtime-work-date");
  const employeeName = dialog.querySelector("#home-overtime-employee");
  const dateLabel = dialog.querySelector("#home-overtime-date");
  const startInput = dialog.querySelector("#home-overtime-start");
  const endInput = dialog.querySelector("#home-overtime-end");
  const hoursInput = dialog.querySelector("#home-overtime-hours");
  const establishedSchedule = dialog.querySelector(
    "#home-established-schedule"
  );
  const establishedDay = dialog.querySelector(
    "#home-established-schedule-day"
  );
  const establishedHours = dialog.querySelector(
    "#home-established-schedule-hours"
  );
  const weekdayNames = [
    "Domingo", "Lunes", "Martes", "Miércoles",
    "Jueves", "Viernes", "Sábado",
  ];

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-enable-overtime]");
    if (!button) return;
    employeeKey.value = button.dataset.employeeKey;
    workDate.value = button.dataset.workDate;
    employeeName.textContent = button.dataset.employee;
    dateLabel.textContent = button.dataset.dateLabel;
    const selectedDate = new Date(`${button.dataset.workDate}T12:00:00`);
    const weekday = selectedDate.getDay();
    establishedSchedule.hidden = button.dataset.showEstablishedSchedule === "0";
    establishedDay.textContent = weekdayNames[weekday];
    if (weekday === 0) {
      establishedHours.textContent = "Sin horario establecido";
    } else if (weekday === 6) {
      establishedHours.textContent = "08:30–13:00";
    } else {
      establishedHours.textContent = "08:00–17:00";
    }
    startInput.value = "17:00";
    endInput.value = "19:00";
    hoursInput.value = "2";
    dialog.showModal();
  });

  dialog.querySelectorAll("[data-enable-overtime-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
})();

(() => {
  const dialog = document.querySelector("#home-authorization-dialog");
  if (!dialog) return;

  const employeeName = dialog.querySelector("#home-authorization-name");
  const workDate = dialog.querySelector("#home-authorization-date");
  const allowedRange = dialog.querySelector("#home-authorization-range");
  const approvedTime = dialog.querySelector("#home-authorization-approved");
  const countedTime = dialog.querySelector("#home-authorization-counted");
  const deleteEmployeeKey = dialog.querySelector(
    "#home-delete-authorization-employee-key"
  );
  const deleteWorkDate = dialog.querySelector(
    "#home-delete-authorization-work-date"
  );

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-home-authorization]");
    if (!button) return;
    employeeName.textContent = button.dataset.employee;
    workDate.textContent = button.dataset.date;
    allowedRange.textContent = button.dataset.range;
    approvedTime.textContent = button.dataset.approved;
    countedTime.textContent = button.dataset.counted;
    deleteEmployeeKey.value = button.dataset.employeeKey;
    deleteWorkDate.value = button.dataset.workDate;
    dialog.showModal();
  });

  dialog.querySelectorAll("[data-home-authorization-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
})();
