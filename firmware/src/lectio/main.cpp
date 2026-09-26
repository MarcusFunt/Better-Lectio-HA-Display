#include <Arduino.h>
#include <ArduinoLog.h>
#include <Preferences.h>
#include <WiFi.h>
#include <display.h>
#include <globals.h>
#include <lectio/device_api.h>
#include <pins.h>

#include <stdlib.h>

namespace {

constexpr char kFirmwareVersion[] = "0.1.0";
constexpr char kPreferencesNamespace[] = "lectio";
constexpr uint32_t kWifiRetrySeconds = 10;
constexpr uint32_t kFailureRetrySeconds = 15;

lectio::DeviceConfig deviceConfig;
String currentContentHash;
uint32_t nextPollAt = 0;
uint32_t nextWifiAttemptAt = 0;
uint32_t lastSuccessfulUpdateAt = 0;
int lastHttpStatus = 0;

bool deadlineReached(uint32_t deadline) {
  return static_cast<int32_t>(millis() - deadline) >= 0;
}

void loadConfiguration() {
  deviceConfig.serverUrl = preferences.getString("server_url", "");
  deviceConfig.deviceId = preferences.getString("device_id", "");
  deviceConfig.deviceSecret = preferences.getString("device_secret", "");
  deviceConfig.wifiSsid = preferences.getString("wifi_ssid", "");
  deviceConfig.wifiPassword = preferences.getString("wifi_password", "");
  currentContentHash = preferences.getString("content_hash", "");
}

void printDiagnostics() {
  Serial.printf("Firmware version: %s\n", kFirmwareVersion);
  Serial.printf("Device ID: %s\n", deviceConfig.deviceId.isEmpty() ? "unprovisioned" : deviceConfig.deviceId.c_str());
  Serial.printf("Wi-Fi status: %s\n", WiFi.status() == WL_CONNECTED ? "connected" : "disconnected");
  Serial.printf("Server URL: %s\n", deviceConfig.serverUrl.isEmpty() ? "not configured" : deviceConfig.serverUrl.c_str());
  Serial.printf("Last HTTP result: %d\n", lastHttpStatus);
  Serial.printf("Current content hash: %s\n", currentContentHash.isEmpty() ? "none" : currentContentHash.c_str());
  Serial.printf("Last successful update uptime: %lu ms\n", static_cast<unsigned long>(lastSuccessfulUpdateAt));
}

void startWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  WiFi.setHostname(deviceConfig.deviceId.c_str());
  WiFi.begin(deviceConfig.wifiSsid.c_str(), deviceConfig.wifiPassword.c_str());
  nextWifiAttemptAt = millis() + kWifiRetrySeconds * 1000;
  Serial.printf("Connecting to configured Wi-Fi (SSID length %u).\n",
                static_cast<unsigned int>(deviceConfig.wifiSsid.length()));
}

bool ensureWifiConnected() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }
  if (deadlineReached(nextWifiAttemptAt)) {
    Serial.println("Wi-Fi is disconnected; reconnecting.");
    WiFi.disconnect(false);
    WiFi.begin(deviceConfig.wifiSsid.c_str(), deviceConfig.wifiPassword.c_str());
    nextWifiAttemptAt = millis() + kWifiRetrySeconds * 1000;
  }
  return false;
}

void scheduleNextPoll(uint16_t seconds) {
  nextPollAt = millis() + static_cast<uint32_t>(seconds) * 1000;
}

void pollDisplayService() {
  lectio::DisplayMetadata metadata;
  String error;
  if (!lectio::fetchDisplayMetadata(deviceConfig, metadata, lastHttpStatus, error)) {
    Serial.printf("Display metadata unavailable (HTTP %d): %s\n", lastHttpStatus, error.c_str());
    scheduleNextPoll(kFailureRetrySeconds);
    printDiagnostics();
    return;
  }

  if (metadata.contentHash == currentContentHash) {
    Serial.printf("Display unchanged: %s\n", currentContentHash.c_str());
    scheduleNextPoll(metadata.nextCheckSeconds);
    printDiagnostics();
    return;
  }

  uint8_t *image = nullptr;
  size_t imageSize = 0;
  if (!lectio::downloadDisplayImage(deviceConfig, metadata, &image, imageSize, lastHttpStatus, error)) {
    Serial.printf("Image update skipped (HTTP %d): %s\n", lastHttpStatus, error.c_str());
    scheduleNextPoll(kFailureRetrySeconds);
    printDiagnostics();
    return;
  }

  const bool displayed = display_show_image(image, static_cast<int>(imageSize), true);
  free(image);
  if (!displayed) {
    Serial.println("E-paper refresh failed; keeping the previous content hash.");
    scheduleNextPoll(kFailureRetrySeconds);
    return;
  }

  currentContentHash = metadata.contentHash;
  preferences.putString("content_hash", currentContentHash);
  lastSuccessfulUpdateAt = millis();
  Serial.printf("Applied display image: %s\n", currentContentHash.c_str());
  scheduleNextPoll(metadata.nextCheckSeconds);
  printDiagnostics();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(250);
  Log.begin(LOG_LEVEL_INFO, &Serial);
  Serial.printf("Better Lectio display firmware %s starting.\n", kFirmwareVersion);

  pins_init();
  if (!preferences.begin(kPreferencesNamespace, false)) {
    Serial.println("Could not open device configuration storage.");
    return;
  }

  loadConfiguration();
  apiDisplayResult.response.temp_profile = 0;
  apiDisplayResult.response.maximum_compatibility = false;
  display_init();

  if (!deviceConfig.isProvisioned()) {
    Serial.println("Provisioning required. Configure server URL, device credentials, and Wi-Fi over USB.");
    display_show_msg(nullptr, LECTIO_PROVISIONING_REQUIRED);
    printDiagnostics();
    return;
  }

  Serial.printf("Configured device %s; polling %s\n", deviceConfig.deviceId.c_str(), deviceConfig.serverUrl.c_str());
  startWifi();
  scheduleNextPoll(1);
}

void loop() {
  if (!deviceConfig.isProvisioned()) {
    delay(1000);
    return;
  }
  if (!ensureWifiConnected() || !deadlineReached(nextPollAt)) {
    delay(100);
    return;
  }
  pollDisplayService();
}
