import { useState } from "react";
import { login } from "./api";

export default function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const failure = await login(password);
    setBusy(false);
    setPassword("");
    if (failure) setError(failure);
    else onDone();
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-950 text-neutral-100">
      <form onSubmit={submit} className="w-80 space-y-4 rounded-lg border border-neutral-800 p-6">
        <h1 className="text-lg font-semibold">liqfade</h1>
        <input
          type="password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="panel password"
          className="w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 outline-none focus:border-neutral-500"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy || !password}
          className="w-full rounded bg-neutral-100 px-3 py-2 font-medium text-neutral-900 disabled:opacity-40"
        >
          {busy ? "checking" : "log in"}
        </button>
      </form>
    </div>
  );
}
