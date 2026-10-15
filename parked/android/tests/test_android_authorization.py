"""Exercise the actual platform-independent Java access gate, without Tapas."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
ROOT = SOURCE.parents[1]
sys.path.insert(0, str(ROOT))
from comic_archive.state_paths import session_path
JDK = session_path(ROOT) / 'mobile/toolchain/jdk/bin'


class AndroidAuthorizationTests(unittest.TestCase):
    def test_strict_access_and_asset_boundaries(self):
        java = str(JDK / 'java') if (JDK / 'java').is_file() else shutil.which('java')
        javac = str(JDK / 'javac') if (JDK / 'javac').is_file() else shutil.which('javac')
        if not java or not javac:
            self.skipTest('JDK unavailable; build_android.py --setup supplies it')
        source = SOURCE / 'src/org/comicarchive/prototype/Authorization.java'
        harness = '''import org.comicarchive.prototype.Authorization;
import java.util.*;
public class GateTest {
  interface Action {void run();}
  static void denied(Action test) {try {test.run();} catch(IllegalArgumentException good) {return;}
    throw new AssertionError("unverified access allowed");}
  static Map<String,Object> flags() {Map<String,Object> f=new HashMap<>();
    for(String k:new String[]{"must_pay","scheduled","book","free","unlocked"})f.put(k,false);
    f.put("free",true);f.put("closing_date",null);return f;}
  public static void main(String[] ignored) {
    if(!Authorization.access(flags()).equals("tapas_free"))throw new AssertionError();
    Map<String,Object> paid=flags();paid.put("free",false);paid.put("unlocked",true);
    if(!Authorization.access(paid).equals("tapas_unlocked"))throw new AssertionError();
    for(String field:new String[]{"must_pay","scheduled","book","free","unlocked","closing_date"}){
      Map<String,Object> missing=flags();missing.remove(field);denied(()->Authorization.access(missing));
    }
    for(String field:new String[]{"must_pay","scheduled","book"}){
      Map<String,Object> wrong=flags();wrong.put(field,true);denied(()->Authorization.access(wrong));}
    for(Object value:new Object[]{"true",1,null}){
      Map<String,Object> wrong=flags();wrong.put("free",value);denied(()->Authorization.access(wrong));}
    Map<String,Object> locked=flags();locked.put("free",false);denied(()->Authorization.access(locked));
    Map<String,Object> rental=flags();rental.put("closing_date","tomorrow");denied(()->Authorization.access(rental));
    for(String url:new String[]{"http://tapas.io/episode/1","https://tapas.io/episode/1?buy=1",
      "https://tapas.io.evil/episode/1","https://u@tapas.io/episode/1","https://tapas.io/episode/0"})denied(()->Authorization.episodeId(url));
    Authorization.comic("https://us-a.tapas.io/pc/a.jpg?signature=private");
    for(String url:new String[]{"https://tapas.io.evil/pc/a.jpg","https://evil.tapas.io.evil/pc/a.jpg",
      "https://u@us-a.tapas.io/pc/a.jpg","https://us-a.tapas.io:444/pc/a.jpg","http://us-a.tapas.io/pc/a.jpg",
      "https://us-a.tapas.io/thumbnail/a.jpg","https://us-a.tapas.io/pc/a.jpg#part"})denied(()->Authorization.comic(url));
    for(String path:new String[]{"/episode/1/unlock","/purchase","/payment/checkout","/claim-ticket","/buy_ink"})
      if(!Authorization.mutationPath(path))throw new AssertionError();
    if(Authorization.mutationPath("/episode/1/info"))throw new AssertionError();
    System.out.println("gate checks passed");
  }
}'''
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary); test = work/'GateTest.java'; test.write_text(harness)
            compiled = subprocess.run([javac, '--release', '11', '-d', str(work), str(source), str(test)],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run([java, '-cp', str(work), 'GateTest'], capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(checked.stdout.strip(), 'gate checks passed')
