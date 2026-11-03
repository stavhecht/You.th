/**
 * The full on-tap article.
 *
 * One column at 800px, where the previous design used two with the evidence
 * pinned in a sidebar. The sidebar answered "what is this resting on?" without
 * scrolling, and that question still has to be answerable mid-paragraph - but
 * the citation chips are what answer it now, each opening its own paper in
 * place. At this measure a 320px sidebar would leave the prose too narrow to be
 * the thing the page is for, so the sources moved to a panel at the end and the
 * grade moved up beside the verdict, where it is read before the article rather
 * than beside it.
 *
 * The disclaimer is rendered from a server-provided constant and is not
 * conditional on anything - it is the one element that must appear on every
 * article regardless of verdict, and it is deliberately not model output.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "@/lib/api/client";
import { GradeBar } from "@/features/evidence/GradeBar";
import { VerdictMark } from "@/features/evidence/VerdictMark";
import { GRADE_NOTES, VERDICT_GLOSS, VERDICT_LABELS } from "@/features/evidence/labels";
import { verdictWording } from "@/features/evidence/styles";
import { subjectLabel, subjectText } from "@/features/evidence/subject";
import { fetchArticle, feedKeys } from "@/lib/api/feed";
import { readingTimeLabel } from "@/lib/readingTime";
import { PublishedDate } from "./ArticleCard";
import { ArticleContent } from "./ArticleContent";
import { citationNumbers } from "./citations";
import { ArticleActions } from "./ArticleActions";
import { ReadMore } from "./ReadMore";
import { SourceList } from "./SourceList";
import {
  ARTICLE_BACK_LINK,
  ARTICLE_BYLINE,
  ARTICLE_BYLINE_LIFT,
  ARTICLE_BYLINE_SEP,
  ARTICLE_BYLINE_STRONG,
  ARTICLE_DISCLAIMER,
  ARTICLE_ERROR_PAGE,
  ARTICLE_ERROR_TITLE,
  ARTICLE_LEDE,
  ARTICLE_PAGE,
  ARTICLE_SKELETON_PAGE,
  ARTICLE_TITLE,
  ARTICLE_VERDICT_BAR,
  ARTICLE_VERDICT_QUALIFIER,
  BACK_TO_FEED_LINK,
  RETRACTION_BANNER,
  RETRACTION_BANNER_LABEL,
  RETRACTION_BANNER_TEXT,
  SKELETON_KICKER,
  SKELETON_LEAD_IMAGE,
  SKELETON_LEDE,
  SKELETON_PARAGRAPH,
  SKELETON_PROSE,
  SKELETON_TITLE,
  VERDICT_GLOSS_TEXT,
  articleKicker,
} from "./styles";

export function ArticleView() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();

  // Which source the reader last opened from the prose. Lives here because both
  // the chips and the list need it, and neither owns the other.
  const [activeHandle, setActiveHandle] = useState<string | null>(null);

  const {
    data: article,
    status,
    error,
  } = useQuery({
    queryKey: feedKeys.article(slug),
    queryFn: () => fetchArticle(slug),
    enabled: Boolean(slug),
    // A 404 here means "not published", which will not become true by
    // retrying - and retrying an unpublished slug is how a draft-preview
    // channel accidentally gets built.
    retry: (count, err) =>
      !(err instanceof ApiError && err.status === 404) && count < 2,
  });

  // One map for both the chips and the list, above the early returns because it
  // is a hook. Deriving it twice would be two objects rather than two
  // numberings - but a reader checking a chip against the list is the whole use
  // of a citation, so it is worth being literal about there being one.
  const numbers = useMemo(() => citationNumbers(article?.sources ?? []), [article?.sources]);

  if (status === "pending") return <ArticleSkeleton />;

  if (status === "error") {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <main className={ARTICLE_ERROR_PAGE}>
        <h1 className={ARTICLE_ERROR_TITLE}>
          {notFound ? "Article not found" : "Something went wrong"}
        </h1>
        <Link to="/" className={BACK_TO_FEED_LINK}>
          Back to the feed
        </Link>
      </main>
    );
  }

  const subject = article.subject;
  const kicker = subjectLabel(subject);

  return (
    <main className={ARTICLE_PAGE}>
      {/*
        `navigate(-1)` rather than a link to `/`, so a reader who arrived from a
        narrowed feed goes back to that feed rather than to an unfiltered one
        they then have to narrow again. Falls through to the feed when there is
        no history to go back to - a shared link opened in a new tab.
      */}
      <button
        onClick={() => (window.history.length > 1 ? navigate(-1) : navigate("/"))}
        className={ARTICLE_BACK_LINK}
      >
        ← Feed
      </button>

      <article>
        {article.retractionNotice ? (
          <aside className={RETRACTION_BANNER} role="note">
            <span className={RETRACTION_BANNER_LABEL}>Source retracted</span>
            <span className={RETRACTION_BANNER_TEXT}>
              A paper cited below has been withdrawn by the journal that
              published it since this article was written. The article is under
              review and has not been updated yet, so weigh the verdict
              accordingly. The affected source is marked in the list.
            </span>
          </aside>
        ) : null}

        {kicker ? (
          <div className={articleKicker(subjectText(subject))}>{kicker}</div>
        ) : null}

        <h1 className={ARTICLE_TITLE}>{article.headline}</h1>
        <p className={ARTICLE_LEDE}>{article.summary}</p>

        <div className={ARTICLE_BYLINE}>
          <span>
            Written by{" "}
            <strong className={ARTICLE_BYLINE_STRONG}>You.th Medical Team</strong>
          </span>
          <span className={ARTICLE_BYLINE_SEP}>|</span>
          <span>
            <PublishedDate iso={article.publishedAt} />
          </span>
          <span className={ARTICLE_BYLINE_SEP}>|</span>
          {/*
            Derived from the body on this page rather than sent by the API, so
            it describes the document the reader was actually given - including
            anything a reviewer cut before publishing. See lib/readingTime.ts.
          */}
          <span className={ARTICLE_BYLINE_LIFT}>
            {readingTimeLabel(article.content)}
          </span>
          <span className={ARTICLE_BYLINE_SEP}>|</span>
          <span>{sourceSummary(article.sources.length)}</span>
          <span className={ARTICLE_BYLINE_SEP}>|</span>
          <span className={ARTICLE_BYLINE_LIFT}>
            Reviewed by a person before publishing
          </span>
        </div>

        {/*
          Verdict and evidence grade on one line, because they are one thought:
          the judgment and the warrant for it. The previous design put the grade
          in a sidebar, which let a reader take the verdict without ever meeting
          what caps it.
        */}
        <div className={ARTICLE_VERDICT_BAR}>
          <GradeBar grade={article.evidenceGrade} />
          <VerdictMark verdict={article.verdict} size="sm" />
          <span className={verdictWording(article.verdict, "sm")}>
            {VERDICT_LABELS[article.verdict]}
          </span>
          {article.verdictQualifier ? (
            <span className={ARTICLE_VERDICT_QUALIFIER}>
              {article.verdictQualifier}
            </span>
          ) : null}
        </div>

        <p className={VERDICT_GLOSS_TEXT}>
          {VERDICT_GLOSS[article.verdict]} {GRADE_NOTES[article.evidenceGrade]}
        </p>

        <ArticleContent
          doc={article.content}
          sources={article.sources}
          numbers={numbers}
          onCite={setActiveHandle}
        />

        <SourceList
          sources={article.sources}
          citations={article.citations}
          numbers={numbers}
          activeHandle={activeHandle}
        />

        <p className={ARTICLE_DISCLAIMER}>{article.disclaimer}</p>

        <ArticleActions slug={article.slug} headline={article.headline} />
      </article>

      <ReadMore slug={article.slug} subject={subject} />
    </main>
  );
}

/** "4 sources cited" - the byline's evidence line, in the reader's terms. */
function sourceSummary(total: number): string {
  if (total === 0) return "No study tests this claim";
  return `${total} ${total === 1 ? "source" : "sources"} cited`;
}

/**
 * Skeleton, not a spinner: a spinner collapses the layout, so the page reflows
 * the moment content lands.
 */
function ArticleSkeleton() {
  return (
    <main
      className={ARTICLE_SKELETON_PAGE}
      aria-busy="true"
      aria-label="Loading article"
    >
      <div className={SKELETON_KICKER} />
      <div className={SKELETON_TITLE} />
      <div className={SKELETON_LEDE} />
      <div className={SKELETON_LEAD_IMAGE} />
      <div className={SKELETON_PROSE}>
        {[0, 1, 2].map((index) => (
          <div key={index} className={SKELETON_PARAGRAPH} />
        ))}
      </div>
    </main>
  );
}
