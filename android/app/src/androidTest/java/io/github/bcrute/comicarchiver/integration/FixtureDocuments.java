package io.github.bcrute.comicarchiver.integration;

import android.content.Context;
import android.os.Build;
import android.os.Bundle;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Platform-only fixture state. Its separate APK process has no target Kotlin classpath. */
final class FixtureDocuments {
    static final String TITLE = "Archive integration fixture";
    static File root;
    static boolean armed;
    static volatile Gate gate;

    static final class Gate {
        final String document;
        final CountDownLatch release = new CountDownLatch(1);
        final CountDownLatch closed = new CountDownLatch(1);
        volatile boolean blocked;
        volatile long bytes;
        Gate(String document) { this.document = document; }
    }

    static void requireEmulator() {
        if (!"ranchu".equals(Build.HARDWARE) && !"goldfish".equals(Build.HARDWARE))
            throw new IllegalStateException("Integration fixtures require a disposable Android emulator.");
    }

    static synchronized void initialize(Context context) {
        requireEmulator();
        if (root != null) return;
        try {
            root = new File(context.getFilesDir(), "export-fixture").getCanonicalFile();
            if (!root.isDirectory() && !root.mkdirs()) throw new IOException("Fixture root creation failed");
        } catch (IOException error) { throw new IllegalStateException(error); }
    }

    static File file(String id) throws FileNotFoundException {
        if ("root".equals(id)) return root;
        if (!id.startsWith("root/")) throw new FileNotFoundException("Unknown fixture document");
        try {
            File result = new File(root, id.substring(5)).getCanonicalFile();
            if (!result.getPath().startsWith(root.getPath() + File.separator))
                throw new FileNotFoundException("Outside fixture tree");
            return result;
        } catch (IOException error) { throw new FileNotFoundException(error.getMessage()); }
    }

    static String id(File file) {
        if (file.equals(root)) return "root";
        if (!file.getPath().startsWith(root.getPath() + File.separator))
            throw new IllegalArgumentException("Outside fixture tree");
        return "root/" + file.getPath().substring(root.getPath().length() + 1);
    }

    private static String digest(File file) throws Exception {
        MessageDigest hash = MessageDigest.getInstance("SHA-256");
        try (FileInputStream input = new FileInputStream(file)) {
            byte[] buffer = new byte[65536];
            int count;
            while ((count = input.read(buffer)) >= 0) hash.update(buffer, 0, count);
        }
        StringBuilder hex = new StringBuilder();
        for (byte value : hash.digest()) hex.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return hex.toString();
    }

    private static void files(File directory, List<File> output) {
        File[] children = directory.listFiles();
        if (children == null) return;
        for (File child : children) {
            if (child.isDirectory()) files(child, output);
            else if (child.isFile()) output.add(child);
        }
    }

    static synchronized void arm() {
        if (gate != null || armed) throw new IllegalStateException("Fixture already armed");
        armed = true;
    }

    static synchronized Gate begin(String id) {
        if (!armed || !id.endsWith(".cbz")) return null;
        armed = false;
        gate = new Gate(id);
        return gate;
    }

    static void abandon() {
        Gate old = gate;
        if (old != null) {
            old.release.countDown();
            try {
                if (!old.closed.await(15, TimeUnit.SECONDS))
                    throw new IllegalStateException("The interrupted provider writer did not close.");
            } catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new IllegalStateException(error); }
        }
        gate = null;
    }

    static Bundle status() {
        Gate current = gate;
        List<File> files = new ArrayList<>();
        files(root, files);
        JSONArray rows = new JSONArray();
        try {
            for (File file : files) rows.put(new JSONObject().put("id", id(file))
                .put("bytes", file.length()).put("sha256", digest(file)));
        } catch (Exception error) { throw new IllegalStateException(error); }
        Bundle result = new Bundle();
        result.putBoolean("blocked", current != null && current.blocked);
        result.putLong("partial_bytes", current == null ? 0 : current.bytes);
        result.putString("document_id", current == null ? "" : current.document);
        result.putInt("file_count", files.size());
        result.putString("files", rows.toString());
        return result;
    }
}
