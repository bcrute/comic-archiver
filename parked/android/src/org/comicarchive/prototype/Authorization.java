package org.comicarchive.prototype;

import java.net.URI;
import java.util.Map;
import java.util.regex.Pattern;

/** The same conservative, explicit entitlement gate as the Python adapter. */
public final class Authorization {
    private Authorization() {}
    public static String access(Map<String,Object> fields) {
        for (String name : new String[]{"must_pay","scheduled","book","free","unlocked"})
            if (!(fields.get(name) instanceof Boolean)) throw new IllegalArgumentException("authorization_unknown");
        if (!fields.containsKey("closing_date")) throw new IllegalArgumentException("authorization_unknown");
        if (Boolean.TRUE.equals(fields.get("must_pay"))) throw new IllegalArgumentException("access_denied");
        if (Boolean.TRUE.equals(fields.get("scheduled"))) throw new IllegalArgumentException("scheduled");
        if (Boolean.TRUE.equals(fields.get("book"))) throw new IllegalArgumentException("novel");
        if (fields.get("closing_date") != null) throw new IllegalArgumentException("temporary_access");
        if (Boolean.TRUE.equals(fields.get("free"))) return "tapas_free";
        if (Boolean.TRUE.equals(fields.get("unlocked"))) return "tapas_unlocked";
        throw new IllegalArgumentException("access_denied");
    }
    public static String episodeId(String url) {
        if (url == null || !url.matches("https://tapas\\.io/episode/[1-9][0-9]{0,19}"))
            throw new IllegalArgumentException("episode_url_invalid");
        return url.substring(url.lastIndexOf('/')+1);
    }
    public static URI asset(String value) {
        try {
            URI uri=new URI(value); String host=uri.getHost();
            if (!"https".equals(uri.getScheme()) || host==null || uri.getUserInfo()!=null ||
                    uri.getPort()!=-1 || uri.getFragment()!=null ||
                    !(host.equals("tapas.io") || host.endsWith(".tapas.io"))) throw new IllegalArgumentException();
            return uri;
        } catch (Exception exception) {throw new IllegalArgumentException("asset_boundary_invalid");}
    }
    public static URI comic(String value) {
        URI uri=asset(value);
        if (!(uri.getRawPath().startsWith("/pc/") || uri.getRawPath().startsWith("/c/")))
            throw new IllegalArgumentException("comic_path_unverified");
        return uri;
    }
    public static boolean mutationPath(String path) {
        return Pattern.compile("(?:^|[/_-])(buy|purchase|unlock|payment|redeem|claim|logout)(?:$|[/_-])",
                Pattern.CASE_INSENSITIVE).matcher(path).find();
    }
}
