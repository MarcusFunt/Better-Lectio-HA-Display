#include <lectio/device_api.h>

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include <algorithm>
#include <stdlib.h>

namespace lectio {
namespace {

constexpr uint32_t kDefaultPollSeconds = 30;
constexpr uint32_t kMinPollSeconds = 5;
constexpr uint32_t kMaxPollSeconds = 600;
constexpr size_t kMetadataLimit = 2048;
constexpr char kDisplayPath[] = "/device/v1/display";
constexpr char kImagePathPrefix[] = "/device/v1/image/";

String normalizedBaseUrl(const String &baseUrl) {
  String normalized = baseUrl;
  while (normalized.endsWith("/")) {
    normalized.remove(normalized.length() - 1);
  }
  return normalized;
}

bool beginAuthenticatedRequest(HTTPClient &http, WiFiClient &client, const DeviceConfig &config, const String &path,
                               String &error) {
  const String baseUrl = normalizedBaseUrl(config.serverUrl);
  if (!baseUrl.startsWith("http://")) {
    error = "server_url must use http:// on the local network";
    return false;
  }

  client.setTimeout(15000);
  http.setTimeout(15000);
  if (!http.begin(client, baseUrl + path)) {
    error = "could not initialize HTTP request";
    return false;
  }

  http.addHeader("Authorization", "Bearer " + config.deviceSecret);
  http.addHeader("X-Device-ID", config.deviceId);
  return true;
}

bool isSafeImagePath(const String &path) {
  return path.startsWith(kImagePathPrefix) && path.endsWith(".bmp") && path.indexOf("..") < 0 &&
         path.indexOf('?') < 0 && path.indexOf('#') < 0 && path.indexOf("://") < 0;
}

uint16_t readLe16(const uint8_t *data) {
  return static_cast<uint16_t>(data[0]) | (static_cast<uint16_t>(data[1]) << 8);
}

uint32_t readLe32(const uint8_t *data) {
  return static_cast<uint32_t>(data[0]) | (static_cast<uint32_t>(data[1]) << 8) |
         (static_cast<uint32_t>(data[2]) << 16) | (static_cast<uint32_t>(data[3]) << 24);
}

}  // namespace

bool DeviceConfig::isProvisioned() const {
  return !serverUrl.isEmpty() && !deviceId.isEmpty() && !deviceSecret.isEmpty() && !wifiSsid.isEmpty();
}

bool fetchDisplayMetadata(const DeviceConfig &config, DisplayMetadata &metadata, int &httpStatus, String &error) {
  WiFiClient client;
  HTTPClient http;
  if (!beginAuthenticatedRequest(http, client, config, kDisplayPath, error)) {
    http.end();
    return false;
  }

  http.addHeader("Accept", "application/json");
  httpStatus = http.GET();
  if (httpStatus != HTTP_CODE_OK) {
    error = "display metadata request returned HTTP " + String(httpStatus);
    http.end();
    return false;
  }

  const String body = http.getString();
  http.end();
  if (body.isEmpty() || body.length() > kMetadataLimit) {
    error = "display metadata response has an invalid size";
    return false;
  }

  JsonDocument document;
  const DeserializationError parseError = deserializeJson(document, body);
  if (parseError) {
    error = "display metadata JSON is invalid";
    return false;
  }

  metadata.contentHash = document["content_hash"] | "";
  metadata.imagePath = document["image_url"] | "";
  const uint32_t nextCheck = document["next_check_seconds"] | kDefaultPollSeconds;
  metadata.nextCheckSeconds = static_cast<uint16_t>(std::max(kMinPollSeconds, std::min(nextCheck, kMaxPollSeconds)));
  if (metadata.contentHash.isEmpty() || !isSafeImagePath(metadata.imagePath)) {
    error = "display metadata is missing a content hash or local image path";
    return false;
  }
  if (metadata.imagePath != String(kImagePathPrefix) + metadata.contentHash + ".bmp") {
    error = "image path does not match the advertised content hash";
    return false;
  }
  return true;
}

bool downloadDisplayImage(const DeviceConfig &config, const DisplayMetadata &metadata, uint8_t **image,
                          size_t &imageSize, int &httpStatus, String &error) {
  *image = nullptr;
  imageSize = 0;
  if (!isSafeImagePath(metadata.imagePath)) {
    error = "image URL must be a local content-addressed BMP path";
    return false;
  }

  WiFiClient client;
  HTTPClient http;
  if (!beginAuthenticatedRequest(http, client, config, metadata.imagePath, error)) {
    http.end();
    return false;
  }

  http.addHeader("Accept", "image/bmp");
  httpStatus = http.GET();
  if (httpStatus != HTTP_CODE_OK) {
    error = "image request returned HTTP " + String(httpStatus);
    http.end();
    return false;
  }

  const int contentLength = http.getSize();
  if (contentLength >= 0 && static_cast<size_t>(contentLength) != kDisplayBitmapSize) {
    error = "image response is not the expected 800x480 monochrome BMP size";
    http.end();
    return false;
  }

  auto *buffer = static_cast<uint8_t *>(malloc(kDisplayBitmapSize));
  if (buffer == nullptr) {
    error = "not enough memory for the display image";
    http.end();
    return false;
  }

  WiFiClient *stream = http.getStreamPtr();
  size_t received = 0;
  while (received < kDisplayBitmapSize) {
    const size_t bytesRead = stream->readBytes(reinterpret_cast<char *>(buffer + received),
                                               kDisplayBitmapSize - received);
    if (bytesRead == 0) {
      break;
    }
    received += bytesRead;
  }
  http.end();

  if (received != kDisplayBitmapSize || !isValidDisplayBitmap(buffer, received)) {
    free(buffer);
    error = "downloaded image is incomplete or is not a supported 800x480 1-bit BMP";
    return false;
  }

  *image = buffer;
  imageSize = received;
  return true;
}

bool isValidDisplayBitmap(const uint8_t *image, size_t imageSize) {
  if (image == nullptr || imageSize != kDisplayBitmapSize) {
    return false;
  }

  // The inherited e-paper driver expects a 14-byte BMP file header, a 40-byte
  // DIB header, an 8-byte monochrome palette, and 48,000 bytes of pixel data.
  return image[0] == 'B' && image[1] == 'M' && readLe32(image + 2) == kDisplayBitmapSize &&
         readLe32(image + 10) == 62 && readLe32(image + 14) == 40 && readLe32(image + 18) == 800 &&
         readLe32(image + 22) == 480 && readLe16(image + 26) == 1 && readLe16(image + 28) == 1 &&
         readLe32(image + 30) == 0 && readLe32(image + 34) == 48000;
}

}  // namespace lectio
