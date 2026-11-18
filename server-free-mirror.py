#!/usr/bin/env python3
"""Jezza: zero-out-of-pocket social archive worker. Initially DRY (no Reddit API)."""
import argparse, datetime as datetime, hashlib, json, logging, os, re, sqlite3, subprocess, tempfile
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parent
DB=ROOT/"free.sqlite3"
TMP=ROOT/"temporary"
YT=ROOT/".venv/bin/yt-dlp"
GDL=ROOT/".venv/bin/gallery-dl"
CLI=ROOT/"apify-cli/node_modules/.bin/apify"
FB="https://www.facebook.com/profile.php?id=100084266395034"
IG="https://www.instagram.com/jezzamariebagaforo_/"
TT="https://www.tiktok.com/@jezzamariebagaforo"
SUB="jezzamariebagaforo"
MAX_BUDGET=4.4
LOG=logging.getLogger("jezza")

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def parse_date(v):
    if not v: return now()
    try:
        if isinstance(v,(int,float)) or str(v).isdigit():
            return datetime.datetime.fromtimestamp(float(v),datetime.timezone.utc).isoformat()
        return datetime.datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(datetime.timezone.utc).isoformat()
    except (ValueError,OverflowError,TypeError): return now()

def caption(v):
    s=re.sub(r"https?://\S+","",str(v or "Jezza Marie Bagaforo").replace("\n"," "))
    return (re.sub(r"\s+"," ",s).strip() or "Jezza Marie Bagaforo")[:240]

def cfg():
    f=ROOT/".env.free"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith("#") or "=" not in line: continue
            k,v=line.split("=",1)
            os.environ.setdefault(k.strip(),v.strip().strip("'\""))

