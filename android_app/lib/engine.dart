import 'dart:async';
import 'dart:io';

import 'settings.dart';

typedef LogFn = void Function(String line);
typedef StatusFn = void Function(String state, String detail);

final RegExp _fgtauthLink =
    RegExp(r'''(https?://[^"'\s<>]*fgtauth[^"'\s<>]*)''', caseSensitive: false);
final RegExp _jsRedirect = RegExp(
    r'''(?:window\.|document\.|top\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']''',
    caseSensitive: false);
final RegExp _metaRefresh = RegExp(
    r'''http-equiv=["']?refresh["']?[^>]*url=([^"'>\s]+)''',
    caseSensitive: false);

const _userAgent =
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0 Safari/537.36';

class StoppedException implements Exception {}

class FormField {
  final String name;
  final String type;
  final String value;
  final bool checked;
  FormField(this.name, this.type, this.value, this.checked);
}

class HtmlForm {
  final String action;
  final String method;
  final List<FormField> inputs;
  HtmlForm(this.action, this.method, this.inputs);
  bool get hasPassword => inputs.any((i) => i.type == 'password');
}

/// Parses <form> blocks and their <input>/<button> fields with regex.
/// FortiGate pages are simple enough that this is reliable.
List<HtmlForm> parseForms(String page) {
  final forms = <HtmlForm>[];
  final formRe = RegExp(r'<form\b([^>]*)>(.*?)</form>',
      caseSensitive: false, dotAll: true);
  final inputRe = RegExp(r'<(input|button)\b([^>]*)>', caseSensitive: false);
  for (final fm in formRe.allMatches(page)) {
    final attrs = fm.group(1) ?? '';
    final body = fm.group(2) ?? '';
    final action = _attr(attrs, 'action') ?? '';
    final method = (_attr(attrs, 'method') ?? 'get').toLowerCase();
    final inputs = <FormField>[];
    for (final im in inputRe.allMatches(body)) {
      final tag = im.group(1)!.toLowerCase();
      final a = im.group(2) ?? '';
      final name = _attr(a, 'name') ?? '';
      final type = (_attr(a, 'type') ?? (tag == 'input' ? 'text' : 'submit'))
          .toLowerCase();
      final value = _attr(a, 'value') ?? '';
      inputs.add(FormField(name, type, value, a.contains('checked')));
    }
    forms.add(HtmlForm(action, method, inputs));
  }
  return forms;
}

String? _attr(String attrs, String key) {
  final m = RegExp('$key\\s*=\\s*"([^"]*)"', caseSensitive: false)
          .firstMatch(attrs) ??
      RegExp("$key\\s*=\\s*'([^']*)'", caseSensitive: false).firstMatch(attrs);
  return m?.group(1);
}

HtmlForm? findLoginForm(String page) {
  for (final f in parseForms(page)) {
    if (f.hasPassword) return f;
  }
  return null;
}

/// Password form, else an accept/continue form (FortiGate disclaimer pages).
HtmlForm? findPortalForm(String page) {
  final forms = parseForms(page);
  for (final f in forms) {
    if (f.hasPassword) return f;
  }
  for (final f in forms) {
    final names = f.inputs.map((i) => i.name.toLowerCase()).toSet();
    final types = f.inputs.map((i) => i.type).toSet();
    if (names.contains('magic') ||
        names.contains('4tredir') ||
        types.intersection({'text', 'email', 'checkbox'}).isNotEmpty) {
      return f;
    }
  }
  for (final f in forms) {
    if (f.inputs.any((i) => i.type == 'submit' || i.type == 'image')) return f;
  }
  return null;
}

bool _isPrivateIp(String host) {
  final parts = host.split('.');
  if (parts.length != 4) return false;
  final n = parts.map(int.tryParse).toList();
  if (n.any((x) => x == null || x < 0 || x > 255)) return false;
  final a = n[0]!, b = n[1]!;
  if (a == 10) return true;
  if (a == 192 && b == 168) return true;
  if (a == 172 && b >= 16 && b <= 31) return true;
  return false;
}

class WifiLoginEngine {
  final Settings settings;
  final LogFn log;
  final StatusFn status;
  bool _stop = false;

  WifiLoginEngine(this.settings, this.log, this.status);

  void stop() => _stop = true;
  void _checkStop() {
    if (_stop) throw StoppedException();
  }

  HttpClient _client() {
    final c = HttpClient();
    c.badCertificateCallback = (cert, host, port) => true; // FortiGate self-signed
    c.userAgent = _userAgent;
    c.connectionTimeout = const Duration(seconds: 5);
    return c;
  }

  Future<_Resp?> _get(String url, {bool follow = true}) async {
    _checkStop();
    final client = _client();
    client.autoUncompress = true;
    try {
      final req = await client.getUrl(Uri.parse(url));
      req.followRedirects = follow;
      req.maxRedirects = 10;
      final resp = await req.close().timeout(const Duration(seconds: 12));
      final body = await resp
          .transform(const SystemEncoding().decoder)
          .join()
          .timeout(const Duration(seconds: 12))
          .catchError((_) => '');
      return _Resp(resp.statusCode, body, resp.redirects, req.uri,
          resp.headers.value('location'));
    } catch (_) {
      return null;
    } finally {
      client.close(force: true);
    }
  }

