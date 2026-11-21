package kg.signal.meter;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.telephony.CellInfo;
import android.telephony.CellInfoGsm;
import android.telephony.CellInfoLte;
import android.telephony.CellInfoNr;
import android.telephony.CellInfoWcdma;
import android.telephony.CellSignalStrength;
import android.telephony.CellSignalStrengthGsm;
import android.telephony.CellSignalStrengthLte;
import android.telephony.CellSignalStrengthNr;
import android.telephony.CellSignalStrengthTdscdma;
import android.telephony.CellSignalStrengthWcdma;
import android.telephony.PhoneStateListener;
import android.telephony.SignalStrength;
import android.telephony.TelephonyCallback;
import android.telephony.TelephonyManager;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.util.List;

/**
 * Tolkun для Android: открывает мобильное приложение tolkun.online/app/ во WebView
 * (весь интерфейс как на сайте), а нативный слой автоматически отдаёт странице реальный
 * уровень сигнала модема в дБм — этого веб-страница сама сделать не может.
 *
 * Как берётся сигнал (по порядку, до первого валидного значения):
 *  1. последнее значение из подписки на изменения сигнала (TelephonyCallback / PhoneStateListener) —
 *     на многих телефонах getSignalStrength() пуст, пока приложение не подписалось;
 *  2. текущее TelephonyManager.getSignalStrength();
 *  3. данные обслуживающей вышки из getAllCellInfo() (нужна геолокация).
 * Для 4G берётся RSRP — та же шкала, что в замерах оператора.
 */
public class MainActivity extends Activity {

    private static final String APP_URL = "https://tolkun.online/app/";
    private static final int REQ_PERMS = 1;
    private static final int UNAVAILABLE = Integer.MAX_VALUE;

    private WebView web;
    private TelephonyManager tm;
    private volatile SignalStrength lastStrength;
    private volatile long lastStrengthAt;
    private Object listener;  // TelephonyCallback (Android 12+) или PhoneStateListener

    @Override
    @SuppressWarnings("SetJavaScriptEnabled")
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        tm = (TelephonyManager) getSystemService(TELEPHONY_SERVICE);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setGeolocationEnabled(true);

