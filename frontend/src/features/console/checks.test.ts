import { describe, expect, it } from "vitest";

import type { ReviewSource, TipTapDoc, ValidationReport } from "@/types/api";
import { buildChecks, citedSentences, missingTerms, sentenceKey } from "./checks";

const src = (over: Partial<ReviewSource>): ReviewSource =>
  ({
    citationHandle: "S1",
    claim: "c",
    title: "A trial",
    year: 2020,
    abstract: "",
    excerpts: [],
    wasCited: true,
    stance: "supports",
    ...over,
  }) as ReviewSource;

const report = {
  citationsResolved: 2,
  citationsTotal: 2,
  verdictCeiling: "mixed",
  failures: [],
  warnings: [],
} as unknown as ValidationReport;

describe("buildChecks", () => {
  it("counts a paper cited for two claims once, and is off-topic only if every row is", () => {
    const sources = [
      src({ claim: "a", stance: "off_topic" }),
      src({ claim: "b", stance: "supports" }),
      src({ citationHandle: "S2", claim: "a", stance: "off_topic" }),
      src({ citationHandle: "S2", claim: "b", stance: "off_topic" }),
    ];
    const relevance = buildChecks(report, sources, "mixed").find((c) => c.name === "citation relevance")!;
    expect(relevance.detail).toBe("1/2 appraised cited sources test the claim; off-topic: [S2]");
    expect(relevance.ok).toBe(false);
  });

  it("flags too confident and too cautious verdicts", () => {
    const u = (v: "supported" | "weak" | "mixed") =>
      buildChecks(report, [src({})], v).find((c) => c.name === "uncertainty")!;
    expect(u("supported").detail).toContain("too confident");
    expect(u("weak").detail).toContain("more cautious");
    expect(u("mixed").ok).toBe(true);
  });
});

describe("citedSentences", () => {
  const doc = {
    type: "doc",
    content: [
      {
        type: "paragraph",
        content: [
          { type: "text", text: "Intro line. Jump height fell after massage guns. " },
          { type: "citation", attrs: { sourceIds: ["S1", "S2"] } },
        ],
      },
    ],
  } as TipTapDoc;

  it("returns the last sentence before a citation, with its handles", () => {
    expect(citedSentences(doc)).toEqual([
      { text: "Jump height fell after massage guns.", handles: ["S1", "S2"] },
    ]);
  });
});

describe("missingTerms", () => {
  const sentence = { text: "Jump height fell by 10 percent after massage guns.", handles: ["S1"] };

  it("flags topic words and numbers the source lacks, not common words", () => {
    const sources = [src({ abstract: "We studied sleep quality in 2010 and found 12 effects." })];
    const missing = missingTerms(sentence, sources);
    expect(missing).toEqual(expect.arrayContaining(["height", "10", "massage"]));
    expect(missing).not.toContain("after");
  });

  it("matches a number as a whole token, so 10 is not found inside 2010", () => {
    const sources = [src({ abstract: "jump height massage guns percent 2010" })];
    expect(missingTerms(sentence, sources)).toEqual(["10"]);
  });

  it("is empty when the source holds every word and number", () => {
    const sources = [src({ abstract: "Jump heights fell by 10 percent; massage guns were used." })];
    expect(missingTerms(sentence, sources)).toEqual([]);
  });
});

it("keys a sentence by its text and handles, not its position", () => {
  expect(sentenceKey({ text: "x.", handles: ["S1", "S2"] })).toBe("S1,S2|x.");
});
