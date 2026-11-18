package com.lnk.wifi_auto_login

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var boundCallback: ConnectivityManager.NetworkCallback? = null

    private val cm by lazy { getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "wifi_auto_login/native")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "bindWifi" -> bindWifi(result)
                    "unbind" -> {
                        unbind()
                        result.success(true)
                    }
                    "ssid" -> result.success(currentSsid())
                    "startService" -> {
                        LoginService.start(this, call.argument<String>("text") ?: "Running")
                        result.success(true)
                    }
                    "updateService" -> {
                        LoginService.start(this, call.argument<String>("text") ?: "Running")
                        result.success(true)
                    }
                    "stopService" -> {
                        LoginService.stop(this)
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
    }

    private fun bindWifi(result: MethodChannel.Result) {
        unbind()
        val req = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            var answered = false
            override fun onAvailable(network: Network) {
                cm.bindProcessToNetwork(network)
                if (!answered) {
                    answered = true
                    result.success(true)
                }
            }
            override fun onUnavailable() {
                if (!answered) {
                    answered = true
                    result.success(false)
                }
            }
        }
        boundCallback = cb
        try {
            cm.requestNetwork(req, cb)
        } catch (e: Exception) {
            result.success(false)
        }
    }

    private fun unbind() {
        try {
            cm.bindProcessToNetwork(null)
        } catch (_: Exception) {}
        boundCallback?.let {
            try {
                cm.unregisterNetworkCallback(it)
            } catch (_: Exception) {}
        }
        boundCallback = null
    }

    @Suppress("DEPRECATION")
    private fun currentSsid(): String {
        return try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val ssid = wm.connectionInfo.ssid ?: ""
            ssid.trim('"').let { if (it == "<unknown ssid>") "" else it }
        } catch (e: Exception) {
            ""
        }
    }

    override fun onDestroy() {
        unbind()
        super.onDestroy()
    }
}
