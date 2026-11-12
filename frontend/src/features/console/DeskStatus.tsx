/**
 * Two small things the desk could not see before.
 *
 * `WorkerStatusLine`: what the worker is doing. A queued run looked the same
 * whether the worker was busy, waiting for the model server, or not running;
 * the only signal was a line in the worker's log.
 *
 * `ContactInbox`: the messages readers sent. They were stored and emailed, but
 * with no screen here a broken mail setting meant nobody ever read them.
 * Rendered as text only: the note and the link are written by strangers.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchContactInbox, fetchWorkerStatus } from "@/lib/api/console";
import {
  CANDIDATES_COUNT,
  CANDIDATES_EMPTY,
  CANDIDATES_HEADER,
  CANDIDATES_LIST,
  CANDIDATES_PANEL,
  CANDIDATES_TITLE,
  CANDIDATES_TOGGLE,
  QUEUE_STANDFIRST,
  RETRACTED_FLAG,
} from "./styles";

export function WorkerStatusLine() {
  const { data } = useQuery({
    queryKey: ["console", "status"],
    queryFn: fetchWorkerStatus,
    refetchInterval: 30_000,
  });
  if (!data) return null;

  const queued = data.queuedArticles + data.queuedResearch + data.queuedCollection;
  if (data.stalled) {
    return (
      <p role="status" className={RETRACTED_FLAG}>
        {queued} waiting and nothing is running. The worker is stopped, or it is
        holding work because the model server is not answering.
      </p>
    );
  }
  const running = data.running.map((run) => `${run.kind}: ${run.label}`).join(" · ");
  return (
    <p role="status" className={QUEUE_STANDFIRST}>
      Worker: {running || "idle"}
      {queued ? ` · ${queued} queued` : ""}
      {data.lastRetractionSweepAt
        ? ` · retractions checked ${new Date(data.lastRetractionSweepAt).toLocaleDateString()}`
        : " · retractions not checked yet"}
    </p>
  );
}

export function ContactInbox() {
  const [open, setOpen] = useState(false);
  const { data } = useQuery({
    queryKey: ["console", "contact"],
    queryFn: fetchContactInbox,
  });
  const messages = data ?? [];

  return (
    <section className={CANDIDATES_PANEL} aria-label="Messages from readers">
      <div className={CANDIDATES_HEADER}>
        <button
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className={CANDIDATES_TOGGLE}
        >
          <span className={CANDIDATES_TITLE}>Messages from readers</span>
          <span className={CANDIDATES_COUNT}>{messages.length}</span>
        </button>
      </div>
      {open ? (
        messages.length === 0 ? (
          <p className={CANDIDATES_EMPTY}>No messages yet.</p>
        ) : (
          <ul className={CANDIDATES_LIST}>
            {messages.map((message) => (
              <li key={message.id} className={CANDIDATES_EMPTY}>
                <b>
                  {message.kind === "topic" ? "Topic" : "Message"} · {message.name ?? "No name"} ·{" "}
                  {message.email}
                </b>{" "}
                · {new Date(message.createdAt).toLocaleDateString()}
                <br />
                {message.note}
                {message.link ? ` (${message.link})` : ""}
              </li>
            ))}
          </ul>
        )
      ) : null}
    </section>
  );
}
