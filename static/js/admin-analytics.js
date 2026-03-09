(function () {
  const config = window.ATTENDANCE_CONFIG || {};
  if (!config.isAdmin || !config.analyticsEndpoint) {
    return;
  }
  if (typeof Chart === "undefined") {
    return;
  }

  const hoursChartCanvas = document.getElementById("hours-by-employee-chart");
  const inOutChartCanvas = document.getElementById("daily-in-out-chart");
  const employeeDailyChartCanvas = document.getElementById("employee-daily-hours-chart");
  const scoreChartCanvas = document.getElementById("score-by-employee-chart");
  const departmentChartCanvas = document.getElementById("department-hours-chart");
  const employeeSelect = document.getElementById("employee-analytics-select");
  const yearsSelect = document.getElementById("analytics-years-select");
  const analyticsStatus = document.getElementById("analytics-status");
  const kpiAvgHours = document.getElementById("kpi-avg-hours");
  const kpiLate = document.getElementById("kpi-late");
  const kpiOvertime = document.getElementById("kpi-overtime");
  const kpiPending = document.getElementById("kpi-pending");

  if (
    !hoursChartCanvas ||
    !inOutChartCanvas ||
    !employeeDailyChartCanvas ||
    !scoreChartCanvas ||
    !departmentChartCanvas ||
    !employeeSelect ||
    !yearsSelect
  ) {
    return;
  }

  const allowedYears = Math.max(1, Number(config.analyticsYearsAllowed || 1));
  const defaultYears = Math.min(
    allowedYears,
    Math.max(1, Number(config.analyticsYearsDefault || 1))
  );
  yearsSelect.value = String(defaultYears);

  let hoursByEmployeeChart = null;
  let dailyInOutChart = null;
  let employeeDailyHoursChart = null;
  let scoreByEmployeeChart = null;
  let departmentHoursChart = null;
  let analyticsPayload = null;

  function setStatus(message, isError) {
    if (!analyticsStatus) {
      return;
    }
    analyticsStatus.textContent = message;
    analyticsStatus.classList.remove("error");
    if (isError) {
      analyticsStatus.classList.add("error");
    }
  }

  function destroyChart(chartRef) {
    if (chartRef) {
      chartRef.destroy();
    }
  }

  function renderKpis(data) {
    const kpis = data.kpis || {};
    if (kpiAvgHours) {
      const value = Number(kpis.avg_period_hours ?? kpis.avg_today_hours ?? 0);
      kpiAvgHours.textContent = `${value.toFixed(2)}h`;
    }
    if (kpiLate) {
      kpiLate.textContent = String(kpis.late_period_days ?? kpis.late_today ?? 0);
    }
    if (kpiOvertime) {
      const value = Number(kpis.overtime_period ?? kpis.overtime_today ?? 0);
      kpiOvertime.textContent = `${value.toFixed(2)}h`;
    }
    if (kpiPending) {
      kpiPending.textContent = String(kpis.pending_corrections || 0);
    }
  }

  function renderHoursByEmployee(data) {
    destroyChart(hoursByEmployeeChart);
    const labels = data.hours_by_employee.map((item) => item.employee);
    const values = data.hours_by_employee.map((item) => item.hours);

    hoursByEmployeeChart = new Chart(hoursChartCanvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: `Hours (${data.period?.label || "Selected Range"})`,
            data: values,
            backgroundColor: "rgba(30, 64, 175, 0.75)",
            borderColor: "rgba(30, 64, 175, 1)",
            borderWidth: 1,
            borderRadius: 10,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        animation: { duration: 420 },
        scales: {
          y: {
            beginAtZero: true,
            title: { display: true, text: "Hours" },
          },
        },
      },
    });
  }

  function selectedYears() {
    const value = Number(yearsSelect.value || defaultYears);
    if (!Number.isFinite(value)) {
      return defaultYears;
    }
    return Math.min(allowedYears, Math.max(1, Math.floor(value)));
  }

  function renderDailyInOut(data) {
    destroyChart(dailyInOutChart);
    const labels = data.daily_in_out.map((item) => item.date);
    const inValues = data.daily_in_out.map((item) => item.in_count);
    const outValues = data.daily_in_out.map((item) => item.out_count);

    dailyInOutChart = new Chart(inOutChartCanvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "IN Count",
            data: inValues,
            borderColor: "rgba(15, 118, 110, 1)",
            backgroundColor: "rgba(15, 118, 110, 0.16)",
            tension: 0.25,
            fill: true,
          },
          {
            label: "OUT Count",
            data: outValues,
            borderColor: "rgba(220, 38, 38, 1)",
            backgroundColor: "rgba(220, 38, 38, 0.14)",
            tension: 0.25,
            fill: true,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 420 },
        scales: {
          y: {
            beginAtZero: true,
            title: { display: true, text: "Count" },
            ticks: { precision: 0 },
          },
        },
      },
    });
  }

  function renderScoreByEmployee(data) {
    destroyChart(scoreByEmployeeChart);
    const top = data.employee_scores.slice(0, 12);
    const labels = top.map((item) => item.employee);
    const values = top.map((item) => item.score);

    scoreByEmployeeChart = new Chart(scoreChartCanvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Performance Score",
            data: values,
            backgroundColor: "rgba(109, 40, 217, 0.72)",
            borderColor: "rgba(109, 40, 217, 1)",
            borderWidth: 1,
            borderRadius: 10,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        animation: { duration: 420 },
        scales: {
          y: {
            beginAtZero: true,
            max: 100,
            title: { display: true, text: "Score" },
          },
        },
      },
    });
  }

  function renderDepartmentHours(data) {
    destroyChart(departmentHoursChart);
    const labels = data.department_hours.map((item) => item.department);
    const values = data.department_hours.map((item) => item.hours);

    departmentHoursChart = new Chart(departmentChartCanvas, {
      type: "doughnut",
      data: {
        labels,
        datasets: [
          {
            label: "Hours",
            data: values,
            backgroundColor: [
              "#0f766e",
              "#1d4ed8",
              "#c2410c",
              "#7c3aed",
              "#be123c",
              "#0e7490",
              "#ca8a04",
            ],
            borderWidth: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 420 },
      },
    });
  }

  function renderEmployeeDailyHours(data, employeeId) {
    const labels = data.labels;
    const values = data.employee_daily_hours[String(employeeId)] || labels.map(() => 0);
    const employee = data.employees.find((item) => String(item.id) === String(employeeId));
    const displayName = employee ? employee.name : "Employee";

    destroyChart(employeeDailyHoursChart);
    employeeDailyHoursChart = new Chart(employeeDailyChartCanvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: `${displayName} Hours`,
            data: values,
            borderColor: "rgba(37, 99, 235, 1)",
            backgroundColor: "rgba(37, 99, 235, 0.18)",
            tension: 0.28,
            fill: true,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 420 },
        scales: {
          y: {
            beginAtZero: true,
            title: { display: true, text: "Hours" },
          },
        },
      },
    });
  }

  function fillEmployeeSelect(data) {
    employeeSelect.innerHTML = "";
    data.employees.forEach((employee, index) => {
      const option = document.createElement("option");
      option.value = String(employee.id);
      option.textContent = employee.name;
      if (index === 0) {
        option.selected = true;
      }
      employeeSelect.appendChild(option);
    });
  }

  async function loadAnalytics() {
    setStatus("Loading analytics...", false);
    try {
      const years = selectedYears();
      const url = `${config.analyticsEndpoint}?years=${encodeURIComponent(String(years))}`;
      const response = await fetch(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      });
      const data = await response.json();
      if (!response.ok || !data || !data.ok) {
        setStatus("Unable to load analytics data.", true);
        return;
      }

      analyticsPayload = data;
      renderKpis(data);
      renderHoursByEmployee(data);
      renderDailyInOut(data);
      renderScoreByEmployee(data);
      renderDepartmentHours(data);

      fillEmployeeSelect(data);
      if (data.employees.length > 0) {
        renderEmployeeDailyHours(data, data.employees[0].id);
      } else {
        setStatus("No employee trend data available yet.", false);
        return;
      }
      if (data.requested_years_limited) {
        setStatus(`Loaded ${data.period?.label || "analytics"} (limited by main admin permission).`, false);
      } else {
        setStatus(`Loaded ${data.period?.label || "analytics"} successfully.`, false);
      }
    } catch (error) {
      setStatus("Network error while loading analytics.", true);
    }
  }

  employeeSelect.addEventListener("change", () => {
    if (!analyticsPayload) {
      return;
    }
    renderEmployeeDailyHours(analyticsPayload, employeeSelect.value);
  });

  yearsSelect.addEventListener("change", () => {
    loadAnalytics();
  });

  loadAnalytics();
})();
