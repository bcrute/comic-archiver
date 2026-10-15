package org.comicarchive.prototype;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.*;
import org.json.*;

/** Original-byte CBZ/JSON and private, verifiable page resume. */
public final class PortableArchive {
    public interface Progress {void update(int complete,int total,int reused);}
    interface PageLoader {byte[] get(String url,String cookies,int limit,AtomicBoolean cancelled) throws Exception;}
    public static String hash(byte[] bytes) throws Exception {
        byte[] digest=MessageDigest.getInstance("SHA-256").digest(bytes);StringBuilder out=new StringBuilder();
        for (byte b:digest) out.append(String.format(Locale.ROOT,"%02x",b&255));return out.toString();
    }
    public static void atomic(File target,byte[] bytes) throws Exception {
        File temporary=File.createTempFile(".archive-",".tmp",target.getParentFile());
        try {
            try(FileOutputStream stream=new FileOutputStream(temporary)){stream.write(bytes);stream.flush();stream.getFD().sync();}
            Files.move(temporary.toPath(),target.toPath(),StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);
        } finally {temporary.delete();}
    }
    private static String name(String title) {
        String clean=title.replaceAll("[\\p{Cntrl}/\\\\:*?\"<>|]","_").trim().replaceAll("[. ]+$","");
        if (clean.isEmpty() || clean.equals(".") || clean.equals("..")) clean="Untitled";
        while (clean.getBytes(StandardCharsets.UTF_8).length>120) clean=clean.substring(0,clean.offsetByCodePoints(0,clean.codePointCount(0,clean.length())-1));
        return clean;
    }
    private static String image(byte[] data) {
        if (data.length>3 && (data[0]&255)==255 && (data[1]&255)==216 && (data[2]&255)==255) return ".jpg";
        if (data.length>8 && (data[0]&255)==137 && data[1]=='P' && data[2]=='N' && data[3]=='G') return ".png";
        if (data.length>12 && new String(data,0,4,StandardCharsets.US_ASCII).equals("RIFF") && new String(data,8,4,StandardCharsets.US_ASCII).equals("WEBP")) return ".webp";
        if (data.length>6 && new String(data,0,3,StandardCharsets.US_ASCII).equals("GIF")) return ".gif";
        throw new IllegalArgumentException("image_signature_invalid");
    }
    private static void decode(byte[] bytes) {
        BitmapFactory.Options options=new BitmapFactory.Options();options.inJustDecodeBounds=true;
        BitmapFactory.decodeByteArray(bytes,0,bytes.length,options);
        if (options.outWidth<1 || options.outHeight<1) throw new IllegalArgumentException("image_decode_failed");
        options.inJustDecodeBounds=false;options.inSampleSize=16;
        Bitmap bitmap=BitmapFactory.decodeByteArray(bytes,0,bytes.length,options);
        if (bitmap==null) throw new IllegalArgumentException("image_decode_failed");bitmap.recycle();
    }
    public static File write(File root,JSONObject info,String cookies,AtomicBoolean cancelled,Progress progress) throws Exception {
        GetTransport transport=new GetTransport();
        return write(root,info,cookies,cancelled,progress,transport::get);
    }
    static File write(File root,JSONObject info,String cookies,AtomicBoolean cancelled,Progress progress,PageLoader loader) throws Exception {
        MainActivity.validate(info,info.getString("episode_id"));
        if(cancelled.get())throw new IllegalStateException("cancelled");
        File cache=new File(root,"page-cache");cache.mkdirs();File library=new File(root,"archives");library.mkdirs();
        JSONObject series=info.getJSONObject("series");String seriesId=String.valueOf(series.get("id"));
        File folder=new File(library,name(series.getString("title"))+" ["+seriesId+"]");folder.mkdirs();
        String episodeId=String.valueOf(info.get("episode_id"));int order=info.getInt("order");
        String stem=String.format(Locale.ROOT,"%03d - %s [%s]",order,name(info.getString("title")),episodeId);
        File cbz=new File(folder,stem+".cbz");File temporary=File.createTempFile(".cbz-",".tmp",folder);
        JSONArray sources=info.getJSONArray("pages");JSONArray hashes=new JSONArray();
        long downloaded=0;int reused=0;long started=System.currentTimeMillis();
        try {
            try(FileOutputStream output=new FileOutputStream(temporary);ZipOutputStream zip=new ZipOutputStream(output)) {
                for (int i=0;i<sources.length();i++) {
                    if (cancelled.get()) throw new IllegalStateException("cancelled");
                    if (System.currentTimeMillis()-started>1800000) throw new IllegalArgumentException("time_limit");
                    String url=sources.getString(i);java.net.URI uri=Authorization.comic(url);
                    String key=hash((uri.getHost()+uri.getRawPath()).getBytes(StandardCharsets.UTF_8));
                    File saved=new File(cache,key+".data"),checksum=new File(cache,key+".sha256");byte[] bytes=null;
                    if (saved.isFile() && checksum.isFile() && checksum.length()==64 && saved.length()>0 && saved.length()<=25*1024*1024) {
                        byte[] candidate=Files.readAllBytes(saved.toPath());
                        if (hash(candidate).equals(new String(Files.readAllBytes(checksum.toPath()),StandardCharsets.US_ASCII))) {bytes=candidate;reused++;}
                    }
                    if (bytes==null) {
                        bytes=loader.get(url,cookies,25*1024*1024,cancelled);downloaded+=bytes.length;
                        if (downloaded>512L*1024*1024) throw new IllegalArgumentException("byte_limit");
                        image(bytes);decode(bytes);atomic(saved,bytes);atomic(checksum,hash(bytes).getBytes(StandardCharsets.US_ASCII));
                    } else {image(bytes);decode(bytes);}
                    String file=String.format(Locale.ROOT,"%04d%s",i+1,image(bytes));CRC32 crc=new CRC32();crc.update(bytes);
                    ZipEntry entry=new ZipEntry(file);entry.setTime(315532800000L);entry.setMethod(ZipEntry.STORED);
                    entry.setSize(bytes.length);entry.setCompressedSize(bytes.length);entry.setCrc(crc.getValue());
                    zip.putNextEntry(entry);zip.write(bytes);zip.closeEntry();
                    hashes.put(new JSONObject().put("file",file).put("size",bytes.length).put("sha256",hash(bytes)));
                    progress.update(i+1,sources.length(),reused);
                }
                if (cancelled.get()) throw new IllegalStateException("cancelled");
                zip.finish();output.flush();output.getFD().sync();
            }
            JSONObject metadata=new JSONObject().put("source","tapas").put("source_episode_id",episodeId)
                .put("source_series_id",seriesId).put("series_id",seriesId).put("title",info.getString("title"))
                .put("episode_number",order).put("episode_order",order).put("source_url","https://tapas.io/episode/"+episodeId)
                .put("access_state",info.getString("access_state")).put("imported_at",Instant.now().toString())
                .put("page_count",sources.length()).put("content_hashes",hashes);
            Files.move(temporary.toPath(),cbz.toPath(),StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);
            atomic(new File(folder,stem+".json"),metadata.toString(2).getBytes(StandardCharsets.UTF_8));
            int count=folder.listFiles((directory,filename)->filename.endsWith(".cbz")).length;
            JSONObject seriesMeta=new JSONObject().put("source","tapas").put("source_series_id",seriesId)
                .put("title",series.getString("title")).put("source_url","https://tapas.io/series/"+series.getString("url")+"/info")
                .put("imported_at",Instant.now().toString()).put("episode_count",count);
            atomic(new File(folder,"series.json"),seriesMeta.toString(2).getBytes(StandardCharsets.UTF_8));
            return cbz;
        } finally {temporary.delete();}
    }
}
