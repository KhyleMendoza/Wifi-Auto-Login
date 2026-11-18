import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

const defaultProbes = [
  'http://landing.lkphils.com',
  'http://www.google.com',
  'http://www.msftconnecttest.com/redirect',
  'http://neverssl.com',
  'http://example.com',
];
const defaultSuccessUrl = 'http://landing.lkphils.com';
const defaultSuccessText = ['Start browsing', 'Welcome to', 'L&K Industries'];

/// One Wi-Fi's login details. The empty key "" is the Default profile.
class Profile {
  String username;
  String password;
  String portalUrl; // "" = find it automatically
  List<String> probeUrls;
  String successUrl;
  List<String> successText;

  Profile({
    this.username = '',
    this.password = '',
    this.portalUrl = '',
    List<String>? probeUrls,
    this.successUrl = defaultSuccessUrl,
    List<String>? successText,
  })  : probeUrls = probeUrls ?? List.of(defaultProbes),
        successText = successText ?? List.of(defaultSuccessText);

  Map<String, dynamic> toJson() => {
        'username': username,
        'password': password,
        'portalUrl': portalUrl,
        'probeUrls': probeUrls,
        'successUrl': successUrl,
        'successText': successText,
      };

  factory Profile.fromJson(Map<String, dynamic> j) => Profile(
        username: j['username'] ?? '',
        password: j['password'] ?? '',
        portalUrl: j['portalUrl'] ?? '',
        probeUrls: (j['probeUrls'] as List?)?.cast<String>(),
        successUrl: j['successUrl'] ?? defaultSuccessUrl,
        successText: (j['successText'] as List?)?.cast<String>(),
      );
}

/// Live settings handed to the engine: the chosen profile's values plus behavior.
class Settings {
  String username = '';
  String password = '';
  String portalUrl = '';
  List<String> probeUrls = List.of(defaultProbes);
  String successUrl = defaultSuccessUrl;
  List<String> successText = List.of(defaultSuccessText);

  int loginRetries = 20;
  int maxAttempts = 0;
  double delay = 2;
  double watchInterval = 30;

  final Set<String> knownHosts = {};

  String? get portalHost =>
      portalUrl.isEmpty ? null : Uri.tryParse(portalUrl)?.host;

  void applyProfile(Profile p) {
    username = p.username;
    password = p.password;
    portalUrl = p.portalUrl.isEmpty
        ? ''
        : (p.portalUrl.contains('://') ? p.portalUrl : 'https://${p.portalUrl}');
    probeUrls = p.probeUrls.map(_fix).toList();
    if (probeUrls.isEmpty) probeUrls = List.of(defaultProbes);
    successUrl = p.successUrl.isEmpty ? '' : _fix(p.successUrl);
    successText = p.successText.where((t) => t.trim().isNotEmpty).toList();
  }

  static String _fix(String url) {
    url = url.trim();
    return url.contains('://') ? url : 'http://$url';
  }
}

/// Everything the app stores: the profiles plus behavior, saved as JSON.
class Store {
  final Map<String, Profile> profiles = {'': Profile()};
  String wifiTarget = '';
  bool watchMode = false;
  int loginRetries = 20;
  int maxAttempts = 0;
  double delay = 2;
  double watchInterval = 30;

  Profile get defaultProfile => profiles['']!;

  Profile profileFor(String ssid) =>
      (ssid.isNotEmpty && profiles.containsKey(ssid))
          ? profiles[ssid]!
          : profiles['']!;

  Settings settingsFor(String ssid) {
    final s = Settings()
      ..loginRetries = loginRetries
      ..maxAttempts = maxAttempts
      ..delay = delay
      ..watchInterval = watchInterval;
    s.applyProfile(profileFor(ssid));
    return s;
  }

  Map<String, dynamic> toJson() => {
        'profiles': profiles.map((k, v) => MapEntry(k, v.toJson())),
        'wifiTarget': wifiTarget,
        'watchMode': watchMode,
        'loginRetries': loginRetries,
        'maxAttempts': maxAttempts,
        'delay': delay,
        'watchInterval': watchInterval,
      };

  void loadJson(Map<String, dynamic> j) {
    profiles.clear();
    final ps = (j['profiles'] as Map?) ?? {};
    for (final e in ps.entries) {
      profiles[e.key as String] =
          Profile.fromJson((e.value as Map).cast<String, dynamic>());
    }
    profiles.putIfAbsent('', () => Profile());
    wifiTarget = j['wifiTarget'] ?? '';
    watchMode = j['watchMode'] ?? false;
    loginRetries = j['loginRetries'] ?? 20;
    maxAttempts = j['maxAttempts'] ?? 0;
    delay = (j['delay'] ?? 2).toDouble();
    watchInterval = (j['watchInterval'] ?? 30).toDouble();
  }

  static const _key = 'wifi_auto_login_store';

  Future<void> save() async {
    final sp = await SharedPreferences.getInstance();
    await sp.setString(_key, jsonEncode(toJson()));
  }

  Future<void> load() async {
    final sp = await SharedPreferences.getInstance();
    final raw = sp.getString(_key);
    if (raw != null) {
      try {
        loadJson(jsonDecode(raw) as Map<String, dynamic>);
      } catch (_) {}
    }
  }
}
