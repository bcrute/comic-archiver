package org.comicarchive.prototype;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.*;
import android.widget.*;
import java.io.*;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.*;

/** One episode reference slice. No remote bridge, purchases or scroll automation. */
public final class MainActivity extends Activity {
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private volatile AtomicBoolean cancelled = new AtomicBoolean(false);
    private volatile boolean readOnly = false;
    private boolean busy = false;
    private int generation = 0;
    private WebView web;
    private EditText episode;
    private TextView status;
    private Button test, login, exportCbz, exportJson;
    private File lastArchive, exportSource;
    private JSONObject diagnostic = new JSONObject();
    private String requestedId;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout layout = new LinearLayout(this);layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(12, 12, 12, 12);layout.setBackgroundColor(Color.WHITE);
        layout.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(12, Math.max(12,insets.getSystemWindowInsetTop()), 12,
                Math.max(12,insets.getSystemWindowInsetBottom()));return insets;
        });
        TextView title = new TextView(this);title.setText("Comic Archive — Android prototype");title.setTextSize(18);layout.addView(title);
        episode = new EditText(this);episode.setSingleLine(true);episode.setText("https://tapas.io/episode/2864705");
        episode.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);layout.addView(episode);
        LinearLayout actions = new LinearLayout(this);layout.addView(actions);
        login = button(actions,"Sign in",v -> signIn());test = button(actions,"Test & archive",v -> begin());
        button(actions,"Cancel",v -> cancel());
        LinearLayout exports = new LinearLayout(this);layout.addView(exports);
        exportCbz = button(exports,"Export CBZ",v -> export(false));exportJson = button(exports,"Export JSON",v -> export(true));
        exportCbz.setEnabled(false);exportJson.setEnabled(false);
        status = new TextView(this);status.setText("Sign in to Tapas here, then test one episode. No purchases or unlocks are automated.");
        status.setTextIsSelectable(true);layout.addView(status);
        web = new WebView(this);layout.addView(web,new LinearLayout.LayoutParams(-1,0,1));setContentView(layout);
        web.getSettings().setJavaScriptEnabled(true);web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setAllowFileAccess(false);web.getSettings().setAllowContentAccess(false);
        web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptCookie(true);CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
        web.setWebChromeClient(new WebChromeClient(){@Override public boolean onConsoleMessage(ConsoleMessage message){return true;}});
        web.setWebViewClient(new WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request){
                try {URI uri = new URI(request.getUrl().toString());
                    return !"https".equals(uri.getScheme()) || uri.getUserInfo()!=null || uri.getPort()!=-1 ||
                        (readOnly && (!"tapas.io".equals(uri.getHost()) || Authorization.mutationPath(uri.getPath())));
                } catch(Exception ignored){return true;}
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request){return intercept(request);}
            @Override public void onPageFinished(WebView view,String url){
                CookieManager.getInstance().flush();
                if (busy && requestedId!=null && url.equals("https://tapas.io/")) capture();
            }
        });
        ServiceWorkerController.getInstance().setServiceWorkerClient(new ServiceWorkerClient(){
            @Override public WebResourceResponse shouldInterceptRequest(WebResourceRequest request){return intercept(request);}
        });
        restoreLastArchive();
        if(getIntent().getBooleanExtra("open_signin",false))signIn();
        if(getIntent().getBooleanExtra("self_test",false)){
            io.execute(() -> {
                JSONObject report;
                try{report=ArchiveSelfTest.run(getFilesDir());}
                catch(Exception ignored){report=new JSONObject();try{report.put("outcome","failed");}catch(Exception impossible){}}
                try{PortableArchive.atomic(new File(getFilesDir(),"self-test.json"),report.toString(2).getBytes(StandardCharsets.UTF_8));}catch(Exception ignored){}
                final boolean passed=report.optString("outcome").equals("passed");
                ui.post(() -> status.setText(passed?"Offline archive checks passed. Sign in to test Tapas.":"Offline archive checks failed."));
            });
        }
    }
    private Button button(LinearLayout row,String label,View.OnClickListener listener){
        Button button = new Button(this);button.setText(label);button.setTextSize(12);button.setOnClickListener(listener);
        row.addView(button,new LinearLayout.LayoutParams(0,-2,1));return button;
    }
    private WebResourceResponse intercept(WebResourceRequest request){
        String path=request.getUrl().getPath();if(path==null) path="";
        boolean comic=path.startsWith("/pc/") || path.startsWith("/c/");
        boolean blocked=comic || (readOnly && (!request.getMethod().equals("GET") || Authorization.mutationPath(path)));
        if (!blocked) return null;
        return new WebResourceResponse("text/plain","UTF-8",403,"Blocked by importer",Collections.emptyMap(),
            new ByteArrayInputStream(new byte[0]));
    }
    private void signIn(){
        if(busy) return;readOnly=false;web.setKeepScreenOn(true);
        ui.postDelayed(() -> {if(!busy)web.setKeepScreenOn(false);},300000);
        web.stopLoading();web.loadUrl("https://tapas.io/account/signin");
        status.setText("Use the ordinary Tapas sign-in page. Then tap Test & archive. Comic images are fetched separately.");
    }
    private void setBusy(boolean value){busy=value;test.setEnabled(!value);login.setEnabled(!value);episode.setEnabled(!value);
        web.setKeepScreenOn(value);}
    private void begin(){
        if(busy)return;
        try {requestedId=Authorization.episodeId(episode.getText().toString().trim());}
        catch(Exception ignored){status.setText("Enter an exact https://tapas.io/episode/<number> URL.");return;}
        cancelled = new AtomicBoolean(false);generation++;readOnly=true;setBusy(true);diagnostic=new JSONObject();
        note("identity","Loading Tapas normally; checking authenticated identity and current access.");
        web.stopLoading();web.loadUrl("https://tapas.io/");
        final int run=generation;
        ui.postDelayed(() -> {if(busy && run==generation && requestedId!=null) hold("identity","page_load_timeout");},60000);
    }
    private boolean tapasOrigin(){
        try {URI uri=new URI(web.getUrl());return "https".equals(uri.getScheme()) && "tapas.io".equals(uri.getHost()) && uri.getPort()==-1 && uri.getUserInfo()==null;}
        catch(Exception ignored){return false;}
    }
    private void capture(){
        if(!busy || requestedId==null || !tapasOrigin())return;
        String id=requestedId;requestedId=null;final int run=generation;
        try {
            String script=new String(read(getAssets().open("capture.js"),65536),StandardCharsets.UTF_8).replace("__EPISODE_ID__",id);
            web.evaluateJavascript(script,ignored -> poll(run,System.currentTimeMillis()+90000));
        } catch(Exception ignored){hold("identity","capture_unavailable");}
    }
    private void poll(int run,long deadline){
        if(run!=generation || !busy || cancelled.get())return;
        if(!tapasOrigin()){hold("identity","origin_changed");return;}
        if(System.currentTimeMillis()>deadline){hold("metadata","metadata_timeout");return;}
        web.evaluateJavascript("JSON.stringify(window.__comicArchiveResult || {state:'pending'})",value -> {
            if(run!=generation || !busy)return;
            try {
                Object decoded=new JSONTokener(value).nextValue();if(!(decoded instanceof String))throw new IllegalArgumentException();
                JSONObject result=new JSONObject((String)decoded);String state=result.getString("state");
                if(state.equals("pending")){ui.postDelayed(() -> poll(run,deadline),350);return;}
                web.evaluateJavascript("delete window.__comicArchiveResult",ignored -> {});
                if(!state.equals("ready")){hold(safe(result.optString("stage")),safe(result.optString("code")));return;}
                validate(result,idFromInput());
                String cookies=CookieManager.getInstance().getCookie("https://tapas.io/");
                note("authorized","Access verified. Reading ordered pages without scrolling.");
                AtomicBoolean token=cancelled;
                io.execute(() -> archive(result,cookies,run,token));
            } catch(Exception ignored){hold("metadata","response_unverified");}
        });
    }
    private String idFromInput(){return Authorization.episodeId(episode.getText().toString().trim());}
    static void validate(JSONObject info,String requested) throws Exception {
        if(!requested.equals(info.getString("episode_id")) || !info.getString("identity").matches("[1-9][0-9]{0,19}"))throw new IllegalArgumentException();
        JSONObject flags=info.getJSONObject("flags");Map<String,Object> values=new HashMap<>();
        for(String key:new String[]{"must_pay","scheduled","book","free","unlocked","closing_date"}){
            Object value=flags.get(key);values.put(key,value==JSONObject.NULL?null:value);
        }
        info.put("access_state",Authorization.access(values));
        Object order=info.get("order");if(!(order instanceof Integer) || (Integer)order<1)throw new IllegalArgumentException();
        JSONObject series=info.getJSONObject("series");
        if(!series.getString("id").matches("[1-9][0-9]{0,19}") || !series.getString("url").matches("[A-Za-z0-9_-]{1,100}"))throw new IllegalArgumentException();
        for(String title:new String[]{info.getString("title"),series.getString("title")})if(title.trim().isEmpty() || title.length()>1000)throw new IllegalArgumentException();
        JSONArray pages=info.getJSONArray("pages");if(pages.length()<1 || pages.length()>500)throw new IllegalArgumentException();
        Set<String> unique=new HashSet<>();for(int i=0;i<pages.length();i++){
            URI uri=Authorization.comic(pages.getString(i));if(!unique.add(uri.getHost()+uri.getRawPath()))throw new IllegalArgumentException();
        }
    }
    private void archive(JSONObject result,String cookies,int run,AtomicBoolean token){
        try {
            // Compare a normal native identity GET; a source refusal is recorded, never evaded.
            int nativeStatus=200;boolean nativeAuthenticated=false;
            try {
                byte[] body=new GetTransport().get("https://tapas.io/who-am-i",cookies,262144,token);
                JSONObject envelope=new JSONObject(new String(body,StandardCharsets.UTF_8));
                if(!envelope.isNull("error_details"))throw new IllegalArgumentException("native_identity_unverified");
                JSONObject identity=envelope.getJSONObject("data").getJSONObject("user");
                Object id=identity.get("id");
                nativeAuthenticated=(id instanceof Number) && String.valueOf(id).equals(result.getString("identity"));
                if(!nativeAuthenticated)throw new IllegalArgumentException("native_identity_mismatch");
            } catch(GetTransport.Refused refused){nativeStatus=refused.status;}
            // A native 403 permits browser-backed metadata only; images still use normal HTTPS GETs.
            final int response=nativeStatus;final boolean authenticated=nativeAuthenticated;
            ui.post(() -> {if(run==generation){try{diagnostic.put("authenticated",true).put("native_identity_status",response)
                .put("native_identity_authenticated",authenticated).put("access_state",result.getString("access_state"))
                .put("page_count",result.getJSONArray("pages").length());saveDiagnostic();}catch(Exception ignored){}}});
            String binding=PortableArchive.hash(result.getString("identity").getBytes(StandardCharsets.UTF_8));
            File root=new File(getFilesDir(),"account-"+binding);root.mkdirs();
            // Revalidate immediately before any cache lookup or image transfer.
            validate(result,result.getString("episode_id"));
            File cbz=PortableArchive.write(root,result,cookies,token,(complete,total,reused) ->
                ui.post(() -> {if(run==generation){note("pages","Saved "+complete+" / "+total+" pages; "+reused+" reused.");
                    try{diagnostic.put("saved_pages",complete).put("cached_pages",reused);saveDiagnostic();}catch(Exception ignored){}}}));
            ui.post(() -> {if(run==generation){lastArchive=cbz;getPreferences(0).edit().putString("last_archive",cbz.getAbsolutePath()).apply();
                exportCbz.setEnabled(true);exportJson.setEnabled(true);setBusy(false);note("complete","CBZ and JSON saved. Use Export to choose their location.");}});
        } catch(GetTransport.Refused refused){ui.post(() -> {if(run==generation)hold("pages","http_"+refused.status);});}
        catch(Exception ignored){ui.post(() -> {if(run==generation)hold("archive",token.get()?"cancelled":"archive_failed");});}
    }
    private static String safe(String code){return code.matches("[a-z_0-9]{1,60}")?code:"unverified";}
    private void note(String stage,String message){status.setText(message);try{diagnostic.put("stage",safe(stage));saveDiagnostic();}catch(Exception ignored){}}
    private void hold(String stage,String code){requestedId=null;setBusy(false);readOnly=true;cancelled.set(true);generation++;
        status.setText("Held: "+safe(stage)+" / "+safe(code)+". Sign in normally if needed, then retry. Saved pages are retained.");
        try{diagnostic.put("stage",safe(stage)).put("outcome","held").put("reason",safe(code));saveDiagnostic();}catch(Exception ignored){}}
    private void saveDiagnostic(){try{PortableArchive.atomic(new File(getFilesDir(),"diagnostic.json"),diagnostic.toString(2).getBytes(StandardCharsets.UTF_8));}catch(Exception ignored){}}
    private void cancel(){if(!busy)return;cancelled.set(true);web.stopLoading();hold("archive","cancelled");}
    private void restoreLastArchive(){
        String saved=getPreferences(0).getString("last_archive",null);if(saved==null)return;
        try{File file=new File(saved);if(file.isFile() && file.getCanonicalPath().startsWith(getFilesDir().getCanonicalPath()+"/")){
            lastArchive=file;exportCbz.setEnabled(true);exportJson.setEnabled(true);status.setText("Previous CBZ is ready to export. Sign in before another import.");
        }}catch(Exception ignored){}
    }
    private void export(boolean json){
        if(lastArchive==null || busy)return;
        File file=json?new File(lastArchive.getParent(),lastArchive.getName().replaceFirst("\\.cbz$",".json")):lastArchive;
        if(!file.isFile()){status.setText("The saved file is unavailable.");return;}exportSource=file;
        Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT);intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(json?"application/json":"application/vnd.comicbook+zip");intent.putExtra(Intent.EXTRA_TITLE,file.getName());startActivityForResult(intent,41);
    }
    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);if(request!=41 || result!=RESULT_OK || data==null || data.getData()==null || exportSource==null)return;
        File file=exportSource;android.net.Uri destination=data.getData();
        io.execute(() -> {try(InputStream input=new FileInputStream(file);OutputStream output=getContentResolver().openOutputStream(destination,"w")){
            if(output==null)throw new IOException();byte[] buffer=new byte[65536];int count;while((count=input.read(buffer))!=-1)output.write(buffer,0,count);
            output.flush();ui.post(() -> status.setText("Export complete: "+file.getName()));
        }catch(Exception ignored){ui.post(() -> status.setText("Export failed. Choose a writable destination and retry."));}});
    }
    private static byte[] read(InputStream stream,int limit) throws IOException {
        try(InputStream input=stream;ByteArrayOutputStream output=new ByteArrayOutputStream()){
            byte[] buffer=new byte[8192];int count;while((count=input.read(buffer))!=-1){if(output.size()+count>limit)throw new IOException();output.write(buffer,0,count);}return output.toByteArray();
        }
    }
    @Override protected void onDestroy(){cancelled.set(true);generation++;io.shutdownNow();web.destroy();super.onDestroy();}
}
