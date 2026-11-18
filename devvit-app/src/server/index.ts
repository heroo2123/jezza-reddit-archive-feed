import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { createServer, getServerPort, reddit, media } from "@devvit/web/server";
import { redis } from "@devvit/redis";
import type { UiResponse } from "@devvit/web/shared";

// Scheduled, app-account native Reddit IMAGE posts, including animated GIFs.
// No external Reddit Data API credentials. No video/audio MP4 capability.
const app = new Hono();
const SUB = "jezzamariebagaforo";
const REPO = "heroo2123/jezza-reddit-archive-feed";
const FEED = "https://api.github.com/repos/" + REPO + "/contents/feed.json";
const ASSET_PREFIX = "https://github.com/" + REPO + "/releases/download/staging/";
const KEY_PREFIX = "jezza:v1:";
const MAX_ITEMS = 64;
const MAX_AGE_MS = 50 * 3600_000;

type Entry = {
  id: string;
  kind: "image" | "gif";
  title: string;
  assetUrl: string;
  assetName: string;
  priority: "fresh" | "archive";
  createdAt: string;
  stagedAt: string;
  expiresAt: string;
  sha256: string;
};
type Feed = {
  enabled: boolean;
  items: Entry[];
  generatedAt: string;
};
type Receipt = {
  state: "uploading" | "pending" | "posted" | "retryable";
  id: string;
  mediaUrl?: string;
  redditId?: string;
  at: string;
  error?: string;
};
const now = () => new Date();
const todayHour = () => now().toISOString().slice(0, 13);
const itemKey = (id: string) => KEY_PREFIX + id;

async function getFeed(): Promise<Feed> {
  const rsp = await fetch(FEED + "?t=" + Math.floor(Date.now() / 60000), {
    method: "GET",
    headers: {
      Accept: "application/vnd.github+json",
      "User-Agent": "jezza-hourly-archive-devvit",
    },
  });
  if (!rsp.ok) throw Error("GitHub feed is unavailable: HTTP " + rsp.status);
  const body = await rsp.json() as { content?: string; encoding?: string };
  if (body.encoding !== "base64" || typeof body.content !== "string")
    throw Error("GitHub feed returned unexpected encoding");
  const raw = Buffer.from(body.content.replace(/\s+/g, ""), "base64").toString("utf8");
  const data: unknown = JSON.parse(raw);
  if (!data || typeof data !== "object" ||
      !Array.isArray((data as Feed).items) ||
      typeof (data as Feed).enabled !== "boolean")
    throw Error("Malformed source feed");
  return data as Feed;
}

function valid(item: Entry): boolean {
  if (!item || typeof item !== "object") return false;
  if (!["gif", "image"].includes(item.kind)) return false;
  if (typeof item.id !== "string" || !/^(instagram|tiktok|facebook):[a-f0-9]{22}$/.test(item.id))
    return false;
  if (typeof item.title !== "string" || !item.title.trim()) return false;
  if (!item.assetName?.startsWith("jezza-") ||
      !/^jezza-[a-f0-9]{16}\.(gif|jpg)$/.test(item.assetName)) return false;
  if (item.kind === "gif" && !item.assetName.endsWith(".gif")) return false;
  if (item.kind === "image" && !item.assetName.endsWith(".jpg")) return false;
  if (item.assetUrl !== ASSET_PREFIX + item.assetName) return false;
  const expiry = new Date(item.expiresAt).getTime();
  const staged = new Date(item.stagedAt).getTime();
  if (!Number.isFinite(expiry) || !Number.isFinite(staged)) return false;
  if (expiry < Date.now() + 60_000 || staged > Date.now() + 60_000) return false;
  if (Date.now() - staged > MAX_AGE_MS) return false;
  if (typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256)) return false;
  return true;
}

async function getReceipt(id: string): Promise<Receipt | null> {
  const content = await redis.get(itemKey(id));
  if (!content) return null;
  try {
    const receipt = JSON.parse(content) as Receipt;
    return receipt && typeof receipt.state === "string" ? receipt : null;
  } catch { return null; }
}
async function setReceipt(receipt: Receipt): Promise<void> {
  await redis.set(itemKey(receipt.id), JSON.stringify(receipt));
}

