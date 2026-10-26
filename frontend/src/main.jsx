import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ShieldCheck, Upload, FileCheck2, Download, AlertCircle, Loader2, LockKeyhole,
  ExternalLink, Copy, Check, RotateCcw, UserRound, Phone, Image as ImageIcon, Mail,
  MapPin, Link2, CheckCircle2, Wifi, WifiOff, FileText, FileImage,
} from 'lucide-react';
import './styles.css';

const API = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '');
const MAX_MB = 15;
const MAX_FILES = 10;
const ACCEPT = '.pdf,.doc,.docx,.odt,.rtf,.jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff,.gif,application/pdf,image/*';
const IMAGE_RE = /\.(jpe?g|png|webp|bmp|tiff?|gif)$/i;
const OK_RE = /\.(pdf|docx?|odt|rtf|jpe?g|png|webp|bmp|tiff?|gif)$/i;
const STEPS = [
  'Uploading CV',
  'Reading the document (OCR for scanned CVs)',
  'Removing personal details and photo',
  'Verifying the result',
];

/* ---------- helpers ---------- */
function fallbackName(cvCode, original) {
  if (cvCode) return `${cvCode.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_|_$/g, '')}_Space_Consultant.pdf`;
  const base = (original || 'CV').replace(/\.pdf$/i, '').replace(/[^A-Za-z0-9]+/g, '_');
  return `${base}_Protected.pdf`;
}

function filenameFromDisposition(h) {
  if (!h) return '';
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(h);
  return m ? decodeURIComponent(m[1]) : '';
}

/** Reliable download on desktop + Android + iOS (no print-to-PDF needed). */
async function saveBlob(blob, filename, mime = 'application/pdf') {
  const file = new File([blob], filename, { type: mime });
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  // iOS Safari ignores the `download` attribute for blobs -> use the share sheet ("Save to Files").
  if (isIOS && navigator.canShare && navigator.canShare({ files: [file] })) {
    try { await navigator.share({ files: [file], title: filename }); return 'shared'; }
    catch (e) { if (e && e.name === 'AbortError') return 'cancelled'; }
  }
  const url = URL.createObjectURL(file);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  return 'downloaded';
}

async function readError(res) {
  let detail = null;
  try { detail = (await res.json()).detail; } catch { /* not json */ }
  if (detail && typeof detail === 'object') return detail;
  return { code: 'ERROR', message: typeof detail === 'string' ? detail : `Processing failed (${res.status}).` };
}