        // Навигацию держим внутри приложения.
        web.setWebViewClient(new WebViewClient());
        // Разрешаем странице геолокацию (если приложению дано разрешение системой).
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                callback.invoke(origin, true, false);
            }
        });
        // Мост: страница читает реальный дБм через window.AndroidSignal.read().
        web.addJavascriptInterface(new SignalBridge(), "AndroidSignal");
        setContentView(web);

        if (!hasPermissions()) {
            // На Android 12+ точную геолокацию можно просить только вместе с приблизительной —
            // запрос одной ACCESS_FINE_LOCATION система молча игнорирует.
            requestPermissions(new String[]{
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION,
                    Manifest.permission.READ_PHONE_STATE}, REQ_PERMS);
        }
        web.loadUrl(APP_URL);
    }

    private boolean granted(String permission) {
        return checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasPermissions() {
        return (granted(Manifest.permission.ACCESS_FINE_LOCATION) || granted(Manifest.permission.ACCESS_COARSE_LOCATION))
                && granted(Manifest.permission.READ_PHONE_STATE);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        if (requestCode == REQ_PERMS) {
            startListening();
            web.reload();  // геолокация и сигнал заработают после выдачи разрешений
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        startListening();
    }

    @Override
    protected void onPause() {
        stopListening();
        super.onPause();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    /* --- подписка на изменения сигнала --- */

    private void onStrength(SignalStrength strength) {
        lastStrength = strength;
        lastStrengthAt = System.currentTimeMillis();
    }

    private void startListening() {
        if (tm == null || listener != null) return;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                SignalCallback callback = new SignalCallback();
                tm.registerTelephonyCallback(getMainExecutor(), callback);
                listener = callback;
            } else {
                LegacyListener legacy = new LegacyListener();
                tm.listen(legacy, PhoneStateListener.LISTEN_SIGNAL_STRENGTHS);
                listener = legacy;
            }
        } catch (RuntimeException e) {
            listener = null;  // без подписки читаем текущее значение напрямую
        }
    }

    private void stopListening() {
        if (tm == null || listener == null) return;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && listener instanceof TelephonyCallback) {
                tm.unregisterTelephonyCallback((TelephonyCallback) listener);
            } else if (listener instanceof PhoneStateListener) {
                tm.listen((PhoneStateListener) listener, PhoneStateListener.LISTEN_NONE);
            }
        } catch (RuntimeException ignored) {
        }
        listener = null;
    }

    /** Android 12+: TelephonyCallback. Класс загружается только на этих версиях. */
    private class SignalCallback extends TelephonyCallback implements TelephonyCallback.SignalStrengthsListener {
        @Override
        public void onSignalStrengthsChanged(SignalStrength signalStrength) {
            onStrength(signalStrength);
        }
    }

    /** Android 10–11: PhoneStateListener. */
    @SuppressWarnings("deprecation")
    private class LegacyListener extends PhoneStateListener {
        @Override
        public void onSignalStrengthsChanged(SignalStrength signalStrength) {
            onStrength(signalStrength);
        }
    }

    /* --- чтение уровня сигнала --- */

    private static boolean valid(int dbm) {
        return dbm != UNAVAILABLE && dbm >= -140 && dbm <= -44;
    }

    /** Значение из SignalStrength: сначала 4G (RSRP), затем 5G, 3G, 2G. Null — валидного нет. */
    private static int[] fromStrength(SignalStrength strength) {
        if (strength == null) return null;
        List<CellSignalStrength> cells = strength.getCellSignalStrengths();
        int[] best = null;
        for (CellSignalStrength c : cells) {
            int dbm;
            int rank;
            if (c instanceof CellSignalStrengthLte) {
                dbm = ((CellSignalStrengthLte) c).getRsrp();
                rank = 0;
            } else if (c instanceof CellSignalStrengthNr) {
                dbm = ((CellSignalStrengthNr) c).getSsRsrp();
                rank = 1;
            } else if (c instanceof CellSignalStrengthWcdma || c instanceof CellSignalStrengthTdscdma) {
                dbm = c.getDbm();
                rank = 2;
            } else if (c instanceof CellSignalStrengthGsm) {
                dbm = c.getDbm();
                rank = 3;
            } else {
                dbm = c.getDbm();
                rank = 4;
            }
            if (!valid(dbm)) continue;
            if (best == null || rank < best[1]) best = new int[]{dbm, rank};
        }
        return best;
    }

    /** Обслуживающая вышка из getAllCellInfo(): нужна геолокация. */
    private int[] fromCellInfo() {
        if (!granted(Manifest.permission.ACCESS_FINE_LOCATION)) return null;
        List<CellInfo> infos;
        try {
            infos = tm.getAllCellInfo();
        } catch (SecurityException e) {
            return null;
        }
        if (infos == null) return null;
        int[] best = null;
        for (CellInfo info : infos) {
            if (!info.isRegistered()) continue;
            int dbm;
            int rank;
            if (info instanceof CellInfoLte) {
                dbm = ((CellInfoLte) info).getCellSignalStrength().getRsrp();
                rank = 0;
            } else if (info instanceof CellInfoNr) {
                CellSignalStrength nr = ((CellInfoNr) info).getCellSignalStrength();
                dbm = nr instanceof CellSignalStrengthNr ? ((CellSignalStrengthNr) nr).getSsRsrp() : nr.getDbm();
                rank = 1;
            } else if (info instanceof CellInfoWcdma) {
                dbm = ((CellInfoWcdma) info).getCellSignalStrength().getDbm();
                rank = 2;
            } else if (info instanceof CellInfoGsm) {
                dbm = ((CellInfoGsm) info).getCellSignalStrength().getDbm();
                rank = 3;
            } else {
                continue;
            }
            if (!valid(dbm)) continue;
            if (best == null || rank < best[1]) best = new int[]{dbm, rank};
        }
        return best;
    }

    private static final String[] NETWORKS = {"4G", "5G", "3G", "2G", null};

    private static String json(int[] value, String source) {
        String net = NETWORKS[value[1]];
        return "{\"dbm\":" + value[0] + (net != null ? ",\"network\":\"" + net + "\"" : "")
                + ",\"source\":\"" + source + "\"}";
    }

    /** Нативный мост для страницы: реальный уровень сигнала модема или понятная причина, почему его нет. */
    public class SignalBridge {
        @JavascriptInterface
        public String read() {
            try {
                if (tm == null) return "{\"error\":\"no_telephony\"}";
                if (tm.getSimState() != TelephonyManager.SIM_STATE_READY) return "{\"error\":\"no_sim\"}";
                // Свежее значение из подписки (не старше 30 с).
                if (lastStrength != null && System.currentTimeMillis() - lastStrengthAt < 30000) {
                    int[] v = fromStrength(lastStrength);
                    if (v != null) return json(v, "listener");
                }
                int[] now = fromStrength(tm.getSignalStrength());
                if (now != null) return json(now, "now");
                int[] cell = fromCellInfo();
                if (cell != null) return json(cell, "cellinfo");
                if (!hasPermissions()) return "{\"error\":\"no_permission\"}";
                return "{\"error\":\"no_signal\"}";
            } catch (Exception e) {
                return "{\"error\":\"failed\"}";
            }
        }
    }
}
