package cn.example.meeting;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * 整个 App 就是一个 WebView。
 *
 * 三件事必须做对，否则网页会表现得像坏了：
 *
 * 1. setMediaPlaybackRequiresUserGesture(false)
 *    助理说话是脚本触发的，不是用户点的播放。默认设置会把它当"自动播放"拦掉，
 *    结果是转写正常、就是没声音。
 *
 * 2. onPermissionRequest 里 grant
 *    getUserMedia 在 WebView 里要求宿主应用明确批准，不批就一直 pending，
 *    页面上表现为"一直连不上麦克风"。
 *
 * 3. CookieManager.flush()
 *    口令存在 cookie 里。不 flush 只留在内存，进程一被杀就要重新输口令。
 */
public class MainActivity extends Activity {

    /**
     * 线上地址。**打包前必须改成你自己的域名**，否则装上去会打开别人的服务。
     *
     * 留占位符而不是留一个能用的地址，是故意的：填一个假域名会在启动时白屏，
     * 一眼就知道没改；如果留一个真实可用的地址，装上去能跑，反而没人会去改。
     */
    private static final String HOME = "https://<你的域名>/";
    private static final int REQ_MIC = 1001;

    private WebView web;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // localStorage：唤醒档位、音色选择
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " MeetingApp/1.0");

        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> request.grant(request.getResources()));
            }
        });

        ViewGroup.LayoutParams lp = new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        setContentView(web, lp);

        askMic();
        web.loadUrl(HOME);
    }

    /** Android 6+ 的麦克风是运行时权限，系统级授权先过，WebView 才有得批。 */
    private void askMic() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] granted) {
        super.onRequestPermissionsResult(code, perms, granted);
        if (code == REQ_MIC && granted.length > 0
                && granted[0] != PackageManager.PERMISSION_GRANTED) {
            // 没权限就别假装能用：转写是核心功能，说清楚比静默失败强
            String html = "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    + "<div style='font:16px/1.7 -apple-system,sans-serif;padding:32px;color:#111827'>"
                    + "<h3 style='margin:0 0 10px'>需要麦克风权限</h3>"
                    + "<p style='color:#6b7280'>没有录音权限就没法转写。请到"
                    + "「设置 → 应用 → Meeting Scribe → 权限」里打开麦克风，然后重新打开本应用。</p>"
                    + "</div>";
            web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onPause() {
        if (web != null) {
            CookieManager.getInstance().flush();   // 口令落盘
            web.onPause();
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) {
            web.onResume();
        }
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
