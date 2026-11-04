/**
 * The editing surface for a picture or a video: resize, wrap, move, remove.
 *
 * One component serves both nodes because the frame, the controls and the
 * resize behaviour are identical and only the contents differ - a picture, or
 * a 16:9 still standing in for the player.
 *
 * The layout it edits is two numbers and a word (`width`, `align`), never CSS.
 * That is what lets the server check it (`backend/app/services/media.py`), what
 * lets the same three classes render the published article, and what makes the
 * whole thing collapse to a full-width block on a phone without the reviewer
 * having to think about phones.
 *
 * Three gestures, deliberately distinct:
 *
 *   - **drag the media** → move it between paragraphs. `data-drag-handle` is
 *     on the media itself, so this is the obvious one.
 *   - **drag the corner** → resize. Its own cursor, and it stops the event
 *     reaching ProseMirror so it cannot start a move instead.
 *   - **the bar** → wrap, size, alt text, remove. Buttons rather than drag
 *     alone, because drag-only has no keyboard path and nothing that tells a
 *     reviewer the feature is there.
 */

import { useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { NodeViewWrapper, type ReactNodeViewProps } from "@tiptap/react";

import {
  clampMediaWidth,
  mediaAlign,
  youtubeThumbnailUrl,
  youtubeWatchUrl,
  type MediaAlign,
} from "@/lib/media";
import {
  EDITOR_IMAGE,
  EDITOR_VIDEO_FRAME,
  EDITOR_VIDEO_ID,
  EDITOR_VIDEO_KICKER,
  EDITOR_VIDEO_OVERLAY,
  EDITOR_VIDEO_STILL,
  MEDIA_CONTROLS,
  MEDIA_CONTROL_DIVIDER,
  MEDIA_FRAME,
  MEDIA_REMOVE_BUTTON,
  MEDIA_RESIZE_HANDLE,
  MEDIA_RESIZE_HANDLE_VISIBLE,
  MEDIA_WIDTH_READOUT,
  mediaControlButton,
} from "./styles";

/** One press of − or +. Fine enough to matter, coarse enough to be quick. */
const WIDTH_STEP = 5;

const WRAP_OPTIONS: { align: MediaAlign; label: string; title: string }[] = [
  { align: "left", label: "◧ Left", title: "Float left: text wraps down the right" },
  { align: "right", label: "◨ Right", title: "Float right: text wraps down the left" },
  { align: "none", label: "≡ None", title: "Own block: text above and below" },
];

export function ImageNodeView(props: ReactNodeViewProps) {
  return <MediaNodeView {...props} kind="image" />;
}

export function YouTubeNodeView(props: ReactNodeViewProps) {
  return <MediaNodeView {...props} kind="youtube" />;
}

function MediaNodeView({
  node,
  selected,
  updateAttributes,
  deleteNode,
  editor,
  kind,
}: ReactNodeViewProps & { kind: "image" | "youtube" }) {
  const align = mediaAlign(node.attrs.align);
  const committedWidth = clampMediaWidth(node.attrs.width);

  // The width under the cursor right now. `null` means nothing is being
  // dragged. Only the readout reads it - the block itself is resized by
  // writing the custom property straight to the DOM during the drag, and the
  // attribute is committed once on release. Committing per frame would put
  // sixty entries in the undo stack and restart the 800ms autosave on each.
  const [draftWidth, setDraftWidth] = useState<number | null>(null);
  const liveWidth = useRef(committedWidth);

  const width = draftWidth ?? committedWidth;
  const editable = editor.isEditable;

  function setWidth(next: number) {
    updateAttributes({ width: clampMediaWidth(next) });
  }

  /**
   * Resize by dragging the corner.
   *
   * The element that carries the width is the one TipTap's renderer wraps
   * around this component (`.ew-media`), not the figure below - see
   * `MediaNodes.ts`. Width is measured against the prose column, so the
   * percentage means what it says at any window size.
   *
   * Pointer events go on `window` rather than the grip, because a quick drag
   * leaves a 16px target behind long before the reviewer stops moving.
   */
  function onResizeStart(event: ReactPointerEvent<HTMLButtonElement>) {
    // Both matter: preventDefault stops the browser starting a native drag,
    // stopPropagation stops ProseMirror reading this as a move of the node.
    event.preventDefault();
    event.stopPropagation();

    const frame = event.currentTarget.closest<HTMLElement>(".ew-media");
    const column = frame?.parentElement;
    if (!frame || !column) return;

    const columnWidth = column.clientWidth;
    if (columnWidth === 0) return;

    const startX = event.clientX;
    const startWidth = frame.getBoundingClientRect().width;
    // A centred block grows from both edges, so its right edge travels half as
    // far as the width changes. Doubling keeps the grip under the pointer.
    const gearing = align === "none" ? 2 : 1;
    const direction = align === "right" ? -1 : 1;

    function onMove(move: PointerEvent) {
      const delta = (move.clientX - startX) * direction * gearing;
      const next = clampMediaWidth(((startWidth + delta) / columnWidth) * 100);
      liveWidth.current = next;
      // Straight to the DOM: this is a preview, and a transaction per frame is
      // sixty undo steps and sixty autosave restarts for one gesture.
      frame?.style.setProperty("--ew-media-width", `${next}%`);
      setDraftWidth(next);
    }

    function onEnd() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      setDraftWidth(null);
      // Now it becomes real: one transaction, one undo step, one autosave.
      // The node view re-applies the property from the attribute, so the
      // preview and the committed value converge on the same number.
      setWidth(liveWidth.current);
    }

    liveWidth.current = committedWidth;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
  }

  function onEditAlt() {
    const next = window.prompt(
      "Describe this image for readers using a screen reader.\n" +
        "Leave this empty if the image is decorative.",
      (node.attrs.alt as string) ?? "",
    );
    if (next !== null) updateAttributes({ alt: next.trim() });
  }

  return (
    <NodeViewWrapper
      as="figure"
      // The node is an atom; nothing inside it is prose, and saying so keeps a
      // stray keystroke from landing in the middle of a picture.
      contentEditable={false}
      className={MEDIA_FRAME}
    >
      {selected && editable ? (
        <div
          className={MEDIA_CONTROLS}
          // Without this, pressing a button blurs the editor, ProseMirror drops
          // the node selection, and the bar disappears under the cursor on its
          // way to being clicked.
          onMouseDown={(event) => event.preventDefault()}
        >
          {WRAP_OPTIONS.map((option) => (
            <button
              key={option.align}
              type="button"
              onClick={() => updateAttributes({ align: option.align })}
              aria-pressed={align === option.align}
              title={option.title}
              className={mediaControlButton(align === option.align)}
            >
              {option.label}
            </button>
          ))}

          <span aria-hidden className={MEDIA_CONTROL_DIVIDER} />

          <button
            type="button"
            onClick={() => setWidth(width - WIDTH_STEP)}
            disabled={width <= 20}
            title="Narrower"
            className={mediaControlButton(false)}
          >
            −
          </button>
          <span className={MEDIA_WIDTH_READOUT} aria-label={`Width ${width} percent`}>
            {width}%
          </span>
          <button
            type="button"
            onClick={() => setWidth(width + WIDTH_STEP)}
            disabled={width >= 100}
            title="Wider"
            className={mediaControlButton(false)}
          >
            +
          </button>

          <span aria-hidden className={MEDIA_CONTROL_DIVIDER} />

          {kind === "image" ? (
            <button
              type="button"
              onClick={onEditAlt}
              title="Describe this image for screen readers"
              className={mediaControlButton(false)}
            >
              Alt…
            </button>
          ) : (
            <button
              type="button"
              onClick={() =>
                window.open(
                  youtubeWatchUrl(node.attrs.videoId as string),
                  "_blank",
                  "noopener,noreferrer",
                )
              }
              title="Open this video on YouTube to check it"
              className={mediaControlButton(false)}
            >
              Watch ↗
            </button>
          )}

          <button type="button" onClick={deleteNode} className={MEDIA_REMOVE_BUTTON}>
            Remove
          </button>
        </div>
      ) : null}

      {kind === "image" ? (
        <img
          src={node.attrs.src as string}
          alt={(node.attrs.alt as string) ?? ""}
          data-drag-handle
          className={EDITOR_IMAGE}
        />
      ) : (
        <VideoStill videoId={node.attrs.videoId as string} />
      )}

      {editable ? (
        <button
          type="button"
          onPointerDown={onResizeStart}
          aria-label="Resize"
          title="Drag to resize"
          className={`${MEDIA_RESIZE_HANDLE} ${selected ? MEDIA_RESIZE_HANDLE_VISIBLE : ""}`}
        />
      ) : null}
    </NodeViewWrapper>
  );
}

/**
 * The video's stand-in: the real still frame at the real aspect ratio, so the
 * reviewer sizes the rectangle a reader will actually get. The id stays on top
 * of it because "is this the right video" is the question the block has to
 * answer, and a thumbnail alone cannot at 25% width.
 */
function VideoStill({ videoId }: { videoId: string }) {
  return (
    <div data-drag-handle className={EDITOR_VIDEO_FRAME}>
      <img
        src={youtubeThumbnailUrl(videoId)}
        alt=""
        draggable={false}
        className={EDITOR_VIDEO_STILL}
      />
      <span className={EDITOR_VIDEO_OVERLAY}>
        <span className={EDITOR_VIDEO_KICKER}>YouTube</span>
        <span className={EDITOR_VIDEO_ID}>{videoId}</span>
      </span>
    </div>
  );
}
