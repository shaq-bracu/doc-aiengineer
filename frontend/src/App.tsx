import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

type Doc = any;
type Inv = any;
const SCALARS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"] as const;
const LABEL: Record<string, string> = { invoice_number: "Invoice number", invoice_date: "Invoice date", supplier_name: "Supplier", customer_name: "Customer", subtotal: "Subtotal", tax: "Tax", total: "Total", currency: "Currency" };
const NUMERIC = new Set(["subtotal", "tax", "total"]);
const STATUS: Record<string, string> = { queued: "Queued", processing: "Processing", completed: "Completed", needs_review: "Needs review", failed: "Failed" };
const ACTIVE = (d: Doc) => d.status === "queued" || d.status === "processing";

function usePoll(fn: () => Promise<void>, ms: number, deps: unknown[]) {
  useEffect(() => { let on = true; const tick = async () => { if (on) await fn().catch(() => {}); }; tick(); const t = setInterval(tick, ms); return () => { on = false; clearInterval(t); }; }, deps); // eslint-disable-line
}
const Chip = ({ s }: { s: string }) => <span className={`chip chip-${s}`}>{STATUS[s] || s}</span>;
const money = (v: number | null, c?: string | null) => (v == null ? "—" : `${v.toLocaleString(undefined, { minimumFractionDigits: 2 })}${c ? " " + c : ""}`);

/* ---------------------------------------------------------------- Upload + live queue */
function UploadView({ open }: { open: (id: number) => void }) {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [msgs, setMsgs] = useState<string[]>([]);
  const [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const load = useCallback(async () => setDocs((await api.documents("?limit=40")).items), []);
  usePoll(load, 1500, []);
  const send = async (files: File[]) => {
    if (!files.length) return;
    const r = await api.upload(files).catch((e) => ({ results: [{ filename: "upload", accepted: false, error: e.message }] }));
    setMsgs((r.results || []).filter((x: any) => !x.accepted).map((x: any) => `${x.filename}: ${x.error}`));
    load();
  };
  const active = docs.filter(ACTIVE).length;
  return (
    <section>
      <h1>Upload documents</h1>
      <div className={`drop ${drag ? "over" : ""}`} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); send(Array.from(e.dataTransfer.files)); }} onClick={() => input.current?.click()}
        role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && input.current?.click()}>
        <strong>Drop invoices here, or click to choose files</strong>
        <span>PDF, JPG, PNG, TIFF, XLSX, XLS, CSV — many at once. Scans are read with OCR.</span>
        <input ref={input} type="file" multiple hidden accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv" onChange={(e) => { send(Array.from(e.target.files || [])); e.target.value = ""; }} />
      </div>
      {msgs.length > 0 && <div className="banner err">{msgs.map((m) => <div key={m}>Not accepted — {m}</div>)}</div>}
      <div className="row between"><h2>Processing queue{active ? ` · ${active} running` : ""}</h2><button className="ghost" onClick={() => api.reprocessFailed().then(load)}>Retry all failed</button></div>
      {docs.length === 0 ? <p className="empty">Nothing uploaded yet. Add a few invoices to see them move through reading, extraction and validation.</p> :
        <ul className="queue">{docs.map((d) => (
          <li key={d.id} onClick={() => open(d.id)}>
            <div className="qmain"><span className="fname">{d.filename}</span><Chip s={d.status} /></div>
            <div className="bar" aria-label={`progress ${Math.round(d.progress * 100)}%`}><i style={{ width: `${d.progress * 100}%` }} /></div>
            <div className="meta">{ACTIVE(d) ? `${d.stage}…` : d.error ? d.error : `${d.invoice_count} invoice(s) · ${d.page_count} ${d.file_type === "pdf" || d.file_type === "image" ? "page(s)" : "sheet(s)"} · ${((d.duration_ms || 0) / 1000).toFixed(1)}s`}</div>
          </li>))}</ul>}
    </section>
  );
}

