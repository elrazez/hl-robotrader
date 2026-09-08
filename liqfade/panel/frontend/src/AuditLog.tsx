import { useCallback, useEffect, useState } from "react";
import { fetchAudit } from "./api";
import type { AuditEntry } from "./types";
import { Section, stamp } from "./ui";

/** How one entry's arguments read on a line. Settings writes carry their own summary. */
function describe(entry: AuditEntry): string {
  if (entry.action === "settings") {
    const changed = (entry.args.changed as Record<string, unknown>) ?? {};
    return Object.entries(changed)
      .map(([key, value]) => `${key} = ${JSON.stringify(value)}`)
      .join(", ");
  }
  const args = Object.entries(entry.args).filter(([, v]) => v !== null && v !== "");
  return args.length === 0 ? "all" : args.map(([k, v]) => `${k}=${v}`).join(" ");
}

/**
 * What the panel has done: every control action and every settings write, newest first.
 * Read from the JSONL the endpoints append to, so a page reload cannot lose the record
 * and neither can a panel restart.
 */
export default function AuditLog({ refreshKey }: { refreshKey: number }) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);

  const load = useCallback(async () => setEntries(await fetchAudit()), []);
  useEffect(() => {
    load();
  }, [load, refreshKey]);

  return (
    <Section
      title="audit log"
      right={
        <button onClick={load} className="text-xs text-neutral-500 hover:text-neutral-300">
          refresh
        </button>
      }
    >
      {entries.length === 0 ? (
        <p className="text-sm text-neutral-500">nothing yet</p>
      ) : (
        <table className="w-full text-sm [&_td:not(:first-child)]:pl-4 [&_th:not(:first-child)]:pl-4">
          <thead className="text-left text-xs uppercase text-neutral-500">
            <tr>
              <th className="pb-1 font-medium">time (UTC)</th>
              <th className="pb-1 font-medium">action</th>
              <th className="pb-1 font-medium">what</th>
              <th className="pb-1 font-medium">outcome</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={`${entry.ts_ms}-${entry.action}`} className="border-t border-neutral-800/60">
                <td className="py-1 tabular-nums text-neutral-400">{stamp(entry.ts_ms)}</td>
                <td className="py-1">{entry.action}</td>
                <td className="py-1 text-neutral-300">{describe(entry)}</td>
                <td className={`py-1 ${entry.ok ? "text-emerald-400" : "text-red-400"}`}>
                  {entry.ok ? "ok" : (entry.error ?? "failed")}
                  {entry.reason ? (
                    <span className="ml-2 text-neutral-500">{entry.reason}</span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}
