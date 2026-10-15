"""Build the small Android reference slice with official standalone SDK tools.

No Gradle/framework is required. Toolchains, debug keys and build output are
local; this script never reads a Tapas session or changes an ADB device.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from comic_archive.state_paths import session_path

PROJECT=Path(__file__).resolve().parents[2]
TOOLCHAIN=session_path(PROJECT)/'mobile/toolchain'
SOURCE=Path(__file__).resolve().parent
GOOGLE='https://dl.google.com/android/repository/'
PACKAGES={
    'platform':('platform-36_r02.zip','2c1a80dd4d9f7d0e6dd336ec603d9b5c55a6f576'),
    'build-tools':('build-tools_r36_linux.zip','b0b6376977657e8ad9b969bacf4093601da2c6fb')}
JDK_NAME='OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz'
JDK_URL='https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1/'+JDK_NAME
JDK_SHA='3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e'


def download(url,target,expected,algorithm):
    if target.exists():
        with target.open('rb') as existing:
            if hashlib.file_digest(existing,algorithm).hexdigest()==expected:return
    temporary=target.with_suffix(target.suffix+'.partial');digest=hashlib.new(algorithm)
    with httpx.stream('GET',url,follow_redirects=True,timeout=120) as reply:
        reply.raise_for_status()
        with temporary.open('wb') as out:
            for chunk in reply.iter_bytes(1024**2):out.write(chunk);digest.update(chunk)
            out.flush();os.fsync(out.fileno())
    if digest.hexdigest()!=expected:
        temporary.unlink(missing_ok=True);raise RuntimeError('Toolchain download checksum mismatch')
    temporary.replace(target);print('Verified toolchain package '+target.name,flush=True)


def setup():
    TOOLCHAIN.mkdir(parents=True,exist_ok=True,mode=0o700)
    packages=[(GOOGLE+name,TOOLCHAIN/name,checksum,'sha1') for name,checksum in PACKAGES.values()]
    packages.append((JDK_URL,TOOLCHAIN/JDK_NAME,JDK_SHA,'sha256'))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda args:download(*args),packages))
    jdk=TOOLCHAIN/'jdk';sdk=TOOLCHAIN/'sdk';sdk.mkdir(exist_ok=True)
    if not (jdk/'bin/java').exists():
        extracted=TOOLCHAIN/'jdk-unpack';extracted.mkdir(exist_ok=True)
        with tarfile.open(TOOLCHAIN/JDK_NAME) as archive:archive.extractall(extracted,filter='data')
        roots=[p for p in extracted.iterdir() if (p/'bin/java').is_file()]
        if len(roots)!=1:raise RuntimeError('Unexpected JDK archive structure')
        roots[0].replace(jdk);extracted.rmdir()
    for kind,(name,_) in PACKAGES.items():
        target=sdk/('platforms/android-36' if kind=='platform' else 'build-tools/36.0.0')
        if (target/('android.jar' if kind=='platform' else 'aapt2')).is_file():continue
        unpack=TOOLCHAIN/(kind+'-unpack');unpack.mkdir(exist_ok=True)
        with zipfile.ZipFile(TOOLCHAIN/name) as archive:
            for member in archive.infolist():
                path=Path(member.filename)
                if path.is_absolute() or '..' in path.parts:raise RuntimeError('Unsafe SDK archive member')
                archive.extract(member,unpack)
                if not member.is_dir() and member.external_attr>>16 & 0o111:
                    (unpack/path).chmod(0o755)
        roots=[p for p in unpack.iterdir() if (p/('android.jar' if kind=='platform' else 'aapt2')).is_file()]
        if len(roots)!=1:raise RuntimeError('Unexpected SDK archive structure')
        target.parent.mkdir(parents=True,exist_ok=True);roots[0].replace(target)
        shutil.rmtree(unpack)
    print('User-local JDK 17 and SDK/build tools 36 ready; existing ADB server unchanged.',flush=True)


def run(args,env):
    process=subprocess.run([str(x) for x in args],env=env,cwd=PROJECT,capture_output=True,text=True)
    if process.returncode:
        print(process.stdout[-6000:]);print(process.stderr[-6000:]);raise RuntimeError('Android build stage failed')


def build():
    jdk=TOOLCHAIN/'jdk';sdk=TOOLCHAIN/'sdk';tools=sdk/'build-tools/36.0.0';android=sdk/'platforms/android-36/android.jar'
    if not (jdk/'bin/javac').is_file() or not android.is_file():raise RuntimeError('Run with --setup to obtain the local build tools')
    env=os.environ.copy();env.update(JAVA_HOME=str(jdk),ANDROID_HOME=str(sdk),
        PATH=str(jdk/'bin')+os.pathsep+env.get('PATH',''))
    output=SOURCE/'build';output.mkdir(parents=True,exist_ok=True)
    classes=output/'classes';dex=output/'dex'
    for path in (classes,dex):
        if path.exists():shutil.rmtree(path)
        path.mkdir()
    files=sorted((SOURCE/'src').rglob('*.java'))
    run([jdk/'bin/javac','--release','11','-encoding','UTF-8','-classpath',android,'-d',classes,*files],env)
    run([tools/'d8','--min-api','26','--lib',android,'--output',dex,*sorted(classes.rglob('*.class'))],env)
    base=output/'resources.apk'
    run([tools/'aapt2','link','--manifest',SOURCE/'AndroidManifest.xml','-A',SOURCE/'assets','-I',android,'-o',base],env)
    unsigned=output/'unsigned.apk';shutil.copyfile(base,unsigned)
    with zipfile.ZipFile(unsigned,'a') as archive:
        for file in sorted(dex.glob('*.dex')):archive.write(file,file.name)
    aligned=output/'aligned.apk';run([tools/'zipalign','-f','-p','4',unsigned,aligned],env)
    key=TOOLCHAIN/'debug.keystore'
    if not key.exists():
        run([jdk/'bin/keytool','-genkeypair','-keystore',key,'-storepass','android','-keypass','android',
             '-alias','androiddebugkey','-keyalg','RSA','-keysize','2048','-validity','10000',
             '-dname','CN=Android Debug,O=Android,C=US'],env);key.chmod(0o600)
    apk=output/'comic-archive-debug.apk'
    run([tools/'apksigner','sign','--ks',key,'--ks-key-alias','androiddebugkey','--ks-pass','pass:android',
         '--key-pass','pass:android','--out',apk,aligned],env)
    run([tools/'apksigner','verify',apk],env)
    with apk.open('rb') as file: digest=hashlib.file_digest(file,'sha256').hexdigest()
    print(json.dumps({'apk':str(apk),'sha256':digest,
        'debug_build':True,'compile_api':36,'minimum_api':26,'installed':False},indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--setup',action='store_true')
    parser.add_argument('--setup-only',action='store_true');args=parser.parse_args()
    if args.setup or args.setup_only:setup()
    if not args.setup_only:build()


if __name__=='__main__':main()
