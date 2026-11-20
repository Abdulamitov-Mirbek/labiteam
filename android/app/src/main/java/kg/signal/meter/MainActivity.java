package kg.signal.meter;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.telephony.CellSignalStrength;
import android.telephony.CellSignalStrengthCdma;
import android.telephony.CellSignalStrengthGsm;
import android.telephony.CellSignalStrengthLte;
import android.telephony.CellSignalStrengthNr;
import android.telephony.CellSignalStrengthTdscdma;
import android.telephony.CellSignalStrengthWcdma;
import android.telephony.SignalStrength;
import android.telephony.TelephonyManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.function.Consumer;

/**
 * Замеряет силу сигнала (dBm) модема телефона и отправляет её с GPS-точкой
 * на сервер Сигнал.kg (POST /api/measurements). Веб-страница dBm прочитать не может —
 * поэтому это нативное приложение.
 */
public class MainActivity extends Activity {

    private static final int REQ_PERMS = 1;
    private static final int RSRP_MIN = -140, RSRP_MAX = -44; // диапазон сервера

    private EditText serverField;
    private TextView dbmView, networkView, locationView, statusView;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean busy = false;

    /** Результат чтения сигнала. */
    private static class Reading {
        final int dbm;
        final String network; // "2G".."5G" или null
        Reading(int dbm, String network) { this.dbm = dbm; this.network = network; }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        serverField = findViewById(R.id.server);
        dbmView = findViewById(R.id.dbm);
        networkView = findViewById(R.id.network);
        locationView = findViewById(R.id.location);
        statusView = findViewById(R.id.status);

        SharedPreferences prefs = getPreferences(Context.MODE_PRIVATE);
        String saved = prefs.getString("server", null);
        if (saved != null) serverField.setText(saved);

        Button measure = findViewById(R.id.measure);
        measure.setOnClickListener(v -> onMeasure());
    }

    private void onMeasure() {
        if (busy) return;
        if (!hasPermissions()) {
            requestPermissions(new String[]{
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.READ_PHONE_STATE}, REQ_PERMS);
            return;
        }
        busy = true;
        status("Определяем место…");
        getLocation(location -> {
            if (location == null) {
                status("Не удалось определить место. Включите GPS и выйдите под открытое небо.");
                busy = false;
                return;
            }
            Reading reading = readSignal();
            if (reading == null) {
                status("Не удалось прочитать сигнал сотовой сети (нет SIM или только Wi-Fi?).");
                busy = false;
                return;
            }
            showReading(reading, location);
            send(location, reading);
        });
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        if (requestCode == REQ_PERMS) {
            if (hasPermissions()) onMeasure();
            else status("Нужны разрешения на геолокацию и телефон, чтобы замерить сигнал.");
        }
    }

    private boolean hasPermissions() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    // --- сигнал ---------------------------------------------------------------

    private Reading readSignal() {
        TelephonyManager tm = (TelephonyManager) getSystemService(TELEPHONY_SERVICE);
        if (tm == null) return null;
        SignalStrength ss;
        try {
            ss = tm.getSignalStrength();
        } catch (SecurityException e) {
            return null;
        }
        if (ss == null) return null;
        List<CellSignalStrength> cells = ss.getCellSignalStrengths();
        for (CellSignalStrength c : cells) {
            int dbm = c.getDbm();
            if (dbm == Integer.MAX_VALUE || dbm >= 0) continue; // недоступно/мусор
            return new Reading(dbm, networkOf(c));
        }
        return null;
    }

    private String networkOf(CellSignalStrength c) {
        if (c instanceof CellSignalStrengthNr) return "5G";
        if (c instanceof CellSignalStrengthLte) return "4G";
        if (c instanceof CellSignalStrengthWcdma || c instanceof CellSignalStrengthTdscdma) return "3G";
        if (c instanceof CellSignalStrengthGsm || c instanceof CellSignalStrengthCdma) return "2G";
        return null;
    }

    // --- место ----------------------------------------------------------------

