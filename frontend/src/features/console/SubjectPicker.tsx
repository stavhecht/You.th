/**
 * Which category this article is filed under.
 *
 * The one field on the review screen a reviewer *adds* rather than checks, and
 * the reason it exists at all: subject is the product's single chromatic axis
 * - it colours the tile, fills the browse drawer and is what a reader's
 * interests are expressed in - and it cannot be derived. `product` is free
 * text, so inferring "Supplements" from "Creatine Monohydrate 5g" would put a
 * confident colour on a guess. "Other" is a classification ("none of these
 * fit"); "None" means nobody has classified it yet.
 *
 * Optional, and staying optional is deliberate. An unclassified article renders
 * in ink and sits under "Everything", which is the design's resting state
 * rather than a broken cell - so this must never block publication. "None" is
 * therefore a real choice in the row, not the absence of one.
 *
 * Writes immediately rather than through the editor's autosave: it is a single
 * enum on its own endpoint, it is legal after publication (unlike the body),
 * and folding it into the content PATCH would make correcting a colour on a
 * live article impossible.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { SUBJECTS, SUBJECT_LABELS } from "@/features/evidence/subject";
import { consoleKeys, setSubject } from "@/lib/api/console";
import { SECTION_LABEL } from "./controls";
import {
  SUBJECT_BLOCK,
  SUBJECT_NOTE,
  SUBJECT_ROW,
  subjectChip,
} from "./styles";
import type { Subject } from "@/types/api";

export function SubjectPicker({
  articleId,
  subject,
}: {
  articleId: string;
  subject: Subject | null;
}) {
  const queryClient = useQueryClient();

  const save = useMutation({
    mutationFn: (next: Subject | null) => setSubject(articleId, next),
    // Both, not just the article: the queue row draws its left rule from the
    // subject, so a change made here has to reach the list behind this screen.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: consoleKeys.article(articleId) });
      void queryClient.invalidateQueries({ queryKey: consoleKeys.queues });
    },
  });

  return (
    <div className={SUBJECT_BLOCK}>
      <span className={SECTION_LABEL}>Subject</span>

      <div className={SUBJECT_ROW}>
        <button
          onClick={() => save.mutate(null)}
          disabled={save.isPending}
          aria-pressed={subject === null}
          className={subjectChip(subject === null)}
        >
          None
        </button>

        {SUBJECTS.map((option) => (
          <button
            key={option}
            onClick={() => save.mutate(option)}
            disabled={save.isPending}
            aria-pressed={subject === option}
            className={subjectChip(subject === option)}
          >
            {SUBJECT_LABELS[option]}
          </button>
        ))}
      </div>

      <p className={SUBJECT_NOTE}>
        {save.isError
          ? "Could not save that. Try again."
          : "Colours the tile and files the article under a category. Optional, and editable after publishing."}
      </p>
    </div>
  );
}