/* ---------------------------------------------------------------- Invoice editor */
function InvoiceEditor({ id, onChange }: { id: number; onChange: () => void }) {
  const [inv, setInv] = useState<Inv | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [items, setItems] = useState<any[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [showText, setShowText] = useState(false);
  const hydrate = (i: Inv) => { setInv(i); setForm(Object.fromEntries(SCALARS.map((k) => [k, i[k] == null ? "" : String(i[k])]))); setItems(i.line_items || []); };
  useEffect(() => { setErr(""); api.invoice(id).then(hydrate).catch((e) => setErr(e.message)); }, [id]);
  if (!inv) return <div className="panel">{err || "Loading…"}</div>;
  const dirty = SCALARS.some((k) => (inv[k] == null ? "" : String(inv[k])) !== form[k]) || JSON.stringify(items) !== JSON.stringify(inv.line_items || []);
  const save = async () => {
    setBusy(true); setErr("");
    try {
      const body: any = {};
      SCALARS.forEach((k) => {
        if ((inv[k] == null ? "" : String(inv[k])) !== form[k]) body[k] = form[k].trim() === "" ? null : NUMERIC.has(k) ? Number(form[k]) : form[k];
      });
      if (JSON.stringify(items) !== JSON.stringify(inv.line_items || [])) body.line_items = items;
      for (const k of Object.keys(body)) if (NUMERIC.has(k) && Number.isNaN(body[k])) throw new Error(`${LABEL[k]} must be a number`);
      await api.patch(id, body); hydrate(await api.invoice(id)); onChange();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const approve = async () => { setBusy(true); try { await api.approve(id); hydrate(await api.invoice(id)); onChange(); } finally { setBusy(false); } };
  const setItem = (n: number, k: string, v: string) => setItems(items.map((it, i) => (i === n ? { ...it, [k]: k === "description" ? v : v === "" ? null : Number(v) } : it)));
  const open = (inv.issues || []).filter((i: any) => !i.resolved && i.severity !== "info");
  return (
    <div className="panel">
      <div className="row between">
        <h3>{inv.invoice_number || "Invoice without number"} <small>#{inv.id} · {inv.page_start ? `page ${inv.page_start}${inv.page_end !== inv.page_start ? `–${inv.page_end}` : ""}` : inv.source_ref} · {inv.method}</small></h3>
        <span className={`chip chip-rv-${inv.review_status}`}>{inv.review_status === "ok" ? "Looks good" : inv.review_status === "approved" ? "Approved" : "Needs review"}</span>
      </div>
      {(inv.issues || []).length > 0 && (
        <ul className="issues">{inv.issues.map((i: any) => (
          <li key={i.id} className={`sev-${i.severity} ${i.resolved ? "done" : ""}`}><b>{i.code.replace(/_/g, " ")}</b> {i.message}</li>))}</ul>)}
      <div className="grid">{SCALARS.map((k) => {
        const unsure = inv.uncertain_fields.includes(k), missing = inv[k] == null && form[k] === "";
        return (
          <label key={k} className={`${unsure ? "unsure" : ""} ${missing ? "missing" : ""}`}>
            <span>{LABEL[k]}{unsure && <em> · low confidence</em>}{missing && <em> · not found</em>}{inv.confidence?.[k] != null && inv[k] != null && <em className="conf"> {Math.round(inv.confidence[k] * 100)}%</em>}</span>
            <input value={form[k] ?? ""} onChange={(e) => setForm({ ...form, [k]: e.target.value })} placeholder={k === "invoice_date" ? "YYYY-MM-DD" : ""} inputMode={NUMERIC.has(k) ? "decimal" : undefined} />
          </label>);
      })}</div>
      <h4>Line items</h4>
      {items.length === 0 ? <p className="empty">No line items were found.</p> : (
        <div className="scroll"><table className="items"><thead><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th><th /></tr></thead><tbody>
          {items.map((it, n) => (<tr key={n}>
            <td><input value={it.description ?? ""} onChange={(e) => setItem(n, "description", e.target.value)} /></td>
            {["quantity", "unit_price", "amount"].map((k) => <td key={k}><input className="num" value={it[k] ?? ""} onChange={(e) => setItem(n, k, e.target.value)} /></td>)}
            <td><button className="ghost" aria-label="Remove line" onClick={() => setItems(items.filter((_, i) => i !== n))}>×</button></td></tr>))}
        </tbody></table></div>)}
      <button className="ghost" onClick={() => setItems([...items, { description: "", quantity: null, unit_price: null, amount: null }])}>Add line</button>
      {err && <div className="banner err">{err}</div>}
      <div className="row actions">
        <button className="primary" disabled={!dirty || busy} onClick={save}>Save changes</button>
        <button disabled={busy || inv.review_status === "approved"} onClick={approve}>{open.length ? "Approve despite issues" : "Approve"}</button>
        <button className="ghost" onClick={() => setShowText(!showText)}>{showText ? "Hide" : "Show"} source text</button>
      </div>
      {showText && <pre className="source">{inv.raw_text || "(no source text stored)"}</pre>}
    </div>
  );
}

/* ---------------------------------------------------------------- Document detail */
function DocumentDetail({ id, onBack, onChange }: { id: number; onBack: () => void; onChange: () => void }) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const load = useCallback(async () => { const d = await api.document(id); setDoc(d); setSel((s) => s ?? d.invoices[0]?.id ?? null); }, [id]);
  usePoll(load, 1500, [id]);
  if (!doc) return <p>Loading…</p>;
  return (
    <section>
      <button className="ghost" onClick={onBack}>← Back</button>
      <div className="row between"><h1>{doc.filename}</h1><Chip s={doc.status} /></div>
      <p className="meta">{doc.file_type.toUpperCase()} · {doc.page_count} {doc.file_type === "pdf" || doc.file_type === "image" ? "page(s)" : "sheet(s)"}{doc.ocr_pages ? ` · ${doc.ocr_pages} read with OCR (${Math.round(doc.mean_ocr_conf || 0)}/100 avg confidence)` : ""} · attempt {doc.attempts}</p>
      {ACTIVE(doc) && <div className="bar"><i style={{ width: `${doc.progress * 100}%` }} /></div>}
      {doc.error && <div className="banner err">{doc.error}</div>}
      {doc.document_issues.filter((i: any) => !i.resolved).map((i: any) => (
        <div key={i.id} className={`banner ${i.severity === "error" ? "err" : "warn"}`}>{i.message}{" "}<button className="ghost" onClick={() => api.resolve(i.id).then(() => { load(); onChange(); })}>Mark resolved</button></div>))}
      <div className="row actions">
        <button onClick={() => api.reprocess(id).then(load)} disabled={ACTIVE(doc)}>Reprocess</button>
        <a className="btn" href={`/api/documents/${id}/file`} target="_blank" rel="noreferrer">Open original</a>
        <button className="ghost danger" onClick={() => confirm("Delete this document and its results?") && api.remove(id).then(() => { onChange(); onBack(); })}>Delete</button>
      </div>
      <div className="split">
        <aside>
          <h2>Invoices ({doc.invoices.length})</h2>
          {doc.invoices.length === 0 && <p className="empty">{ACTIVE(doc) ? "Extraction in progress…" : "No invoices were extracted from this file."}</p>}
          <ul className="list">{doc.invoices.map((i: Inv) => (
            <li key={i.id} className={sel === i.id ? "sel" : ""} onClick={() => setSel(i.id)}>
              <b>{i.invoice_number || "(no number)"}</b><span>{i.supplier_name || "unknown supplier"}</span><span>{money(i.total, i.currency)}</span>
              <span className={`dot dot-${i.review_status}`} title={i.review_status} /></li>))}</ul>
          <h2>History</h2>
          <ol className="events">{doc.events.map((e: any, n: number) => <li key={n} className={`lv-${e.level}`}><b>{e.stage}</b> {e.message}{e.duration_ms != null && <em> {e.duration_ms} ms</em>}</li>)}</ol>
        </aside>
        <div>{sel ? <InvoiceEditor id={sel} onChange={() => { load(); onChange(); }} /> : <p className="empty">Select an invoice to inspect and correct it.</p>}</div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- Documents + review queue */
function DocumentsView({ open, initial }: { open: (id: number) => void; initial: string }) {
  const [filter, setFilter] = useState(initial);
  const [data, setData] = useState<{ total: number; items: Doc[] }>({ total: 0, items: [] });
  usePoll(async () => setData(await api.documents(`?limit=100${filter ? `&status=${filter}` : ""}`)), 2500, [filter]);
  const tabs: [string, string][] = [["", "All"], ["needs_review", "Needs review"], ["failed", "Failed"], ["completed", "Completed"], ["processing,queued", "In progress"]];
  return (
    <section>
      <h1>Documents</h1>
      <div className="tabs" role="tablist">{tabs.map(([v, l]) => <button key={v} role="tab" aria-selected={filter === v} className={filter === v ? "on" : ""} onClick={() => setFilter(v)}>{l}</button>)}</div>
      {data.items.length === 0 ? <p className="empty">No documents match this filter.</p> :
        <div className="scroll"><table className="docs"><thead><tr><th>File</th><th>Status</th><th>Invoices</th><th>Pages</th><th>Time</th><th>Uploaded</th></tr></thead><tbody>
          {data.items.map((d) => (<tr key={d.id} onClick={() => open(d.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && open(d.id)}>
            <td className="fname">{d.filename}</td><td><Chip s={d.status} /></td><td>{d.invoice_count}</td><td>{d.page_count}</td>
            <td>{d.duration_ms != null ? `${(d.duration_ms / 1000).toFixed(1)}s` : "—"}</td><td>{new Date(d.created_at + "Z").toLocaleString()}</td></tr>))}
        </tbody></table></div>}
      <p className="meta">{data.total} document(s)</p>
    </section>
  );
}

/* ---------------------------------------------------------------- Search */
function SearchView({ open }: { open: (id: number) => void }) {
  const [f, setF] = useState({ q: "", mode: "hybrid", supplier: "", date_from: "", date_to: "", min_total: "", max_total: "", currency: "" });
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    const p = new URLSearchParams(); Object.entries(f).forEach(([k, v]) => v && p.set(k, v));
    try { setRes(await api.search(p.toString())); } catch (e: any) { setRes({ error: e.message }); } finally { setBusy(false); }
  };
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <section>
      <h1>Search processed documents</h1>
      <div className="searchbar">
        <input aria-label="Search" placeholder="Supplier, invoice number, or a description like “office supplies from Apex”" value={f.q} onChange={set("q")} onKeyDown={(e) => e.key === "Enter" && run()} />
        <select aria-label="Mode" value={f.mode} onChange={set("mode")}><option value="hybrid">Keyword + semantic</option><option value="keyword">Keyword only</option><option value="semantic">Semantic only</option></select>
        <button className="primary" onClick={run} disabled={busy}>Search</button>
      </div>
      <div className="filters">
        <input placeholder="Supplier" value={f.supplier} onChange={set("supplier")} /><input type="date" aria-label="From date" value={f.date_from} onChange={set("date_from")} /><input type="date" aria-label="To date" value={f.date_to} onChange={set("date_to")} />
        <input placeholder="Min total" inputMode="decimal" value={f.min_total} onChange={set("min_total")} /><input placeholder="Max total" inputMode="decimal" value={f.max_total} onChange={set("max_total")} /><input placeholder="Currency (USD)" maxLength={3} value={f.currency} onChange={set("currency")} />
      </div>
      {res?.error && <div className="banner err">{res.error}</div>}
      {res && !res.error && (res.items.length === 0 ? <p className="empty">No documents matched. Try fewer filters or a broader phrase.</p> :
        <ul className="results">{res.items.map((h: any) => (
          <li key={h.document_id} onClick={() => open(h.document_id)}>
            <div className="qmain"><span className="fname">{h.document.filename}</span><Chip s={h.document.status} /><span className="tag">{h.match}</span></div>
            {h.invoices.slice(0, 3).map((i: Inv) => <div key={i.id} className="meta">{i.invoice_number || "?"} · {i.supplier_name || "?"} · {i.invoice_date || "no date"} · {money(i.total, i.currency)}</div>)}
            {h.snippet && <pre className="snippet">{h.snippet}</pre>}
          </li>))}</ul>)}
    </section>
  );
}

/* ---------------------------------------------------------------- Metrics */
function MetricsView() {
  const [m, setM] = useState<any>(null); const [h, setH] = useState<any>(null);
  usePoll(async () => { setM(await api.metrics()); setH(await api.health()); }, 4000, []);
  if (!m) return <p>Loading…</p>;
  const pct = (x: number | null) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
  return (
    <section>
      <h1>System health</h1>
      <dl className="stats">
        <div><dt>Documents</dt><dd>{m.documents_total}</dd></div><div><dt>Success rate</dt><dd>{pct(m.success_rate)}</dd></div><div><dt>Failure rate</dt><dd>{pct(m.failure_rate)}</dd></div>
        <div><dt>Avg processing</dt><dd>{m.avg_processing_ms ? `${(m.avg_processing_ms / 1000).toFixed(1)}s` : "—"}</dd></div><div><dt>Invoices</dt><dd>{m.invoices_total}</dd></div><div><dt>Awaiting review</dt><dd>{m.invoices_needing_review}</dd></div>
        <div><dt>Queue depth</dt><dd>{m.queue_depth}</dd></div>
      </dl>
      <h2>Field fill rate</h2>
      <ul className="fill">{Object.entries(m.field_fill_rate).map(([k, v]: any) => <li key={k}><span>{LABEL[k] || k}</span><div className="bar"><i style={{ width: `${(v || 0) * 100}%` }} /></div><b>{pct(v)}</b></li>)}</ul>
      <p className="meta">OCR engine: {h?.ocr_available ? "ready" : "missing — install Tesseract"} · Extraction: {h?.llm_enabled ? "LLM + rules" : "rules only (set ANTHROPIC_API_KEY for LLM)"} · Workers: {h?.workers}</p>
    </section>
  );
}

/* ---------------------------------------------------------------- Shell */
export default function App() {
  const [view, setView] = useState<"upload" | "docs" | "search" | "metrics">("upload");
  const [docId, setDocId] = useState<number | null>(null);
  const [review, setReview] = useState(0);
  const [tick, setTick] = useState(0);
  usePoll(async () => setReview((await api.metrics()).invoices_needing_review), 5000, [tick]);
  const nav = (v: typeof view) => { setView(v); setDocId(null); };
  const open = (id: number) => { setDocId(id); setView("docs"); };
  return (
    <div className="shell">
      <nav aria-label="Main">
        <div className="brand">Ledgerline<small>document intelligence</small></div>
        <button className={view === "upload" ? "on" : ""} onClick={() => nav("upload")}>Upload</button>
        <button className={view === "docs" ? "on" : ""} onClick={() => nav("docs")}>Documents{review > 0 && <span className="badge">{review}</span>}</button>
        <button className={view === "search" ? "on" : ""} onClick={() => nav("search")}>Search</button>
        <button className={view === "metrics" ? "on" : ""} onClick={() => nav("metrics")}>System</button>
      </nav>
      <main>
        {view === "upload" && <UploadView open={open} />}
        {view === "docs" && (docId ? <DocumentDetail id={docId} onBack={() => setDocId(null)} onChange={() => setTick((t) => t + 1)} /> : <DocumentsView open={open} initial={review ? "needs_review" : ""} />)}
        {view === "search" && <SearchView open={open} />}
        {view === "metrics" && <MetricsView />}
      </main>
    </div>
  );
}
