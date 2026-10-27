package io.github.bcrute.comicarchiver.integration;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;

/** Emulator-only control restricted to the target UID or fleet adb shell. */
public final class FixtureControlProvider extends ContentProvider {
    @Override public boolean onCreate() { FixtureDocuments.initialize(getContext()); return true; }

    @Override public Bundle call(String method, String arg, Bundle extras) {
        FixtureDocuments.requireEmulator();
        try {
            int targetUid = getContext().getPackageManager().getApplicationInfo("io.github.bcrute.comicarchiver", 0).uid;
            int caller = Binder.getCallingUid();
            if (caller != targetUid && caller != 2000)
                throw new IllegalStateException("Caller is not this fixture's target or fleet shell.");
        } catch (PackageManager.NameNotFoundException error) { throw new IllegalStateException(error); }
        switch (method) {
            case "arm": FixtureDocuments.arm(); break;
            case "abandon": FixtureDocuments.abandon(); break;
            case "status": break;
            default: throw new IllegalArgumentException("Unknown fixture control");
        }
        return FixtureDocuments.status();
    }

    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) { return null; }
    @Override public String getType(Uri uri) { return null; }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException(); }
    @Override public int delete(Uri uri, String selection, String[] args) { throw new UnsupportedOperationException(); }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) { throw new UnsupportedOperationException(); }
}
