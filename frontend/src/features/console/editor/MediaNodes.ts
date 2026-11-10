/**
 * The `image` and `youtube` TipTap nodes.
 *
 * Both are block-level atoms, the same shape as `CitationNode.ts` and for the
 * same reason: a reviewer may place one or remove one, never edit its insides.
 * What is inside is not prose - it is a path the upload endpoint issued and an
 * eleven-character video id - and prose editing of either produces a `src`
 * that resolves to nothing. Atoms select whole and delete whole.
 *
 * Neither node holds a URL that anyone typed. The image node holds the path
 * the server returned from an upload, and `parseHTML` below refuses any other
 * shape, so a picture dragged in from another site never enters the document
 * at all. The video node holds an id, and the embed URL is built from it at
 * render time. Both are re-checked server-side on save and on approve
 * (`backend/app/services/media.py`) - that is the enforcement; this is the
 * half that keeps a reviewer from hitting it by accident.
 *
 * They are block-level rather than inline so that nothing they do can disturb
 * the three beats. Beat paragraphs are addressed by `attrs.beat`, so a picture
 * placed above beat 1 does not change which paragraph the feed card is derived
 * from.
 */

import { Node, mergeAttributes } from "@tiptap/core";
import { ReactNodeViewRenderer } from "@tiptap/react";

import type { Node as ProseMirrorNode } from "@tiptap/pm/model";

import {
  MEDIA_MAX_WIDTH,
  clampMediaWidth,
  isMediaAlign,
  isStoredMedia,
  isYouTubeId,
  mediaAlign,
  mediaWrapClass,
} from "@/lib/media";
import { ImageNodeView, YouTubeNodeView } from "./MediaNodeView";

/**
 * The layout attributes both media nodes carry.
 *
 * Declared once because a picture and a video are laid out by the same
 * controls and validated by the same server rule; two copies of this would be
 * two things to keep in step with `backend/app/services/media.py`.
 *
 * They live in `data-` attributes rather than a `style` string on purpose. The
 * document stores *what the reviewer chose* - 45%, floated left - and the
 * renderer decides what that looks like. A stored stylesheet would be a
 * reviewer-authored `style` on a published page, and it could not collapse on
 * a phone, because an inline style cannot carry a media query.
 */
function layoutAttributes() {
  return {
    width: {
      default: MEDIA_MAX_WIDTH,
      parseHTML: (element: HTMLElement) => clampMediaWidth(element.getAttribute("data-width")),
      renderHTML: (attributes: Record<string, unknown>) => ({
        "data-width": String(clampMediaWidth(attributes.width)),
      }),
    },
    align: {
      default: "none",
      parseHTML: (element: HTMLElement) => {
        const raw = element.getAttribute("data-align");
        return isMediaAlign(raw) ? raw : "none";
      },
      renderHTML: (attributes: Record<string, unknown>) => ({
        "data-align": isMediaAlign(attributes.align) ? attributes.align : "none",
      }),
    },
  };
}

/**
 * Layout applied to the element TipTap's React renderer puts *around* a node
 * view - the one that actually sits in the editor's flow, and therefore the
 * one that has to float.
 *
 * `attrs` is re-evaluated on every node update, so changing the wrap or the
 * width re-lays-out the block immediately. The live drag is the exception: it
 * writes the custom property straight to this element and only commits an
 * attribute on release, so a resize costs one transaction rather than sixty.
 */
function layoutElementAttrs({ node }: { node: ProseMirrorNode }) {
  return {
    class: mediaWrapClass(mediaAlign(node.attrs.align)),
    style: `--ew-media-width: ${clampMediaWidth(node.attrs.width)}%`,
  };
}

export const ArticleImage = Node.create({
  name: "image",
  group: "block",
  atom: true,
  selectable: true,
  draggable: true,

  addAttributes() {
    return {
      src: { default: null },
      alt: { default: "" },
      ...layoutAttributes(),
    };
  },

  addNodeView() {
    return ReactNodeViewRenderer(ImageNodeView, { attrs: layoutElementAttrs });
  },

  /**
   * The gate on pasted content.
   *
   * Copying an image out of a web page puts an `<img>` with a remote `src` on
   * the clipboard, and without this that URL would land in the document,
   * survive until the reviewer's next autosave, and come back as a rejected
   * save they did not know they had caused. Returning `false` declines the
   * match, so ProseMirror drops the element the same way it drops any other
   * markup this schema has no node for.
   *
   * Pasting or dropping the image *file* is the supported path and does work,
   * `ArticleEditor` uploads it first. That is the difference this enforces:
   * bytes we hold, not links we hope stay up.
   */
  parseHTML() {
    return [
      {
        tag: "img[src]",
        getAttrs: (element) => {
          const src = (element as HTMLElement).getAttribute("src");
          if (!isStoredMedia(src)) return false;
          return { src, alt: (element as HTMLElement).getAttribute("alt") ?? "" };
        },
      },
    ];
  },

  /**
   * Only the clipboard and `getHTML()` see this - the editor renders the node
   * view above and the feed renders React. What matters here is that every
   * attribute it writes, `parseHTML` reads back off the *same* element, so a
   * picture copied from one draft into another keeps its size and its wrap.
   */
  renderHTML({ HTMLAttributes }) {
    return ["img", mergeAttributes(HTMLAttributes, { draggable: "false" })];
  },
});

export const YouTubeEmbed = Node.create({
  name: "youtube",
  group: "block",
  atom: true,
  selectable: true,
  draggable: true,

  addAttributes() {
    return {
      videoId: {
        default: null,
        parseHTML: (element) => element.getAttribute("data-youtube-video-id"),
        renderHTML: (attributes) => ({
          "data-youtube-video-id": attributes.videoId as string,
        }),
      },
      ...layoutAttributes(),
    };
  },

  addNodeView() {
    return ReactNodeViewRenderer(YouTubeNodeView, { attrs: layoutElementAttrs });
  },

  parseHTML() {
    return [
      {
        tag: "div[data-youtube-video-id]",
        getAttrs: (element) =>
          isYouTubeId((element as HTMLElement).getAttribute("data-youtube-video-id"))
            ? null
            : false,
      },
    ];
  },

  /**
   * As with the image: the clipboard's view of the node, not the reviewer's.
   * The still and the controls come from the node view; this only has to carry
   * the id and the layout back out and in again.
   */
  renderHTML({ HTMLAttributes }) {
    return ["div", mergeAttributes(HTMLAttributes)];
  },
});
