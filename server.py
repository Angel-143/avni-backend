package com.example.avni;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import androidx.core.app.NotificationCompat;

import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;

import java.net.URI;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.UUID;

public class AudioForegroundService extends Service {

    private static final String TAG = "AvniService";
    private static final String CHANNEL_ID = "avni_channel";
    private static final int NOTIF_ID = 1001;

    // ── Render Server URL ─────────────────────────────────────────────────────
    private static final String WS_BASE = "wss://avni-backend.onrender.com";
    // ─────────────────────────────────────────────────────────────────────────

    private static final int SAMPLE_RATE = 16000;

    private WebSocketClient wsClient;
    private AudioRecord audioRecord;
    private Thread recordThread;
    private volatile boolean isRecording = false;
    private volatile boolean shouldRun = true;
    private String deviceId = "unknown";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private Runnable reconnectRunnable;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        startForeground(NOTIF_ID, buildNotification("Avni Security", "Starting..."));
        deviceId = loadDeviceId();
        connectWebSocket();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (wsClient == null || !wsClient.isOpen()) {
            connectWebSocket();
        }
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        restartService();
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        shouldRun = false;
        stopMic();
        closeWs();
        handler.postDelayed(this::restartService, 1000);
        super.onDestroy();
    }

    private void restartService() {
        Intent restart = new Intent(getApplicationContext(), AudioForegroundService.class);
        restart.setPackage(getPackageName());
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(restart);
        } else {
            startService(restart);
        }
    }

    // ── WebSocket ─────────────────────────────────────────────────────────────
    private void connectWebSocket() {
        if (!shouldRun) return;
        if (reconnectRunnable != null) {
            handler.removeCallbacks(reconnectRunnable);
            reconnectRunnable = null;
        }
        closeWs();

        try {
            URI uri = new URI(WS_BASE + "/ws/audio/" + deviceId);
            Log.d(TAG, "Connecting: " + uri);

            wsClient = new WebSocketClient(uri) {
                @Override
                public void onOpen(ServerHandshake h) {
                    Log.d(TAG, "WS connected!");
                    updateNotification("Avni Security", "Connected — waiting for command");
                }

                @Override
                public void onMessage(String message) {
                    Log.d(TAG, "CMD: " + message);
                    if (message.contains("START_MIC")) startMic();
                    else if (message.contains("STOP_MIC")) stopMic();
                }

                @Override
                public void onMessage(ByteBuffer bytes) {}

                @Override
                public void onClose(int code, String reason, boolean remote) {
                    Log.d(TAG, "WS closed: " + reason);
                    stopMic();
                    updateNotification("Avni Security", "Reconnecting...");
                    scheduleReconnect(3000);
                }

                @Override
                public void onError(Exception ex) {
                    Log.e(TAG, "WS error: " + (ex != null ? ex.getMessage() : "unknown"));
                    scheduleReconnect(3000);
                }
            };
            wsClient.setConnectionLostTimeout(20);
            wsClient.connect();

        } catch (Exception e) {
            Log.e(TAG, "Connect error: " + e.getMessage());
            scheduleReconnect(3000);
        }
    }

    private void scheduleReconnect(long ms) {
        if (!shouldRun) return;
        if (reconnectRunnable != null) handler.removeCallbacks(reconnectRunnable);
        reconnectRunnable = () -> { if (shouldRun) connectWebSocket(); };
        handler.postDelayed(reconnectRunnable, ms);
    }

    private void closeWs() {
        if (wsClient != null) {
            try { wsClient.close(); } catch (Exception e) {}
            wsClient = null;
        }
    }

    // ── Mic ───────────────────────────────────────────────────────────────────
    private void startMic() {
        if (isRecording) return;
        Log.d(TAG, "Starting mic");
        updateNotification("Avni Security", "🔴 Mic active — streaming");

        try {
            int bufSize = AudioRecord.getMinBufferSize(SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);

            audioRecord = new AudioRecord(MediaRecorder.AudioSource.MIC,
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, bufSize * 4);

            if (audioRecord.getState() != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord init failed");
                audioRecord.release(); audioRecord = null; return;
            }

            audioRecord.startRecording();
            isRecording = true;

            final int fBufSize = bufSize;
            recordThread = new Thread(() -> {
                byte[] buf = new byte[fBufSize];
                while (isRecording) {
                    int read = audioRecord.read(buf, 0, buf.length);
                    if (read > 0 && wsClient != null && wsClient.isOpen()) {
                        try {
                            wsClient.send(Arrays.copyOf(buf, read));
                        } catch (Exception e) {
                            Log.e(TAG, "Send error: " + e.getMessage());
                        }
                    }
                }
            });
            recordThread.setDaemon(true);
            recordThread.start();

        } catch (Exception e) {
            Log.e(TAG, "Mic error: " + e.getMessage());
        }
    }

    private void stopMic() {
        if (!isRecording) return;
        isRecording = false;
        if (audioRecord != null) {
            try { audioRecord.stop(); audioRecord.release(); } catch (Exception e) {}
            audioRecord = null;
        }
        updateNotification("Avni Security", "Connected — waiting for command");
    }

    // ── Notification ──────────────────────────────────────────────────────────
    private Notification buildNotification(String title, String text) {
        Intent intent = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, intent,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title).setContentText(text)
            .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off)
            .setContentIntent(pi).setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW).setSilent(true).build();
    }

    private void updateNotification(String title, String text) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) nm.notify(NOTIF_ID, buildNotification(title, text));
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "Avni Audio", NotificationManager.IMPORTANCE_LOW);
            ch.setSound(null, null); ch.enableVibration(false);
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.createNotificationChannel(ch);
        }
    }

    private String loadDeviceId() {
        android.content.SharedPreferences p = getSharedPreferences("avni_prefs", MODE_PRIVATE);
        String id = p.getString("device_id", null);
        if (id == null) {
            id = UUID.randomUUID().toString();
            p.edit().putString("device_id", id).apply();
        }
        return id;
    }
}
