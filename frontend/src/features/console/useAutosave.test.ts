// @vitest-environment jsdom
/**
 * The autosave hook's promises, each the reason a reviewer's edit is not lost
 * or written over someone else's. The save function is passed in, so this
 * needs no network: `save` is a fake that records what it was asked to write.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import type { TipTapDoc } from "@/types/api";
import { useAutosave } from "./useAutosave";

const doc = (text: string): TipTapDoc =>
  ({
    type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text }] }],
  }) as TipTapDoc;

type Save = (id: string, content: TipTapDoc, base: TipTapDoc | null) => Promise<void>;

function setup(save: Save, loaded: TipTapDoc | null = null) {
  return renderHook(
    ({ content }: { content: TipTapDoc | null | undefined }) =>
      useAutosave("article-1", content, save),
    { initialProps: { content: loaded } },
  );
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("useAutosave", () => {
  it("waits for a pause in typing, then saves only the latest document", async () => {
    const save = vi.fn<Save>().mockResolvedValue(undefined);
    const { result } = setup(save);

    act(() => result.current.schedule(doc("a")));
    act(() => result.current.schedule(doc("ab")));
    expect(save).not.toHaveBeenCalled();
    expect(result.current.hasUnsavedChanges).toBe(true);

    await act(async () => vi.advanceTimersByTimeAsync(800));

    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith("article-1", doc("ab"), null);
    expect(result.current.state).toBe("saved");
    expect(result.current.hasUnsavedChanges).toBe(false);
  });

  it("names the document each save started from", async () => {
    const save = vi.fn<Save>().mockResolvedValue(undefined);
    const { result } = setup(save, doc("loaded"));

    act(() => result.current.schedule(doc("first")));
    await act(async () => vi.advanceTimersByTimeAsync(800));
    act(() => result.current.schedule(doc("second")));
    await act(async () => vi.advanceTimersByTimeAsync(800));

    expect(save.mock.calls.map((call) => call[2])).toEqual([doc("loaded"), doc("first")]);
  });

  it("keeps an edit whose save failed, and the next flush sends it again", async () => {
    const save = vi
      .fn<Save>()
      .mockRejectedValueOnce(new ApiError(500, "down"))
      .mockResolvedValue(undefined);
    const { result } = setup(save);

    act(() => result.current.schedule(doc("kept")));
    await act(async () => vi.advanceTimersByTimeAsync(800));
    expect(result.current.state).toBe("error");
    expect(result.current.hasUnsavedChanges).toBe(true);

    let saved = false;
    await act(async () => {
      saved = await result.current.flush();
    });
    expect(saved).toBe(true);
    expect(save).toHaveBeenLastCalledWith("article-1", doc("kept"), null);
  });

  it("stops on a conflict instead of writing over another reviewer's save", async () => {
    const save = vi.fn<Save>().mockRejectedValue(new ApiError(412, "conflict"));
    const { result } = setup(save);

    act(() => result.current.schedule(doc("mine")));
    await act(async () => vi.advanceTimersByTimeAsync(800));
    expect(result.current.state).toBe("conflict");

    // Neither more typing nor an explicit flush retries the older copy.
    act(() => result.current.schedule(doc("mine, more")));
    await act(async () => vi.advanceTimersByTimeAsync(800));
    let saved = true;
    await act(async () => {
      saved = await result.current.flush();
    });
    expect(saved).toBe(false);
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("answers flush honestly: done with nothing pending, not done while a save runs", async () => {
    let finish: () => void = () => {};
    const save = vi.fn<Save>(() => new Promise<void>((resolve) => (finish = resolve)));
    const { result } = setup(save);

    let idle = false;
    await act(async () => {
      idle = await result.current.flush();
    });
    expect(idle).toBe(true);

    act(() => result.current.schedule(doc("slow")));
    let first: Promise<boolean> = Promise.resolve(false);
    act(() => {
      first = result.current.flush();
    });
    let second = true;
    await act(async () => {
      second = await result.current.flush();
    });
    expect(second).toBe(false); // the first save has not landed yet
    await act(async () => {
      finish();
      await first;
    });
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("a reloaded document replaces what was pending, so the old copy is never saved over it", async () => {
    const save = vi.fn<Save>().mockResolvedValue(undefined);
    const { result, rerender } = setup(save, doc("v1"));

    act(() => result.current.schedule(doc("typed on v1")));
    rerender({ content: doc("v2 from the server") });
    await act(async () => vi.advanceTimersByTimeAsync(800));

    expect(save).not.toHaveBeenCalled();
    expect(result.current.hasUnsavedChanges).toBe(false);

    act(() => result.current.schedule(doc("typed on v2")));
    await act(async () => vi.advanceTimersByTimeAsync(800));
    expect(save).toHaveBeenCalledWith("article-1", doc("typed on v2"), doc("v2 from the server"));
  });

  it("saves at once when the tab is hidden, before the debounce", async () => {
    const save = vi.fn<Save>().mockResolvedValue(undefined);
    const { result } = setup(save);

    act(() => result.current.schedule(doc("closing the tab")));
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    expect(save).toHaveBeenCalledWith("article-1", doc("closing the tab"), null);
  });
});
