import { ArrowUpRight, MessagesSquare } from "lucide-react";

import { EmptyState } from "./primitives";

/**
 * Interview Prep — reserved, not designed.
 *
 * A real page rather than a disabled nav tile, because a tile that does nothing
 * when clicked reads as broken. This one says what it is and what it is not, so
 * the "Soon" badge is a promise rather than a dead end.
 *
 * Deliberately empty of guesses. The scope has not been decided, and stubbing
 * plausible-looking sections here would turn an open question into an
 * implementation that then has to be argued out of.
 */
export function PrepView({ onOpenDashboard }: { onOpenDashboard: () => void }) {
  return (
    <div className="glass-strong flex flex-1 items-center justify-center rounded-2xl px-6">
      <div className="max-w-md">
        <EmptyState
          icon={<MessagesSquare size={26} />}
          title="Interview Prep — coming soon"
          description="Nothing is built here yet, and the shape of it is still an open question. The pieces it would draw on already exist: the job description, your profile and evidence lines, and the gaps the deep read named for each posting."
          action={
            <button
              onClick={onOpenDashboard}
              className="btn-primary mt-1 flex items-center gap-1.5 rounded-xl px-4 py-2 text-[13px] font-semibold"
            >
              See what's in flight
              <ArrowUpRight size={14} />
            </button>
          }
        />
      </div>
    </div>
  );
}
