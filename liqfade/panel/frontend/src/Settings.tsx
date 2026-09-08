import { useCallback, useEffect, useMemo, useState } from "react";
import AuditLog from "./AuditLog";
import { fetchConfig, writeConfig } from "./api";
import type { ConfigView, ConfigValue, DiffRow, WriteResult } from "./types";
import { Banner, Section } from "./ui";

/**
 * The settings page: sizing fractions, risk limits, trigger thresholds and universe
 * overrides.
 *
 * The form is built from what `/api/config` returns, including which sections are
 * writable and which need a typed confirmation, so the form and the endpoint cannot
 * disagree about what may be changed. Nothing here can enable live trading: `env` is not
 * in the writable set, and the endpoint refuses it whatever this page sends.
 */

const INPUT =
  "w-40 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-right text-sm tabular-nums outline-none focus:border-neutral-500";

/** Parse a form string back to the type the current value has. */
function coerce(raw: string, before: unknown): unknown {
  if (typeof before === "number") {
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : raw;
  }
  if (typeof before === "boolean") return raw === "true";
  if (Array.isArray(before)) {
    return raw
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
  }
  return raw;
}

function show(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (value === null || value === undefined) return "";
  return String(value);
}

function Field({
  row,
  draft,
  onEdit,
}: {
  row: ConfigValue;
  draft: string | undefined;
  onEdit: (raw: string) => void;
}) {
  const edited = draft !== undefined && draft !== show(row.value);
  return (
    <div className="flex items-center justify-between gap-4 border-t border-neutral-800/60 py-1.5">
      <div className="min-w-0">
        <div className="truncate text-sm">{row.key}</div>
        {/* Which layer set the value that is there now. Without it a number on this page
            is just a number, and the owner cannot tell a default from their own change. */}
        <div className="text-xs text-neutral-500">from {row.source ?? "unset"}</div>
      </div>
      {typeof row.value === "boolean" ? (
        <select
          className={INPUT}
          value={draft ?? show(row.value)}
          onChange={(e) => onEdit(e.target.value)}
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      ) : (
        <input
          className={`${INPUT} ${edited ? "border-amber-500 text-amber-200" : ""}`}
          value={draft ?? show(row.value)}
          onChange={(e) => onEdit(e.target.value)}
        />
      )}
    </div>
  );
}

