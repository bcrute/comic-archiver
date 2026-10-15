package org.comicarchive.prototype;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.util.concurrent.atomic.AtomicBoolean;

/** Sequential native HTTPS; no redirects, URL/header logs or browser spoofing. */
public final class GetTransport {
    public static final class Refused extends Exception {
        public final int status;
        public Refused(int status) {super("request_refused");this.status=status;}
    }
    private long last=0;
    public byte[] get(String url,String ownTapasCookies,int limit,AtomicBoolean cancelled) throws Exception {
        URI uri=Authorization.asset(url);
        if (Authorization.mutationPath(uri.getRawPath())) throw new IllegalArgumentException("mutation_blocked");
        if (cancelled.get()) throw new IllegalStateException("cancelled");
        long pause=last+1000-System.currentTimeMillis(); if (pause>0) Thread.sleep(pause);
        last=System.currentTimeMillis();
        HttpURLConnection connection=(HttpURLConnection)uri.toURL().openConnection();
        connection.setRequestMethod("GET");connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(30000);connection.setReadTimeout(30000);
        connection.setRequestProperty("Referer","https://tapas.io/");
        if (uri.getHost().equals("tapas.io") && ownTapasCookies!=null)
            connection.setRequestProperty("Cookie",ownTapasCookies);
        try {
            int code=connection.getResponseCode();if (code!=200) throw new Refused(code);
            if (connection.getContentLengthLong()>limit) throw new IllegalArgumentException("byte_limit");
            try (InputStream stream=connection.getInputStream();ByteArrayOutputStream bytes=new ByteArrayOutputStream()) {
                byte[] buffer=new byte[65536];int count;
                while ((count=stream.read(buffer))!=-1) {
                    if (cancelled.get()) throw new IllegalStateException("cancelled");
                    if (bytes.size()+count>limit) throw new IllegalArgumentException("byte_limit");
                    bytes.write(buffer,0,count);
                }
                return bytes.toByteArray();
            }
        } finally {connection.disconnect();}
    }
}
