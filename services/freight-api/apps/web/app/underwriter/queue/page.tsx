'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Sidebar } from '../../../components/Sidebar';
import { InvoiceTable } from '../../../components/InvoiceTable';
import { MoneyDisplay } from '../../../components/MoneyDisplay';
import { invoicesApi } from '../../../lib/api';
import { getToken, getUser } from '../../../lib/auth';
import type { Invoice } from '../../../lib/api';
import type { AuthUser } from '../../../lib/auth';

const BASE = '';

export default function ApprovalQueuePage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  useEffect(() => {
    const u = getUser();
    if (!getToken() || !u || !['underwriter', 'admin'].includes(u.role)) {
      router.replace(`${BASE}/login/`);
      return;
    }
    setUser(u);
  }, [router]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await invoicesApi.list();
      setInvoices(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleAction = async (id: number, action: 'approve' | 'reject' | 'disburse' | 'collect') => {
    setError(''); setSuccess('');
    try {
      if (action === 'approve') await invoicesApi.approve(id);
      else if (action === 'reject') await invoicesApi.reject(id);
      else if (action === 'disburse') await invoicesApi.disburse(id);
      else await invoicesApi.collect(id);
      setSuccess(`Invoice #${id} ${action}d`);
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : `Failed: ${action}`);
    }
  };

  const pending = invoices.filter(i => i.status === 'pending');
  const approved = invoices.filter(i => i.status === 'approved');
  const pendingFaceValue = pending.reduce((s, i) => s + i.faceValueMinorUnits, 0);
  const approvedAdvance = approved.reduce((s, i) => s + (i.advanceMinorUnits ?? 0), 0);

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner">
          <h1 className="page-title">Approval Queue</h1>
          <p className="page-subtitle">Review and act on pending and approved invoices</p>

          {error && <div className="error-box">{error}</div>}
          {success && <div className="success-box">{success}</div>}

          {/* Summary */}
          <div className="grid-2" style={{ marginBottom: '24px' }}>
            <div className="stat-card" style={{ borderLeft: '3px solid var(--status-pending)' }}>
              <div className="stat-label">Pending Review</div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: '12px', marginTop: '8px' }}>
                <span style={{ fontSize: '2rem', fontWeight: 800, color: 'var(--status-pending)' }}>{pending.length}</span>
                <div>
                  <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>Face value</div>
                  <MoneyDisplay minorUnits={pendingFaceValue} size="sm" />
                </div>
              </div>
            </div>
            <div className="stat-card" style={{ borderLeft: '3px solid var(--status-disbursed)' }}>
              <div className="stat-label">Ready to Disburse</div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: '12px', marginTop: '8px' }}>
                <span style={{ fontSize: '2rem', fontWeight: 800, color: 'var(--status-disbursed)' }}>{approved.length}</span>
                <div>
                  <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>Advance due</div>
                  <MoneyDisplay minorUnits={approvedAdvance} size="sm" />
                </div>
              </div>
            </div>
          </div>

          {/* Pending section */}
          {pending.length > 0 && (
            <div style={{ marginBottom: '24px' }}>
              <h2 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--status-pending)', marginBottom: '12px' }}>
                Pending ({pending.length})
              </h2>
              <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                <InvoiceTable invoices={pending} showActions loading={loading} onAction={handleAction} />
              </div>
            </div>
          )}

          {/* Approved / disburse section */}
          {approved.length > 0 && (
            <div style={{ marginBottom: '24px' }}>
              <h2 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--status-disbursed)', marginBottom: '12px' }}>
                Approved — Awaiting Disburse ({approved.length})
              </h2>
              <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                <InvoiceTable invoices={approved} showActions loading={loading} onAction={handleAction} />
              </div>
            </div>
          )}

          {pending.length === 0 && approved.length === 0 && !loading && (
            <div className="card" style={{ textAlign: 'center', padding: '48px', color: 'var(--text-muted)' }}>
              <div style={{ fontSize: '2rem', marginBottom: '12px' }}>✅</div>
              Queue is clear — no pending or approved invoices.
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
