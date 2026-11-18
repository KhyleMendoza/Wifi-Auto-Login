import 'package:flutter/services.dart';

/// Thin wrapper over the Kotlin side (MainActivity).
class Native {
  static const _ch = MethodChannel('wifi_auto_login/native');

  /// Force this app's network traffic onto Wi-Fi, so requests reach the captive
  /// portal even while Wi-Fi has "no internet" and Android prefers mobile data.
  static Future<bool> bindToWifi() async {
    try {
      return await _ch.invokeMethod<bool>('bindWifi') ?? false;
    } catch (_) {
      return false;
    }
  }

  static Future<void> unbind() async {
    try {
      await _ch.invokeMethod('unbind');
    } catch (_) {}
  }

  /// The connected Wi-Fi name, or "" if unknown (needs location permission).
  static Future<String> currentSsid() async {
    try {
      return await _ch.invokeMethod<String>('ssid') ?? '';
    } catch (_) {
      return '';
    }
  }

  /// Start/stop the foreground service that keeps the app running in the
  /// background with a notification.
  static Future<void> startService(String text) async {
    try {
      await _ch.invokeMethod('startService', {'text': text});
    } catch (_) {}
  }

  static Future<void> updateService(String text) async {
    try {
      await _ch.invokeMethod('updateService', {'text': text});
    } catch (_) {}
  }

  static Future<void> stopService() async {
    try {
      await _ch.invokeMethod('stopService');
    } catch (_) {}
  }
}
