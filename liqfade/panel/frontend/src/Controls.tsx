import { useState } from "react";
import { sendControl } from "./api";
import type { ControlOutcome } from "./api";
import type { StateSnapshot } from "./types";
import { Section, stamp } from "./ui";

/**
 * Pause, resume and flat, each behind a click and a confirm.
 *
 * There is no kill button, on purpose. `/kill` stays Telegram and the `data/KILL` file:
 * it is the one control that has to work when the panel is the thing that is broken,
 * and a button in a browser tab is the easiest thing in this system to hit by accident.
 * The confirm step is enforced by the endpoint too, so a reload mid-dialog cannot half
 * submit one.
 */

type Pending = { action: "pause" | "resume" | "flat"; coin?: string; label: string } | null;

const BUTTON = "rounded px-3 py-1.5 text-sm font-medium disabled:opacity-30";

export default function Controls({
  state,
  readOnly,
  onDone,
}: {
  state: StateSnapshot | null;
  readOnly: boolean;
  onDone: () => void;
}) {
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<ControlOutcome | null>(null);

  async function confirm() {
    if (!pending) return;
    setBusy(true);
    const result = await sendControl(pending.action, pending.coin);
    setBusy(false);
    setPending(null);
    setOutcome(result);
    onDone();
  }

  const coins = state?.positions.map((p) => p.coin) ?? [];
  const paused = state?.risk.paused ?? false;
  const disabled = readOnly || busy;

  return (
    <Section
      title="controls"
      right={
        readOnly ? (
          <span className="text-xs text-amber-300">read only: no fresh snapshot</span>
        ) : null
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          disabled={disabled || paused}
          onClick={() => setPending({ action: "pause", label: "Pause new entries" })}
          className={`${BUTTON} bg-amber-500/20 text-amber-200 hover:bg-amber-500/30`}
        >
          pause
        </button>
        <button
          disabled={disabled || !paused}
          onClick={() => setPending({ action: "resume", label: "Resume new entries" })}
          className={`${BUTTON} bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30`}
        >
          resume
        </button>
        <button
          disabled={disabled}
          onClick={() =>
            setPending({ action: "flat", label: "Close every position and cancel every order" })
          }
          className={`${BUTTON} bg-red-500/20 text-red-200 hover:bg-red-500/30`}
        >
          flat all
        </button>
        {coins.map((coin) => (
          <button
            key={coin}
            disabled={disabled}
            onClick={() => setPending({ action: "flat", coin, label: `Close ${coin}` })}
            className={`${BUTTON} bg-red-500/10 text-red-200 hover:bg-red-500/20`}
          >
            flat {coin}
          </button>
        ))}
        <span className="ml-auto text-xs text-neutral-500">
          kill is Telegram /kill and the data/KILL file, never a button here
        </span>
      </div>

      {pending && (
        <div className="mt-3 rounded border border-amber-700/60 bg-amber-950/30 p-3 text-sm">
          <p className="text-amber-200">{pending.label}. This takes effect immediately.</p>
          <div className="mt-2 flex gap-2">
            <button
              onClick={confirm}
              disabled={busy}
              className={`${BUTTON} bg-amber-400 text-neutral-900`}
            >
              {busy ? "sending" : "confirm"}
            </button>
            <button
              onClick={() => setPending(null)}
              className={`${BUTTON} text-neutral-400 hover:text-neutral-200`}
            >
              cancel
            </button>
          </div>
        </div>
      )}

      {outcome && (
        <div
          className={`mt-3 rounded border px-3 py-2 text-sm ${
            outcome.ok
              ? "border-emerald-800/60 bg-emerald-950/30 text-emerald-300"
              : "border-red-800/60 bg-red-950/30 text-red-300"
          }`}
        >
          <div className="flex items-baseline justify-between gap-4">
            <span>
              {outcome.action}
              {outcome.ok ? " done" : " failed"}: {outcome.detail}
            </span>
            <button
              onClick={() => setOutcome(null)}
              className="text-xs text-neutral-500 hover:text-neutral-300"
            >
              dismiss
            </button>
          </div>
          <p className="mt-1 text-xs text-neutral-500">{stamp(outcome.ts_ms)} UTC, in the audit log</p>
        </div>
      )}
    </Section>
  );
}
