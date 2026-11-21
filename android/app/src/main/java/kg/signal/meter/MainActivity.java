package kg.signal.meter;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.telephony.CellSignalStrength;
import android.telephony.CellSignalStrengthCdma;
import android.telephony.CellSignalStrengthGsm;
import android.telephony.CellSignalStrengthLte;
import android.telephony.CellSignalStrengthNr;
import android.telephony.CellSignalStrengthTdscdma;
import android.telephony.CellSignalStrengthWcdma;
import android.telephony.SignalStrength;
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
 * (весь интерфейс как на сайте), а нативный слой отдаёт странице реальный dBm модема —
 * этого веб-страница сама сделать не может. Геолокацию берёт сам WebView.
 */
public class MainActivity extends Activity {

    private static final String APP_URL = "https://tolkun.online/app/";
    private static final int REQ_PERMS = 1;

    private WebView web;

    @Override
    @SuppressWarnings("SetJavaScriptEnabled")
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setGeolocationEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);

        // Навигацию держим внутри приложения.
        web.setWebViewClient(new WebViewClient());
        // Разрешаем странице геолокацию (если приложению дано разрешение системой).
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                callback.invoke(origin, true, false);
            }
        });
        // Мост: страница читает реальный dBm через window.AndroidSignal.read().
        web.addJavascriptInterface(new SignalBridge(), "AndroidSignal");

        setContentView(web);

        if (!hasLocation()) {
            requestPermissions(new String[]{
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.READ_PHONE_STATE}, REQ_PERMS);
        }
        web.loadUrl(APP_URL);
    }

    private boolean hasLocation() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        // После выдачи разрешения перезагружаем — геолокация и dBm заработают.
        if (requestCode == REQ_PERMS) web.reload();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    /** Нативный мост: реальный уровень сигнала модема в dBm и тип сети. */
    public class SignalBridge {
        @JavascriptInterface
        public String read() {
            try {
                TelephonyManager tm = (TelephonyManager) getSystemService(TELEPHONY_SERVICE);
                if (tm == null) return "{}";
                SignalStrength ss = tm.getSignalStrength();
                if (ss == null) return "{}";
                List<CellSignalStrength> cells = ss.getCellSignalStrengths();
                for (CellSignalStrength c : cells) {
                    int dbm = c.getDbm();
                    if (dbm == Integer.MAX_VALUE || dbm >= 0) continue; // недоступно/мусор
                    String net = networkOf(c);
                    return "{\"dbm\":" + dbm + (net != null ? ",\"network\":\"" + net + "\"" : "") + "}";
                }
                return "{}";
            } catch (Exception e) {
                return "{}";
            }
        }

        private String networkOf(CellSignalStrength c) {
            if (c instanceof CellSignalStrengthNr) return "5G";
            if (c instanceof CellSignalStrengthLte) return "4G";
            if (c instanceof CellSignalStrengthWcdma || c instanceof CellSignalStrengthTdscdma) return "3G";
            if (c instanceof CellSignalStrengthGsm || c instanceof CellSignalStrengthCdma) return "2G";
            return null;
        }
    }
}
