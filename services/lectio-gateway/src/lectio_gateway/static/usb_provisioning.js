(() => {
  const form = document.getElementById("usb-provisioning-form");
  if (!form) return;

  const status = document.getElementById("usb-provisioning-status");
  const submit = document.getElementById("usb-connect");
  const revokePending = document.getElementById("usb-revoke-pending");
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  let pendingDeviceId = null;
  let port = null;
  let reader = null;
  let writer = null;
  let inputBuffer = "";

  function setStatus(message) {
    status.textContent = message;
  }

  function setPendingDevice(deviceId) {
    pendingDeviceId = deviceId;
    revokePending.hidden = !deviceId;
  }

  function newDeviceId() {
    if (crypto.randomUUID) return crypto.randomUUID().replaceAll("-", "");
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function validateConfiguration(config) {
    const fields = [
      ["server_url", config.server_url, 200, false],
      ["device_secret", config.device_secret, 128, false],
      ["wifi_ssid", config.wifi_ssid, 32, false],
      ["wifi_password", config.wifi_password, 63, true],
    ];
    for (const [name, value, maxBytes, allowEmpty] of fields) {
      if (typeof value !== "string" || (!allowEmpty && !value.trim())) {
        throw new Error(`${name} is required.`);
      }
      if (encoder.encode(value).length > maxBytes || /[\x00-\x1f\x7f]/.test(value)) {
        throw new Error(`${name} is too long or contains unsupported characters.`);
      }
    }
    const url = new URL(config.server_url);
    if (
      url.protocol !== "http:" ||
      !url.hostname ||
      url.username ||
      url.password ||
      !["", "/"].includes(url.pathname) ||
      url.search ||
      url.hash
    ) {
      throw new Error("Use the device-reachable HTTP base URL for display-service.");
    }
  }

  async function writeLine(value) {
    const bytes = encoder.encode(`${JSON.stringify(value)}\n`);
    if (bytes.length > 2048) throw new Error("USB configuration exceeds the device limit.");
    await writer.write(bytes);
    bytes.fill(0);
  }

  async function readProtocolResponse() {
    while (true) {
      const { value, done } = await reader.read();
      if (done) throw new Error("The USB serial connection closed.");
      inputBuffer += decoder.decode(value, { stream: true });
      if (inputBuffer.length > 4096) inputBuffer = inputBuffer.slice(-2048);
      while (inputBuffer.includes("\n")) {
        const newline = inputBuffer.indexOf("\n");
        const line = inputBuffer.slice(0, newline).replace(/\r$/, "");
        inputBuffer = inputBuffer.slice(newline + 1);
        if (line.length > 2048) continue;
        try {
          const response = JSON.parse(line);
          if (response && response.version === 1 && typeof response.status === "string") {
            return response;
          }
        } catch {
          // Ignore boot diagnostics and other non-protocol serial lines.
        }
      }
    }
  }

  async function withTimeout(promise, milliseconds) {
    let timeoutId;
    try {
      return await Promise.race([
        promise,
        new Promise((_, reject) => {
          timeoutId = setTimeout(() => reject(new Error("The device did not respond in time.")), milliseconds);
        }),
      ]);
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async function revoke(deviceId) {
    const response = await fetch(`/auth/provisioning/devices/${encodeURIComponent(deviceId)}/revoke`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
    });
    if (!response.ok && response.status !== 404) {
      throw new Error("The unprovisioned device credential could not be revoked automatically.");
    }
  }

  revokePending.addEventListener("click", async () => {
    const deviceId = pendingDeviceId;
    if (!deviceId) return;
    revokePending.disabled = true;
    try {
      await revoke(deviceId);
      setPendingDevice(null);
      setStatus(`Device credential ${deviceId} was revoked.`);
    } catch (error) {
      setStatus(error.message || "The device credential could not be revoked.");
    } finally {
      revokePending.disabled = false;
    }
  });

  async function waitForDevice(deviceId) {
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      const response = await fetch(`/auth/provisioning/devices/${encodeURIComponent(deviceId)}`, {
        cache: "no-store",
      });
      if (response.ok) {
        const device = await response.json();
        if (device.registered && device.last_seen) return true;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    return false;
  }

  async function closePort() {
    try { if (reader) await reader.cancel(); } catch {}
    try { if (reader) reader.releaseLock(); } catch {}
    try { if (writer) writer.releaseLock(); } catch {}
    try { if (port?.readable || port?.writable) await port.close(); } catch {}
    reader = null;
    writer = null;
    port = null;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!("serial" in navigator)) {
      setStatus("Web Serial is unavailable. Open this page in Chrome or Edge on localhost or HTTPS.");
      return;
    }

    const config = {
      name: document.getElementById("usb-device-name").value.trim(),
      server_url: document.getElementById("usb-server-url").value.trim(),
      wifi_ssid: document.getElementById("usb-wifi-ssid").value,
      wifi_password: document.getElementById("usb-wifi-password").value,
    };
    if (!config.name || config.name.length > 120 || !config.wifi_ssid.trim()) {
      setStatus("Enter a device name and Wi-Fi network name.");
      return;
    }
    try {
      validateConfiguration({
        ...config,
        device_secret: "validation-only",
      });
    } catch (error) {
      setStatus(error.message || "Check the provisioning details and try again.");
      return;
    }

    let deviceId = null;
    let credential = null;
    let usbConfiguration = null;
    let registrationAttempted = false;
    let registrationConflict = false;
    let configWritten = false;
    let deviceRejected = false;
    submit.disabled = true;
    setPendingDevice(null);
    try {
      setStatus("Choose the connected display in the browser's USB device picker.");
      port = await navigator.serial.requestPort();
      await port.open({ baudRate: 115200 });
      reader = port.readable.getReader();
      writer = port.writable.getWriter();

      setStatus("Checking for a ready display over USB…");
      const readyResponse = readProtocolResponse();
      let ready = null;
      const handshakeDeadline = Date.now() + 45000;
      while (!ready && Date.now() < handshakeDeadline) {
        await writeLine({ version: 1, command: "hello" });
        try {
          ready = await withTimeout(readyResponse, 1000);
        } catch (error) {
          if (error.message !== "The device did not respond in time.") throw error;
        }
      }
      if (ready?.status !== "ready") throw new Error("The display did not become ready over USB.");

      deviceId = newDeviceId();
      setStatus("Registering a device credential…");
      registrationAttempted = true;
      const registration = await fetch("/auth/provisioning/devices", {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: deviceId, name: config.name }),
      });
      registrationConflict = registration.status === 409;
      const registrationResult = await registration.json();
      if (!registration.ok) throw new Error(registrationResult.detail || "The display service could not register this device.");
      credential = registrationResult;

      usbConfiguration = {
        version: 1,
        command: "provision",
        server_url: config.server_url,
        device_id: credential.device_id,
        device_secret: credential.device_secret,
        wifi_ssid: config.wifi_ssid,
        wifi_password: config.wifi_password,
      };
      validateConfiguration(usbConfiguration);
      const configurationLine = encoder.encode(`${JSON.stringify(usbConfiguration)}\n`);
      if (configurationLine.length > 2048) throw new Error("USB configuration exceeds the device limit.");
      try {
        await writer.write(configurationLine);
        configWritten = true;
      } finally {
        configurationLine.fill(0);
        usbConfiguration.device_secret = "";
        usbConfiguration.wifi_password = "";
        config.wifi_password = "";
        document.getElementById("usb-wifi-password").value = "";
      }

      const accepted = await withTimeout(readProtocolResponse(), 8000);
      if (accepted.status !== "ok") {
        deviceRejected = accepted.status === "error";
        throw new Error(deviceRejected ? "The display rejected the configuration; its temporary credential will be revoked." : "USB provisioning was not confirmed.");
      }
      credential.device_secret = "";
      credential = null;
      setStatus(`USB accepted configuration for ${deviceId}. Waiting for authenticated service contact…`);
      const contacted = await waitForDevice(deviceId);
      setStatus(contacted
        ? `Provisioning complete for ${deviceId}. The display authenticated with the service.`
        : `USB configuration was accepted for ${deviceId}, but service contact was not observed yet. Check Wi-Fi and LAN reachability; the registration is retained.`);
    } catch (error) {
      const message = error?.name === "NotFoundError"
        ? "USB device selection was cancelled."
        : (error?.message || "USB provisioning failed.");
      if (
        deviceId &&
        (!configWritten || deviceRejected) &&
        (credential || registrationAttempted) &&
        !registrationConflict
      ) {
        try {
          await revoke(deviceId);
          credential.device_secret = "";
          credential = null;
          setStatus(`${message} The temporary device credential was revoked.`);
        } catch (revokeError) {
          setPendingDevice(deviceId);
          setStatus(`${message} ${revokeError.message} Device ID: ${deviceId}.`);
        }
      } else if (deviceId && configWritten) {
        if (credential) credential.device_secret = "";
        credential = null;
        setPendingDevice(deviceId);
        setStatus(`${message} The board may have accepted the configuration. Device ID ${deviceId} remains registered; check Wi-Fi and LAN reachability.`);
      } else {
        setStatus(message);
      }
    } finally {
      config.wifi_password = "";
      if (usbConfiguration) {
        usbConfiguration.device_secret = "";
        usbConfiguration.wifi_password = "";
      }
      usbConfiguration = null;
      document.getElementById("usb-wifi-password").value = "";
      credential = null;
      await closePort();
      submit.disabled = false;
    }
  });
})();
