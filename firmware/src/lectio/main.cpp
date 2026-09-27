#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoLog.h>
#include <Preferences.h>
#include <WiFi.h>
#include <config.h>
#include <display.h>
#include <globals.h>
#include <lectio/device_api.h>
#include <pins.h>

#include <stdlib.h>
#include <string.h>

namespace {

constexpr char kFirmwareVersion[] = "0.1.0";
constexpr char kPreferencesNamespace[] = "lectio";
constexpr uint32_t kWifiRetrySeconds = 10;
constexpr uint32_t kFailureRetrySeconds = 15;
constexpr uint32_t kButtonMinimumPressMs = 50;
constexpr uint32_t kButtonMaximumPressMs = 1500;
constexpr size_t kProvisioningLineLimit = 2048;

lectio::DeviceConfig deviceConfig;
String currentContentHash;
uint32_t nextPollAt = 0;
uint32_t nextWifiAttemptAt = 0;
uint32_t lastSuccessfulUpdateAt = 0;
int lastHttpStatus = 0;
bool reviewButtonPressed = false;
uint32_t reviewButtonPressedAt = 0;
char provisioningLine[kProvisioningLineLimit + 1] = {};
size_t provisioningLineLength = 0;
bool provisioningLineTooLong = false;

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

bool isValidDeviceId(const String &value) {
  if (value.isEmpty() || value.length() > 128) {
    return false;
  }
  for (size_t index = 0; index < value.length(); ++index) {
    const char character = value[index];
    const bool alphanumeric = (character >= 'A' && character <= 'Z') || (character >= 'a' && character <= 'z') ||
                              (character >= '0' && character <= '9');
    if ((!alphanumeric && index == 0) ||
        (!alphanumeric && character != '.' && character != '_' && character != '-')) {
      return false;
    }
  }
  return true;
}

bool isValidServerUrl(const String &value) {
  if (!value.startsWith("http://") || value.length() > 200 || value.indexOf('@') >= 0 || value.indexOf('?') >= 0 ||
      value.indexOf('#') >= 0) {
    return false;
  }
  String authority = value.substring(7);
  if (authority.endsWith("/")) {
    authority.remove(authority.length() - 1);
  }
  if (authority.isEmpty() || authority.indexOf('/') >= 0 || authority.indexOf('\\') >= 0) {
    return false;
  }
  for (size_t index = 0; index < authority.length(); ++index) {
    if (authority[index] <= 32 || authority[index] == 127) {
      return false;
    }
  }
  return true;
}

void replyProvisioningError(const char *code) {
  Serial.printf("{\"version\":1,\"status\":\"error\",\"code\":\"%s\"}\n", code);
}

void processProvisioningLine(char *line) {
  JsonDocument document;
  const DeserializationError parseError = deserializeJson(document, line);
  if (parseError || !document.is<JsonObject>()) {
    replyProvisioningError("invalid_request");
    return;
  }
  if (document["version"].as<int>() != 1) {
    replyProvisioningError("unsupported_version");
    return;
  }
  const char *command = document["command"] | "";
  if (strcmp(command, "hello") == 0) {
    Serial.println("{\"version\":1,\"status\":\"ready\"}");
    return;
  }
  if (strcmp(command, "provision") != 0) {
    replyProvisioningError("invalid_request");
    return;
  }

  const String serverUrl = document["server_url"] | "";
  const String deviceId = document["device_id"] | "";
  const String deviceSecret = document["device_secret"] | "";
  const String wifiSsid = document["wifi_ssid"] | "";
  const String wifiPassword = document["wifi_password"] | "";
  if (!isValidServerUrl(serverUrl) || !isValidDeviceId(deviceId) || deviceSecret.isEmpty() ||
      deviceSecret.length() > 128 || wifiSsid.isEmpty() || wifiSsid.length() > 32 || wifiPassword.length() > 63) {
    replyProvisioningError("invalid_configuration");
    return;
  }

  const String previousServerUrl = preferences.getString("server_url", "");
  const String previousDeviceId = preferences.getString("device_id", "");
  const String previousDeviceSecret = preferences.getString("device_secret", "");
  const String previousWifiSsid = preferences.getString("wifi_ssid", "");
  const String previousWifiPassword = preferences.getString("wifi_password", "");
  const bool stored = preferences.putString("server_url", serverUrl) > 0 &&
                      preferences.putString("device_id", deviceId) > 0 &&
                      preferences.putString("device_secret", deviceSecret) > 0 &&
                      preferences.putString("wifi_ssid", wifiSsid) > 0 &&
                      preferences.putString("wifi_password", wifiPassword) > 0;
  if (!stored) {
    preferences.putString("server_url", previousServerUrl);
    preferences.putString("device_id", previousDeviceId);
    preferences.putString("device_secret", previousDeviceSecret);
    preferences.putString("wifi_ssid", previousWifiSsid);
    preferences.putString("wifi_password", previousWifiPassword);
    replyProvisioningError("storage_error");
    return;
  }

  Serial.println("{\"version\":1,\"status\":\"ok\"}");
  Serial.flush();
  delay(250);
  ESP.restart();
}

void serviceUsbProvisioning() {
  while (Serial.available() > 0) {
    const int incoming = Serial.read();
    if (incoming < 0 || incoming == '\r') {
      continue;
    }
    if (incoming == '\n') {
      if (provisioningLineTooLong) {
        replyProvisioningError("line_too_long");
      } else if (provisioningLineLength > 0) {
        provisioningLine[provisioningLineLength] = '\0';
        processProvisioningLine(provisioningLine);
      }
      provisioningLineLength = 0;
      provisioningLineTooLong = false;
      continue;
    }
    if (provisioningLineLength >= kProvisioningLineLimit) {
      provisioningLineTooLong = true;
      continue;
    }
    if (!provisioningLineTooLong) {
      provisioningLine[provisioningLineLength++] = static_cast<char>(incoming);
    }
  }
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

void handleReviewButton() {
  const bool pressed = digitalRead(PIN_INTERRUPT) == LOW;
  if (pressed && !reviewButtonPressed) {
    reviewButtonPressed = true;
    reviewButtonPressedAt = millis();
    return;
  }
  if (pressed || !reviewButtonPressed) {
    return;
  }

  reviewButtonPressed = false;
  const uint32_t pressDuration = millis() - reviewButtonPressedAt;
  if (pressDuration < kButtonMinimumPressMs || pressDuration > kButtonMaximumPressMs) {
    return;
  }
  if (!deviceConfig.isProvisioned() || WiFi.status() != WL_CONNECTED) {
    Serial.println("Plan review acknowledgement is unavailable while the service is offline.");
    return;
  }

  String error;
  if (lectio::acknowledgeDisplayChanges(deviceConfig, currentContentHash, lastHttpStatus, error)) {
    Serial.println("Plan changes acknowledged.");
    scheduleNextPoll(1);
  } else {
    Serial.printf("Plan review acknowledgement failed (HTTP %d): %s\n", lastHttpStatus, error.c_str());
  }
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
  serviceUsbProvisioning();
  handleReviewButton();
  if (!deviceConfig.isProvisioned()) {
    delay(1000);
    return;
  }
  if (!ensureWifiConnected() || !deadlineReached(nextPollAt)) {
    delay(20);
    return;
  }
  pollDisplayService();
}
