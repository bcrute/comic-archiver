package io.github.bcrute.comicarchiver.integration;

import android.database.Cursor;
import android.database.MatrixCursor;
import android.os.CancellationSignal;
import android.os.ParcelFileDescriptor;
import android.provider.DocumentsContract.Document;
import android.provider.DocumentsContract.Root;
import android.provider.DocumentsProvider;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.util.Arrays;
import java.util.Comparator;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/** Same real SAF provider and blocking pipe, using only platform/Java classes. */
public final class FixtureDocumentsProvider extends DocumentsProvider {
    private static final String[] DOCUMENT_COLUMNS = {Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME,
        Document.COLUMN_MIME_TYPE, Document.COLUMN_FLAGS, Document.COLUMN_SIZE, Document.COLUMN_LAST_MODIFIED};

    @Override public boolean onCreate() { FixtureDocuments.initialize(getContext()); return true; }

    @Override public Cursor queryRoots(String[] projection) {
        String[] columns = projection != null ? projection : new String[] {Root.COLUMN_ROOT_ID, Root.COLUMN_TITLE,
            Root.COLUMN_DOCUMENT_ID, Root.COLUMN_FLAGS, Root.COLUMN_MIME_TYPES, Root.COLUMN_AVAILABLE_BYTES};
        Map<String, Object> values = new HashMap<>();
        values.put(Root.COLUMN_ROOT_ID, "fixture"); values.put(Root.COLUMN_TITLE, FixtureDocuments.TITLE);
        values.put(Root.COLUMN_DOCUMENT_ID, "root");
        values.put(Root.COLUMN_FLAGS, Root.FLAG_SUPPORTS_CREATE | Root.FLAG_SUPPORTS_IS_CHILD | Root.FLAG_LOCAL_ONLY);
        values.put(Root.COLUMN_MIME_TYPES, "*/*"); values.put(Root.COLUMN_AVAILABLE_BYTES, 128L * 1024 * 1024);
        MatrixCursor cursor = new MatrixCursor(columns);
        MatrixCursor.RowBuilder row = cursor.newRow();
        for (String column : columns) row.add(values.get(column));
        return cursor;
    }

    private void add(MatrixCursor cursor, File file) throws FileNotFoundException {
        if (!file.exists()) throw new FileNotFoundException("Fixture document missing");
        Map<String, Object> values = new HashMap<>();
        values.put(Document.COLUMN_DOCUMENT_ID, FixtureDocuments.id(file));
        values.put(Document.COLUMN_DISPLAY_NAME, file.equals(FixtureDocuments.root) ? FixtureDocuments.TITLE : file.getName());
        values.put(Document.COLUMN_MIME_TYPE, file.isDirectory() ? Document.MIME_TYPE_DIR
            : file.getName().endsWith(".epub") ? "application/epub+zip" : "application/vnd.comicbook+zip");
        values.put(Document.COLUMN_FLAGS, file.isDirectory() ? Document.FLAG_DIR_SUPPORTS_CREATE
            : Document.FLAG_SUPPORTS_WRITE | Document.FLAG_SUPPORTS_DELETE);
        values.put(Document.COLUMN_SIZE, file.length()); values.put(Document.COLUMN_LAST_MODIFIED, file.lastModified());
        MatrixCursor.RowBuilder row = cursor.newRow();
        for (String column : cursor.getColumnNames()) row.add(values.get(column));
    }

    @Override public Cursor queryDocument(String id, String[] projection) throws FileNotFoundException {
        MatrixCursor cursor = new MatrixCursor(projection != null ? projection : DOCUMENT_COLUMNS);
        add(cursor, FixtureDocuments.file(id));
        return cursor;
    }

