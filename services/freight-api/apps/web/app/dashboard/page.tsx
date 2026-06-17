'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Sidebar } from '../../components/Sidebar';
import { MoneyDisplay } from '../../components/MoneyDisplay';
import { StatusBadge } from '../../components/StatusBadge';
import { invoicesApi, ledgerApi } from '../../lib/api';
import { getToken, getUser } from '../../lib/auth';
import type { Invoice, Account } from '../../lib/api';
import type { AuthUser } from '../../lib/auth';
import Link from 'next/link';

const BASE = '';

export default function DashboardPage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getToken()) { router.replace(`${BASE}/login/`); return; }
    const u = getUser();
    setUser(u);
  }, [router]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [invs, accts] = await Promise.all([invoicesApi.list(), ledgerApi.accounts()]);
      setInvoices(invs);
      setAccounts(accts);
    } catch { /* handled by api.ts redirect */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const statusCounts = {
    pending: invoices.filter(i => i.status === 'pending').length,
    approved: invoices.filter(i => i.status === 'approved').length,
    disbursed: invoices.filter(i => i.status === 'disbursed').length,
    collected: invoices.filter(i => i.status === 'collected').length,
  };

  const totalFaceValue = invoices.reduce((s, i) => s + i.faceValueMinorUnits, 0);
  const totalDisbursed = invoices
    .filter(i => ['disbursed', 'collected'].includes(i.status))
    .reduce((s, i) => s + (i.advanceMinorUnits ?? 0), 0);

  const cashAccount = accounts.find(a => a.code === 'CASH_BANK');
  const receivableAccount = accounts.find(a => a.code === 'FACTORING_REC');
  const feeAccount = accounts.find(a => a.code === 'FEE_REVENUE');

  const recentInvoices = invoices.slice(0, 5);

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner">
          <div style={{ marginBottom: '28px' }}>
            <h1 className="page-title">Dashboard</h1>
            <p className="page-subtitle">
              {user ? `Welcome back, ${user.name} (${user.role})` : 'Loading...'}
            </p>
          </div>

          {loading ? (
            <div style={{ color: 'var(--text-muted)' }}>Loading dashboard...</div>
          ) : (
            <>
              {/* KPI row */}
              <div className="grid-4" style={{ marginBottom: '24px' }}>
                <StatCard label="Cash Available" value={
                  <MoneyDisplay minorUnits={cashAccount?.balanceMinorUnits ?? 0} size="lg" />
                } icon="💵" accent="#10B981" />
                <StatCard label="Receivables" value={
                  <MoneyDisplay minorUnits={receivableAccount?.balanceMinorUnits ?? 0} size="lg" />
                } icon="📄" accent="#6366F1" />
                <StatCard label="Fee Revenue" value={
                  <MoneyDisplay minorUnits={feeAccount?.balanceMinorUnits ?? 0} size="lg" />
                } icon="💹" accent="#F59E0B" />
                <StatCard label="Total Disbursed" value={
                  <MoneyDisplay minorUnits={totalDisbursed} size="lg" />
                } icon="🚀" accent="#3B82F6" />
              </div>

              {/* Invoice pipeline */}
              <div style={{ marginBottom: '24px' }}>
                <SectionHeader title="Invoice Pipeline" link={`${BASE}/invoices/`} linkLabel="View all" />
                <div className="grid-4">
                  {[
                    { label: 'Pending', count: statusCounts.pending, color: '#F59E0B', status: 'pending' },
                    { label: 'Approved', count: statusCounts.approved, color: '#10B981', status: 'approved' },
                    { label: 'Disbursed', count: statusCounts.disbursed, color: '#6366F1', status: 'disbursed' },
                    { label: 'Collected', count: statusCounts.collected, color: '#3B82F6', status: 'collected' },
                  ].map(item => (
                    <div key={item.status} className="stat-card" style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                      <div style={{
                        width: '48px', height: '48px', borderRadius: '12px',
                        background: `${item.color}22`, border: `1px solid ${item.color}44`,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        fontSize: '1.4rem', flexShrink: 0,
                      }}>
                        {item.count}
                      </div>
                      <div>
                        <div className="stat-label">{item.label}</div>
                        <StatusBadge status={item.status as Invoice['status']} />
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Recent invoices */}
              <div style={{ marginBottom: '24px' }}>
                <SectionHeader title="Recent Invoices" link={`${BASE}/invoices/`} linkLabel="All invoices" />
                <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                  {recentInvoices.length === 0 ? (
                    <div style={{ padding: '32px', textAlign: 'center', color: 'var(--text-muted)' }}>No invoices yet.</div>
                  ) : (
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
                      <thead>
                        <tr style={{ borderBottom: '1px solid var(--bg-border)' }}>
                          {['Invoice #', 'Payer', 'Face Value', 'Advance', 'Status'].map(h => (
                            <th key={h} style={{
                              padding: '12px 16px', textAlign: h === 'Face Value' || h === 'Advance' ? 'right' : 'left',
                              color: 'var(--text-muted)', fontWeight: 600, fontSize: '0.75rem',
                              textTransform: 'uppercase', letterSpacing: '0.05em',
                            }}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {recentInvoices.map((inv) => (
                          <tr key={inv.id} style={{ borderBottom: '1px solid var(--bg-border)' }}>
                            <td style={{ padding: '12px 16px' }}>
                              <Link href={`${BASE}/invoices/${inv.id}/`} style={{ color: 'var(--accent)', textDecoration: 'none', fontFamily: 'var(--font-mono)', fontWeight: 600 }}>
                                {inv.invoiceNumber}
                              </Link>
                            </td>
                            <td style={{ padding: '12px 16px', color: 'var(--text-primary)' }}>{inv.payerName}</td>
                            <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                              <MoneyDisplay minorUnits={inv.faceValueMinorUnits} size="sm" />
                            </td>
                            <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                              {inv.advanceMinorUnits != null ? <MoneyDisplay minorUnits={inv.advanceMinorUnits} size="sm" /> : '—'}
                            </td>
                            <td style={{ padding: '12px 16px' }}><StatusBadge status={inv.status} /></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </div>

              {/* Accounts summary */}
              <div>
                <SectionHeader title="Account Balances" link={`${BASE}/accounts/`} linkLabel="View ledger" />
                <div className="grid-4">
                  {accounts.map(acct => (
                    <Link key={acct.id} href={`${BASE}/accounts/${acct.id}/`} style={{ textDecoration: 'none' }}>
                      <div className="stat-card" style={{ cursor: 'pointer', transition: 'border-color 0.15s' }}
                        onMouseEnter={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)'}
                        onMouseLeave={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--bg-border)'}
                      >
                        <div className="stat-label">{acct.name}</div>
                        <MoneyDisplay minorUnits={acct.balanceMinorUnits} currency={acct.currency} size="md" />
                        <div style={{ marginTop: '8px', fontSize: '0.72rem', color: 'var(--text-muted)', textTransform: 'capitalize' }}>
                          {acct.type}
                        </div>
                      </div>
                    </Link>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}

function StatCard({ label, value, icon, accent }: { label: string; value: React.ReactNode; icon: string; accent: string }) {
  return (
    <div className="stat-card">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div className="stat-label">{label}</div>
        <span style={{ fontSize: '1.3rem' }}>{icon}</span>
      </div>
      <div style={{ color: accent }}>{value}</div>
    </div>
  );
}

function SectionHeader({ title, link, linkLabel }: { title: string; link: string; linkLabel: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
      <h2 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>{title}</h2>
      <Link href={link} style={{ color: 'var(--accent)', fontSize: '0.8rem', textDecoration: 'none', fontWeight: 500 }}>
        {linkLabel} →
      </Link>
    </div>
  );
}
