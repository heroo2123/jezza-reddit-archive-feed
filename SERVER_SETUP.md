# Source-side helpers (alpha-dev)

These scripts are included for auditability. They are already running from
`/home/alphaadmin/jezza-reddit-mirror` on the owner's server.

- `server-free-mirror.py`: source metadata collection with a local Apify free-tier
  cap, TikTok fallback, and media conversion helpers.
- `server-github-stage.py`: publishes one short-lived optimized JPEG/GIF to the
  public staging release, updates the disabled-by-default feed and cleans up.

Do not place Reddit tokens, Apify tokens, GitHub tokens, `.env.free`, cookie
storage or SQLite database files in this GitHub repository.

To finish installation, the owner must authorize Devvit CLI, create the Devvit
app under Reddit's Developer Portal, install it to the subreddit and test one
image + one GIF. The feed must remain disabled until those tests pass.

The GitHub release is only temporary transport: local downloaded media is
never retained after staging and expired release assets are cleaned every six
hours. Free GitHub release staging is best-effort and subject to fair-use limits.
