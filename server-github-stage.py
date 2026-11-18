#!/usr/bin/env python3
"""Stage ONE authorized Jezza image/GIF on public GitHub release, without permanent local media.
This tool never publishes to Reddit. Publication is handled by the Reddit Devvit app.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from imageio_ffmpeg import get_ffmpeg_exe
import free_mirror as base

ROOT = Path(__file__).resolve().parent
GH = "/home/alphaadmin/.local/bin/gh"
REPO = "heroo2123/jezza-reddit-archive-feed"
TAG = "staging"
LOG = logging.getLogger("stage")

def github(*args: str, timeout: int = 40, input: str | None = None) -> str:
    p = subprocess.run([GH, *args], input=input, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError("GitHub operation failed: " + p.stderr[-350:])
    return p.stdout

def gh_json(path: str):
    try:
        return json.loads(github("api", "repos/" + REPO + "/contents/" + path))
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise

def get_manifest() -> dict:
    response = gh_json("feed.json")
    if not response:
        return {"version": 1, "enabled": False, "items": [], "generatedAt": base.now()}
    import base64
    text = base64.b64decode(response.get("content", "").replace("\n","")).decode()
    result = json.loads(text)
    if not isinstance(result,dict) or not isinstance(result.get("items"),list):
        raise ValueError("Unexpected feed.json structure")
    return result

def put_manifest(obj:dict) -> None:
    import base64
    obj["generatedAt"]=base.now()
    data=json.dumps(obj,ensure_ascii=False,separators=(",",":"),sort_keys=True)
    encoded=base64.b64encode(data.encode()).decode()
    current=gh_json("feed.json")
    args=["api","--method","PUT","repos/"+REPO+"/contents/feed.json",
          "-f","message=Update media staging queue","-f","content="+encoded]
    if current:
        args += ["-f","sha="+current["sha"]]
    github(*args,timeout=60)

def same_media(c,sha,phash):
    import imagehash
    row=c.execute("""SELECT uid FROM posts WHERE state IN ('staged','posted')
           AND sha=? LIMIT 1""",(sha,)).fetchone()
    if row:return row["uid"]
    try:v=json.loads(phash)
    except Exception:return None
    if not v or len(v)!=1 or not v[0]:return None
    bits=v[0]
    if bits[0] not in ("I","V"):return None
    a=bits[1:].split(":")
    for row in c.execute("""SELECT uid,phash FROM posts WHERE state IN ('staged','posted')
                AND phash IS NOT NULL"""):
        try: o=json.loads(row["phash"])
        except Exception:continue
        if len(o)!=1 or not o[0] or o[0][0]!=bits[0]:continue
        b=o[0][1:].split(":")
        if len(a)!=len(b):continue
        try:
            threshold=5 if bits[0]=="I" else 10
            if all(imagehash.hex_to_hash(x)-imagehash.hex_to_hash(y)<=threshold for x,y in zip(a,b)):
                return row["uid"]
        except ValueError:continue
    return None

def convert_video_to_gif(video:Path,target_dir:Path)->Path:
    """High-quality GIF only: preserve 720px width, full video, no silent 360p fallback.

    GIF cannot preserve video audio or 24-bit color. Optimized 720px/12fps
    beats the previous low-quality 360px/6fps output at the cost of larger files.
    If a full clip cannot be fitted below the safe 18MB cap we skip it instead
    of posting an unacceptably blurry, tiny clip.
    """
    import time
    ffmpeg=get_ffmpeg_exe()
    gifsicle=ROOT/"local-gifsicle/usr/bin/gifsicle"
    if not gifsicle.is_file():
        raise RuntimeError("Free local gifsicle optimizer missing; refusing low-quality fallback")
    began=time.monotonic()
    # Resolution always 720 wide; when needed sacrifice fps/palette rather
    # than downsample to 480/360. The 12fps 96-color loss90 preset was tested
    # against Jezza's original 1080x1920 MP4 (17.0MB GIF).
    trials=[(12,96,90),(10,80,105),(8,48,120)]
    for fps,colors,lossiness in trials:
        if time.monotonic()-began>310:
            break
        raw=target_dir/f"hd-raw-{fps}fps.gif"
        optimized=target_dir/f"hd-optimized-{fps}fps.gif"
        filters=(f"fps={fps},scale=720:-2:flags=lanczos,"
                 f"split[a][b];[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
                 f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
        p=subprocess.run([ffmpeg,"-nostdin","-y","-loglevel","error","-i",str(video),
                          "-vf",filters,str(raw)],capture_output=True,timeout=110)
        if p.returncode or not raw.exists():
            LOG.warning("720px GIF encoding failed for fps=%s",fps)
            continue
        raw_size=raw.stat().st_size
        if 0<raw_size<18_000_000:
            LOG.info("720px GIF %sfps ready: %s bytes (no optimization needed)",fps,raw_size)
            return raw
        if raw_size>65_000_000:
            raw.unlink(missing_ok=True)
            LOG.info("720px %sfps GIF too large to optimize; trying lower fps",fps)
            continue
        try:
            p=subprocess.run(["/usr/bin/nice","-n","12",str(gifsicle),
                        "--optimize=3",f"--lossy={lossiness}","--threads=4",
                        "-o",str(optimized),str(raw)],
                        capture_output=True,timeout=135)
            if p.returncode==0 and optimized.exists() and 0<optimized.stat().st_size<18_000_000:
                LOG.info("720px HD GIF %sfps, %s colors ready: %s bytes",
                         fps,colors,optimized.stat().st_size)
                raw.unlink(missing_ok=True)
                return optimized
        except subprocess.TimeoutExpired:
            LOG.warning("720px GIF optimizer timeout at fps=%s",fps)
        raw.unlink(missing_ok=True)
        optimized.unlink(missing_ok=True)
    raise RuntimeError("Full-length 720px animated GIF cannot fit below 18 MB; skipping poor-quality conversion")

def convert_photo_to_jpg(image:Path,target_dir:Path)->Path:
    output=target_dir/"ready.jpg"
    with Image.open(image) as photo:
        photo=photo.convert("RGB")
        photo.thumbnail((1600,1600),Image.Resampling.LANCZOS)
        photo.save(output,format="JPEG",quality=86,optimize=True)
    if output.stat().st_size>7_000_000:
        with Image.open(image) as photo:
            photo=photo.convert("RGB")
            photo.thumbnail((1280,1280),Image.Resampling.LANCZOS)
            photo.save(output,"JPEG",quality=76,optimize=True)
    if not output.exists() or output.stat().st_size>7_000_000:
        raise RuntimeError("Image too large to stage")
    return output

ARCHIVE_SOURCE_ORDER = ("tiktok", "instagram", "facebook")
ARCHIVE_ROTATION_KEY = "jezza_archive_last_staged_platform"

def candidate(c):
    # New content from ANY platform always wins. Among new items use the
    # original published timestamp (not the source platform or scan order).
    fresh = c.execute(
        """SELECT * FROM posts WHERE state='fresh' AND kind='image'
           ORDER BY created DESC,uid ASC LIMIT 1"""
    ).fetchone()
    if fresh:
        return fresh

    # When idle, cycle TikTok -> Instagram -> Facebook across the full
    # historical pool. If the next source is empty, skip to the next with
    # eligible unused content. This cursor only advances on successful
    # ARCHIVE staging, never on a failed retrieval or a new post.
    last = base.setting(c, ARCHIVE_ROTATION_KEY) or "facebook"
    begin = (ARCHIVE_SOURCE_ORDER.index(last) + 1) % len(ARCHIVE_SOURCE_ORDER) if last in ARCHIVE_SOURCE_ORDER else 0
    for delta in range(len(ARCHIVE_SOURCE_ORDER)):
        platform = ARCHIVE_SOURCE_ORDER[(begin + delta) % len(ARCHIVE_SOURCE_ORDER)]
        row = c.execute(
            """SELECT * FROM posts WHERE state='archive' AND kind='image' AND platform=?
               ORDER BY created ASC,uid ASC LIMIT 1""",
            (platform,),
        ).fetchone()
        if row:
            return row
    return None

def cleanup_manifest(manifest:dict,days:int=2)->None:
    # While the publishing feed is disabled, no Reddit posting can occur.
    # Retain already-staged assets until the application is ready, rather
    # than silently dropping them during the Reddit developer onboarding.
    if not manifest.get("enabled", False):
        LOG.info("Publishing feed disabled; staged assets retained until activation")
        return
    now=dt.datetime.now(dt.timezone.utc)
    keep=[]
    for entry in manifest.get("items",[]):
        try:
            expires=dt.datetime.fromisoformat(entry["expiresAt"].replace("Z","+00:00"))
            expired=expires<now
        except (ValueError,KeyError):
            expired=True
        if not expired:
            keep.append(entry)
            continue
        asset=entry.get("assetName")
        if isinstance(asset,str) and asset.startswith("jezza-") and asset.endswith((".gif",".jpg")):
            try:
                github("release","delete-asset",TAG,asset,"--yes","--repo",REPO,timeout=25)
                LOG.info("Removed expired public staging asset: %s",asset)
            except RuntimeError as e:
                LOG.warning("Asset cleanup delayed: %s",str(e)[:100])
                keep.append(entry)
                continue
        # Staged assets are temporary transport. Expiry after 50 hours is
        # normal whether the Devvit upload was asynchronous or verified.
        # Keep SQLite row state='staged' (and its perceptual hash) to avoid
        # accidentally treating old media as new/reposting duplicates.
        # Verified Reddit publication receipts are held in Devvit Redis.
        LOG.info("Expired staging URL for %s; local metadata remains for dedup",
                 entry.get("id"))
    manifest["items"]=keep[-64:]

def stage_one(c,manifest:dict)->bool:
    for ix in range(8):
        item=candidate(c)
        if not item:
            LOG.info("No unprepared posts left")
            return False
        uid=item["uid"]
        try:
            base.TMP.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="jezza-stage-",dir=base.TMP) as folder:
                work=Path(folder)
                sources=base.fetch(item["source"],work/"download")
                source=sources[0]
                sha,phash=base.fingerprints([source])
                duplicated=same_media(c,sha,phash)
                if duplicated:
                    c.execute("UPDATE posts SET state='duplicate',sha=?,phash=?,error=? WHERE uid=?",
                              (sha,phash,"Same media as "+duplicated,uid))
                    c.commit()
                    LOG.info("Skipped same-media duplicate %s",uid)
                    continue
                if source.suffix.lower() in (".jpg",".jpeg",".png",".webp",".gif"):
                    ready=convert_photo_to_jpg(source,work)
                    kind="image"
                else:
                    # A source marked as a photo unexpectedly resolved to
                    # video; defer it rather than degrading the video to GIF.
                    c.execute("UPDATE posts SET state='deferred_video',error=? WHERE uid=?",
                              ("Video encountered in photo-only mode; preserve MP4 source URL",uid))
                    c.commit()
                    LOG.warning("Video deferred without a low-frame-rate GIF: %s",uid)
                    continue
                asset="jezza-"+uid.split(":")[-1][:16]+ready.suffix
                renamed=work/asset
                ready.rename(renamed)
                github("release","upload",TAG,str(renamed),"--repo",REPO,timeout=120)
                url=f"https://github.com/{REPO}/releases/download/{TAG}/{asset}"
                expires=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=50)).isoformat()
                entry={
                    "id":uid,"kind":kind,"title":item["title"][:220],
                    "assetUrl":url,"assetName":asset,
                    "priority":item["state"],"createdAt":item["created"],
                    "stagedAt":base.now(),"expiresAt":expires,
                    "sha256":sha,
                }
                manifest["items"].append(entry)
                try:
                    put_manifest(manifest)
                except Exception:
                    # Asset is not registered in the feed. Delete it so storage stays clean.
                    try:github("release","delete-asset",TAG,asset,"--yes","--repo",REPO)
                    except Exception:pass
                    raise
                c.execute("UPDATE posts SET state='staged',sha=?,phash=?,error=NULL WHERE uid=?",
                          (sha,phash,uid))
                if item["state"] == "archive":
                    base.setsetting(c, ARCHIVE_ROTATION_KEY, item["platform"])
                c.commit()
                LOG.info("Staged %s as %s (priority=%s,platform=%s), size=%d MB; local media deleted",
                         uid,kind,item["state"],item["platform"],renamed.stat().st_size//1048576)
                return True
        except Exception as e:
            c.execute("UPDATE posts SET state='needs_review',error=? WHERE uid=?",
                      ("Staging error: "+str(e))[:450],uid)
            c.commit()
            LOG.warning("Failed to stage %s: %s",uid,str(e)[:180])
    return False

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--stage",action="store_true")
    parser.add_argument("--enable-feed",choices=["yes","no"])
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    manifest=get_manifest()
    if args.enable_feed:
        manifest["enabled"]=args.enable_feed=="yes"
        # Reset staged URL TTL only on first activation; activation itself
        # must follow a verified Reddit app install and fetch approval.
        if manifest["enabled"]:
            stamp=base.now()
            expiry=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=50)).isoformat()
            for entry in manifest["items"]:
                entry["stagedAt"]=stamp
                entry["expiresAt"]=expiry
        put_manifest(manifest)
        LOG.info("GitHub feed enabled=%s; does not publish directly to Reddit",manifest["enabled"])
        return
    cleanup_manifest(manifest)
    if args.stage:
        db=base.connect()
        try:
            if not stage_one(db,manifest):
                put_manifest(manifest)
        finally:db.close()
    else:
        put_manifest(manifest)
    LOG.info("Feed holds %d staged items; enabled=%s",len(manifest["items"]),manifest["enabled"])

if __name__=="__main__":
    main()
