/**
 * What counts as media, for both halves of the product.
 *
 * The console writes these values into an article document and the public feed
 * renders them, so the two must agree about the shape of an image `src` and
 * about what a YouTube video id is. This module is where they agree - and it
 * imports nothing, which is the point: the reader's bundle can use it without
 * pulling in TipTap, and `lib/api` stays free of the editor for the same
 * reason it stays free of the router.
 *
 * The server has the last word on all of this (`backend/app/services/media.py`)
 * and refuses a save that breaks it. The checks here are so that a reviewer
 * finds out at the moment they paste something, rather than at the moment
 * their autosave fails.
 */

/**
 * The only image `src` an article may hold: the path the upload endpoint
 * returned, which is a content hash under a two-character shard.
 *
 * Mirrors `MEDIA_SRC_RE` in `backend/app/services/media.py`. Anchored, and no
 * `.` or `/` inside, so neither a remote URL nor `/api/media/../../etc/passwd`
 * matches. Images are uploaded, never linked: a linked image breaks when the
 * other site reorganises, and tracks the reader until it does.
 */
const MEDIA_SRC_RE = /^\/api\/media\/[0-9a-f]{2}\/[0-9a-f]{62}\.(?:png|jpg|gif|webp)$/;

/** YouTube ids are 11 characters of an unpadded base64url alphabet. */
const YOUTUBE_ID_RE = /^[A-Za-z0-9_-]{11}$/;

/** Hosts a watch link can arrive from. Exact, so `evilyoutube.com` is not one. */
const YOUTUBE_HOSTS = new Set([
  "youtube.com",
  "m.youtube.com",
  "music.youtube.com",
  "youtube-nocookie.com",
  "youtu.be",
]);

/**
 * How a picture or a video sits in the prose.
 *
 * `left` and `right` float, and the prose wraps down the other side - Word's
 * "Square" wrap. `none` is a block with text above and below; when it is
 * narrower than the column it centres, because a narrow block hugging the left
 * margin reads as a mistake rather than a choice.
 *
 * There are no coordinates here on purpose. Media are siblings of the beat
 * paragraphs and are placed by being dragged between them, which is what keeps
 * `attrs.beat` - and therefore the derived feed card - meaningful.
 */
export type MediaAlign = "none" | "left" | "right";

const MEDIA_ALIGNMENTS: readonly MediaAlign[] = ["none", "left", "right"];

/**
 * Width is a percentage of the prose column, never pixels.
 *
 * The editor's column is around 850px and the published article's is 64ch, so
 * a percentage is the only unit under which a reviewer's choice means the same
 * thing in both. The floor keeps a picture from being dragged down to a speck
 * that no reader can make anything out of.
 */
export const MEDIA_MIN_WIDTH = 20;
export const MEDIA_MAX_WIDTH = 100;

/** True for a path this system's own media store produced. */
export function isStoredMedia(src: unknown): src is string {
  return typeof src === "string" && MEDIA_SRC_RE.test(src);
}

export function isMediaAlign(value: unknown): value is MediaAlign {
  return MEDIA_ALIGNMENTS.includes(value as MediaAlign);
}

/**
 * The classes that lay a media block out, from `styles/youth.css`.
 *
 * Here rather than in either styles module because the console and the feed
 * must produce the *same* classes - that identity is what makes "the editor
 * shows what publishes" true of layout. Two copies of this mapping would be
 * two things that could drift, and the symptom of drift is an article that
 * looks one way to the reviewer approving it and another to everyone else.
 *
 * Pair it with a `--ew-media-width` custom property for the width.
 */
export function mediaWrapClass(align: MediaAlign): string {
  if (align === "left") return "ew-media ew-media--left";
  if (align === "right") return "ew-media ew-media--right";
  return "ew-media";
}

/**
 * The alignment on a node, defaulting for anything else.
 *
 * Absent is the common case, not an error: every document written before media
 * could be laid out has no `align`, and those must keep rendering.
 */
export function mediaAlign(value: unknown): MediaAlign {
  return isMediaAlign(value) ? value : "none";
}

/**
 * A width in range, or full width.
 *
 * Rounded to whole percents - the resize handle produces fractions of a pixel
 * and storing `44.7391304347826` makes a diff between the original and the
 * edited document unreadable for the sake of a difference nobody can see.
 */
export function clampMediaWidth(value: unknown): number {
  const width = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(width)) return MEDIA_MAX_WIDTH;
  return Math.min(MEDIA_MAX_WIDTH, Math.max(MEDIA_MIN_WIDTH, Math.round(width)));
}

export function isYouTubeId(value: unknown): value is string {
  return typeof value === "string" && YOUTUBE_ID_RE.test(value);
}

/**
 * The video id inside whatever the reviewer pasted, or null.
 *
 * Accepts the four links YouTube itself hands out - `watch?v=`, `youtu.be/`,
 * `/embed/`, `/shorts/` - and a bare id, because someone will paste one. Only
 * the id is kept: a document stores data, and the renderer builds the embed
 * URL from it, so no pasted string ever reaches an iframe's `src` intact.
 */
export function youtubeIdFromUrl(input: string): string | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  if (isYouTubeId(trimmed)) return trimmed;

  let url: URL;
  try {
    // Bare `youtu.be/xyz` is a URL to a human and a relative path to `URL`.
    url = new URL(/^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`);
  } catch {
    return null;
  }

  const host = url.hostname.replace(/^www\./, "");
  if (!YOUTUBE_HOSTS.has(host)) return null;

  const lastSegment = url.pathname.split("/").filter(Boolean).pop() ?? "";
  const candidate = url.searchParams.get("v") ?? lastSegment;
  return isYouTubeId(candidate) ? candidate : null;
}

/**
 * The still frame, for the console's block and the feed's click-to-play cover.
 *
 * `i.ytimg.com` serves thumbnails without the player, so showing one costs a
 * reader an image request rather than YouTube's whole JavaScript bundle and
 * the cookies that come with it.
 */
export function youtubeThumbnailUrl(videoId: string): string {
  return `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`;
}

/**
 * The watch page, for the console's "open it and check" control.
 *
 * A reviewer sizing a video down to a third of the column can no longer read
 * its title off the still, and "is this the right video" is the one question
 * the block has to be able to answer.
 */
export function youtubeWatchUrl(videoId: string): string {
  return `https://www.youtube.com/watch?v=${videoId}`;
}

/**
 * The player, built here from an id that has been checked against
 * {@link isYouTubeId} - never from a string anyone typed.
 *
 * `youtube-nocookie.com` because a reader arriving at a health article has not
 * asked to be tracked by Google, and this is the domain that honours that
 * until they press play.
 */
export function youtubeEmbedUrl(videoId: string, { autoplay = false } = {}): string {
  const params = new URLSearchParams({ rel: "0", modestbranding: "1" });
  if (autoplay) params.set("autoplay", "1");
  return `https://www.youtube-nocookie.com/embed/${videoId}?${params}`;
}