/* ---------- app ---------- */
function App() {
  const [files, setFiles] = useState([]);
  const [exporting, setExporting] = useState('');
  const file = files[0] || null;
  const totalBytes = files.reduce((n, f) => n + f.size, 0);
  const [status, setStatus] = useState('idle'); // idle | processing | done | error | details
  const [step, setStep] = useState(0);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [drag, setDrag] = useState(false);
  const [server, setServer] = useState('checking'); // checking | online | waking
  const [manual, setManual] = useState({ name: '', mobile: '', missing: [] });
  const [copied, setCopied] = useState(false);
  const [saveNote, setSaveNote] = useState('');
  const timer = useRef(null);
  const inputRef = useRef(null);

  // Warm up the API (free hosting sleeps) and show its state.
  useEffect(() => {
    let alive = true;
    const slow = setTimeout(() => alive && setServer((s) => (s === 'checking' ? 'waking' : s)), 2500);
    fetch(`${API}/api/health`).then((r) => r.ok && alive && setServer('online')).catch(() => alive && setServer('waking'));
    return () => { alive = false; clearTimeout(slow); };
  }, []);

  useEffect(() => () => { clearInterval(timer.current); }, []);

  const reset = () => {
    setFiles([]); setResult(null); setStatus('idle'); setError(''); setStep(0); setExporting('');
    setManual({ name: '', mobile: '', missing: [] }); setSaveNote('');
    if (inputRef.current) inputRef.current.value = '';
  };

  const choose = (list) => {
    setError(''); setResult(null); setSaveNote('');
    const picked = Array.from(list || []);
    if (!picked.length) return;
    const bad = picked.find((f) => !OK_RE.test(f.name));
    if (bad) { setStatus('error'); return setError(`${bad.name}: please upload a PDF, Word (DOCX) or image (JPG/PNG) file.`); }
    if (picked.length > 1 && !picked.every((f) => IMAGE_RE.test(f.name))) {
      setStatus('error'); return setError('To upload several files, all of them must be images (pages of the same CV). Otherwise upload one PDF or Word file.');
    }
    if (picked.length > MAX_FILES) { setStatus('error'); return setError(`Please upload at most ${MAX_FILES} images.`); }
    const big = picked.find((f) => f.size > MAX_MB * 1024 * 1024);
    if (big) { setStatus('error'); return setError(`${big.name} is larger than ${MAX_MB} MB.`); }
    setFiles(picked); setStatus('idle');
  };

  const run = async (extra = {}) => {
    if (!files.length) return;
    setStatus('processing'); setError(''); setStep(0);
    clearInterval(timer.current);
    timer.current = setInterval(() => setStep((s) => Math.min(s + 1, STEPS.length - 1)), 4500);
    const ctrl = new AbortController();
    const abort = setTimeout(() => ctrl.abort(), 180000);
    try {
      const fd = new FormData();
      files.forEach((f) => fd.append('files', f));
      if (extra.name) fd.append('name_override', extra.name);
      if (extra.mobile) fd.append('phone_override', extra.mobile);
      const res = await fetch(`${API}/api/process-cv`, { method: 'POST', body: fd, signal: ctrl.signal });
      if (!res.ok) {
        const d = await readError(res);
        if (d.code === 'DETAILS_NOT_DETECTED') {
          setManual({
            name: extra.name || d.hints?.name_guess || '',
            mobile: extra.mobile || '',
            missing: d.missing || ['name', 'mobile'],
          });
          setError(d.message); setStatus('details'); return;
        }
        throw new Error(d.message);
      }
      const blob = await res.blob();
      const cvCode = res.headers.get('X-CV-Code') || '';
      const filename = res.headers.get('X-Output-Filename')
        || filenameFromDisposition(res.headers.get('Content-Disposition'))
        || fallbackName(cvCode, files[0].name);
      let report = null;
      try { report = JSON.parse(res.headers.get('X-Report') || 'null'); } catch { /* optional */ }
      setResult({ blob, url: URL.createObjectURL(blob), cvCode, filename, report });
      setServer('online'); setStatus('done');
    } catch (e) {
      setStatus('error');
      setError(e.name === 'AbortError'
        ? 'Processing took too long. The server may be starting up – please try again.'
        : (e.message === 'Failed to fetch' ? 'Cannot reach the server. Please check your connection and try again.' : e.message));
    } finally {
      clearInterval(timer.current); clearTimeout(abort);
    }
  };

  const download = async () => {
    if (!result) return;
    const how = await saveBlob(result.blob, result.filename);
    setSaveNote(how === 'cancelled' ? '' : how === 'shared'
      ? 'Choose “Save to Files” in the share sheet.' : `Saved as ${result.filename}`);
  };

  const exportAs = async (format) => {
    if (!result || exporting) return;
    setExporting(format); setSaveNote('');
    try {
      const base = result.filename.replace(/\.pdf$/i, '');
      const fd = new FormData();
      fd.append('file', new File([result.blob], result.filename, { type: 'application/pdf' }));
      fd.append('format', format);
      fd.append('base_name', base);
      fd.append('cv_code', result.cvCode || '');
      const res = await fetch(`${API}/api/export`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await readError(res)).message);
      const blob = await res.blob();
      const name = res.headers.get('X-Output-Filename') || `${base}.${format}`;
      const mime = res.headers.get('Content-Type') || 'application/octet-stream';
      const how = await saveBlob(blob, name, mime);
      const notes = [];
      if (format === 'docx' && res.headers.get('X-Editable') === '0') notes.push('This CV is a scan/picture, so the Word file contains the pages as pictures (text is not editable).');
      else if (format === 'docx') notes.push('Editable Word copy – layout may differ slightly from the PDF.');
      if (name.endsWith('.zip')) notes.push('Multiple pages are saved as a ZIP of images.');
      if (how === 'shared') notes.push('Choose “Save to Files” in the share sheet.');
      setSaveNote(notes.join(' ') || `Saved as ${name}`);
    } catch (e) {
      setSaveNote(`Could not export: ${e.message}`);
    } finally {
      setExporting('');
    }
  };

  const copyCode = async () => {
    try { await navigator.clipboard.writeText(result.cvCode); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* ignore */ }
  };

  const submitManual = (e) => {
    e.preventDefault();
    run({ name: manual.name.trim(), mobile: manual.mobile.trim() });
  };

  const r = result?.report;
  const chips = r ? [
    { icon: UserRound, label: 'Name', n: r.name_removed },
    { icon: Phone, label: 'Phone', n: r.phone_removed },
    { icon: Mail, label: 'Email', n: r.email_removed },
    { icon: MapPin, label: 'Location', n: r.address_removed },
    { icon: ImageIcon, label: 'Photo', n: r.photos_removed },
    { icon: Link2, label: 'Links', n: r.links_removed },
  ].filter((c) => c.n > 0) : [];

  return (
    <div className="page">
      <header>
        <div className="brand">
          <span className="mark"><ShieldCheck /></span>
          <div><b>Space Consultant</b><small>CV Privacy Tool</small></div>
        </div>
        <div className="header-right">
          <span className={`server ${server}`} title="Processing server status">
            {server === 'online' ? <Wifi /> : <WifiOff />}
            {server === 'online' ? 'Server online' : server === 'waking' ? 'Server waking up…' : 'Connecting…'}
          </span>
          <div className="secure"><LockKeyhole /> Files are not stored</div>
        </div>
      </header>

      <main>
        <section className="hero">
          <label>CV PRIVACY &amp; WATERMARK</label>
          <h1>Protect candidate data.<br /><span>Keep the CV professional.</span></h1>
          <p>Upload a CV as PDF, Word or photo. The candidate’s name, photo, contact details and IDs are removed, your CV Code and watermark are added, and you can download it as PDF, editable Word or image.</p>
        </section>

        <section className="panel">
          {status !== 'done' && (
            <div
              className={'drop ' + (drag ? 'drag ' : '') + (file ? 'has-file' : '')}
              role="button" tabIndex={0}
              onClick={() => inputRef.current?.click()}
              onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && inputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => { e.preventDefault(); setDrag(false); choose(e.dataTransfer.files); }}
            >
              <input ref={inputRef} type="file" accept={ACCEPT} multiple hidden onChange={(e) => choose(e.target.files)} />
              <div className="upload">{file ? <FileCheck2 /> : <Upload />}</div>
              <h2>{file ? (files.length > 1 ? `${files.length} images selected` : file.name) : 'Drop your CV here'}</h2>
              <p>{file ? `${(totalBytes / 1024 / 1024).toFixed(2)} MB · Ready to process` : `or click to browse · up to ${MAX_MB} MB`}</p>
              {!file && (
                <div className="formats">
                  <span><FileText />PDF</span><span><FileText />Word</span><span><FileImage />JPG / PNG</span>
                </div>
              )}
              {files.length > 1 && <div className="filelist">{files.map((f) => <span key={f.name + f.size}>{f.name}</span>)}</div>}
            </div>
          )}

          {file && (status === 'idle' || status === 'error') && (
            <button className="primary" onClick={() => run()}><ShieldCheck /> Process &amp; Protect CV</button>
          )}

          {status === 'processing' && (
            <div className="progress" aria-live="polite">
              <div className="progress-head"><Loader2 className="spin" /> Protecting CV…</div>
              <ol>
                {STEPS.map((s, i) => (
                  <li key={s} className={i < step ? 'done' : i === step ? 'active' : ''}>
                    {i < step ? <CheckCircle2 /> : i === step ? <Loader2 className="spin" /> : <span className="dot" />} {s}
                  </li>
                ))}
              </ol>
              {server !== 'online' && <small>The server may take up to a minute to start the first time.</small>}
            </div>
          )}

          {status === 'error' && <div className="status err" role="alert"><AlertCircle />{error}</div>}

          {status === 'details' && (
            <form className="manual" onSubmit={submitManual}>
              <div className="status warn"><AlertCircle />{error}</div>
              {manual.missing.includes('name') && (
                <label>Candidate name
                  <input required value={manual.name} placeholder="e.g. Dr. Amit Kumar Sharma"
                    onChange={(e) => setManual({ ...manual, name: e.target.value })} />
                </label>
              )}
              {manual.missing.includes('mobile') && (
                <label>Mobile number
                  <input required inputMode="tel" value={manual.mobile} placeholder="10-digit mobile number"
                    onChange={(e) => setManual({ ...manual, mobile: e.target.value })} />
                </label>
              )}
              <small>These are used only to build the CV Code and to remove the details from the PDF.</small>
              <button className="primary" type="submit"><ShieldCheck /> Continue</button>
            </form>
          )}

          {status === 'done' && result && (
            <div className="result">
              <div className="resulttop">
                <div><small>GENERATED CV CODE</small><strong>{result.cvCode || '—'}</strong></div>
                <span className={'ok ' + (r && !r.verified ? 'warn' : '')}>
                  <ShieldCheck /> {r && !r.verified ? 'Review needed' : 'Verified & protected'}
                </span>
              </div>

              {chips.length > 0 && (
                <div className="chips">
                  {chips.map((c) => (<span key={c.label} className="chip"><c.icon />{c.label} removed</span>))}
                </div>
              )}
              {r && !r.verified && (
                <div className="status warn"><AlertCircle />Some details may still be visible ({r.issues.join('; ')}). Please check the preview before sharing.</div>
              )}

              <div className="filename"><FileCheck2 />{result.filename}</div>

              <div className="actions">
                <button className="primary" onClick={download}><Download /> Download PDF</button>
                <a className="secondary" href={result.url} target="_blank" rel="noopener noreferrer"><ExternalLink /> Open</a>
                <button className="secondary" onClick={copyCode}>{copied ? <Check /> : <Copy />} {copied ? 'Copied' : 'Copy code'}</button>
              </div>
              <div className="alsoas">
                <span>Also download as</span>
                <button className="chipbtn" disabled={!!exporting} onClick={() => exportAs('docx')}>
                  {exporting === 'docx' ? <Loader2 className="spin" /> : <FileText />} Word (editable)
                </button>
                <button className="chipbtn" disabled={!!exporting} onClick={() => exportAs('png')}>
                  {exporting === 'png' ? <Loader2 className="spin" /> : <FileImage />} PNG
                </button>
                <button className="chipbtn" disabled={!!exporting} onClick={() => exportAs('jpg')}>
                  {exporting === 'jpg' ? <Loader2 className="spin" /> : <FileImage />} JPG
                </button>
              </div>
              {saveNote && <p className="note">{saveNote}</p>}

              <object className="preview" data={`${result.url}#toolbar=0&navpanes=0`} type="application/pdf" aria-label="Protected CV preview" />

              <button className="link" onClick={reset}><RotateCcw /> Process another CV</button>
            </div>
          )}
        </section>

        <section className="features">
          <Feature t="PDF, Word or photo" d="Upload DOCX, PDF or phone photos of the CV; scans are read with OCR and face detection." />
          <Feature t="Checked after processing" d="The finished PDF is re-read to confirm name, phone and email are gone." />
          <Feature t="Clean file" d="Metadata, hidden links and bookmarks are stripped. Watermark on every page." />
        </section>
      </main>
      <footer>CV Privacy &amp; Watermark · Space Consultant</footer>
    </div>
  );
}

function Feature({ t, d }) {
  return (<div className="feature"><ShieldCheck /><div><b>{t}</b><p>{d}</p></div></div>);
}

createRoot(document.getElementById('root')).render(<App />);