    private void getLocation(Consumer<Location> callback) {
        LocationManager lm = (LocationManager) getSystemService(LOCATION_SERVICE);
        if (lm == null) { callback.accept(null); return; }

        Location fallback = null;
        try {
            for (String p : new String[]{LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER}) {
                Location l = lm.getLastKnownLocation(p);
                if (l != null && (fallback == null || l.getTime() > fallback.getTime())) fallback = l;
            }
        } catch (SecurityException ignored) {}
        final Location last = fallback;
        final boolean[] done = {false};

        LocationListener listener = new LocationListener() {
            @Override public void onLocationChanged(Location location) {
                if (done[0]) return;
                done[0] = true;
                try { lm.removeUpdates(this); } catch (SecurityException ignored) {}
                callback.accept(location);
            }
            @Override public void onStatusChanged(String provider, int status, Bundle extras) {}
            @Override public void onProviderEnabled(String provider) {}
            @Override public void onProviderDisabled(String provider) {}
        };

        try {
            boolean any = false;
            if (lm.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 0, 0, listener, Looper.getMainLooper());
                any = true;
            }
            if (lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 0, 0, listener, Looper.getMainLooper());
                any = true;
            }
            if (!any) { callback.accept(last); return; }
        } catch (SecurityException e) {
            callback.accept(null);
            return;
        }

        handler.postDelayed(() -> {
            if (done[0]) return;
            done[0] = true;
            try { lm.removeUpdates(listener); } catch (SecurityException ignored) {}
            callback.accept(last); // свежего фикса нет — отдаём последний известный (может быть null)
        }, 9000);
    }

    // --- вывод и отправка -----------------------------------------------------

    private void showReading(Reading r, Location loc) {
        dbmView.setText(String.valueOf(r.dbm));
        networkView.setText("Сеть: " + (r.network != null ? r.network : "неизвестно"));
        locationView.setText(String.format("Место: %.5f, %.5f  (±%d м)",
                loc.getLatitude(), loc.getLongitude(), Math.round(loc.getAccuracy())));
    }

    private void send(Location loc, Reading r) {
        if (r.dbm < RSRP_MIN || r.dbm > RSRP_MAX) {
            status("Сигнал " + r.dbm + " dBm вне диапазона −140…−44 — сервер такой не примет.");
            busy = false;
            return;
        }
        String base = serverField.getText().toString().trim();
        if (base.endsWith("/")) base = base.substring(0, base.length() - 1);
        if (!base.startsWith("http")) { status("Адрес сервера должен начинаться с http:// или https://"); busy = false; return; }
        getPreferences(Context.MODE_PRIVATE).edit().putString("server", base).apply();

        int acc = Math.max(0, Math.round(loc.getAccuracy()));
        StringBuilder b = new StringBuilder();
        b.append('{')
         .append("\"lat\":").append(loc.getLatitude()).append(',')
         .append("\"lon\":").append(loc.getLongitude()).append(',')
         .append("\"accuracy_m\":").append(acc).append(',')
         .append("\"method\":\"manual\",")
         .append("\"rsrp_dbm\":").append(r.dbm);
        if (r.network != null) b.append(",\"network\":\"").append(r.network).append('\"');
        b.append('}');
        final String body = b.toString();
        final String url = base + "/api/measurements";

        status("Отправляем…");
        new Thread(() -> {
            String msg;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setRequestMethod("POST");
                c.setRequestProperty("Content-Type", "application/json");
                c.setConnectTimeout(12000);
                c.setReadTimeout(12000);
                c.setDoOutput(true);
                try (OutputStream os = c.getOutputStream()) {
                    os.write(body.getBytes(StandardCharsets.UTF_8));
                }
                int code = c.getResponseCode();
                c.disconnect();
                if (code == 201) msg = "Отправлено оператору ✓";
                else if (code == 429) msg = "Слишком часто — подождите минуту.";
                else if (code == 422) msg = "Вы вне Бишкека — замер не принят.";
                else msg = "Сервер отклонил замер (код " + code + ").";
            } catch (Exception e) {
                msg = "Нет связи с сервером. Проверьте адрес и интернет.";
            }
            final String result = msg;
            runOnUiThread(() -> { status(result); busy = false; });
        }).start();
    }

    private void status(String text) {
        statusView.setText(text);
    }
}
