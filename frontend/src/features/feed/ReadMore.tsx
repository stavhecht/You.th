/**
 * "Read more on this" - three articles under the one just read.
 *
 * Same subject first, then anything else, which is the comp's ordering and is
 * also the honest one: these are *related*, not recommended. Nothing here is
 * personalised and nothing is ranked by engagement - it is a subject match and
 * recency, and the standfirst says so.
 *
 * Reuses the feed query rather than adding a related-articles endpoint. The
 * first page is already cached from the feed the reader arrived through, so in
 * the common case this row costs no request at all; when it does fetch, it
 * fetches the same page the back button would have shown anyway.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { VERDICT_LABELS } from "@/features/evidence/labels";
import { SUBJECT_LABELS } from "@/features/evidence/subject";
import { feedKeys, fetchFeed } from "@/lib/api/feed";
import {
  REC_CARD,
  REC_KICKER,
  REC_TITLE,
  RECS_GRID,
  RECS_SECTION,
  RECS_STANDFIRST,
  RECS_TITLE,
} from "./styles";
import type { FeedCard, Subject } from "@/types/api";

const HOW_MANY = 3;

export function ReadMore({
  slug,
  subject,
}: {
  slug: string;
  subject: Subject | null;
}) {
  const { data } = useQuery({
    // Deliberately the *unfiltered* feed's key, so this shares a cache entry
    // with the feed page rather than creating a parallel one that has to be
    // fetched and then invalidated separately.
    queryKey: feedKeys.list({ personalise: false }),
    queryFn: () => fetchFeed({ personalise: false, limit: 24 }),
    staleTime: 60_000,
  });

  const others = (data?.items ?? []).filter((card) => card.slug !== slug);
  if (others.length === 0) return null;

  const sameSubject = subject
    ? others.filter((card) => card.subject === subject)
    : [];
  const rest = others.filter((card) => !sameSubject.includes(card));
  const picks = [...sameSubject, ...rest].slice(0, HOW_MANY);

  return (
    <section className={RECS_SECTION}>
      <h2 className={RECS_TITLE}>Read more on this</h2>
      <p className={RECS_STANDFIRST}>
        Different studies, same shelf. These ask a different research question.
      </p>

      <div className={RECS_GRID}>
        {picks.map((card) => (
          <Link key={card.slug} to={`/a/${card.slug}`} className={REC_CARD}>
            <div className={REC_KICKER}>{kicker(card)}</div>
            <div className={REC_TITLE}>{card.headline}</div>
          </Link>
        ))}
      </div>
    </section>
  );
}

/**
 * Subject where there is one, verdict otherwise.
 *
 * Never blank: this row is three headlines with nothing else to tell them
 * apart, and a kicker is what stops them reading as one undifferentiated list.
 */
function kicker(card: FeedCard): string {
  return card.subject
    ? SUBJECT_LABELS[card.subject]
    : VERDICT_LABELS[card.verdict];
}
