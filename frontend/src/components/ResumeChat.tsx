/**
 * Refine a tailored document by chatting about it — the "Chat" tab of the
 * Tailor modal.
 *
 * A reply never edits the document. It carries a proposal: each change shows the
 * text before and after, and is either applicable or **blocked** by the
 * backend's fact check (an invented metric, or a stack the profile lists under
 * gaps). Blocked changes stay on screen with their reason, because "why didn't
 * it add Next.js?" is exactly what the user will ask next. Applying is free;
 * only sending a message costs an LLM call.
 *
 * Serves the résumé and the cover letter both. The two address different things
 * — the résumé's template regions, the letter's body paragraphs — but the
 * backend builds proposals in one shape (`resume_chat.py`, `cover_chat.py`), so
 * the only thing that varies here is the wording. A letter can also propose an
 * **added** paragraph, which a résumé never can: it has no slot to add one to.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowUp, Check, Loader2, MessageSquare, Trash2, X } from "lucide-react";

import { ApiError, applyChat, clearChat, dismissChat, fetchChat, sendChat } from "../lib/api";
import type { ChatMessage, ChatProposal, ResumeChat as Chat, TailoredDocument } from "../lib/types";
import { cx } from "./primitives";

export function ResumeChat({
  doc,
  dirty,
  onApplied,
}: {
  doc: TailoredDocument;
  /** Unsaved LaTeX edits in the editor: applying would overwrite them. */
  dirty: boolean;
  onApplied: (next: TailoredDocument) => void;
}) {
  const queryClient = useQueryClient();
  const what = doc.kind === "cover_letter" ? "cover letter" : "résumé";
  const key = ["resume-chat", doc.id];
  const [input, setInput] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const chat = useQuery({ queryKey: key, queryFn: () => fetchChat(doc.id) });
  const setChat = (next: Chat) => queryClient.setQueryData(key, next);

  const send = useMutation({
    mutationFn: (message: string) => sendChat(doc.id, message),
    onMutate: (message) => {
      setPending(message);
      setInput("");
      setError(null);
    },
    onSuccess: setChat,
    onError: (e: unknown, message) => {
      setError(describe(e));
      setInput(message); // hand the text back rather than losing it
    },
    onSettled: () => setPending(null),
  });

  const apply = useMutation({
    mutationFn: ({ id, accept }: { id: string; accept: string[] }) =>
      applyChat(doc.id, id, accept),
    onSuccess: ({ document, chat: next }) => {
      setChat(next);
      setError(null);
      onApplied(document);
    },
    onError: (e: unknown) => setError(describe(e)),
  });

  const dismiss = useMutation({
    mutationFn: (id: string) => dismissChat(doc.id, id),
    onSuccess: setChat,
    onError: (e: unknown) => setError(describe(e)),
  });

  const clear = useMutation({
    mutationFn: () => clearChat(doc.id),
    onSuccess: setChat,
  });

  const messages = chat.data?.messages ?? [];

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, pending]);

  const submit = (text: string) => {
    const message = text.trim();
    if (message && !send.isPending) send.mutate(message);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={scroller} className="min-h-0 flex-1 space-y-3 overflow-auto px-4 py-3">
        {chat.isLoading ? (
          <div className="flex items-center gap-2 text-[12px] text-subtle">
            <Loader2 size={13} className="animate-spin" /> Loading…
          </div>
        ) : messages.length === 0 && !pending ? (
          <Intro what={what} />
        ) : (
          messages.map((message) => (
            <Message
              key={message.id}
              message={message}
              what={what}
              dirty={dirty}
              applying={apply.isPending && apply.variables?.id === message.id}
              onApply={(accept) => apply.mutate({ id: message.id, accept })}
              onDismiss={() => dismiss.mutate(message.id)}
            />
          ))
        )}

        {pending && (
          <>
            <UserBubble text={pending} />
            <div className="flex items-center gap-2 text-[12px] text-subtle">
              <Loader2 size={13} className="animate-spin" />
              Reading the posting and your {what}…
            </div>
          </>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-2 border-t border-edge bg-rose-500/10 px-4 py-2 text-[12px] text-rose-200">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span className="flex-1">{error}</span>
          <button onClick={() => setError(null)} aria-label="Dismiss error">
            <X size={13} />
          </button>
        </div>
      )}

      {dirty && (
        <div className="border-t border-edge bg-amber-500/10 px-4 py-1.5 text-[11.5px] text-amber-200">
          You have unsaved LaTeX edits. Save them (Ctrl+S) before applying a suggestion, so
          neither overwrites the other.
        </div>
      )}

      <div className="shrink-0 border-t border-edge px-3 pt-2 pb-3">
        {(chat.data?.suggestions.length ?? 0) > 0 && (
          <div className="mb-2 flex gap-1.5 overflow-x-auto pb-1">
            {chat.data!.suggestions.map((suggestion) => (
              <button
                key={suggestion}
                onClick={() => submit(suggestion)}
                disabled={send.isPending}
                className="shrink-0 rounded-full border border-edge bg-panel px-2.5 py-1 text-[11px] text-muted hover:border-accent/50 hover:text-ink disabled:opacity-50"
              >
                {suggestion}
              </button>
            ))}
          </div>
        )}

        <div className="flex items-end gap-2 rounded-xl border border-edge bg-black/20 px-3 py-2 focus-within:border-accent/60">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit(input);
              }
            }}
            rows={2}
            maxLength={2000}
            placeholder="Ask for a change — “lead with the WebSockets work”, “shorten the summary”…"
            className="max-h-32 min-h-[2.5rem] flex-1 resize-none bg-transparent text-[12.5px] text-ink outline-none placeholder:text-subtle"
          />
          <button
            onClick={() => submit(input)}
            disabled={!input.trim() || send.isPending}
            className="rounded-lg bg-accent p-1.5 text-black disabled:opacity-40"
            aria-label="Send"
            title="Send (Enter). One LLM call."
          >
            {send.isPending ? <Loader2 size={14} className="animate-spin" /> : <ArrowUp size={14} />}
          </button>
        </div>

        <div className="mt-1.5 flex items-center gap-2 text-[10.5px] text-subtle">
          <span>
            Each message is one LLM call (~6-9k tokens)
            {(chat.data?.tokens ?? 0) > 0 && ` · ${chat.data!.tokens.toLocaleString()} used here`}
          </span>
          {messages.length > 0 && (
            <button
              onClick={() => clear.mutate()}
              disabled={clear.isPending || send.isPending}
              className="ml-auto flex items-center gap-1 hover:text-ink"
              title={`Start the conversation over. Your ${what} is not changed.`}
            >
              <Trash2 size={11} />
              clear chat
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Intro({ what }: { what: string }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-8 text-center">
      <MessageSquare size={20} className="text-accent" />
      <p className="max-w-sm text-[12.5px] text-muted">
        Ask for changes to this {what}, or ask what it should do about this posting. Each reply
        suggests edits you can apply or dismiss. Nothing changes until you apply it.
      </p>
      <p className="max-w-sm text-[11px] text-subtle">
        Edits are checked against your profile. A metric or a stack you haven&apos;t listed is
        blocked, and the reply tells you why.
      </p>
    </div>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent/15 px-3 py-2 text-[12.5px] whitespace-pre-wrap text-ink">
        {text}
      </div>
    </div>
  );
}

function Message({
  message,
  what,
  dirty,
  applying,
  onApply,
  onDismiss,
}: {
  message: ChatMessage;
  what: string;
  dirty: boolean;
  applying: boolean;
  onApply: (accept: string[]) => void;
  onDismiss: () => void;
}) {
  if (message.role === "user") return <UserBubble text={message.text} />;
  return (
    <div className="space-y-2">
      <div className="max-w-[92%] text-[12.5px] leading-relaxed text-ink">
        <Marked text={message.text} />
      </div>
      {message.proposal && message.proposal.status !== "none" && (
        <Proposal
          proposal={message.proposal}
          what={what}
          dirty={dirty}
          applying={applying}
          onApply={onApply}
          onDismiss={onDismiss}
        />
      )}
    </div>
  );
}

/** `add` is cover-letter only — the résumé has no slot to add a bullet to. */
const ACTION_LABELS: Record<string, string> = {
  rewrite: "reword",
  drop: "remove",
  add: "new",
};

function Proposal({
  proposal,
  what,
  dirty,
  applying,
  onApply,
  onDismiss,
}: {
  proposal: ChatProposal;
  /** "résumé" or "cover letter", for the apply button's tooltip. */
  what: string;
  dirty: boolean;
  applying: boolean;
  onApply: (accept: string[]) => void;
  onDismiss: () => void;
}) {
  const applicable = [
    ...proposal.changes.filter((c) => c.status === "proposed").map((c) => c.region_id),
    ...(proposal.order.length > 0 ? ["order"] : []),
  ];
  const [selected, setSelected] = useState<Set<string>>(() => new Set(applicable));
  const open = proposal.status === "pending";
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const wasApplied = (id: string) => proposal.applied.includes(id);

  return (
    <div
      className={cx(
        "rounded-xl border border-edge bg-panel/60 p-2.5",
        !open && "opacity-75",
      )}
    >
      <ul className="space-y-2">
        {proposal.changes.map((change) => {
          const blocked = change.status === "blocked";
          return (
            <li key={change.region_id} className="text-[12px]">
              <label className={cx("flex gap-2", open && !blocked && "cursor-pointer")}>
                {open && !blocked ? (
                  <input
                    type="checkbox"
                    checked={selected.has(change.region_id)}
                    onChange={() => toggle(change.region_id)}
                    className="mt-0.5 accent-accent"
                  />
                ) : (
                  <span className="mt-0.5 w-[13px] shrink-0">
                    {blocked ? (
                      <AlertTriangle size={12} className="text-rose-300" />
                    ) : wasApplied(change.region_id) ? (
                      <Check size={12} className="text-emerald-300" />
                    ) : null}
                  </span>
                )}
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5 text-[10.5px] text-subtle">
                    <span className="font-semibold text-muted">{change.label}</span>
                    <span
                      className={cx(
                        "rounded px-1 py-px",
                        blocked
                          ? "bg-rose-500/15 text-rose-200"
                          : change.action === "drop"
                            ? "bg-amber-500/15 text-amber-200"
                            : change.action === "add"
                              ? "bg-emerald-500/15 text-emerald-200"
                              : "bg-accent/15 text-accent",
                      )}
                    >
                      {blocked ? "blocked" : ACTION_LABELS[change.action]}
                    </span>
                  </div>
                  {/* An added paragraph has no "before", so the strikethrough
                      line is omitted rather than rendered empty. */}
                  {change.before && (
                    <p className="text-subtle line-through decoration-rose-400/40">
                      <Marked text={change.before} />
                    </p>
                  )}
                  {change.action !== "drop" && (
                    <p className={blocked ? "text-muted" : "text-ink"}>
                      <Marked text={change.after} />
                    </p>
                  )}
                  {change.reason && <p className="text-[11px] text-subtle">{change.reason}</p>}
                  {change.blocked.map((reason) => (
                    <p key={reason} className="text-[11px] text-rose-200/90">
                      Blocked: {reason}
                    </p>
                  ))}
                  {change.warnings.slice(0, 3).map((warning) => (
                    <p key={warning} className="text-[11px] text-amber-200/80">
                      Check: {warning}
                    </p>
                  ))}
                </div>
              </label>
            </li>
          );
        })}

        {proposal.order.length > 0 && (
          <li className="text-[12px]">
            <label className={cx("flex gap-2", open && "cursor-pointer")}>
              {open ? (
                <input
                  type="checkbox"
                  checked={selected.has("order")}
                  onChange={() => toggle("order")}
                  className="mt-0.5 accent-accent"
                />
              ) : (
                <span className="mt-0.5 w-[13px] shrink-0">
                  {wasApplied("order") && <Check size={12} className="text-emerald-300" />}
                </span>
              )}
              <div className="min-w-0 flex-1">
                <p className="mb-1 text-[10.5px] font-semibold text-muted">New bullet order</p>
                <ol className="list-decimal space-y-0.5 pl-4 text-[11.5px] text-muted">
                  {proposal.order_preview.map((item) => (
                    <li key={item.region_id} className="truncate">
                      <Marked text={item.text} />
                    </li>
                  ))}
                </ol>
              </div>
            </label>
          </li>
        )}
      </ul>

      <div className="mt-2.5 flex items-center gap-2 border-t border-edge pt-2">
        {open ? (
          applicable.length > 0 ? (
            <>
              <button
                onClick={() => onApply([...selected])}
                disabled={applying || dirty || selected.size === 0}
                title={dirty ? "Save your LaTeX edits first" : `Apply to the ${what} and recompile`}
                className="flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1 text-[11.5px] font-semibold text-black disabled:opacity-40"
              >
                {applying ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                Apply {selected.size === applicable.length ? "" : `${selected.size} of `}
                {applicable.length}
              </button>
              <button
                onClick={onDismiss}
                className="rounded-lg px-2 py-1 text-[11.5px] text-subtle hover:text-ink"
              >
                Dismiss
              </button>
            </>
          ) : (
            <span className="text-[11px] text-subtle">
              Nothing here can be applied: every change failed the fact check.
            </span>
          )
        ) : (
          <span className="text-[11px] text-subtle">
            {proposal.status === "applied" ? "Applied" : "Dismissed"}
          </span>
        )}
      </div>
    </div>
  );
}

/** The documents' `**bold**` marker dialect, rendered. */
function Marked({ text }: { text: string }) {
  const parts = text.split(/\*\*(.+?)\*\*/g);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <strong key={index} className="font-semibold">
            {part}
          </strong>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}
