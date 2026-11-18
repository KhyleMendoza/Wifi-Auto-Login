import 'package:flutter/material.dart';
import 'package:permission_handler/permission_handler.dart';

import 'engine.dart';
import 'native.dart';
import 'settings.dart';
import 'userlist.dart';

void main() => runApp(const WifiAutoLoginApp());

class WifiAutoLoginApp extends StatelessWidget {
  const WifiAutoLoginApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'WiFi Auto Login',
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFF1e6fd9),
        useMaterial3: true,
        brightness: Brightness.light,
      ),
      darkTheme: ThemeData(
        colorSchemeSeed: const Color(0xFF5aa0ff),
        useMaterial3: true,
        brightness: Brightness.dark,
      ),
      home: const HomePage(),
    );
  }
}

const defaultLabel = 'Default (any other Wi-Fi)';
// Address of the office download server that hosts the user list.
const defaultServer = 'http://192.168.210.112:3030';

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final store = Store();
  final userCtrl = TextEditingController();
  final passCtrl = TextEditingController();
  final portalCtrl = TextEditingController();
  final log = <String>[];
  final scrollCtrl = ScrollController();

  String curProfile = '';
  String ssid = '';
  String statusText = 'Idle';
  Color statusColor = Colors.grey;
  bool running = false;
  bool watch = false;
  bool showPass = false;
  WifiLoginEngine? engine;
  final List<DateTime> _headerTaps = [];

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    await store.load();
    watch = store.watchMode;
    _showProfile('');
    await _refreshSsid();
    setState(() {});
  }

  Future<void> _refreshSsid() async {
    await Permission.locationWhenInUse.request();
    final s = await Native.currentSsid();
    setState(() {
      ssid = s;
      if (s.isNotEmpty && store.profiles.containsKey(s)) {
        _showProfile(s);
      }
    });
  }

  void _stashProfile() {
    final p = store.profiles[curProfile] ?? Profile();
    p.username = userCtrl.text.trim();
    p.password = passCtrl.text;
    p.portalUrl = portalCtrl.text.trim();
    store.profiles[curProfile] = p;
  }

  void _showProfile(String name) {
    curProfile = name;
    final p = store.profileFor(name);
    userCtrl.text = p.username;
    passCtrl.text = p.password;
    portalCtrl.text = p.portalUrl;
  }

  void _addLog(String line) {
    final stamped = '[${TimeOfDay.now().format(context)}] $line';
    setState(() {
      log.add(stamped);
      if (log.length > 500) log.removeRange(0, log.length - 500);
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (scrollCtrl.hasClients) {
        scrollCtrl.jumpTo(scrollCtrl.position.maxScrollExtent);
      }
    });
  }

  void _setStatus(String state, String detail) {
    const colors = {
      'checking': Color(0xFF2f71b2),
      'logging in': Color(0xFFc97a00),
      'offline': Color(0xFFc0392b),
      'connected': Color(0xFF1e8e3e),
      'failed': Color(0xFFc0392b),
      'stopped': Colors.grey,
    };
    setState(() {
      statusText = _cap(state) + (detail.isNotEmpty ? '  —  $detail' : '');
      statusColor = colors[state] ?? Colors.grey;
    });
    if (running) Native.updateService(statusText);
  }

  String _cap(String s) => s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);

  Future<void> _start() async {
    if (running) return;
    _stashProfile();
    await store.save();
    await Permission.notification.request();

    setState(() {
      running = true;
      statusText = 'Starting...';
    });
    await Native.startService('Starting...');
    final bound = await Native.bindToWifi();
    _addLog(bound
        ? 'Using Wi-Fi for this app.'
        : 'Could not lock onto Wi-Fi - will still try.');

    ssid = await Native.currentSsid();
    final s = store.settingsFor(ssid);
    _addLog('Using settings "${curProfileLabel(ssid)}": '
        'user=${s.username.isEmpty ? "(none)" : s.username}  '
        'portal=${s.portalUrl.isEmpty ? "auto" : s.portalUrl}');

    final e = WifiLoginEngine(s, _addLog, _setStatus);
    engine = e;
    final ok = await e.run(watch: watch);
    engine = null;
    await Native.unbind();
    await Native.stopService();
    setState(() => running = false);
    if (!watch && !ok && !statusText.startsWith('Stopped')) {
      _setStatus('offline', 'not connected');
    }
  }

  String curProfileLabel(String s) =>
      (s.isNotEmpty && store.profiles.containsKey(s)) ? s : 'Default';

  void _stop() {
    engine?.stop();
    setState(() => statusText = 'Stopping...');
  }

  // ---- hidden user picker (5 taps on the Login header) ----
  void _headerTap() {
    final now = DateTime.now();
    _headerTaps.removeWhere((t) => now.difference(t).inSeconds >= 3);
    _headerTaps.add(now);
    if (_headerTaps.length >= 5) {
      _headerTaps.clear();
      _openUserPicker();
    }
  }

  Future<void> _openUserPicker() async {
    final pass = await _askPassword();
    if (pass == null) return;
    _addLog('Fetching user list...');
    Map<String, String> users;
    try {
      users = await UserList.fetch(defaultServer, pass);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$e')));
      }
      return;
    }
    if (!mounted) return;
    final picked = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) => _UserSheet(users: users),
    );
    if (picked != null) {
      setState(() {
        userCtrl.text = picked;
        passCtrl.text = users[picked] ?? '';
        showPass = false;
      });
    }
  }

  Future<String?> _askPassword() async {
    final ctrl = TextEditingController();
    return showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Password'),
        content: TextField(
          controller: ctrl,
          obscureText: true,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'Password'),
          onSubmitted: (v) => Navigator.pop(ctx, v),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, ctrl.text),
              child: const Text('OK')),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('WiFi Auto Login')),
      body: ListView(
        padding: const EdgeInsets.all(14),
        controller: scrollCtrl,
        children: [
          Row(children: [
            Icon(Icons.circle, color: statusColor, size: 18),
            const SizedBox(width: 8),
            Expanded(
                child: Text(statusText,
                    style: const TextStyle(
                        fontSize: 18, fontWeight: FontWeight.bold))),
          ]),
          const SizedBox(height: 8),
          Card(
            child: ListTile(
              title: Text(ssid.isEmpty ? 'Not on Wi-Fi' : 'Wi-Fi: $ssid'),
              subtitle: Text('Settings used: ${curProfileLabel(ssid)}'),
              trailing: IconButton(
                  icon: const Icon(Icons.refresh), onPressed: _refreshSsid),
            ),
          ),
          const SizedBox(height: 6),
          GestureDetector(
            onTap: _headerTap,
            child: const Padding(
              padding: EdgeInsets.symmetric(vertical: 6),
              child: Text('Login for each Wi-Fi',
                  style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
            ),
          ),
          _profileDropdown(),
          TextField(
            controller: userCtrl,
            decoration: const InputDecoration(labelText: 'Username'),
          ),
          TextField(
            controller: passCtrl,
            obscureText: !showPass,
            decoration: InputDecoration(
              labelText: 'Password',
              suffixIcon: IconButton(
                icon: Icon(showPass ? Icons.visibility_off : Icons.visibility),
                onPressed: () => setState(() => showPass = !showPass),
              ),
            ),
          ),
          TextField(
            controller: portalCtrl,
            decoration: const InputDecoration(
                labelText: 'Portal address (blank = find it)'),
          ),
          const SizedBox(height: 10),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Watch mode (keep me logged in)'),
            value: watch,
            onChanged: running
                ? null
                : (v) => setState(() {
                      watch = v;
                      store.watchMode = v;
                    }),
          ),
          Row(children: [
            Expanded(
              child: FilledButton.icon(
                icon: Icon(running ? Icons.stop : Icons.play_arrow),
                label: Text(running ? 'Stop' : 'Start'),
                onPressed: running ? _stop : _start,
              ),
            ),
          ]),
          const Divider(height: 24),
          const Text('Log', style: TextStyle(fontWeight: FontWeight.bold)),
          Container(
            height: 220,
            margin: const EdgeInsets.only(top: 6),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(
              color: Theme.of(context).colorScheme.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(8),
            ),
            child: ListView.builder(
              itemCount: log.length,
              itemBuilder: (_, i) => Text(log[i],
                  style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
            ),
          ),
        ],
      ),
    );
  }

  Widget _profileDropdown() {
    final names = <String>[''];
    for (final n in store.profiles.keys) {
      if (n.isNotEmpty && !names.contains(n)) names.add(n);
    }
    if (ssid.isNotEmpty && !names.contains(ssid)) names.add(ssid);
    return Row(children: [
      const Text('Settings for '),
      Expanded(
        child: DropdownButton<String>(
          isExpanded: true,
          value: curProfile,
          items: names
              .map((n) => DropdownMenuItem(
                  value: n, child: Text(n.isEmpty ? defaultLabel : n)))
              .toList(),
          onChanged: (v) {
            if (v == null || v == curProfile) return;
            _stashProfile();
            if (!store.profiles.containsKey(v)) {
              final copy = Profile(
                username: store.defaultProfile.username,
                password: store.defaultProfile.password,
              );
              store.profiles[v] = copy;
            }
            setState(() => _showProfile(v));
          },
        ),
      ),
    ]);
  }
}

