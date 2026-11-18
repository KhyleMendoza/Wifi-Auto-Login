package com.lnk.wifi_auto_login

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder

/// A foreground service that keeps the app alive in the background while it
/// watches the Wi-Fi and re-logs in. The login loop itself runs in Dart; this
/// just holds the process up and shows the ongoing notification.
class LoginService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val text = intent?.getStringExtra("text") ?: "Keeping you logged in"
        startForeground(NOTIF_ID, buildNotification(text))
        return START_STICKY
    }

    private fun buildNotification(text: String): Notification {
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(CHANNEL, "WiFi Auto Login",
                NotificationManager.IMPORTANCE_LOW)
            ch.setShowBadge(false)
            mgr.createNotificationChannel(ch)
        }
        val launch = packageManager.getLaunchIntentForPackage(packageName)
        val pi = PendingIntent.getActivity(this, 0, launch,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            Notification.Builder(this, CHANNEL) else @Suppress("DEPRECATION") Notification.Builder(this)
        return builder
            .setContentTitle("WiFi Auto Login")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth.let { _ ->
                applicationInfo.icon
            })
            .setOngoing(true)
            .setContentIntent(pi)
            .build()
    }

    companion object {
        private const val CHANNEL = "wifi_auto_login"
        private const val NOTIF_ID = 1

        fun start(context: Context, text: String) {
            val i = Intent(context, LoginService::class.java).putExtra("text", text)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(i)
            } else {
                context.startService(i)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, LoginService::class.java))
        }
    }
}
