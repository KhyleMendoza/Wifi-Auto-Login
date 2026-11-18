import 'dart:convert';
import 'dart:io';

/// Downloads the hidden user list from the office download server. The list is
/// never bundled in the APK; it is fetched with the admin password, exactly like
/// the "with user list" download on the web page.
class UserList {
  /// Returns {username: password}, or throws with a readable message.
  static Future<Map<String, String>> fetch(String serverUrl, String password) async {
    final base = serverUrl.trim().replaceAll(RegExp(r'/+$'), '');
    final url = Uri.parse('$base/download/users.json');
    final client = HttpClient()
      ..badCertificateCallback = ((cert, host, port) => true)
      ..connectionTimeout = const Duration(seconds: 8);
    try {
      final req = await client.getUrl(url);
      req.headers.set('X-Admin-Key', password);
      final resp = await req.close().timeout(const Duration(seconds: 12));
      if (resp.statusCode == 403) {
        throw 'Wrong password.';
      }
      if (resp.statusCode != 200) {
        throw 'Server returned ${resp.statusCode}.';
      }
      final body = await resp.transform(utf8.decoder).join();
      final map = (jsonDecode(body) as Map).cast<String, dynamic>();
      return map.map((k, v) => MapEntry(k, v.toString()));
    } on SocketException {
      throw 'Could not reach $base.';
    } finally {
      client.close(force: true);
    }
  }
}
