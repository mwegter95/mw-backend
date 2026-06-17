'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { Sidebar } from '../../../components/Sidebar';
import { MoneyDisplay } from '../../../components/MoneyDisplay';
import { LedgerTable } from '../../../components/LedgerTable';
import { ledgerApi } from '../../../lib/api';
import { getToken } from '../../../lib/auth';
import type { Account, LedgerEntry } from '../../../lib/api';

const BASE = '';

const typeColors: Record<string, string> = {
  asset: '#10B981',
  liability: '#6366F1',
  revenue: '#F59E0B',
  expense: '#EF4444',
};

export default function AccountDetailClient({ id }: { id: number }) {
  const router = useRouter();

  const [account, setAccount] = useState<Account | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!getToken()) router.replace(`${BASE}/login/`);
  }, [router]);

  const load = useCallback(async () => {
    if (isNaN(id)) return;
    setLoading(true);
    try {
      const data = await ledgerApi.accountEntries(id);
      setAccount(data.account);
      setEntries(data.entries);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally { setLoading(false); }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  const accentColor = account ? (typeColors[account.type] ?? '#9CA3AF') : '#9CA3AF';

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner">
          <div style={{ marginBottom: '16px' }}>
            <Link href={`${BASE}/accounts/`} style={{ color: 'var(--text-muted)', textDecoration: 'none', fontSize: '0.85rem' }}>
              ← Accounts
            </Link>
          </div>

          {error && <div className="error-box">{error}</div>}

          {loading ? (
            <div style={{ color: 'var(--text-muted)' }}>Loading account...</div>
          ) : account ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '24px' }}>
                <div style={{
                  width: '52px', height: '52px', borderRadius: '12px',
                  background: `${accentColor}22`, border: `2px solid ${accentColor}44`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: '0.75rem', fontWeight: 800, color: accentColor,
                  textTransform: 'uppercase', letterSpacing: '0.05em',
                }}>
                  {account.type.slice(0, 3)}
                </div>
                <div>
                  <h1 className="page-title" style={{ marginBottom: 0 }}>{account.name}</h1>
                  <div style={{ color: accentColor, fontFamily: 'var(--font-mono)', fontSize: '0.85rem', marginTop: '2px' }}>
                    {account.code} — {account.type}
                  </div>
                </div>
              </div>

              <div className="card" style={{ marginBottom: '24px', borderLeft: `3px solid ${accentColor}` }}>
                <div className="stat-label">Current Balance</div>
                <div style={{ marginTop: '10px' }}>
                  <MoneyDisplay minorUnits={account.balanceMinorUnits} currency={account.currency} size="xl" />
                </div>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.78rem', marginTop: '8px' }}>
                  {account.balanceMinorUnits} minor units (integer cents) — {account.currency}
                </div>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.72rem', marginTop: '4px' }}>
                  Updated: {new Date(account.updatedAt).toLocaleString()}
                </div>
              </div>

              <div style={{ marginBottom: '12px', fontWeight: 700, color: 'var(--text-primary)', fontSize: '0.95rem' }}>
                Ledger Entries ({entries.length})
              </div>
              <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                <LedgerTable entries={entries} loading={loading} />
              </div>
            </>
          ) : (
            <div className="error-box">Account not found</div>
          )}
        </div>
      </main>
    </div>
  );
}
