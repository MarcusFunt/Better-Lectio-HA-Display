#pragma once

#include <Arduino.h>

#include <stddef.h>
#include <stdint.h>

namespace lectio {

constexpr size_t kDisplayBitmapSize = 48062;

struct DeviceConfig {
  String serverUrl;
  String deviceId;
  String deviceSecret;
  String wifiSsid;
  String wifiPassword;

  bool isProvisioned() const;
};

struct DisplayMetadata {
  String contentHash;
  String imagePath;
  uint16_t nextCheckSeconds = 30;
};

bool fetchDisplayMetadata(const DeviceConfig &config, DisplayMetadata &metadata, int &httpStatus, String &error);

bool downloadDisplayImage(const DeviceConfig &config, const DisplayMetadata &metadata, uint8_t **image,
                          size_t &imageSize, int &httpStatus, String &error);

bool isValidDisplayBitmap(const uint8_t *image, size_t imageSize);

}  // namespace lectio