async function reconcile(entry: Entry, receipt: Receipt): Promise<Receipt> {
  if (receipt.state !== "pending" || !receipt.mediaUrl) return receipt;
  const recent = await reddit.getNewPosts({
    subredditName: SUB,
    limit: 100,
    pageSize: 100,
  }).all();
  // Exact unique Reddit-hosted image CDN URL prevents title-based false matches.
  const found = recent.find((post) =>
    post.url === receipt.mediaUrl ||
    post.url.split("?")[0] === receipt.mediaUrl!.split("?")[0]
  );
  if (found) {
    const updated: Receipt = {
      ...receipt, state: "posted", redditId: found.id, at: now().toISOString()
    };
    await setReceipt(updated);
    console.log("Verified async image post", entry.id, found.id);
    return updated;
  }
  return receipt;
}

async function publishNext(): Promise<string> {
  const feed = await getFeed();
  if (!feed.enabled) return "disabled by feed owner";
  if (await redis.get(KEY_PREFIX + "lastAttemptHour") === todayHour())
    return "hourly safety limit reached";
  const candidates = feed.items.slice(-MAX_ITEMS)
    .filter(valid)
    .sort((a, b) => {
      if (a.priority !== b.priority) return a.priority === "fresh" ? -1 : 1;
      return a.priority === "fresh" ?
        Date.parse(b.createdAt) - Date.parse(a.createdAt) :
        Date.parse(a.createdAt) - Date.parse(b.createdAt);
    });
  for (const item of candidates) {
    const existing = await getReceipt(item.id);
    if (existing?.state === "pending") {
      try { await reconcile(item, existing); }
      catch (error) { console.error("Reconciliation unavailable", item.id, error); }
      // A pending upload is deliberately never resubmitted to avoid duplicates.
      continue;
    }
    if (existing && existing.state !== "retryable") continue;
    // Reserve the hour before making any upload or post call.
    await redis.set(KEY_PREFIX + "lastAttemptHour", todayHour());
    await setReceipt({ state: "uploading", id: item.id, at: now().toISOString() });
    let uploaded: {mediaUrl: string};
    try {
      uploaded = await media.upload({
        url: item.assetUrl,
        type: item.kind,
      });
      if (!uploaded?.mediaUrl?.startsWith("https://"))
        throw Error("No Reddit-hosted media URL returned");
    } catch (e) {
      await setReceipt({
        state: "retryable", id: item.id, at: now().toISOString(),
        error: String(e).slice(0, 200),
      });
      console.error("Media upload failed before posting", item.id, e);
      return "media upload failed; no Reddit submission attempted";
    }
    // Save unique CDN URL BEFORE calling Reddit so uncertain outcomes can be
    // reconciled via new posts instead of accidentally being posted twice.
    await setReceipt({
      state: "pending", id: item.id, mediaUrl: uploaded.mediaUrl, at: now().toISOString()
    });
    try {
      const post = await reddit.submitPost({
        subredditName: SUB,
        title: item.title.replace(/https?:\/\/\S+/g, "").slice(0, 290).trim() || "Jezza Marie Bagaforo",
        kind: "image",
        imageUrls: [uploaded.mediaUrl],
      });
      const receipt: Receipt = {
        state: "posted", id: item.id, mediaUrl: uploaded.mediaUrl,
        redditId: post.id, at: now().toISOString()
      };
      await setReceipt(receipt);
      console.log("Reddit image post verified", item.id, post.id);
      return "published " + item.id + " post=" + post.id;
    } catch (error) {
      // Devvit may throw when an image post is still processing asynchronously.
      // Keep pending and never retry the submission; reconcile on later ticks.
      console.warn("Reddit submit may be async; awaiting reconciliation",
        item.id, String(error).slice(0, 250));
      return "submitted; verification pending for " + item.id;
    }
  }
  return "no eligible unposted content";
}

app.post("/internal/scheduler/hourly-post", async (c) => {
  try {
    const result = await publishNext();
    console.log("Jezza scheduler", result);
    return c.json({status:"ok", result});
  } catch (e) {
    console.error("Scheduler error", e);
    return c.json({status:"error"});
  }
});

app.post("/internal/menu/archive-status", async (c) => {
  const enabled = (await getFeed().catch(() => null))?.enabled ?? false;
  const lastHour = await redis.get(KEY_PREFIX + "lastAttemptHour");
  const message = enabled
    ? "Jezza archive enabled. Last posting attempt: " + (lastHour || "none")
    : "Jezza archive disabled; no posts will be submitted.";
  return c.json<UiResponse>({ showToast: message });
});

serve({
  fetch: app.fetch,
  createServer,
  port: getServerPort(),
});