  bool _looksLikePortal(Uri? u) {
    if (u == null) return false;
    final url = u.toString().toLowerCase();
    if (url.contains('fgtauth')) return true;
    final host = u.host;
    if (host.isNotEmpty &&
        (host == settings.portalHost || settings.knownHosts.contains(host))) {
      return true;
    }
    return _isPrivateIp(host);
  }

  String? _portalInPage(_Resp r) {
    for (final rd in r.redirects) {
      if (_looksLikePortal(rd.location)) return rd.location.toString();
    }
    if (_looksLikePortal(r.finalUri)) return r.finalUri.toString();
    final m = _fgtauthLink.firstMatch(r.body);
    if (m != null) return m.group(1)!.replaceAll('&amp;', '&');
    for (final rx in [_jsRedirect, _metaRefresh]) {
      final mm = rx.firstMatch(r.body);
      if (mm != null) {
        final target = r.finalUri.resolve(mm.group(1)!.replaceAll('&amp;', '&'));
        if (_looksLikePortal(target)) return target.toString();
      }
    }
    if (findLoginForm(r.body) != null) return r.finalUri.toString();
    return null;
  }

  /// Returns ('portal', url) / ('online', url) / ('offline', null).
  Future<MapEntry<String, String?>> _checkState() async {
    for (final url in settings.probeUrls) {
      _checkStop();
      log('Checking via $url ...');
      final r = await _get(url);
      if (r == null) {
        log('   $url -> no response');
        continue;
      }
      final portal = _portalInPage(r);
      if (portal != null) {
        log('   $url -> redirected to portal');
        return MapEntry('portal', portal);
      }
      log('   $url -> loaded normally (${r.statusCode})');
      return MapEntry('online', url);
    }
    return const MapEntry('offline', null);
  }

  Future<bool> _submitLogin(String portalUrl) async {
    final client = _client();
    try {
      _Resp? maybe = await _get(portalUrl);
      if (maybe == null) {
        log('   could not open portal page');
        return false;
      }
      _Resp r = maybe;
      var form = findPortalForm(r.body);
      for (var i = 0; i < 3 && form == null; i++) {
        final nxt = _forwardLink(r);
        if (nxt == null || nxt == r.finalUri.toString()) break;
        log('   portal forwards to $nxt');
        maybe = await _get(nxt);
        if (maybe == null) return false;
        r = maybe;
        form = findPortalForm(r.body);
      }
      if (form == null) {
        if (r.body.toLowerCase().contains('keepalive')) {
          log('   already authenticated');
          return true;
        }
        log('   portal page has no login form');
        return false;
      }

      final data = <String, String>{};
      String? userField, passField;
      var submitDone = false;
      for (final inp in form.inputs) {
        if (inp.name.isEmpty) continue;
        final t = inp.type;
        if (t == 'password' && passField == null) {
          passField = inp.name;
        } else if ((t == 'text' || t == 'email' || t == 'tel') &&
            userField == null) {
          userField = inp.name;
        } else if (t == 'hidden') {
          data[inp.name] = inp.value;
        } else if (t == 'checkbox') {
          data[inp.name] = inp.value.isEmpty ? 'on' : inp.value;
        } else if (t == 'radio' && (inp.checked || !data.containsKey(inp.name))) {
          data[inp.name] = inp.value;
        } else if ((t == 'submit' || t == 'image') && !submitDone) {
          data[inp.name] = inp.value;
          submitDone = true;
        }
      }

      final kind = passField != null ? 'login' : 'accept';
      if (passField != null) {
        if (settings.username.isEmpty || settings.password.isEmpty) {
          log('   portal needs a username/password but none is set');
          return false;
        }
        if (userField != null) data[userField] = settings.username;
        data[passField] = settings.password;
      } else {
        if (data.containsKey('answer')) data['answer'] = '1';
        if (userField != null) data[userField] = settings.username;
        log('   no password box - submitting as accept/continue');
      }

      final action = r.finalUri.resolve(form.action.isEmpty ? r.finalUri.toString() : form.action);
      _checkStop();
      try {
        final req = form.method == 'post'
            ? await client.postUrl(action)
            : await client.getUrl(_withQuery(action, data));
        req.followRedirects = false;
        if (form.method == 'post') {
          final body = data.entries
              .map((e) =>
                  '${Uri.encodeQueryComponent(e.key)}=${Uri.encodeQueryComponent(e.value)}')
              .join('&');
          req.headers.contentType =
              ContentType('application', 'x-www-form-urlencoded');
          req.write(body);
        }
        final resp = await req.close().timeout(const Duration(seconds: 15));
        final text = (await resp
                .transform(const SystemEncoding().decoder)
                .join()
                .catchError((_) => ''))
            .toLowerCase();
        if (resp.statusCode >= 300 && resp.statusCode < 400) {
          final loc = (resp.headers.value('location') ?? '').toLowerCase();
          if (loc.contains('fgtauth') && !loc.contains('keepalive')) {
            log('   portal sent us back to login - not accepted');
            return false;
          }
          log('   portal accepted the login');
          return true;
        }
        if (text.contains('keepalive')) {
          log('   portal accepted the login (keepalive)');
          return true;
        }
        if (kind == 'login' && findLoginForm(text) != null) {
          log('   portal rejected the login');
          return false;
        }
        log('   login submitted - verifying');
        return true;
      } catch (_) {
        log('   submit dropped the connection - will verify');
        return true;
      }
    } finally {
      client.close(force: true);
    }
  }

