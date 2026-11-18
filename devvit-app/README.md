# Jezza Hourly Archive — Reddit Devvit publisher

Non-commercial moderator tool for r/jezzamariebagaforo.

## Purpose
Automatically publishes authorized public creator media (one at most per UTC hour)
as native Reddit image posts. Original photos are posted as JPEGs; videos
are converted into animated GIFs (silent) because this Devvit implementation
does not support native MP4-with-audio uploads.

## Architecture
1. The owner's existing Linux alpha-dev server monitors Jezza's Instagram,
   TikTok and Facebook accounts and holds a SQLite metadata-only queue.
2. Once per hour, alpha-dev selects new media first, otherwise an old media item.
3. The selected media is downloaded into a temporary directory, converted
   to one optimized JPEG/GIF below 7MB, and uploaded to a short-lived public
   GitHub release asset; temporary local media is deleted immediately.
4. A small public feed.json is updated with the title and temporary asset URL.
5. The Devvit scheduler runs at minute 8 each hour (UTC). It fetches feed.json,
   uploads the media to Reddit's CDN with media.upload(), and creates a normal
   native image post via reddit.submitPost() under the app account.
6. The app stores per-item receipts in Redis, reconciles asynchronous submissions,
   and will never blindly repeat an accepted upload.
7. GitHub staging assets expire after approximately 50 hours.

The GitHub feed includes only public authorized post metadata and short-lived
media URLs, with no account passwords or private Reddit data. The GitHub repo
is public because Devvit must read the feed without storing a personal GitHub
credential.

## Safety and operational controls
- Feed is disabled by default. Explicitly set enabled=true after app installation.
- Redis enforces a maximum of one submission attempt per UTC hour.
- No app payments, advertising, engagement automation, DMs or unsolicited comments.
- No source URLs are included in Reddit titles or bodies.
- Reddit app account publishes to the named subreddit only.
- Media uploads are limited to 7 MB for resilience (Reddit's maximum 20 MB).
- Failed or uncertain submissions are held for review; no blind reposts.
- No permanent local media archive is retained.
- The free Apify usage budget is locally capped; the source scanner stops
  before exceeding its free allocation.

## Fetch Domains
- api.github.com: GitHub's documented public Contents API provides the small
  public feed.json used by the app. Fetch is read-only and does not transmit
  private user data or secrets. This is required because subreddit scheduled
  jobs cannot read the owner's local SQLite database.

The Devvit app fetches no other external domains itself. Reddit's media
uploader downloads the temporary media asset from the URL in the feed.

## User privacy
See PRIVACY.md and TERMS.md at the repository root.

## Status
Built and typechecked with Devvit 0.15.4. NOT yet registered, approved, or
installed into r/jezzamariebagaforo. No Reddit posts have been made.
The CLI requires the subreddit owner's one-time Reddit authorization.

## Developer build
npm install
npm run build
npx devvit login --copy-paste
npx devvit upload
npx devvit playtest r/jezzamariebagaforo

The build script compiles src/server/index.ts into dist/server/index.cjs.
Actual Devvit service behavior requires a Reddit app installation and testing.
Do not activate the source staging cron or the feed until a test publication
is verified and the needed HTTP fetch domain is approved by Reddit.
