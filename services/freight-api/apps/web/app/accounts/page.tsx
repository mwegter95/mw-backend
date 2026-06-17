'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { Sidebar } from '../../components/Sidebar';
import { MoneyDisplay } from '../../components/MoneyDisplay';
import { ledgerApi } from '../../lib/api';
import { getToken } from '../../lib/auth';
import type { Account } from '../../lib/api';

const BASE = '';

const typeColors: Record<string, string> = {
  asset: '#10B981',
  liability: '#6366F1',
  revenue: '#F59E0B',
  expense: '#EF4444',
};

export default function AccountsPage() {
  const router = useRouter();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!getToken()) router.replace(`${BASE}/login/`);
  }, [router]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await ledgerApi.accounts();
      setAccounts(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const totalAssets = accounts.filter(a => a.type === 'asset').reduce((s, a) => s + a.balanceMinorUnits, 0);

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner">
          <h1 className="page-title">Chart of Accounts</h1>
          <p className="page-subtitle">All ledger accounts — balances in integer cents</p>

          {error && <div className="error-box">{error}</div>}

          {loading ? (
            <div style={{ color: 'var(--text-muted)' }}>Loading accounts...</div>
          ) : (
            <>
              {/* Summary */}
              <div className="card" style={{ marginBottom: '24px', display: 'flex', alignItems: 'center', gap: '32px' }}>
                <div>
                  <div className="stat-label">Total Assets</div>
                  <MoneyDisplay minorUnits={totalAssets} size="xl" />
                </div>
                <div style={{ flex: 1 }} />
                <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  {accounts.length} accounts — click any row to view entries
                </div>
              </div>

              {/* Account list */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {accounts.map(acct => (
                  <Link
                    key={acct.id}
                    href={`${BASE}/accounts/${acct.id}/`}
                    style={{ textDecoration: 'none' }}
                  >
                    <div
                      className="card"
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '16px',
                        cursor: 'pointer',
                        transition: 'border-color 0.15s',
                        padding: '16px 20px',
                      }}
                      onMouseEnter={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)'}
                      onMouseLeave={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--bg-border)'}
                    >
                      <div style={{
                        width: '40px', height: '40px', borderRadius: '10px',
                        background: `${typeColors[acct.type] ?? '#9CA3AF'}22`,
                        border: `1px solid ${typeColors[acct.type] ?? '#9CA3AF'}44`,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        fontSize: '0.7rem', fontWeight: 700, color: typeColors[acct.type] ?? '#9CA3AF',
                        textTransform: 'uppercase', letterSpacing: '0.05em', flexShrink: 0,
                      }}>
                        {acct.type.slice(0, 3)}
                      </div>

                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontWeight: 700, color: 'var(--text-primary)', fontSize: '0.95rem' }}>{acct.name}</div>
                        <div style={{ color: 'var(--text-muted)', fontSize: '0.78rem', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
                          {acct.code}
                        </div>
                      </div>

                      <div style={{ textAlign: 'right' }}>
                        <MoneyDisplay minorUnits={acct.balanceMinorUnits} currency={acct.currency} size="md" />
                        <div style={{ color: 'var(--text-muted)', fontSize: '0.72rem', marginTop: '2px' }}>
                          {acct.currency}
                        </div>
                      </div>

                      <span style={{ color: 'var(--text-muted)', fontSize: '1rem' }}>→</span>
                    </div>
                  </Link>
                ))}
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
