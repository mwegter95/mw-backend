'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Sidebar } from '../../components/Sidebar';
import { LedgerTable } from '../../components/LedgerTable';
import { ledgerApi } from '../../lib/api';
import { getToken } from '../../lib/auth';
import type { LedgerEntry } from '../../lib/api';

const BASE = '';

export default function LedgerPage() {
  const router = useRouter();
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!getToken()) router.replace(`${BASE}/login/`);
  }, [router]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await ledgerApi.recent();
      setEntries(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const totalDebit = entries.filter(e => e.entryType === 'debit').reduce((s, e) => s + e.amountMinorUnits, 0);
  const totalCredit = entries.filter(e => e.entryType === 'credit').reduce((s, e) => s + e.amountMinorUnits, 0);

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner">
          <div style={{ marginBottom: '24px' }}>
            <h1 className="page-title">Double-Entry Ledger</h1>
            <p className="page-subtitle">Recent entries across all accounts. FIDC-compliant double-entry bookkeeping with integer-cent amounts.</p>
          </div>

          {error && <div className="error-box">{error}</div>}

          {/* Summary */}
          <div className="grid-2" style={{ marginBottom: '24px' }}>
            <div className="stat-card">
              <div className="stat-label">Total Debits (this view)</div>
              <div style={{ color: 'var(--accent)', marginTop: '8px', fontFamily: 'var(--font-mono)', fontSize: '1.25rem', fontWeight: 600 }}>
                ${(totalDebit / 100).toLocaleString('en-US', { minimumFractionDigits: 2 })}
              </div>
              <div style={{ color: 'var(--text-muted)', fontSize: '0.75rem', marginTop: '4px' }}>{entries.filter(e => e.entryType === 'debit').length} entries</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Total Credits (this view)</div>
              <div style={{ color: 'var(--status-disbursed)', marginTop: '8px', fontFamily: 'var(--font-mono)', fontSize: '1.25rem', fontWeight: 600 }}>
                ${(totalCredit / 100).toLocaleString('en-US', { minimumFractionDigits: 2 })}
              </div>
              <div style={{ color: 'var(--text-muted)', fontSize: '0.75rem', marginTop: '4px' }}>{entries.filter(e => e.entryType === 'credit').length} entries</div>
            </div>
          </div>

          <div style={{ marginBottom: '12px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <h2 style={{ fontSize: '0.9rem', fontWeight: 700, color: 'var(--text-secondary)' }}>Recent 50 Entries</h2>
            <button onClick={load} style={{ background: 'none', border: '1px solid var(--bg-border)', borderRadius: '6px', color: 'var(--text-muted)', padding: '5px 12px', cursor: 'pointer', fontSize: '0.8rem' }}>
              Refresh
            </button>
          </div>

          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <LedgerTable entries={entries} loading={loading} />
          </div>

          {/* Architecture note */}
          <div style={{ marginTop: '20px', padding: '16px', background: 'var(--bg-surface)', border: '1px solid var(--bg-border)', borderRadius: '10px', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
            <strong style={{ color: 'var(--text-secondary)' }}>FIDC Double-Entry Model:</strong> Each financial event posts paired debit/credit entries.
            Disburse: Debit FACTORING_REC + Credit CASH_BANK.
            Collect: Debit CASH_BANK + Credit FACTORING_REC + Credit FEE_REVENUE + Credit CARRIER_RESERVE.
            All amounts stored as integer minor units — no floating-point on money paths.
          </div>
        </div>
      </main>
    </div>
  );
}
