(function () {
  const form = document.getElementById("attendance-form");
  if (!form) {
    return;
  }

  const endpoint = window.ATTENDANCE_CONFIG?.endpoint || "/attendance";
  const csrfToken =
    document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
  const personName = document.getElementById("person_name");
  const fetchLocationBtn = document.getElementById("fetch-location");
  const latitudeInput = document.getElementById("latitude");
  const longitudeInput = document.getElementById("longitude");
  const locationTextInput = document.getElementById("location_text");
  const actionTypeInput = document.getElementById("action_type");
  const locationStatus = document.getElementById("location-status");
  const submitFeedback = document.getElementById("submit-feedback");
  const recordRows = Array.from(document.querySelectorAll("#records-table tbody .record-row"));
  const searchInput = document.getElementById("record-search");
  const eventFilter = document.getElementById("record-event-filter");
  const sourceFilter = document.getElementById("record-source-filter");
  const markInBtn = document.getElementById("mark-in-btn");
  const markOutBtn = document.getElementById("mark-out-btn");

  const correctionType = document.getElementById("correction_type");
  const proposedEventWrap = document.getElementById("proposed_event_wrap");
  const requestedDatetime = document.getElementById("requested_datetime");
  const recordCache = recordRows.map((row) => ({
    row,
    text: row.textContent.toLowerCase(),
    event: (row.dataset.event || "").toUpperCase(),
    source: (row.dataset.source || "").toUpperCase(),
  }));

  function setFeedback(message, isError) {
    submitFeedback.textContent = message;
    submitFeedback.style.color = isError ? "#b91c1c" : "#0f766e";
  }

  function setLocationStatus(message, tone) {
    if (!locationStatus) {
      return;
    }
    locationStatus.textContent = message;
    locationStatus.classList.remove("status-ok", "status-error");
    if (tone === "ok") {
      locationStatus.classList.add("status-ok");
    } else if (tone === "error") {
      locationStatus.classList.add("status-error");
    }
  }

  function setLocationFetchState(isBusy) {
    if (!fetchLocationBtn) {
      return;
    }
    fetchLocationBtn.disabled = isBusy;
    fetchLocationBtn.textContent = isBusy ? "Fetching..." : "Auto Fetch";
  }

  async function reverseGeocode(lat, lon) {
    try {
      const response = await fetch(
        `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lon}`,
        {
          headers: {
            Accept: "application/json",
          },
        }
      );
      if (!response.ok) {
        throw new Error("reverse geocoding failed");
      }
      const data = await response.json();
      return data.display_name || "";
    } catch (error) {
      return "";
    }
  }

  function fetchLocation() {
    if (!navigator.geolocation) {
      setLocationStatus("Geolocation is not supported by this browser.", "error");
      return;
    }

    setLocationFetchState(true);
    setLocationStatus("Fetching your location...", "");

    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const lat = position.coords.latitude.toFixed(6);
        const lon = position.coords.longitude.toFixed(6);
        latitudeInput.value = lat;
        longitudeInput.value = lon;

        const displayName = await reverseGeocode(lat, lon);
        if (displayName) {
          locationTextInput.value = displayName;
          setLocationStatus(`Location fetched: ${displayName}`, "ok");
        } else {
          locationTextInput.value = `${lat}, ${lon}`;
          setLocationStatus(`Coordinates fetched: ${lat}, ${lon}`, "ok");
        }
        setLocationFetchState(false);
      },
      (error) => {
        if (error.code === 1) {
          setLocationStatus("Location permission denied. Please allow and retry.", "error");
        } else if (error.code === 2) {
          setLocationStatus("Location unavailable. Retry in a moment.", "error");
        } else {
          setLocationStatus("Unable to fetch location.", "error");
        }
        setLocationFetchState(false);
      },
      {
        enableHighAccuracy: true,
        timeout: 12000,
        maximumAge: 0,
      }
    );
  }

  function setActionButtonsState(isBusy, currentAction) {
    if (markInBtn) {
      markInBtn.disabled = isBusy;
      markInBtn.textContent = isBusy && currentAction === "IN" ? "Submitting IN..." : "Mark IN";
    }
    if (markOutBtn) {
      markOutBtn.disabled = isBusy;
      markOutBtn.textContent = isBusy && currentAction === "OUT" ? "Submitting OUT..." : "Mark OUT";
    }
  }

  async function submitAttendance(actionType) {
    setFeedback("", false);

    const nameValue = personName.value.trim();
    const latitude = latitudeInput.value.trim();
    const longitude = longitudeInput.value.trim();
    const locationText = locationTextInput.value.trim();

    if (!nameValue) {
      setFeedback("Please enter person name.", true);
      return;
    }
    if (!latitude || !longitude) {
      setFeedback("Please auto-fetch location before submitting.", true);
      return;
    }

    const payload = {
      person_name: nameValue,
      action_type: actionType,
      latitude,
      longitude,
      location_text: locationText,
    };

    try {
      actionTypeInput.value = actionType;
      setActionButtonsState(true, actionType);

      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        credentials: "same-origin",
        body: JSON.stringify(payload),
      });

      let data = null;
      try {
        data = await response.json();
      } catch (e) {
        data = null;
      }

      if (!response.ok || !data || !data.ok) {
        const message = data && data.error ? data.error : `Attendance submission failed (${response.status}).`;
        setFeedback(message, true);
        return;
      }

      let successMessage = `${actionType} submitted successfully.`;
      if (typeof data.distance_m === "number") {
        successMessage += ` Distance from office: ${data.distance_m}m.`;
      }
      setFeedback(successMessage, false);
      setTimeout(() => {
        window.location.reload();
      }, 900);
    } catch (error) {
      setFeedback("Network error while submitting attendance.", true);
    } finally {
      setActionButtonsState(false, "");
    }
  }

  function applyRecordFilters() {
    if (recordCache.length === 0) {
      return;
    }

    const keyword = (searchInput?.value || "").trim().toLowerCase();
    const eventValue = (eventFilter?.value || "").trim().toUpperCase();
    const sourceValue = (sourceFilter?.value || "").trim().toUpperCase();

    recordCache.forEach((item) => {
      const matchesKeyword = !keyword || item.text.includes(keyword);
      const matchesEvent = !eventValue || item.event === eventValue;
      const matchesSource = !sourceValue || item.source === sourceValue;

      item.row.style.display = matchesKeyword && matchesEvent && matchesSource ? "" : "none";
    });
  }

  function debounce(fn, wait) {
    let timeoutId = null;
    return (...args) => {
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
      timeoutId = setTimeout(() => fn(...args), wait);
    };
  }

  function setupRecordFilters() {
    const applyDebounced = debounce(applyRecordFilters, 90);
    if (searchInput) {
      searchInput.addEventListener("input", applyDebounced);
    }
    if (eventFilter) {
      eventFilter.addEventListener("change", applyRecordFilters);
    }
    if (sourceFilter) {
      sourceFilter.addEventListener("change", applyRecordFilters);
    }
  }

  function syncCorrectionControls() {
    if (!correctionType || !proposedEventWrap) {
      return;
    }
    const show = correctionType.value === "TIME_FIX";
    proposedEventWrap.style.display = show ? "block" : "none";
  }

  function setupCorrectionForm() {
    if (correctionType) {
      correctionType.addEventListener("change", syncCorrectionControls);
      syncCorrectionControls();
    }

    if (requestedDatetime) {
      const now = new Date();
      const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
      const value = local.toISOString().slice(0, 16);
      requestedDatetime.max = value;
      if (!requestedDatetime.value) {
        requestedDatetime.value = value;
      }
    }
  }

  if (fetchLocationBtn) {
    fetchLocationBtn.addEventListener("click", fetchLocation);
  }
  if (markInBtn) {
    markInBtn.addEventListener("click", () => submitAttendance("IN"));
  }
  if (markOutBtn) {
    markOutBtn.addEventListener("click", () => submitAttendance("OUT"));
  }

  form.addEventListener("submit", (event) => event.preventDefault());

  setupRecordFilters();
  setupCorrectionForm();

  fetchLocation();
})();