def connect():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS posts(
          uid TEXT PRIMARY KEY, platform TEXT NOT NULL, source TEXT NOT NULL,
          title TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL,
          state TEXT NOT NULL, tries INTEGER DEFAULT 0, sha TEXT, phash TEXT,
          reddit_id TEXT, reddit_url TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS spend(month TEXT,platform TEXT,upper_bound REAL);
    """)
    return c

def setting(c,k):
    v=c.execute("SELECT v FROM settings WHERE k=?",(k,)).fetchone()
    return v["v"] if v else None

def setsetting(c,k,v):
    c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES (?,?)",(k,str(v)))

def insert(c,platform,url,text,kind,created):
    if not url.startswith("https://"):return 0
    if platform=="facebook" and "/permalink.php" in url:
        from urllib.parse import urlparse,parse_qs,urlencode
        obj=urlparse(url)
        q=parse_qs(obj.query)
        required={k:v[0] for k,v in q.items() if k in ("story_fbid","id") and v}
        url=obj.scheme+"://"+obj.netloc+obj.path+"?"+urlencode(required)
    else:
        url=url.split("?")[0].rstrip("/")
    uid=platform+":"+hashlib.sha1(url.encode()).hexdigest()[:22]
    state="archive" if not setting(c,"initialized_"+platform) else "fresh"
    cur=c.execute("""INSERT OR IGNORE INTO posts
      (uid,platform,source,title,kind,created,state) VALUES (?,?,?,?,?,?,?)""",
      (uid,platform,url,caption(text),kind,parse_date(created),state))
    return cur.rowcount

def apify(c,actor,payload,upper,platform):
    month=now()[:7]
    reserved=c.execute("SELECT COALESCE(SUM(upper_bound),0) FROM spend WHERE month=?",(month,)).fetchone()[0]
    if reserved+upper>MAX_BUDGET:
        raise RuntimeError("Free Apify usage cap reached; skipping paid-run risk")
    c.execute("INSERT INTO spend(month,platform,upper_bound) VALUES (?,?,?)",(month,platform,upper))
    c.commit()
    login=subprocess.run([str(CLI),"auth","token"],capture_output=True,text=True,timeout=25,
                          env={**os.environ,"APIFY_CLI_DISABLE_TELEMETRY":"1",
                               "PATH":"/home/alphaadmin/.local/node22/bin:"+os.environ.get("PATH","")})
    if login.returncode or not login.stdout.strip():
        raise RuntimeError("Apify CLI authorization expired; run official CLI login again")
    token=login.stdout.strip().splitlines()[-1].strip() # secret used only in memory
    r=requests.post("https://api.apify.com/v2/acts/"+actor.replace("/","~")+"/run-sync-get-dataset-items",
       params={"maxTotalChargeUsd":upper,"format":"json"},
       headers={"Authorization":"Bearer "+token},
       json=payload,timeout=210)
    r.raise_for_status()
    data=r.json()
    if not isinstance(data,list): raise RuntimeError("Apify returned unexpected output")
    return data

def scan_ig(c):
    init=bool(setting(c,"initialized_instagram"))
    rows=apify(c,"socialscrapers/instagram-scraper",
        {"directUrls":[IG],"resultsType":"posts","resultsLimit":8 if init else 75},
        0.005 if init else 0.28,"instagram")
    n=0
    for x in rows:
        url=x.get("url") or ""
        if "instagram.com/" in url:
            kind="video" if x.get("type") in ("Video","Reel") else "image"
            n+=insert(c,"instagram",url,x.get("caption"),kind,x.get("timestamp"))
    setsetting(c,"initialized_instagram",now());c.commit()
    LOG.info("instagram items=%d newly-queued=%d",len(rows),n)

def scan_fb(c):
    init=bool(setting(c,"initialized_facebook"))
    rows=apify(c,"parsebird/facebook-user-posts-scraper",
      {"findPostsBy":"timeline","targets":[FB],"maxPostsPerTarget":6 if init else 65,
       "includeTranscripts":False},0.023 if init else 0.33,"facebook")
    n=0
    for x in rows:
        if x.get("postType") in ("status","text") and not x.get("media"):continue
        url=x.get("postUrl") or ""
        if "facebook.com/" in url:
            kind="video" if x.get("postType") in ("reel","video") else "image"
            n+=insert(c,"facebook",url,x.get("text"),kind,x.get("createdAt"))
    setsetting(c,"initialized_facebook",now());c.commit()
    LOG.info("facebook items=%d newly-queued=%d",len(rows),n)

def scan_tt(c):
    init=bool(setting(c,"initialized_tiktok"))
    rows=[]
    for count in ([12,6] if not init else [10,4]):
        p=subprocess.run([str(YT),"--flat-playlist","--playlist-end",str(count),
            "--socket-timeout","12","--extractor-retries","0","--dump-json",TT],
            capture_output=True,text=True,timeout=50)
        for line in p.stdout.splitlines():
            try: rows.append(json.loads(line))
            except ValueError: pass
        if rows:
            break
    if not rows:
        # Apify fallback is capped at ONCE per day and uses only free credits.
        day=now()[:10]
        if setting(c,"tiktok_fallback_day")==day:
            raise RuntimeError("Direct TikTok unavailable; daily free fallback already attempted")
        setsetting(c,"tiktok_fallback_day",day)
        c.commit()
        rows=apify(c,"clockworks/tiktok-profile-scraper",
            {"profiles":["jezzamariebagaforo"],"resultsPerPage":20 if not init else 7,
             "shouldDownloadVideos":False,"shouldDownloadCovers":False,
             "shouldDownloadSlideshowImages":False},0.085 if not init else 0.025,"tiktok-fallback")
    if not rows: raise RuntimeError("No TikTok records retrieved")
    n=0
    for x in rows:
        url=x.get("webVideoUrl") or x.get("webpage_url") or x.get("url") or ""
        if "tiktok.com/@" in url:
            n+=insert(c,"tiktok",url,x.get("text") or x.get("description") or x.get("title"),
                      "image" if "/photo/" in url else "video",
                      x.get("createTimeISO") or x.get("createTime") or x.get("timestamp"))
    setsetting(c,"initialized_tiktok",now());c.commit()
    LOG.info("tiktok items=%d newly-queued=%d",len(rows),n)

def scan(c,platform):
    for name,fn in [("tiktok",scan_tt),("instagram",scan_ig),("facebook",scan_fb)]:
        if platform not in (name,"all"):continue
        try:fn(c)
        except Exception as e:LOG.error("%s: %s",name,e)

def media_files(directory):
    ext={".jpg",".jpeg",".png",".webp",".gif",".mp4",".mov",".mkv",".webm"}
    return sorted([p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in ext])[:10]

def fetch(url,directory):
    import imageio_ffmpeg
    directory.mkdir(parents=True,exist_ok=True)
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    cmd=[str(YT),"--no-playlist","--socket-timeout","15","--extractor-retries","1",
        "--ffmpeg-location",ffmpeg,"--merge-output-format","mp4","-o",str(directory/"%(id)s.%(ext)s"),url]
    try:
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=210)
        if p.returncode:LOG.warning("yt-dlp not available for this post; trying gallery-dl")
    except subprocess.TimeoutExpired:LOG.warning("yt-dlp timeout; trying gallery-dl")
    if not media_files(directory):
        try:subprocess.run([str(GDL),"-D",str(directory),"--range","1-10",url],
                           capture_output=True,timeout=210)
        except subprocess.TimeoutExpired:pass
    paths=media_files(directory)
    if not paths and "instagram.com/p/" in url:
        # Public OG image fallback for Instagram photo/carousel posts.
        # When carousel slides are unavailable, this captures the first image only.
        from html.parser import HTMLParser
        from urllib.parse import urlparse
        class PhotoMeta(HTMLParser):
            def __init__(self):super().__init__();self.image=None
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=="meta" and attrs.get("property")=="og:image":
                    self.image=attrs.get("content")
        page=requests.get(url+"/" if not url.endswith("/") else url,
                         headers={"User-Agent":"Mozilla/5.0"},timeout=18)
        if page.ok:
            parser=PhotoMeta();parser.feed(page.text)
            imageurl=parser.image
            host=urlparse(imageurl or "").hostname or ""
            if imageurl and (host.endswith(".cdninstagram.com") or host=="cdninstagram.com"):
                response=requests.get(imageurl,timeout=25,stream=True)
                response.raise_for_status()
                image=directory/"instagram-first-photo.jpg"
                size=0
                with image.open("wb") as out:
                    for chunk in response.iter_content(64*1024):
                        size+=len(chunk)
                        if size>18*1024*1024:
                            raise RuntimeError("Instagram image exceeds upload limit")
                        out.write(chunk)
                paths=media_files(directory)
    if not paths:raise RuntimeError("No native media available at this public source URL")
    return paths

def prepare(files,folder):
    from PIL import Image
    import imageio_ffmpeg
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    result=[]
    for ix,p in enumerate(files):
        if p.suffix.lower() in (".jpg",".jpeg",".png",".webp",".gif"):
            out=folder/("upload-%d.jpg"%ix)
            with Image.open(p) as img:img.convert("RGB").save(out,"JPEG",quality=88,optimize=True)
            if out.stat().st_size>18*1024*1024:raise RuntimeError("Image too large for native Reddit post")
        else:
            out=folder/("upload-%d.mp4"%ix)
            if p.suffix.lower()==".mp4" and p.stat().st_size<=190*1024*1024:
                import shutil
                shutil.copy2(p,out)
            else:
                x=subprocess.run([ffmpeg,"-y","-i",str(p),"-vf","scale=min(1080\\,iw):-2",
                   "-c:v","libx264","-crf","27","-preset","fast","-c:a","aac","-b:a","128k",str(out)],
                   capture_output=True,timeout=220)
                if x.returncode:raise RuntimeError("Video conversion failed")
            if out.stat().st_size>190*1024*1024:raise RuntimeError("Video too large to upload safely")
        result.append(out)
    # Reddit galleries can contain images; if mixed, publish only first video.
    if len(result)>1 and any(p.suffix==".mp4" for p in result):
        result=[next(p for p in result if p.suffix==".mp4")]
    return result

def fingerprints(files):
    from PIL import Image
    from io import BytesIO
    import imagehash, imageio_ffmpeg
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    exact=hashlib.sha256()
    records=[]
    for p in files:
        with p.open("rb") as handle:
            while True:
                chunk=handle.read(1024*1024)
                if not chunk:break
                exact.update(chunk)
        if p.suffix==".jpg":
            with Image.open(p) as im:records.append("I"+str(imagehash.phash(im.convert("RGB"))))
        else:
            frames=[]
            for seconds in ("0.5","2.0","5.0"):
                try:
                    call=subprocess.run([ffmpeg,"-v","error","-ss",seconds,"-i",str(p),
                         "-frames:v","1","-vf","scale=96:96","-f","image2pipe","-vcodec","mjpeg","-"],
                         capture_output=True,timeout=20)
                    if call.returncode==0 and call.stdout:
                        with Image.open(BytesIO(call.stdout)) as im:
                            frames.append(str(imagehash.phash(im.convert("RGB"))))
                except (subprocess.TimeoutExpired,ValueError,OSError):pass
            records.append("V"+":".join(frames) if frames else "")
    return exact.hexdigest(),json.dumps(records)

def dup(c,sha,hashes):
    from imagehash import hex_to_hash
    row=c.execute("SELECT uid FROM posts WHERE state='posted' AND sha=? LIMIT 1",(sha,)).fetchone()
    if row:return row["uid"]
    try: incoming=json.loads(hashes)
    except (ValueError,TypeError):return None
    if len(incoming)!=1 or not incoming[0]:return None
    for r in c.execute("SELECT uid,phash FROM posts WHERE state='posted' AND phash IS NOT NULL"):
        try: older=json.loads(r["phash"])
        except (ValueError,TypeError):continue
        if len(older)!=1 or not older[0] or older[0][0]!=incoming[0][0]:continue
        first=incoming[0][1:].split(":")
        second=older[0][1:].split(":")
        if len(first)!=len(second) or not first:continue
        if incoming[0][0]=="V" and len(first)<3:continue
        threshold=5 if incoming[0][0]=="I" else 10
        try:
            if all(hex_to_hash(a)-hex_to_hash(b)<=threshold for a,b in zip(first,second)):
                return r["uid"]
        except (ValueError,TypeError):continue
    return None

def reddit_account():
    need=("REDDIT_CLIENT_ID","REDDIT_CLIENT_SECRET","REDDIT_REFRESH_TOKEN")
    if not all(os.getenv(k) for k in need):
        raise RuntimeError("Approved Reddit Data API OAuth app required; nothing will be posted")
    import praw
    return praw.Reddit(client_id=os.environ["REDDIT_CLIENT_ID"],
       client_secret=os.environ["REDDIT_CLIENT_SECRET"],
       refresh_token=os.environ["REDDIT_REFRESH_TOKEN"],
       user_agent="linux:jezza_reddit_mirror:v1.0 (by /u/hero2123)")

def post(reddit,files,headline):
    from praw.models import PostMedia
    sub=reddit.subreddit(SUB)
    payload=[PostMedia(str(p)) for p in files]
    if all(x.suffix==".jpg" for x in files):
        result=sub.submit(headline,image=payload[0]) if len(files)==1 else sub.submit(headline,gallery=payload)
    else:
        result=sub.submit(headline,video=payload[0])
    if not result or not result.id:raise RuntimeError("Post result unknown; quarantine without automatic retry")
    verified=reddit.submission(id=result.id)
    if verified.subreddit.display_name.lower()!=SUB:raise RuntimeError("Wrong subreddit; investigate manually")
    return result.id,"https://www.reddit.com"+result.permalink

def dispatch(c,dry_run=False):
    if c.execute("SELECT COUNT(*) FROM posts WHERE state='uploading'").fetchone()[0]:
        raise RuntimeError("Unresolved upload; not risking duplicate")
    item=c.execute("SELECT * FROM posts WHERE state='fresh' ORDER BY created DESC LIMIT 1").fetchone()
    if not item and setting(c,"last_archive_hour")!=now()[:13]:
        item=c.execute("SELECT * FROM posts WHERE state='archive' ORDER BY created LIMIT 1").fetchone()
    if not item:
        LOG.info("No fresh post or archive slot available")
        return
    LOG.info("Selected %s %s %s",item["state"],item["platform"],item["title"][:80])
    if dry_run or os.getenv("ENABLE_POSTING","no")!="yes":
        LOG.info("Posting disabled; queue untouched");return
    reddit=reddit_account()  # validates authorized API access before any file download
    c.execute("UPDATE posts SET state='uploading',tries=tries+1 WHERE uid=?",(item["uid"],));c.commit()
    TMP.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="jezza-",dir=TMP) as folder:
            folder=Path(folder)
            files=prepare(fetch(item["source"],folder/"src"),folder)
            sha,phash=fingerprints(files)
            duplicate=dup(c,sha,phash)
            if duplicate:
                c.execute("UPDATE posts SET state='duplicate',sha=?,phash=?,error=? WHERE uid=?",
                    (sha,phash,"Duplicate media of "+duplicate,item["uid"]))
                c.commit();LOG.info("Skipped duplicate");return
            post_id,link=post(reddit,files,item["title"])
            c.execute("""UPDATE posts SET state='posted',sha=?,phash=?,reddit_id=?,reddit_url=?,error=NULL
                         WHERE uid=?""",(sha,phash,post_id,link,item["uid"]))
            if item["state"]=="archive":setsetting(c,"last_archive_hour",now()[:13])
            c.commit()
            LOG.info("VERIFIED: %s",link)
        # Entire folder is deleted here even if a failure occurred.
    except Exception as err:
        c.execute("UPDATE posts SET state='needs_review',error=? WHERE uid=?",(str(err)[:500],item["uid"]))
        c.commit();LOG.error("Upload quarantined: %s",err)

def status(c):
    for r in c.execute("SELECT platform,state,count(*) AS n FROM posts GROUP BY platform,state ORDER BY platform,state"):
        print(r["platform"],r["state"],r["n"])
    for r in c.execute("SELECT uid,error FROM posts WHERE state='needs_review' LIMIT 5"):
        print("REVIEW",r["uid"],r["error"])
    reserve=c.execute("SELECT COALESCE(SUM(upper_bound),0) FROM spend WHERE month=?",(now()[:7],)).fetchone()[0]
    print("Apify worst-case monthly reserved: USD",round(reserve,3),"of USD",MAX_BUDGET)
    print("Reddit posting enabled:",os.getenv("ENABLE_POSTING","no"))
    print("Reddit OAuth present:",bool(os.getenv("REDDIT_REFRESH_TOKEN")))
    print("Temporary media folders:",len(list(TMP.iterdir())) if TMP.exists() else 0)

def main():
    cfg()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    s=sub.add_parser("scan");s.add_argument("--platform",choices=["tiktok","instagram","facebook","all"],default="all")
    d=sub.add_parser("dispatch");d.add_argument("--dry-run",action="store_true")
    sub.add_parser("status")
    args=ap.parse_args()
    c=connect()
    try:
        if args.cmd=="scan":scan(c,args.platform)
        elif args.cmd=="dispatch":dispatch(c,args.dry_run)
        else:status(c)
    finally:c.close()

if __name__=="__main__":main()