export default function SettingsPage() {
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [confirms, setConfirms] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<DiffRow[] | null>(null);
  const [outcome, setOutcome] = useState<WriteResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [written, setWritten] = useState(0);

  const load = useCallback(async () => setConfig(await fetchConfig()), []);
  useEffect(() => {
    load();
  }, [load]);

  const bySection = useMemo(() => {
    const groups = new Map<string, ConfigValue[]>();
    for (const row of config?.values ?? []) {
      const section = row.key.split(".")[0];
      if (!config?.writable_sections.includes(section)) continue;
      const list = groups.get(section) ?? [];
      list.push(row);
      groups.set(section, list);
    }
    return groups;
  }, [config]);

  /** The changed fields, typed back to the shape their current value has. */
  const changes = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const row of config?.values ?? []) {
      const draft = drafts[row.key];
      if (draft === undefined || draft === show(row.value)) continue;
      out[row.key] = coerce(draft, row.value);
    }
    return out;
  }, [drafts, config]);

  const changedKeys = Object.keys(changes);
  const needsConfirm = changedKeys.filter((key) =>
    config?.confirm_sections.includes(key.split(".")[0]),
  );

  async function send(dryRun: boolean) {
    setBusy(true);
    const result = await writeConfig({ changes, reason, confirm: confirms, dry_run: dryRun });
    setBusy(false);
    if (dryRun) {
      setPreview(result.ok ? result.diff : null);
      setOutcome(result.ok ? null : result);
      return;
    }
    setOutcome(result);
    setPreview(null);
    if (result.ok) {
      setDrafts({});
      setConfirms({});
      setReason("");
      setWritten(written + 1);
      load();
    }
  }

  if (!config) return <p className="text-sm text-neutral-500">loading</p>;

  return (
    <div className="grid gap-4">
      <Banner kind="warn">
        Changes here are written to {config.overrides_path} and applied by the trading
        loop at its next safe point. Nothing on this page can enable live trading: that
        stays an env var on the box.
      </Banner>

      {Array.from(bySection.entries()).map(([section, rows]) => (
        <Section key={section} title={section}>
          {rows.map((row) => (
            <Field
              key={row.key}
              row={row}
              draft={drafts[row.key]}
              onEdit={(raw) => setDrafts({ ...drafts, [row.key]: raw })}
            />
          ))}
        </Section>
      ))}

      <Section
        title={`pending: ${changedKeys.length} change${changedKeys.length === 1 ? "" : "s"}`}
        right={
          changedKeys.length > 0 ? (
            <button
              onClick={() => {
                setDrafts({});
                setConfirms({});
                setPreview(null);
              }}
              className="text-xs text-neutral-500 hover:text-neutral-300"
            >
              discard
            </button>
          ) : null
        }
      >
        {changedKeys.length === 0 ? (
          <p className="text-sm text-neutral-500">edit a field above to begin</p>
        ) : (
          <>
            <label className="mb-3 flex flex-col gap-1 text-xs text-neutral-500">
              reason (required, goes in the audit log)
              <input
                className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm outline-none focus:border-neutral-500"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="why this change"
              />
            </label>

            {/* Typed confirmation, one per risk field. Enforced by the endpoint too, so
                this is the prompt rather than the check. */}
            {needsConfirm.map((key) => (
              <label key={key} className="mb-2 flex flex-col gap-1 text-xs text-amber-300">
                risk field: type <span className="font-mono">{key}</span> to confirm
                <input
                  className="rounded border border-amber-700 bg-neutral-900 px-2 py-1 font-mono text-sm outline-none focus:border-amber-500"
                  value={confirms[key] ?? ""}
                  onChange={(e) => setConfirms({ ...confirms, [key]: e.target.value })}
                />
              </label>
            ))}

            <div className="mt-3 flex gap-2">
              <button
                disabled={busy}
                onClick={() => send(true)}
                className="rounded bg-neutral-800 px-3 py-1.5 text-sm hover:bg-neutral-700 disabled:opacity-40"
              >
                preview diff
              </button>
              <button
                disabled={busy || !reason || preview === null}
                onClick={() => send(false)}
                className="rounded bg-amber-400 px-3 py-1.5 text-sm font-medium text-neutral-900 disabled:opacity-30"
                title={preview === null ? "preview the diff first" : "write and reload"}
              >
                {busy ? "writing" : "write and reload"}
              </button>
            </div>
          </>
        )}
      </Section>

      {preview && (
        <Section title="diff preview">
          <table className="w-full text-sm [&_td:not(:first-child)]:pl-4 [&_th:not(:first-child)]:pl-4">
            <thead className="text-left text-xs uppercase text-neutral-500">
              <tr>
                <th className="pb-1 font-medium">key</th>
                <th className="pb-1 text-right font-medium">before</th>
                <th className="pb-1 text-right font-medium">after</th>
                <th className="pb-1 font-medium">set by</th>
              </tr>
            </thead>
            <tbody>
              {preview.map((row) => (
                <tr key={row.key} className="border-t border-neutral-800/60">
                  <td className="py-1">{row.key}</td>
                  <td className="py-1 text-right tabular-nums text-neutral-400">
                    {show(row.before)}
                  </td>
                  <td className="py-1 text-right tabular-nums text-amber-200">
                    {show(row.after)}
                  </td>
                  <td className="py-1 text-xs text-neutral-500">{row.source ?? "unset"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      {outcome && (
        <Banner kind={outcome.ok ? "warn" : "error"}>
          {outcome.ok ? (
            <>
              written.{" "}
              {outcome.reload?.error
                ? `The loop did not reload: ${outcome.reload.error}. The file is on disk and the change applies at the next start.`
                : `The loop reloaded ${outcome.reload?.changed.length ?? 0} key(s).`}
            </>
          ) : (
            <>refused: {outcome.detail}</>
          )}
        </Banner>
      )}

      <AuditLog refreshKey={written} />
    </div>
  );
}
