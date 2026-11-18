# Privacy Policy — Jezza Hourly Archive

Last updated: 18 November 2026

This non-commercial Reddit Developer Platform app is used by the moderators
of r/jezzamariebagaforo to republish creator-authorized public photos/videos.

The service does NOT ask users for account credentials, access private Reddit
messages, profile data, cookies, health data, financial data, or contact lists.
It does not track readers or collect analytical identifiers.

The app reads a publicly accessible GitHub feed containing IDs and captions
of authorized public social media posts and a short-lived JPEG/GIF staging URL.
It uploads that media into Reddit-hosted media and posts it to the subreddit.
It stores post IDs, processing states, and a limited duplicate-prevention
record in Reddit's app-scoped Redis storage.

The external helper uses public profile content from social networks chosen
by the creator/moderator. Its SQLite database retains only source URLs,
captions, timestamps, and hashes; downloaded media is deleted immediately
after staging. Temporary public GitHub media assets are scheduled for removal
within about 50 hours. Reddit-hosted media and posts remain until removed
under Reddit's normal deletion/retention rules.

No payments, advertising tracking or sale of data are performed. The app
does not disclose private data to third parties. Reddit and GitHub process
content according to their respective policies.

For content removal or questions, contact the moderators of
r/jezzamariebagaforo through Reddit modmail. The creator's republication
permission can be revoked, in which case the moderators can disable new
publishing and remove previously published posts where appropriate.
