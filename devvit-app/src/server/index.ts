import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { createServer, getServerPort, reddit } from "@devvit/web/server";
import { redis } from "@devvit/redis";
import type { UiResponse } from "@devvit/web/shared";

// Production Reddit-only publishing app. It never talks to Facebook,
// TikTok or Instagram; the owner's private Linux collector handles them.
// Video is converted to GIF (silent) before it appears in the staging feed.
const app = new Hono();
const SUB = "jezzamariebagaforo";
const FEED = "https://api.github.com/repos/heroo2123/jezza-reddit-archive-feed/contents/feed.json";
const ASSET_PREFIX = "https://github.com/heroo2123/jezza-reddit-archive-feed/releases/download/staging/";
const NS = "jezza:v2:"; // keep existing Redis receipts; never reset dedup history
const ARCHIVE_SOURCE_ORDER = ["tiktok", "instagram", "facebook"] as const;
const ARCHIVE_ROTATION_KEY = NS + "lastArchivePlatform";
const MAX_ITEMS = 64;
const NOW = () => new Date();

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
type Feed = { enabled: boolean; items: Entry[]; generatedAt: string };
type Receipt = {
  state: "pending" | "posted" | "failed";
  id: string;
  title: string;
  remoteUrl: string;
  redditId?: string;
  at: string;
  error?: string;
};
const itemKey = (id: string) => NS + "item:" + id;
const thisHour = () => NOW().toISOString().slice(0, 13);

async function feed(): Promise<Feed> {
  const r = await fetch(FEED + "?minute=" + Math.floor(Date.now() / 60_000), {
    headers: { Accept: "application/vnd.github+json", "User-Agent": "jezza-devvit-hourly-archive" },
  });
  if (!r.ok) throw Error("Public feed unavailable, HTTP " + r.status);
  const body = await r.json() as { encoding?: string; content?: string };
  if (body.encoding !== "base64" || !body.content) throw Error("Unsupported feed encoding");
  const doc: unknown = JSON.parse(Buffer.from(body.content.replace(/\s/g, ""), "base64").toString("utf8"));
  if (!doc || typeof doc !== "object" || !Array.isArray((doc as Feed).items) ||
      typeof (doc as Feed).enabled !== "boolean")
    throw Error("Malformed feed contents");
  return doc as Feed;
}

function valid(item: Entry): boolean {
  if (!item || typeof item !== "object") return false;
  if (!/^(instagram|facebook|tiktok):[a-f0-9]{22}$/.test(item.id)) return false;
  // Do not publish degraded silent GIFs. MP4 originals remain queued
  // separately until a compliant native-video uploader is available.
  if (item.kind !== "image" || !["archive", "fresh"].includes(item.priority)) return false;
  if (typeof item.title !== "string" || !item.title.trim()) return false;
  if (!/^jezza-[a-f0-9]{16}\.(gif|jpg)$/.test(item.assetName)) return false;
  if (item.assetUrl !== ASSET_PREFIX + item.assetName) return false;
  if (!item.assetName.endsWith(".jpg")) return false;
  if (!/^[a-f0-9]{64}$/.test(item.sha256)) return false;
  const staging = Date.parse(item.stagedAt);
  const expiry = Date.parse(item.expiresAt);
  return Number.isFinite(staging) && Number.isFinite(expiry) &&
    staging <= Date.now() + 60_000 && expiry > Date.now() + 120_000 &&
    Date.now() - staging < 50 * 3600_000;
}

function postTitle(entry: Entry): string {
  const scrubbed = entry.title.replace(/https?:\/\/\S+/g, "").replace(/\s+/g, " ").trim();
  return scrubbed.slice(0, 270) || "Jezza Marie Bagaforo";
}

async function getReceipt(id: string): Promise<Receipt | null> {
  const raw = await redis.get(itemKey(id));
  if (!raw) return null;
  try {
    const receipt = JSON.parse(raw) as Receipt;
    if (!receipt || receipt.id !== id || !["pending", "posted", "failed"].includes(receipt.state))
      throw Error("Invalid receipt");
    return receipt;
  } catch {
    // Corrupt receipts are never reset automatically. Avoid duplicate posts.
    throw Error("Unreadable receipt; publication halted for safety");
  }
}
async function saveReceipt(receipt: Receipt): Promise<void> {
  await redis.set(itemKey(receipt.id), JSON.stringify(receipt));
}

async function reconcile(receipt: Receipt): Promise<void> {
  if (receipt.state !== "pending") return;
  try {
    const list = await reddit.getNewPosts({ subredditName: SUB, limit: 100, pageSize: 100 }).all();
    const started = Date.parse(receipt.at) - 120_000;
    const found = list.find((p) => {
      if (p.title !== receipt.title || !p.url?.startsWith("https://i.redd.it/")) return false;
      const created = new Date(p.createdAt).getTime();
      return Number.isFinite(created) && created >= started;
    });
    if (!found) {
      console.log("Asynchronous post still unverified", receipt.id);
      return;
    }
    await saveReceipt({ ...receipt, state: "posted", redditId: found.id, at: NOW().toISOString() });
    console.log("Verified native i.redd.it post", receipt.id, found.id);
  } catch (error) {
    // A failed Reddit listing is not permission to submit again.
    console.warn("Cannot reconcile unknown outcome", receipt.id, String(error).slice(0, 150));
  }
}