    @Override public Cursor queryChildDocuments(String id, String[] projection, String sortOrder) throws FileNotFoundException {
        MatrixCursor cursor = new MatrixCursor(projection != null ? projection : DOCUMENT_COLUMNS);
        File[] children = FixtureDocuments.file(id).listFiles();
        if (children != null) {
            Arrays.sort(children, Comparator.comparing(File::getName));
            for (File child : children) add(cursor, child);
        }
        return cursor;
    }

    @Override public boolean isChildDocument(String parent, String child) throws FileNotFoundException {
        return FixtureDocuments.file(child).getPath().startsWith(FixtureDocuments.file(parent).getPath() + File.separator);
    }

    @Override public synchronized String createDocument(String parentId, String mimeType, String name) throws FileNotFoundException {
        if (name == null || name.trim().isEmpty() || name.length() > 240 || name.equals(".") || name.equals("..")
                || name.indexOf('/') >= 0 || name.indexOf('\\') >= 0)
            throw new IllegalArgumentException("Invalid fixture document name");
        for (int i = 0; i < name.length(); i++) if (name.charAt(i) < 32) throw new IllegalArgumentException("Invalid name");
        File parent = FixtureDocuments.file(parentId);
        if (!parent.isDirectory()) throw new IllegalStateException("Fixture parent is not a directory");
        File file = new File(parent, name);
        int dot = name.lastIndexOf('.');
        for (int index = 1; file.exists(); index++) {
            if (index > 1000) throw new IllegalStateException("Too many fixture collisions");
            file = new File(parent, (dot < 0 ? name : name.substring(0, dot)) + " (" + index + ")"
                + (dot < 0 ? "" : name.substring(dot)));
        }
        try {
            if (!(Document.MIME_TYPE_DIR.equals(mimeType) ? file.mkdir() : file.createNewFile()))
                throw new IOException("Fixture document creation failed");
        } catch (IOException error) { throw new FileNotFoundException(error.getMessage()); }
        return FixtureDocuments.id(file);
    }

    @Override public ParcelFileDescriptor openDocument(String id, String mode, CancellationSignal signal) throws FileNotFoundException {
        File file = FixtureDocuments.file(id);
        try {
            if (mode.contains("w")) {
                FixtureDocuments.Gate gate = FixtureDocuments.begin(id);
                if (gate != null) {
                    ParcelFileDescriptor[] pipe = ParcelFileDescriptor.createPipe();
                    new Thread(() -> {
                        try (FileOutputStream output = new FileOutputStream(file, false);
                             ParcelFileDescriptor.AutoCloseInputStream input = new ParcelFileDescriptor.AutoCloseInputStream(pipe[0])) {
                            byte[] chunk = new byte[8192];
                            int count = input.read(chunk);
                            if (count <= 0) throw new IOException("No real partial bytes arrived");
                            output.write(chunk, 0, count); output.flush();
                            gate.bytes = count; gate.blocked = true;
                            if (!gate.release.await(180, TimeUnit.SECONDS)) throw new IOException("Fixture copy was not abandoned");
                        } catch (Exception error) {
                            throw new IllegalStateException(error);
                        } finally { gate.blocked = false; gate.closed.countDown(); }
                    }, "fixture-paused-export").start();
                    return pipe[1];
                }
            } else {
                FixtureDocuments.Gate gate = FixtureDocuments.gate;
                if (gate != null && gate.document.equals(id) && !gate.closed.await(180, TimeUnit.SECONDS))
                    throw new IOException("Fixture writer did not close");
            }
            return ParcelFileDescriptor.open(file, ParcelFileDescriptor.parseMode(mode));
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt(); throw new FileNotFoundException("Fixture read interrupted");
        } catch (IOException error) { throw new FileNotFoundException(error.getMessage()); }
    }

    @Override public void deleteDocument(String id) throws FileNotFoundException {
        if ("root".equals(id)) throw new IllegalArgumentException("Cannot delete the fixture root");
        if (!FixtureDocuments.file(id).delete()) throw new IllegalStateException("Fixture delete failed");
    }
}
