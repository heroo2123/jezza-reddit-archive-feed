# Alpha-dev source-side automation

The production code running on alpha-dev is under /home/alphaadmin/jezza-reddit-mirror/.

- server-free-mirror.py collects public source metadata and retains source links, captions, and fingerprints.
- server-github-stage.py stages one native photo at a time and cleans temporary downloads, with no silent GIF conversion.
- Devvit app in devvit-app handles one native photo per hour maximum in r/jezzamariebagaforo.
- Historical photo sources rotate TikTok, Instagram, Facebook. New photos from any source win.
- Video records remain in the queue for a future authorized MP4 uploader; no audio or source video is deliberately discarded.
- Free Apify usage includes a hard local cap. Never deploy passwords, session cookies, tokens, local SQLite files, or private environment files to GitHub.

The code can support higher-quality original MP4 output only when Reddit permits native video uploading via an authorized API; Devvit's current media capability supports still images and animated GIFs, not MP4 with audio.

The public GitHub release is temporary transport, not permanent local storage. Expired release assets are removed by periodic cleanup.