  String? _forwardLink(_Resp r) {
    final m = _fgtauthLink.firstMatch(r.body);
    if (m != null) return m.group(1)!.replaceAll('&amp;', '&');
    for (final rx in [_jsRedirect, _metaRefresh]) {
      final mm = rx.firstMatch(r.body);
      if (mm != null) {
        return r.finalUri.resolve(mm.group(1)!.replaceAll('&amp;', '&')).toString();
      }
    }
    return null;
  }

  Uri _withQuery(Uri base, Map<String, String> data) {
    final q = Map<String, String>.from(base.queryParameters)..addAll(data);
    return base.replace(queryParameters: q);
  }

  Future<bool> _confirmConnected() async {
    if (settings.successUrl.isEmpty) {
      final r = await _get('http://www.msftconnecttest.com/connecttest.txt',
          follow: false);
      final ok = r != null &&
          r.statusCode == 200 &&
          r.body.trim() == 'Microsoft Connect Test';
      if (ok) log('CONNECTED - internet works');
      return ok;
    }
    final r = await _get(settings.successUrl);
    if (r == null) return false;
    if (_portalInPage(r) != null) {
      log('   still goes to the portal - not connected yet');
      return false;
    }
    if (r.statusCode >= 400) return false;
    if (settings.successText.isNotEmpty) {
      final body = r.body.toLowerCase();
      final found = settings.successText
          .where((t) => body.contains(t.toLowerCase()))
          .toList();
      if (found.isEmpty) {
        log('   ${settings.successUrl} opened but not the real landing page yet');
        return false;
      }
      log("CONNECTED - landing page loaded (found '${found.first}')");
    } else {
      log('CONNECTED - ${settings.successUrl} opened for real');
    }
    return true;
  }

  /// Keeps trying until online, or until maxAttempts. Returns true when connected.
  Future<bool> run({bool watch = false}) async {
    _stop = false;
    try {
      do {
        final ok = await _connectOnce();
        if (ok && !watch) return true;
        if (!watch) return ok;
        await _wait(settings.watchInterval);
      } while (watch && !_stop);
    } on StoppedException {
      log('Stopped.');
      status('stopped', '');
    }
    return false;
  }

  Future<bool> _connectOnce() async {
    var attempts = 0;
    while (true) {
      _checkStop();
      final state = await _checkState();
      if (state.key == 'online') {
        if (await _confirmConnected()) {
          status('connected', attempts > 0 ? 'after $attempts attempt(s)' : '');
          return true;
        }
      } else if (state.key == 'portal') {
        attempts++;
        final url = state.value!;
        final u = Uri.parse(url);
        settings.knownHosts.add(u.host);
        status('logging in', 'attempt $attempts');
        for (var i = 1; i <= settings.loginRetries; i++) {
          _checkStop();
          log('Attempt $attempts: logging in (login $i/${settings.loginRetries})');
          final ok = await _submitLogin(url);
          await _wait(1.5);
          if (ok && await _confirmConnected()) {
            status('connected', 'after $attempts attempt(s)');
            return true;
          }
          log(ok ? '   still not through after login' : '   login not accepted');
          if (i < settings.loginRetries) await _wait(settings.delay);
        }
      } else {
        attempts++;
        status('offline', 'attempt $attempts');
        log('Attempt $attempts: no site or portal reachable');
      }

      if (settings.maxAttempts > 0 && attempts >= settings.maxAttempts) {
        log('Giving up after $attempts attempts.');
        status('failed', 'gave up after $attempts attempts');
        return false;
      }
      await _wait(settings.delay);
    }
  }

  Future<void> _wait(double seconds) async {
    final end = DateTime.now().add(
        Duration(milliseconds: (seconds * 1000).round()));
    while (DateTime.now().isBefore(end)) {
      _checkStop();
      await Future.delayed(const Duration(milliseconds: 200));
    }
  }
}

class _Resp {
  final int statusCode;
  final String body;
  final List<RedirectInfo> redirects;
  final Uri finalUri;
  final String? location;
  _Resp(this.statusCode, this.body, this.redirects, this.finalUri, this.location);
}
