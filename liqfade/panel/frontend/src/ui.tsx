// The handful of primitives every page shares. Lifted out of App.tsx when the Live and
// Settings pages needed the same card and the same label/value row.

export function Section({
  title,
  right,
  children,
}: {
  title: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-500">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Row({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex justify-between gap-4 py-1 text-sm">
      <span className="text-neutral-400">{label}</span>
      <span className={`tabular-nums ${tone ?? "text-neutral-100"}`}>{value}</span>
    </div>
  );
}

export function Banner({ kind, children }: { kind: "warn" | "error"; children: React.ReactNode }) {
  const tone =
    kind === "error"
      ? "border-red-800/60 bg-red-950/40 text-red-300"
      : "border-amber-700/60 bg-amber-950/40 text-amber-300";
  return <div className={`mb-4 rounded border px-4 py-2 text-sm ${tone}`}>{children}</div>;
}

export function ms(value: number | null | undefined) {
  return value === null || value === undefined ? "-" : `${value.toFixed(0)} ms`;
}

export function usd(value: number | null | undefined, digits = 2) {
  return value === null || value === undefined ? "-" : value.toFixed(digits);
}

/** UTC, to the second. Every timestamp in this system is ms UTC; none of them are local. */
export function stamp(msUtc: number) {
  return new Date(msUtc).toISOString().replace("T", " ").slice(0, 19);
}

/** A signed number's tone: green up, red down, neutral at zero. */
export function pnlTone(value: number) {
  if (value > 0) return "text-emerald-400";
  if (value < 0) return "text-red-400";
  return "text-neutral-100";
}
