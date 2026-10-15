package org.comicarchive.prototype;

import android.graphics.Bitmap;
import android.graphics.Color;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.*;
import org.json.*;

/** Offline runtime checks with synthetic PNGs, never Tapas sessions or requests. */
final class ArchiveSelfTest {
    private static void check(boolean value){if(!value)throw new IllegalStateException("self_test_assertion");}
    private static byte[] png(int color){
        Bitmap bitmap=Bitmap.createBitmap(32,32,Bitmap.Config.ARGB_8888);bitmap.eraseColor(color);
        ByteArrayOutputStream output=new ByteArrayOutputStream();bitmap.compress(Bitmap.CompressFormat.PNG,100,output);bitmap.recycle();return output.toByteArray();
    }
    private static JSONObject fixture() throws Exception {
        return new JSONObject().put("identity","1").put("episode_id","1").put("order",1).put("title","../../Synthetic : Episode")
            .put("series",new JSONObject().put("id","1").put("title","../Synthetic Series").put("url","synthetic"))
            .put("flags",new JSONObject().put("must_pay",false).put("scheduled",false).put("book",false)
                .put("free",true).put("unlocked",false).put("closing_date",JSONObject.NULL))
            .put("pages",new JSONArray().put("https://us-a.tapas.io/pc/synthetic/first.png?private=one")
                .put("https://us-a.tapas.io/pc/synthetic/second.png?private=two"));
    }
    static JSONObject run(File folder) throws Exception {
        File root=new File(folder,"self-test-"+UUID.randomUUID());root.mkdirs();
        byte[][] images={png(Color.RED),png(Color.BLUE)};AtomicBoolean token=new AtomicBoolean();
        JSONObject info=fixture();int[] requests={0};PortableArchive.Progress silent=(a,b,c)->{};
        try{
            // Interruption leaves the first verified page resumable, no final CBZ.
            try{PortableArchive.write(root,info,null,token,silent,(url,cookies,limit,cancel)->{
                requests[0]++;if(url.contains("second"))throw new IOException("synthetic_interruption");return images[0];
            });throw new IllegalStateException("interruption_ignored");}catch(IOException expected){}
            check(requests[0]==2);requests[0]=0;
            File cbz=PortableArchive.write(root,info,null,token,silent,(url,cookies,limit,cancel)->{
                requests[0]++;check(url.contains("second"));return images[1];});
            check(requests[0]==1);
            try(ZipFile zip=new ZipFile(cbz)){
                check(zip.size()==2);Enumeration<? extends ZipEntry> entries=zip.entries();
                for(int i=0;i<2;i++){ZipEntry entry=entries.nextElement();check(entry.getName().equals(i==0?"0001.png":"0002.png"));
                    try(InputStream stream=zip.getInputStream(entry)){check(Arrays.equals(stream.readAllBytes(),images[i]));}}
            }
            File metadata=new File(cbz.getParent(),cbz.getName().replace(".cbz",".json"));
            String json=new String(Files.readAllBytes(metadata.toPath()),StandardCharsets.UTF_8);JSONObject parsed=new JSONObject(json);
            check(parsed.getInt("page_count")==2);check(!json.contains("private=") && !json.contains("identity"));
            for(int i=0;i<2;i++)check(parsed.getJSONArray("content_hashes").getJSONObject(i).getString("sha256").equals(PortableArchive.hash(images[i])));
            check(cbz.getCanonicalPath().startsWith(root.getCanonicalPath()+"/"));
            // Corrupted cached data must be fetched again, identical valid data reused.
            String key=PortableArchive.hash("us-a.tapas.io/pc/synthetic/first.png".getBytes(StandardCharsets.UTF_8));
            Files.write(new File(root,"page-cache/"+key+".data").toPath(),new byte[]{1});requests[0]=0;
            PortableArchive.write(root,fixture(),null,token,silent,(url,cookies,limit,cancel)->{
                requests[0]++;check(url.contains("first"));return images[0];});check(requests[0]==1);
            JSONObject denied=fixture();denied.getJSONObject("flags").put("must_pay",true);requests[0]=0;
            try{PortableArchive.write(root,denied,null,token,silent,(url,cookies,limit,cancel)->{requests[0]++;return images[0];});
                throw new IllegalStateException("locked_episode_written");}catch(IllegalArgumentException expected){}
            check(requests[0]==0);
            JSONObject broken=fixture();broken.put("episode_id","2");broken.put("pages",new JSONArray().put("https://us-a.tapas.io/pc/synthetic/broken.jpg"));
            try{PortableArchive.write(root,broken,null,token,silent,(url,cookies,limit,cancel)->"<html>broken</html>".getBytes(StandardCharsets.UTF_8));
                throw new IllegalStateException("broken_image_written");}catch(IllegalArgumentException expected){}
            token.set(true);requests[0]=0;
            try{PortableArchive.write(root,fixture(),null,token,silent,(url,cookies,limit,cancel)->{requests[0]++;return images[0];});
                throw new IllegalArgumentException("cancel_ignored");}catch(IllegalStateException expected){}
            check(requests[0]==0);
            return new JSONObject().put("outcome","passed").put("checks",new JSONArray(Arrays.asList(
                "original_bytes_order_hashes_metadata", "interrupted_resume", "corrupt_cache_refetch", "locked_before_cache_or_transfer",
                "broken_image_rejected", "cancelled_before_cache_or_transfer"))).put("network_requests",0);
        }finally{remove(root);}
    }
    private static void remove(File file){File[] children=file.listFiles();if(children!=null)for(File child:children)remove(child);file.delete();}
}
