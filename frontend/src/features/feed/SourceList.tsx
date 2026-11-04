/**
 * The source list beside a public article.
 *
 * Shows study type on every entry. That is not decoration: it is how a reader
 * calibrates a verdict for themselves rather than taking ours on trust, which
 * is the whole proposition of the product. Weak types are drawn in the accent
 * ink - the one place a colour appears on evidence, and it flags the *source*,
 * never the verdict.
 *
 * Each entry carries `id="source-S1"` so the citation chips in the body can
 * anchor to it, and the row the reader just came from is washed with the accent
 * so the jump lands somewhere visible. The *label* on the entry is not that
 * handle: it is the reader's number, 1..n with no gaps, from the same map the
 * chips read - see `citations.ts`. The id keeps the handle because it is the
 * identity, and because changing it would break every link already shared.
 */

import { STUDY_TYPE_LABELS, isWeakStudyType } from "@/features/evidence/labels";
import { citationLabel } from "./citations";
import {
  CITATION_MAP,
  CITATION_MAP_CLAIM,
  CITATION_MAP_HANDLE,
  CITATION_MAP_ITEM,
  CITATION_MAP_LIST,
  CITATION_MAP_SUMMARY,
  SOURCES_TITLE,
  SOURCE_SUBLINE,
  SOURCES_COUNT,
  SOURCES_EMPTY_NOTE,
  SOURCES_FOOTNOTE,
  SOURCES_HEAD,
  SOURCES_SECTION,
  SOURCE_HANDLE,
  SOURCE_LIST,
  SOURCE_META,
  SOURCE_RETRACTED,
  SOURCE_ROW_HEAD,
  SOURCE_TITLE_LINK,
  sourceRow,
  sourceStudyType,
} from "./styles";
import type { Citation, Source } from "@/types/api";

interface Props {
  sources: Source[];
  citations: Citation[];
  /** Handle → the number the reader sees. Shared with the body's chips. */
  numbers: Map<string, number>;
  /** Handle of the chip the reader last pressed. */
  activeHandle?: string | null;
}

export function SourceList({ sources, citations, numbers, activeHandle }: Props) {
  if (sources.length === 0) {
    return (
      <section className={SOURCES_SECTION}>
        <h2 className={SOURCES_TITLE}>Scientific sources &amp; clinical studies</h2>
        <p className={SOURCES_EMPTY_NOTE}>
          No studies were found that test this claim directly. That is a gap in the
          literature, not a judgment on the product.
        </p>
      </section>
    );
  }

  return (
    <section className={SOURCES_SECTION}>
      <div className={SOURCES_HEAD}>
        <h2 className={SOURCES_TITLE}>Scientific sources &amp; clinical studies</h2>
        <span className={SOURCES_COUNT}>
          {sources.length} {sources.length === 1 ? "paper" : "papers"}
        </span>
      </div>

      <ol className={SOURCE_LIST}>
        {sources.map((source) => {
          const active = activeHandle === source.citationHandle;
          return (
            <li
              key={source.citationHandle}
              id={`source-${source.citationHandle}`}
              className={sourceRow(active)}
            >
              <div className={SOURCE_ROW_HEAD}>
                <span className={SOURCE_HANDLE}>
                  {citationLabel(numbers, source.citationHandle)}
                </span>
                <a
                  href={source.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className={SOURCE_TITLE_LINK}
                >
                  {source.title}
                </a>
              </div>

              {/* Indented to the handle pill's right edge, so the study
                  type and the journal read as belonging to the title above
                  them rather than as a second list. */}
              <div className={SOURCE_SUBLINE}>
                {source.retracted ? (
                  // Above the study type, not beside it: a retraction is not a
                  // grade. "Randomised controlled trial" is still true of a
                  // withdrawn paper, and is exactly the wrong thing for a
                  // reader to weigh it by.
                  <div className={SOURCE_RETRACTED}>Retracted by the journal</div>
                ) : null}
                <div className={sourceStudyType(isWeakStudyType(source.studyType))}>
                  {STUDY_TYPE_LABELS[source.studyType]}
                </div>
                <div className={SOURCE_META}>
                  {[source.journal, source.year].filter(Boolean).join(" · ")}
                </div>
              </div>
            </li>
          );
        })}
      </ol>

      <p className={SOURCES_FOOTNOTE}>
        Study type is shown on every source so you can weigh the verdict yourself.
      </p>

      {citations.length > 0 ? (
        <details className={CITATION_MAP}>
          <summary className={CITATION_MAP_SUMMARY}>
            Which source backs which claim
          </summary>
          <ul className={CITATION_MAP_LIST}>
            {citations.map((citation) => (
              <li key={citation.claim} className={CITATION_MAP_ITEM}>
                <span className={CITATION_MAP_CLAIM}>{citation.claim}</span>
                {": "}
                {citation.handles.map((handle, index) => (
                  <span key={handle}>
                    {index > 0 ? ", " : ""}
                    <a href={`#source-${handle}`} className={CITATION_MAP_HANDLE}>
                      {citationLabel(numbers, handle)}
                    </a>
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
