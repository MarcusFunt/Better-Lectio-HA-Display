import { ESPLoader, Transport } from "/auth/esptool-js-v0.7.0.js";

(() => {
  const button = document.getElementById("usb-flash-firmware");
  if (!button) return;

  const status = document.getElementById("usb-flash-status");
  const progress = document.getElementById("usb-flash-progress");
  const log = document.getElementById("usb-flash-log");
  const manifestUrl = "/auth/firmware/manifest.json";
  const firmwareUrl = "/auth/firmware/lectio_s3_merged.bin";
  const maxFirmwareBytes = 4 * 1024 * 1024;

  function setStatus(message) {
    status.textContent = message;
  }

  function appendLog(message) {
    if (!message) return;
    log.hidden = false;
    log.textContent = `${log.textContent}${message}\n`.slice(-6000);
  }

  async function readManifest() {
    const response = await fetch(manifestUrl, { cache: "no-store" });
    if (!response.ok) throw new Error("The packaged firmware manifest is unavailable.");
    const manifest = await response.json();
    if (
      manifest.target !== "lectio_s3" ||
      manifest.chip !== "ESP32-S3" ||
      manifest.file !== "lectio_s3_merged.bin" ||
      manifest.flash_address !== "0x0" ||
      manifest.flash_mode !== "dio" ||
      manifest.flash_frequency !== "40m" ||
      manifest.flash_size !== "4MB" ||
      !Number.isSafeInteger(manifest.size_bytes) ||
      manifest.size_bytes <= 0 ||
      manifest.size_bytes > maxFirmwareBytes ||
      !/^[a-f0-9]{64}$/.test(manifest.sha256) ||
      typeof manifest.version !== "string" ||
      !manifest.version
    ) {
      throw new Error("The firmware manifest is invalid or targets different hardware.");
    }
    return manifest;
  }

  async function packageFirmware(manifest) {
    const response = await fetch(firmwareUrl, { cache: "no-store" });
    if (!response.ok) throw new Error("The packaged Lectio firmware could not be downloaded.");
    const firmware = new Uint8Array(await response.arrayBuffer());
    if (firmware.byteLength !== manifest.size_bytes) {
      throw new Error("The firmware file size does not match its manifest.");
    }
    const digest = await crypto.subtle.digest("SHA-256", firmware);
    const checksum = Array.from(new Uint8Array(digest), (value) =>
      value.toString(16).padStart(2, "0"),
    ).join("");
    if (checksum !== manifest.sha256) {
      firmware.fill(0);
      throw new Error("Firmware checksum verification failed; nothing was flashed.");
    }
    return firmware;
  }

  async function refreshManifestStatus() {
    try {
      const manifest = await readManifest();
      setStatus(`Ready: Lectio firmware ${manifest.version} (${(manifest.size_bytes / 1024).toFixed(0)} KiB).`);
    } catch (error) {
      setStatus(error.message || "Packaged firmware is unavailable.");
      button.disabled = true;
    }
  }

  button.addEventListener("click", async () => {
    if (!globalThis.isSecureContext || !("serial" in navigator)) {
      setStatus("Web Serial is unavailable. Open this page in Chrome or Edge on localhost or HTTPS.");
      return;
    }

    let port = null;
    let transport = null;
    let firmware = null;
    let flashed = false;
    let flashStarted = false;
    button.disabled = true;
    progress.hidden = false;
    progress.value = 0;
    log.textContent = "";
    log.hidden = true;
    try {
      setStatus("Choose the XIAO ESP32-S3 in the browser's USB device picker.");
      port = await navigator.serial.requestPort();

      setStatus("Checking the packaged firmware and its SHA-256…");
      const manifest = await readManifest();
      firmware = await packageFirmware(manifest);

      setStatus("Connecting to the ESP32-S3 bootloader…");
      transport = new Transport(port);
      const loader = new ESPLoader({
        transport,
        baudrate: 115200,
        terminal: {
          clean: () => { log.textContent = ""; },
          writeLine: (message) => appendLog(message),
          write: (message) => appendLog(message),
        },
      });
      const chipName = await loader.main();
      if (!/ESP32-S3/i.test(chipName)) {
        throw new Error(`Wrong chip detected (${chipName}). This firmware requires an ESP32-S3.`);
      }

      setStatus(`Flashing ${manifest.version} to ${chipName}…`);
      flashStarted = true;
      await loader.writeFlash({
        fileArray: [{ data: firmware, address: Number.parseInt(manifest.flash_address, 16) }],
        flashMode: manifest.flash_mode,
        flashFreq: manifest.flash_frequency,
        flashSize: manifest.flash_size,
        eraseAll: false,
        compress: true,
        reportProgress: (_fileIndex, written, total) => {
          const percent = total > 0 ? Math.min(100, Math.floor((written / total) * 100)) : 0;
          progress.value = percent;
          setStatus(`Flashing ${manifest.version}: ${percent}%`);
        },
      });
      flashed = true;
      progress.value = 100;
      setStatus("Firmware written. Restarting the board…");
      try {
        await loader.after("hard_reset");
        setStatus(`Firmware ${manifest.version} flashed. Use USB device provisioning below to configure Wi-Fi.`);
      } catch {
        setStatus(`Firmware ${manifest.version} was written. Tap RESET, then use USB device provisioning below.`);
      }
    } catch (error) {
      const message = error?.name === "NotFoundError"
        ? "USB device selection was cancelled."
        : (error?.message || "Firmware flashing failed.");
      setStatus(flashed
        ? `Firmware was written, but the board did not restart cleanly: ${message}`
        : (flashStarted
          ? `Flashing failed and the firmware may be incomplete. Flash again before provisioning. ${message}`
          : message));
    } finally {
      firmware?.fill(0);
      try { await transport?.disconnect(); } catch {}
      button.disabled = false;
      if (!flashed) progress.hidden = true;
    }
  });

  refreshManifestStatus();
})();
