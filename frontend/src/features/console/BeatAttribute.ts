/**
 * Keeps `attrs.beat` alive through an edit.
 *
 * The body is three beat paragraphs, and the server addresses them by
 * `attrs.beat` rather than by position - `services/tiptap.py::beat_text()`,
 * which the feed card excerpt is derived from, and which stays correct when a
 * reviewer inserts an image above beat 1 (see MediaNodes.ts).
 *
 * TipTap drops attributes the schema does not declare. Without this extension
 * StarterKit's plain `paragraph` parses the loaded document, discards `beat`,
 * and the first autosave writes an `edited_content` in which no paragraph is
 * addressable any more - the feed then renders those articles with an empty
 * excerpt, silently, because nothing about the document is otherwise wrong.
 *
 * Declared as a global attribute rather than a replacement paragraph node so
 * StarterKit keeps ownership of the node itself.
 */

import { Extension } from "@tiptap/core";

export const BeatAttribute = Extension.create({
  name: "beatAttribute",

  addGlobalAttributes() {
    return [
      {
        types: ["paragraph"],
        attributes: {
          beat: {
            // Null, not 1: a paragraph a reviewer adds is a sibling of the
            // beats, not a fourth beat, and must not answer to beat_text().
            default: null,
            keepOnSplit: false,
            parseHTML: (element) => {
              const raw = element.getAttribute("data-beat");
              return raw === null ? null : Number(raw);
            },
            renderHTML: (attributes) =>
              attributes.beat == null ? {} : { "data-beat": String(attributes.beat) },
          },
        },
      },
    ];
  },
});
