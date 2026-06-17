'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Sidebar } from '../../../components/Sidebar';
import { MoneyDisplay } from '../../../components/MoneyDisplay';
import { invoicesApi } from '../../../lib/api';
import { getToken } from '../../../lib/auth';
import { calcAdvance, calcFee, calcReserve, parseDollarsToMinorUnits } from '../../../lib/money';

const BASE = '';

export default function NewInvoicePage() {
  const router = useRouter();

  useEffect(() => {
    if (!getToken()) router.replace(`${BASE}/login/`);
  }, [router]);

  const [form, setForm] = useState({
    invoiceNumber: `INV-${Date.now().toString().slice(-6)}`,
    payerName: '',
    payerDaysToPay: '30',
    faceValueDollars: '',
    advanceRatePct: '90',
    feeRatePct: '2',
    currency: 'USD',
    notes: '',
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  // Live preview calcs
  const faceMinor = parseDollarsToMinorUnits(form.faceValueDollars);
  const advRateBps = Math.round(parseFloat(form.advanceRatePct || '0') * 100);
  const feeRateBps = Math.round(parseFloat(form.feeRatePct || '0') * 100);
  const advMinor = faceMinor > 0 ? calcAdvance(faceMinor, advRateBps) : 0;
  const feeMinor = faceMinor > 0 ? calcFee(faceMinor, feeRateBps) : 0;
  const reserveMinor = faceMinor > 0 ? calcReserve(faceMinor, advMinor) : 0;

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(''); setSuccess('');
    if (faceMinor <= 0) { setError('Enter a valid face value'); return; }

    setLoading(true);
    try {
      const inv = await invoicesApi.create({
        invoiceNumber: form.invoiceNumber,
        payerName: form.payerName,
        payerDaysToPay: parseInt(form.payerDaysToPay, 10),
        faceValueMinorUnits: faceMinor,
        advanceRateBps: advRateBps,
        feeRateBps: feeRateBps,
        currency: form.currency,
        notes: form.notes || undefined,
      });
      setSuccess(`Invoice ${inv.invoiceNumber} submitted successfully!`);
      setTimeout(() => router.push(`${BASE}/invoices/`), 1500);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Submission failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner" style={{ maxWidth: '780px' }}>
          <h1 className="page-title">Submit Invoice for Factoring</h1>
          <p className="page-subtitle">Enter freight invoice details. Advance and fee are calculated server-side with integer arithmetic.</p>

          {error && <div className="error-box">{error}</div>}
          {success && <div className="success-box">{success}</div>}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: '24px', alignItems: 'start' }}>
            {/* Form */}
            <form onSubmit={handleSubmit}>
              <div className="card">
                <div style={{ fontWeight: 700, marginBottom: '16px', color: 'var(--text-primary)' }}>Invoice Details</div>

                <div className="form-group">
                  <label className="form-label">Invoice Number</label>
                  <input className="input" value={form.invoiceNumber} onChange={e => set('invoiceNumber', e.target.value)} required />
                </div>

                <div className="form-group">
                  <label className="form-label">Payer / Debtor Name</label>
                  <input className="input" value={form.payerName} onChange={e => set('payerName', e.target.value)} placeholder="Swift Logistics Inc." required />
                </div>

                <div className="grid-2" style={{ gap: '12px' }}>
                  <div className="form-group">
                    <label className="form-label">Face Value ($)</label>
                    <input
                      className="input"
                      type="number"
                      step="0.01"
                      min="0.01"
                      value={form.faceValueDollars}
                      onChange={e => set('faceValueDollars', e.target.value)}
                      placeholder="12500.00"
                      required
                      style={{ fontFamily: 'var(--font-mono)' }}
                    />
                    <div className="form-hint">Stored as integer cents — no float math on money paths</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Days to Pay</label>
                    <input className="input" type="number" min="1" max="365" value={form.payerDaysToPay} onChange={e => set('payerDaysToPay', e.target.value)} />
                  </div>
                </div>

                <div className="grid-2" style={{ gap: '12px' }}>
                  <div className="form-group">
                    <label className="form-label">Advance Rate (%)</label>
                    <input className="input" type="number" step="0.01" min="50" max="95" value={form.advanceRatePct} onChange={e => set('advanceRatePct', e.target.value)} style={{ fontFamily: 'var(--font-mono)' }} />
                    <div className="form-hint">Stored in basis points ({advRateBps} bps)</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Fee Rate (%)</label>
                    <input className="input" type="number" step="0.01" min="0.5" max="10" value={form.feeRatePct} onChange={e => set('feeRatePct', e.target.value)} style={{ fontFamily: 'var(--font-mono)' }} />
                    <div className="form-hint">Stored in basis points ({feeRateBps} bps)</div>
                  </div>
                </div>

                <div className="form-group">
                  <label className="form-label">Currency</label>
                  <select className="input" value={form.currency} onChange={e => set('currency', e.target.value)} style={{ cursor: 'pointer' }}>
                    <option value="USD">USD — US Dollar</option>
                    <option value="BRL">BRL — Brazilian Real</option>
                  </select>
                </div>

                <div className="form-group">
                  <label className="form-label">Notes (optional)</label>
                  <textarea className="input" rows={2} value={form.notes} onChange={e => set('notes', e.target.value)} placeholder="Any underwriting notes..." style={{ resize: 'vertical' }} />
                </div>

                <button type="submit" className="btn-primary" disabled={loading} style={{ width: '100%', marginTop: '8px' }}>
                  {loading ? 'Submitting...' : 'Submit for Factoring'}
                </button>
              </div>
            </form>

            {/* Live preview */}
            <div>
              <div className="card">
                <div style={{ fontWeight: 700, marginBottom: '16px', color: 'var(--text-primary)' }}>Advance Preview</div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: '16px' }}>
                  Computed in integer cents — identical to server logic
                </div>

                <PreviewRow label="Face Value" value={<MoneyDisplay minorUnits={faceMinor} currency={form.currency as 'USD' | 'BRL'} size="md" />} />
                <div style={{ borderTop: '1px solid var(--bg-border)', margin: '12px 0' }} />
                <PreviewRow
                  label={`Advance (${form.advanceRatePct}%)`}
                  value={<MoneyDisplay minorUnits={advMinor} currency={form.currency as 'USD' | 'BRL'} size="md" />}
                  accent="var(--accent)"
                />
                <PreviewRow
                  label={`Fee (${form.feeRatePct}%)`}
                  value={<MoneyDisplay minorUnits={feeMinor} currency={form.currency as 'USD' | 'BRL'} size="md" />}
                  accent="var(--status-pending)"
                />
                <PreviewRow
                  label="Reserve Holdback"
                  value={<MoneyDisplay minorUnits={reserveMinor} currency={form.currency as 'USD' | 'BRL'} size="md" />}
                  accent="var(--status-disbursed)"
                />
                <div style={{ borderTop: '1px solid var(--bg-border)', margin: '12px 0' }} />
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', lineHeight: 1.6 }}>
                  Carrier receives <strong style={{ color: 'var(--accent)' }}>advance</strong> immediately upon disburse. Reserve minus fee released on collection.
                </div>
              </div>

              <div style={{ marginTop: '16px', padding: '14px', background: 'var(--bg-surface)', borderRadius: '10px', border: '1px solid var(--bg-border)', fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                <div style={{ color: 'var(--accent)', fontWeight: 600, marginBottom: '6px' }}>Integer Math</div>
                advance = floor(face × rateBps / 10000)<br />
                {faceMinor > 0 && <>= floor({faceMinor} × {advRateBps} / 10000) = {advMinor}</>}
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

function PreviewRow({ label, value, accent }: { label: string; value: React.ReactNode; accent?: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0' }}>
      <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{label}</span>
      <span style={{ color: accent }}>{value}</span>
    </div>
  );
}
