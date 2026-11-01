/**
 * TipTap editor over the draft.
 *
 * Loads `editedContent ?? originalContent` and saves only to edited content.
 * The `Citation` node makes citations first-class rather than prose (see
 * CitationNode.ts for why that matters), and `ArticleImage` / `YouTubeEmbed`
 * do the same for the two things a reviewer can add that the model cannot
 * (see MediaNodes.ts).
 *
 * The editing surface is drawn as a recessed panel on the ground with a rule
 * around it — the same inversion the console's text fields use. In a system
 * with no corner radius, the border is the only thing that can say "this region
 * is yours to change", and the reviewer needs to know that at a glance on a
 * screen where everything else is read-only.
 */

import { useEffect, useRef } from "react";
import { EditorContent, useEditor, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";

import { BeatAttribute } from "./BeatAttribute";
import { Citation } from "./CitationNode";
import { ArticleImage, YouTubeEmbed } from "./MediaNodes";
import {
  EDITOR_NOTE,
  EDITOR_PROSE,
  EDITOR_STATUS_ROW,
  EDITOR_SURFACE,
  MEDIA_ERROR,
  TOOLBAR,
  TOOLBAR_DIVIDER,
  saveIndicator,
  toolbarButton,
} from "./styles";
import type { Autosave } from "./useAutosave";
import { ACCEPTED_IMAGE_TYPES, useMediaInsert, type MediaInsert } from "./useMediaInsert";
import { useIllustration, type Illustration } from "./useIllustration";
import type { TipTapDoc } from "@/types/api";

interface Props {
  content: TipTapDoc;
  autosave: Autosave;
  /** Which draft this is. Only the regenerate control needs it. */
  articleId: string;
  /** Clicking a citation chip scrolls the sources panel to that handle. */
  onCitationClick?: (handle: string) => void;
}

export function ArticleEditor({ content, autosave, articleId, onCitationClick }: Props) {
  const media = useMediaInsert();
  const illustration = useIllustration(articleId);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        // Level 2 only, and it is not optional. The pipeline writes an `h2` for
        // every `ArticleBody` section, and **TipTap silently drops any node its
        // schema does not declare** — with `heading: false` the editor would
        // parse a sectioned draft, discard every heading, and the first
        // autosave would write an `edited_content` with the article flattened.
        // Same failure `BeatAttribute` exists to prevent for `attrs.beat`, and
        // just as invisible: nothing about the saved document is otherwise
        // wrong.
        //
        // Deeper levels stay off because the public renderer draws one heading
        // style and an h3 under an h2 would render identically — a hierarchy
        // the reviewer can express and the reader cannot see.
        heading: { levels: [2] },
        // Still out: lists, code blocks and quotes are not part of the format,
        // and offering them invites edits the public renderer has no way to
        // display.
        codeBlock: false,
        bulletList: false,
        orderedList: false,
        blockquote: false,
      }),
      BeatAttribute,
      Citation,
      ArticleImage,
      YouTubeEmbed,
    ],
    content,
    editorProps: {
      attributes: { class: EDITOR_PROSE },
      handleClick(_view, _pos, event) {
        const target = (event.target as HTMLElement).closest("[data-citation]");
        if (!target || !onCitationClick) return false;
        const handles = (target.getAttribute("data-source-ids") ?? "").split(",");
        if (handles[0]) onCitationClick(handles[0]);
        return true;
      },
      // Dropping a photo onto the draft and pasting one out of a screenshot
      // tool are how anyone actually adds a picture; the toolbar button is the
      // discoverable path, not the only one. Both go through the upload, so
      // what lands in the document is a file we hold rather than a link to
      // wherever it came from.
      handlePaste(_view, event) {
        return uploadImagesFrom(event.clipboardData, media);
      },
      handleDrop(_view, event) {
        return uploadImagesFrom(event.dataTransfer, media);
      },
    },
    onUpdate: ({ editor }) => autosave.schedule(editor.getJSON() as TipTapDoc),
  });

  // The paste and drop handlers above were built before this editor existed;
  // this is what lets them insert into it.
  const { bind } = media;
  useEffect(() => {
    bind(editor);
  }, [bind, editor]);

  // Swapping to a different article must replace the document, or the previous
  // draft's text stays in the editor and autosaves onto the new article.
  useEffect(() => {
    if (editor && content) {
      // emitUpdate=false: loading a document must not look like an edit, or
      // opening a draft immediately autosaves it back over itself.
      editor.commands.setContent(content, false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, content]);

  return (
    <div>
      <Toolbar editor={editor} media={media} illustration={illustration} />
      <EditorContent editor={editor} className={EDITOR_SURFACE} />
      {media.error ? (
        <p role="alert" className={MEDIA_ERROR}>
          {media.error}
        </p>
      ) : null}
      {illustration.error ? (
        <p role="alert" className={MEDIA_ERROR}>
          {illustration.error}
        </p>
      ) : null}
      <div className={EDITOR_STATUS_ROW}>
        <span className={EDITOR_NOTE}>
          Edits save to the reviewed copy — the AI draft is preserved.
        </span>
        <SaveIndicator state={autosave.state} />
      </div>
    </div>
  );
}

/**
 * Image files out of a paste or a drop, uploaded.
 *
 * Returns true — which stops ProseMirror's own handling — only when there were
 * files to take, so an ordinary text paste is untouched.
 *
 * Copying a picture out of a web page puts both an `<img>` and the decoded
 * bytes on the clipboard. Taking the bytes is deliberate: the markup would be
 * a link to someone else's server, and the image node refuses those.
 */
function uploadImagesFrom(transfer: DataTransfer | null, media: MediaInsert): boolean {
  const files = Array.from(transfer?.files ?? []).filter((file) =>
    file.type.startsWith("image/"),
  );
  if (files.length === 0) return false;

  void media.insertFiles(files);
  return true;
}

/**
 * Two marks, a section heading, and the two blocks a reviewer can add.
 *
 * Heading is here because the editor has to be able to *remove* one as well as
 * render it: a reviewer who merges two sections needs to delete the heading
 * between them, and a node the schema accepts but no control can toggle is one
 * the reviewer can only work around.
 *
 * The design comp draws Quote and Insert citation here as well, and both would
 * be controls that break something. Quote is disabled in StarterKit above
 * because the public renderer has no blockquote case, and hand-inserting a
 * citation is how a handle that resolves to nothing gets into a draft, which is
 * precisely what validation exists to reject (invariant #2). A citation picker
 * driven by the retrieved sources is the shape that would work; it is not this.
 *
 * Image and Video are a different case: they add nothing the model asserted and
 * nothing that can contradict the evidence, so there is no handle to orphan and
 * no claim to overstate.
 */
function Toolbar({
  editor,
  media,
  illustration,
}: {
  editor: Editor | null;
  media: MediaInsert;
  illustration: Illustration;
}) {
  const filePicker = useRef<HTMLInputElement>(null);
  if (!editor) return null;

  const marks = [
    { name: "bold" as const, label: "Bold", run: () => editor.chain().focus().toggleBold().run() },
    { name: "italic" as const, label: "Italic", run: () => editor.chain().focus().toggleItalic().run() },
  ];

  function onVideo() {
    const url = window.prompt(
      "Paste the YouTube link.\nThe video plays inline in the published article.",
      "",
    );
    if (url !== null) media.insertYouTube(url);
  }

  return (
    <div className={TOOLBAR}>
      {marks.map((mark) => (
        <button
          key={mark.name}
          type="button"
          onClick={mark.run}
          aria-pressed={editor.isActive(mark.name)}
          className={toolbarButton(editor.isActive(mark.name))}
        >
          {mark.label}
        </button>
      ))}

      <button
        type="button"
        onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
        aria-pressed={editor.isActive("heading", { level: 2 })}
        title="Turn this line into a section heading, or back into prose"
        className={toolbarButton(editor.isActive("heading", { level: 2 }))}
      >
        Heading
      </button>

      <span aria-hidden className={TOOLBAR_DIVIDER} />

      <button
        type="button"
        onClick={() => filePicker.current?.click()}
        disabled={media.uploading}
        title="Add a picture from this computer (PNG, JPEG, GIF or WebP)"
        className={toolbarButton(false)}
      >
        {media.uploading ? "Uploading…" : "Image"}
      </button>

      <button
        type="button"
        onClick={onVideo}
        disabled={media.uploading}
        title="Embed a YouTube video by its link"
        className={toolbarButton(false)}
      >
        Video
      </button>

      {/*
        Beside Image because they produce the same thing by another route. These
        are the only controls on this screen that spend money per press — a GPU
        render each on a hosted provider — which is why the server keeps a
        budget in front of them and answers 429 with the wait.

        Two buttons rather than one because an article has two pictures and they
        are judged in different places. This pair redraws the picture in the
        prose, which is the one visible from here; the tile's own is redrawn
        from Show draft, where a reviewer can see what changed. "…and the tile"
        stays because it is the only way to get a pair drawn from one seed, and
        because it is one press rather than two when neither picture is right.
      */}
      <button
        type="button"
        onClick={() => void illustration.regenerate("picture", editor)}
        disabled={illustration.working !== null || media.uploading}
        title="Draw the article's own picture again, with a new seed. One render."
        className={toolbarButton(false)}
      >
        {illustration.working === "picture" ? "Generating…" : "Regenerate picture"}
      </button>

      <button
        type="button"
        onClick={() => void illustration.regenerate("both", editor)}
        disabled={illustration.working !== null || media.uploading}
        title="Draw the article's picture and the feed tile's, from one new seed. Two renders."
        className={toolbarButton(false)}
      >
        {illustration.working === "both" ? "Generating…" : "…and the tile"}
      </button>

      {/*
        The button is the control; this input only opens the file dialog. It is
        reset after every pick so choosing the same file twice still fires a
        change event — otherwise re-adding an image a reviewer just deleted
        silently does nothing.
      */}
      <input
        ref={filePicker}
        type="file"
        accept={ACCEPTED_IMAGE_TYPES}
        multiple
        hidden
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          void media.insertFiles(files);
        }}
      />
    </div>
  );
}

function SaveIndicator({ state }: { state: Autosave["state"] }) {
  const labels: Record<Autosave["state"], string> = {
    idle: "",
    saving: "Saving…",
    saved: "Saved",
    error: "Save failed — retrying",
    conflict: "Another reviewer saved this draft. Copy your change, then reload.",
  };
  return (
    <span className={saveIndicator(state === "error" || state === "conflict")}>
      {labels[state]}
    </span>
  );
}