class _UserSheet extends StatefulWidget {
  final Map<String, String> users;
  const _UserSheet({required this.users});
  @override
  State<_UserSheet> createState() => _UserSheetState();
}

class _UserSheetState extends State<_UserSheet> {
  String query = '';
  @override
  Widget build(BuildContext context) {
    final names = widget.users.keys.toList()
      ..sort((a, b) => a.toLowerCase().compareTo(b.toLowerCase()));
    final shown =
        names.where((n) => n.toLowerCase().contains(query.toLowerCase())).toList();
    return Padding(
      padding: EdgeInsets.only(
          bottom: MediaQuery.of(context).viewInsets.bottom,
          left: 12,
          right: 12,
          top: 12),
      child: SizedBox(
        height: MediaQuery.of(context).size.height * 0.6,
        child: Column(children: [
          TextField(
            autofocus: true,
            decoration: const InputDecoration(
                prefixIcon: Icon(Icons.search), hintText: 'Search user'),
            onChanged: (v) => setState(() => query = v),
          ),
          Expanded(
            child: ListView.builder(
              itemCount: shown.length,
              itemBuilder: (_, i) => ListTile(
                title: Text(shown[i]),
                onTap: () => Navigator.pop(context, shown[i]),
              ),
            ),
          ),
        ]),
      ),
    );
  }
}
