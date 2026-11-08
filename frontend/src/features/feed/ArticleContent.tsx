/**
 * Renders a stored TipTap document as read-only article prose.
 *
 * A plain recursive walk rather than a read-only TipTap instance: the editor
 * bundle is large, and shipping it to every public reader to render static
 * text is a cost with no benefit. The console loads the real editor; the feed
 * does not.
 *
 * Citation nodes become bracketed chips - `[1]` - that open the paper's card
 * in place. The number is the reader's, not the pipeline's: the stored handle
 * is `S4`, a position in the ranked retrieval set, and `citations.ts` maps it
 * onto the source list's own ordering so the brackets count 1, 2, 3 with no
 * gaps. The handle stays the identity behind it - anchor, popover key, and the
 * row the source list highlights. The design comp weighed three treatments and this is the one that
 * shipped: a superscript is a 4px tap target that vanishes at body size, and a
 * dotted underline on the sentence makes the *claim* look uncertain rather than
 * making its source available. The chip is the primary affordance for the thing
 * this product exists to let you do, so it is sized to be pressed.
 *
 * Section headings render as `h2` under the article's own `h1`. They come from
 * the pipeline (`ArticleBody.sections`) and a reviewer can add or remove one in
 * the console; either way they are plain labels, never claims, so nothing here
 * needs to treat them as content that could assert something.
 *
 * Two more block nodes sit alongside the beat paragraphs - a picture and a
 * YouTube embed, both added by the reviewer in the console. They are re-checked
 * here before they render: the server refuses to store either one pointing
 * anywhere it did not put it, so anything that fails these checks means that
 * guarantee has broken, and a reader is the wrong person to find that out from.
 */

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";

import { STUDY_TYPE_LABELS } from "@/features/evidence/labels";
import { citationLabel } from "./citations";
import {
  clampMediaWidth,
  isStoredMedia,
  isYouTubeId,
  mediaAlign,
  youtubeEmbedUrl,
  youtubeThumbnailUrl,
} from "@/lib/media";
import {
  ARTICLE_IMAGE,
  articleFigure,
  CHIP_ANCHOR,
  CITATION_CHIP,
  POPOVER_KICKER,
  POPOVER_LINK,
  POPOVER_META,
  POPOVER_TITLE,
  POPOVER_UNRESOLVED,
  PROSE_HEADING,
  PROSE_MEASURE,
  PROSE_PARAGRAPH,
  SOURCE_POPOVER,
  VIDEO_CAPTION,
  VIDEO_COVER,
  VIDEO_FRAME,
  VIDEO_IFRAME,
  VIDEO_PLAY_BUTTON,
  VIDEO_PLAY_MARK,
  VIDEO_PLAY_TRIANGLE,
} from "./styles";
import type { Source, TipTapDoc, TipTapNode } from "@/types/api";

