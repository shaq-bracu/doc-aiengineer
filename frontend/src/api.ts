const KEY = (import.meta as any).env?.VITE_API_KEY || "";
const H: Record<string, string> = KEY ? { "X-API-Key": KEY } : {};

async function req(path: string, init: RequestInit = {}) {
  const r = await fetch("/api" + path, { ...init, headers: { ...H, ...(init.headers || {}) } });
  const body = r.status === 204 ? null : await r.json().catch(() => null);
  if (!r.ok) {
    const d = body?.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x: any) => x.msg).join("; ") : `Request failed (${r.status})`);
  }
  return body;
}
const json = (method: string, data?: unknown): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: data === undefined ? undefined : JSON.stringify(data),
});

export const api = {
  upload: (files: File[]) => { const f = new FormData(); files.forEach((x) => f.append("files", x)); return fetch("/api/documents", { method: "POST", body: f, headers: H }).then((r) => r.json()); },
  documents: (qs = "") => req("/documents" + qs),
  document: (id: number) => req(`/documents/${id}`),
  reprocess: (id: number) => req(`/documents/${id}/reprocess`, json("POST")),
  reprocessFailed: () => req("/documents/reprocess-failed", json("POST")),
  remove: (id: number) => req(`/documents/${id}`, json("DELETE")),
  invoices: (qs = "") => req("/invoices" + qs),
  invoice: (id: number) => req(`/invoices/${id}`),
  patch: (id: number, data: unknown) => req(`/invoices/${id}`, json("PATCH", data)),
  approve: (id: number) => req(`/invoices/${id}/approve`, json("POST")),
  resolve: (id: number) => req(`/issues/${id}/resolve`, json("POST")),
  search: (qs: string) => req("/search?" + qs),
  metrics: () => req("/metrics"),
  health: () => fetch("/api/health").then((r) => r.json()),
};