async function publishOnce(): Promise<string> {
  const data = await feed();
  if (!data.enabled) return "feed disabled";
  if (await redis.get(NS + "lastAttemptHour") === thisHour()) {
    // Read-only reconciliation may run while the posting hour is reserved.
    // NEVER submit a second post in the same UTC hour.
    const pending = data.items.slice(-MAX_ITEMS).filter(valid);
    for (const item of pending) {
      const receipt = await getReceipt(item.id);
      if (receipt?.state === "pending") await reconcile(receipt);
    }
    return "hourly cap used; pending submissions audited";
  }

  const eligible = data.items.slice(-MAX_ITEMS).filter(valid);
  // Genuinely new content always has priority, regardless of platform.
  const fresh = eligible.filter(x => x.priority === "fresh")
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
  const older = eligible.filter(x => x.priority === "archive");

  // Rotate historical sources in succession across the three accounts:
  // TikTok -> Instagram -> Facebook -> TikTok. Within a source, oldest first.
  // Skip sources without eligible unposted media. The cursor persists across
  // cron executions, and advances ONLY when an archived item is submitted.
  const lastArchive = await redis.get(ARCHIVE_ROTATION_KEY);
  const lastIndex = ARCHIVE_SOURCE_ORDER.indexOf(lastArchive as typeof ARCHIVE_SOURCE_ORDER[number]);
  const start = (lastIndex + 1) % ARCHIVE_SOURCE_ORDER.length;
  const archiveOrder = Array.from({length: ARCHIVE_SOURCE_ORDER.length}, (_, offset) =>
    ARCHIVE_SOURCE_ORDER[(start + offset) % ARCHIVE_SOURCE_ORDER.length]!);
  const rotatedArchive = archiveOrder.flatMap(platform =>
    older.filter(x => x.id.startsWith(platform + ":"))
      .sort((a,b) => Date.parse(a.createdAt) - Date.parse(b.createdAt)));
  const candidates = [...fresh, ...rotatedArchive];

  for (const item of candidates) {
    const existing = await getReceipt(item.id);
    if (existing) {
      if (existing.state === "pending") await reconcile(existing);
      continue; // Never re-submit an item with any prior receipt.
    }
    await redis.set(NS + "lastAttemptHour", thisHour()); // before any submit attempt
    const receipt: Receipt = {
      id: item.id, title: postTitle(item), remoteUrl: item.assetUrl,
      state: "pending", at: NOW().toISOString(),
    };
    await saveReceipt(receipt);
    if (item.priority === "archive") {
      // Advance the archival rotation on submission ATTEMPT, including an
      // asynchronous/uncertain result; never retry the same media blindly.
      await redis.set(ARCHIVE_ROTATION_KEY, item.id.split(":")[0]!);
    }
    console.log("Jezza selected",item.id,"priority",item.priority,
                "archive order",archiveOrder.join(" -> "));
    try {
      // Verified in private subreddit: Reddit asynchronously imports GitHub
      // Release GIFs into native i.redd.it media using SubmitMediaOptions.
      const posted = await reddit.submitPost({
        subredditName: SUB,
        title: receipt.title,
        kind: "image",
        imageUrls: [receipt.remoteUrl],
      });
      if (posted?.id) {
        await saveReceipt({ ...receipt, state: "posted", redditId: posted.id, at: NOW().toISOString() });
        console.log("Native image post returned ID", item.id, posted.id);
        return "posted " + item.id;
      }
      console.log("Reddit image submission accepted without synchronous ID", item.id);
      return "asynchronous submission " + item.id;
    } catch (error) {
      // In testing, an SDK error about asynchronous creation really DID
      // publish the Reddit image. The receipt stays pending; no blind retry.
      console.warn("Asynchronous/uncertain Reddit response", item.id, String(error).slice(0, 180));
      return "submitted, waiting verification " + item.id;
    }
  }
  return "no unposted eligible staged content";
}

app.post("/internal/scheduler/hourly-post", async (c) => {
  try {
    const result = await publishOnce();
    console.log("Jezza hourly result", result);
    return c.json({ status: "ok", result });
  } catch (error) {
    console.error("Jezza hourly error", String(error).slice(0, 200));
    return c.json({ status: "error" });
  }
});

app.post("/internal/menu/archive-status", async (c) => {
  const enabled = (await feed().catch(() => null))?.enabled || false;
  const latest = await redis.get(NS + "lastAttemptHour");
  const message = enabled
    ? "Jezza archive enabled. Last attempted hour: " + (latest ?? "none")
    : "Jezza archive is disabled; no images will be posted.";
  return c.json<UiResponse>({ showToast: { text: message, appearance: "success" } });
});

serve({ fetch: app.fetch, createServer, port: getServerPort() });