/** Citation nodes carry `attrs.sourceIds`; everything else is text. */
function sourceIds(attrs: Record<string, unknown> | undefined): string[] {
  const value = attrs?.sourceIds;
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

interface Props {
  doc: TipTapDoc;
  sources: Source[];
  /**
   * Handle → the number the reader sees. Passed in rather than derived here so
   * the chips and the source list cannot disagree - see `citations.ts`.
   */
  numbers: Map<string, number>;
  /**
   * Raised whenever a chip is pressed, so the source list can highlight the
   * matching row. The chip's `href` still anchors there - the highlight is what
   * makes the jump legible once you arrive.
   */
  onCite?: (handle: string) => void;
}

export function ArticleContent({ doc, sources, numbers, onCite }: Props) {
  // One popover at a time, keyed by paragraph + handle so the same source cited
  // twice does not open both cards.
  const [openKey, setOpenKey] = useState<string | null>(null);

  return (
    <div className={PROSE_MEASURE}>
      {(doc.content ?? []).map((block, index) => {
        if (block.type === "image") return <ArticleFigure key={index} node={block} />;
        if (block.type === "youtube") return <ArticleVideo key={index} node={block} />;
        if (block.type === "heading") {
          // Always h2. The article's own headline is the h1, and the document
          // has no level below this one - `body_text_to_doc` writes level 2 and
          // the console editor only offers level 2, so the attribute is not
          // read. Reading it would invite an h4 nested under nothing.
          return (
            <h2 key={index} className={PROSE_HEADING}>
              {(block.content ?? []).map((node) => node.text).join("")}
            </h2>
          );
        }

        return (
          <p key={index} className={PROSE_PARAGRAPH}>
            {(block.content ?? []).map((node, childIndex) =>
              node.type === "citation" ? (
                <CitationChips
                  key={childIndex}
                  handles={sourceIds(node.attrs)}
                  sources={sources}
                  numbers={numbers}
                  keyPrefix={`${index}-${childIndex}`}
                  openKey={openKey}
                  onOpenChange={setOpenKey}
                  onCite={onCite}
                />
              ) : (
                <span key={childIndex}>{node.text}</span>
              ),
            )}
          </p>
        );
      })}
    </div>
  );
}

/**
 * The width and wrap a reviewer set, as a class and a custom property.
 *
 * Both values go through the shared validators, so an attribute the server
 * would have refused falls back to a full-width block rather than reaching the
 * page. The server is the enforcement; this is the renderer refusing to be the
 * hole in it.
 */
function layoutOf(node: TipTapNode): { className: string; style: CSSProperties } {
  const align = mediaAlign(node.attrs?.align);
  return {
    className: articleFigure(align),
    style: { "--ew-media-width": `${clampMediaWidth(node.attrs?.width)}%` } as CSSProperties,
  };
}

/**
 * A picture the reviewer uploaded.
 *
 * `alt` is whatever they typed when they inserted it, including nothing - an
 * empty `alt` is the correct markup for a decorative image and the console
 * says so when it asks. `loading="lazy"` because an article's pictures are
 * below the lede by construction.
 */
function ArticleFigure({ node }: { node: TipTapNode }) {
  const src = node.attrs?.src;
  const alt = node.attrs?.alt;
  if (!isStoredMedia(src)) return null;

  return (
    <figure {...layoutOf(node)}>
      <img
        src={src}
        alt={typeof alt === "string" ? alt : ""}
        loading="lazy"
        decoding="async"
        className={ARTICLE_IMAGE}
      />
    </figure>
  );
}

/**
 * A YouTube embed, as a still until the reader presses it.
 *
 * The iframe is mounted by the click, not by the page - see `VIDEO_FRAME` for
 * the reasoning. `autoplay` on that first mount is what makes the press feel
 * like a play button rather than a loading step.
 */
function ArticleVideo({ node }: { node: TipTapNode }) {
  const [playing, setPlaying] = useState(false);
  const videoId = node.attrs?.videoId;
  if (!isYouTubeId(videoId)) return null;

  return (
    <figure {...layoutOf(node)}>
      <div className={VIDEO_FRAME}>
        {playing ? (
          <iframe
            src={youtubeEmbedUrl(videoId, { autoplay: true })}
            title="YouTube video"
            allow="accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture"
            allowFullScreen
            referrerPolicy="strict-origin-when-cross-origin"
            className={VIDEO_IFRAME}
          />
        ) : (
          <>
            <img
              src={youtubeThumbnailUrl(videoId)}
              alt=""
              loading="lazy"
              decoding="async"
              className={VIDEO_COVER}
            />
            <button
              type="button"
              onClick={() => setPlaying(true)}
              aria-label="Play the video on YouTube"
              className={VIDEO_PLAY_BUTTON}
            >
              <span className={VIDEO_PLAY_MARK}>
                <span aria-hidden className={VIDEO_PLAY_TRIANGLE} />
              </span>
            </button>
          </>
        )}
      </div>
      {playing ? null : (
        <figcaption className={VIDEO_CAPTION}>
          Plays on YouTube. Pressing play loads their player.
        </figcaption>
      )}
    </figure>
  );
}

function CitationChips({
  handles,
  sources,
  numbers,
  keyPrefix,
  openKey,
  onOpenChange,
  onCite,
}: {
  handles: string[];
  sources: Source[];
  numbers: Map<string, number>;
  keyPrefix: string;
  openKey: string | null;
  onOpenChange: (key: string | null) => void;
  onCite?: (handle: string) => void;
}) {
  if (handles.length === 0) return null;

  return (
    <>
      {handles.map((handle) => (
        <CitationChip
          key={handle}
          handle={handle}
          label={citationLabel(numbers, handle)}
          source={sources.find((s) => s.citationHandle === handle)}
          open={openKey === `${keyPrefix}-${handle}`}
          onOpenChange={(next) => onOpenChange(next ? `${keyPrefix}-${handle}` : null)}
          onCite={onCite}
        />
      ))}
    </>
  );
}

function CitationChip({
  handle,
  label,
  source,
  open,
  onOpenChange,
  onCite,
}: {
  /** The stored handle. Still the identity: anchor, key, highlighted row. */
  handle: string;
  /** What the reader reads inside the brackets. */
  label: string;
  source: Source | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCite?: (handle: string) => void;
}) {
  const container = useRef<HTMLSpanElement>(null);
  const popover = useRef<HTMLSpanElement>(null);

  // A chip can sit anywhere on the line, so slide the card back onto the screen
  // before it paints. shortcut: not redone on rotate while open; reopening fixes it.
  useLayoutEffect(() => {
    const card = popover.current;
    if (!card) return;
    const overflow = card.getBoundingClientRect().right - (document.documentElement.clientWidth - 8);
    if (overflow > 0) card.style.left = `${-overflow}px`;
  }, [open]);

  useEffect(() => {
    if (!open) return;

    function onPointerDown(event: MouseEvent) {
      if (!container.current?.contains(event.target as Node)) onOpenChange(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onOpenChange(false);
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onOpenChange]);

  return (
    <span ref={container} className={CHIP_ANCHOR}>
      <a
        href={`#source-${handle}`}
        onClick={(event) => {
          // The anchor is the fallback, not the behaviour: opening the card in
          // place keeps the reader in the sentence they were checking. The href
          // still resolves for middle-click, and without JS.
          event.preventDefault();
          onOpenChange(!open);
          onCite?.(handle);
        }}
        aria-expanded={open}
        className={CITATION_CHIP}
        aria-label={`Source ${label}`}
      >
        {label}
      </a>

      {open ? (
        <span ref={popover} className={SOURCE_POPOVER}>
          <span className={POPOVER_KICKER}>
            {`Source ${label}`}
            {source ? ` · ${STUDY_TYPE_LABELS[source.studyType]}` : ""}
          </span>

          {source ? (
            <>
              <span className={POPOVER_TITLE}>{source.title}</span>
              <span className={POPOVER_META}>
                {[source.journal, source.year].filter(Boolean).join(" · ")}
              </span>
              <a
                href={source.url}
                target="_blank"
                rel="noreferrer noopener"
                className={POPOVER_LINK}
              >
                Open the paper
              </a>
            </>
          ) : (
            // Validation guarantees every handle resolves before an article can
            // be published (invariant #2), so this is unreachable in the feed.
            // It exists because "silently render nothing" is the wrong way to
            // find out the guarantee has broken.
            <span className={POPOVER_UNRESOLVED}>
              This citation could not be matched to a source.
            </span>
          )}
        </span>
      ) : null}
    </span>
  );
}
