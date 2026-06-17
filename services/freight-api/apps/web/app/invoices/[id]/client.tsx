'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { Sidebar } from '../../../components/Sidebar';
import { MoneyDisplay } from '../../../components/MoneyDisplay';
import { StatusBadge } from '../../../components/StatusBadge';
import { invoicesApi } from '../../../lib/api';
import { getToken, getUser } from '../../../lib/auth';
import type { Invoice } from '../../../lib/api';
import type { AuthUser } from '../../../lib/auth';

const BASE = '';

export default function InvoiceDetailClient({ id }: { id: number }) {
  const router = useRouter();

  const [user, setUser] = useState<AuthUser | null>(null);
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  useEffect(() => {
    if (!getToken()) { router.replace(`${BASE}/login/`); return; }
    setUser(getUser());
  }, [router]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await invoicesApi.get(id);
      setInvoice(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally { setLoading(false); }
  }, [id]);

  useEffect(() => { if (!isNaN(id)) load(); }, [id, load]);

  const handleAction = async (action: 'approve' | 'reject' | 'disburse' | 'collect') => {
    setError(''); setSuccess(''); setActionLoading(true);
    try {
      if (action === 'approve') await invoicesApi.approve(id);
      else if (action === 'reject') await invoicesApi.reject(id);
      else if (action === 'disburse') await invoicesApi.disburse(id);
      else await invoicesApi.collect(id);
      setSuccess(`Invoice ${action}d successfully`);
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : `Failed to ${action}`);
    } finally { setActionLoading(false); }
  };

  const canAct = user && ['underwriter', 'admin'].includes(user.role);

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner" style={{ maxWidth: '900px' }}>
          <div style={{ marginBottom: '20px' }}>
            <Link href={`${BASE}/invoices/`} style={{ color: 'var(--text-muted)', textDecoration: 'none', fontSize: '0.85rem' }}>
              ← Invoices
            </Link>
          </div>

          {loading ? (
            <div style={{ color: 'var(--text-muted)' }}>Loading invoice...</div>
          ) : invoice ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '24px' }}>
                <h1 className="page-title" style={{ marginBottom: 0, fontFamily: 'var(--font-mono)' }}>
                  {invoice.invoiceNumber}
                </h1>
                <StatusBadge status={invoice.status} />
              </div>

              {error && <div className="error-box">{error}</div>}
              {success && <div className="success-box">{success}</div>}

              <div className="grid-2" style={{ gap: '20px', marginBottom: '20px' }}>
                <div className="card">
                  <div style={{ fontWeight: 700, color: 'var(--text-primary)', marginBottom: '16px' }}>Invoice Details</div>
                  <DetailRow label="Payer" value={invoice.payerName} />
                  <DetailRow label="Days to Pay" value={`${invoice.payerDaysToPay} days`} />
                  <DetailRow label="Currency" value={invoice.currency} />
                  <DetailRow label="Submitted" value={new Date(invoice.submittedAt).toLocaleString()} />
                  {invoice.approvedAt && <DetailRow label="Approved" value={new Date(invoice.approvedAt).toLocaleString()} />}
                  {invoice.disbursedAt && <DetailRow label="Disbursed" value={new Date(invoice.disbursedAt).toLocaleString()} />}
                  {invoice.collectedAt && <DetailRow label="Collected" value={new Date(invoice.collectedAt).toLocaleString()} />}
                  {invoice.notes && <DetailRow label="Notes" value={invoice.notes} />}
                </div>

                <div className="card">
                  <div style={{ fontWeight: 700, color: 'var(--text-primary)', marginBottom: '16px' }}>Money Breakdown</div>
                  <MoneyDetailRow label="Face Value" minorUnits={invoice.faceValueMinorUnits} currency={invoice.currency} size="lg" />
                  <div style={{ borderTop: '1px solid var(--bg-border)', margin: '14px 0' }} />
                  <MoneyDetailRow
                    label={`Advance Rate (${(invoice.advanceRateBps / 100).toFixed(2)}%)`}
                    minorUnits={invoice.advanceMinorUnits ?? 0}
                    currency={invoice.currency}
                    accent="var(--accent)"
                  />
                  <MoneyDetailRow
                    label={`Fee Rate (${(invoice.feeRateBps / 100).toFixed(2)}%)`}
                    minorUnits={invoice.feeMinorUnits ?? 0}
                    currency={invoice.currency}
                    accent="var(--status-pending)"
                  />
                  <MoneyDetailRow
                    label="Reserve Holdback"
                    minorUnits={invoice.reserveMinorUnits ?? 0}
                    currency={invoice.currency}
                    accent="var(--status-disbursed)"
                  />
                  <div style={{ borderTop: '1px solid var(--bg-border)', margin: '14px 0' }} />
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                    advance_rate_bps: {invoice.advanceRateBps}<br />
                    fee_rate_bps: {invoice.feeRateBps}<br />
                    floor(face × bps / 10000) — no floats
                  </div>
                </div>
              </div>

              {canAct && (
                <div className="card">
                  <div style={{ fontWeight: 700, color: 'var(--text-primary)', marginBottom: '16px' }}>Actions</div>
                  <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
                    {invoice.status === 'pending' && (
                      <>
                        <ActionBtn label="Approve" onClick={() => handleAction('approve')} color="#10B981" loading={actionLoading} />
                        <ActionBtn label="Reject" onClick={() => handleAction('reject')} color="#EF4444" loading={actionLoading} />
                      </>
                    )}
                    {invoice.status === 'approved' && (
                      <ActionBtn label="Disburse Advance" onClick={() => handleAction('disburse')} color="#6366F1" loading={actionLoading} />
                    )}
                    {invoice.status === 'disbursed' && user?.role === 'admin' && (
                      <ActionBtn label="Mark Collected" onClick={() => handleAction('collect')} color="#3B82F6" loading={actionLoading} />
                    )}
                    {['rejected', 'collected'].includes(invoice.status) && (
                      <span style={{ color: 'var(--text-muted)', fontSize: '0.875rem' }}>No further actions available</span>
                    )}
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="error-box">{error || 'Invoice not found'}</div>
          )}
        </div>
      </main>
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--bg-border)' }}>
      <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{label}</span>
      <span style={{ color: 'var(--text-primary)', fontSize: '0.875rem' }}>{value}</span>
    </div>
  );
}

function MoneyDetailRow({ label, minorUnits, currency, size = 'md', accent }: {
  label: string;
  minorUnits: number;
  currency: 'USD' | 'BRL';
  size?: 'sm' | 'md' | 'lg';
  accent?: string;
}) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0' }}>
      <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{label}</span>
      <span style={{ color: accent }}>
        <MoneyDisplay minorUnits={minorUnits} currency={currency} size={size} />
      </span>
    </div>
  );
}

function ActionBtn({ label, onClick, color, loading }: { label: string; onClick: () => void; color: string; loading: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={loading}
      style={{
        padding: '10px 20px', borderRadius: '8px', border: `1px solid ${color}66`,
        background: `${color}22`, color, fontSize: '0.875rem', fontWeight: 700,
        cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.6 : 1,
        transition: 'background 0.15s',
      }}
    >
      {loading ? '...' : label}
    </button>
  );
}
