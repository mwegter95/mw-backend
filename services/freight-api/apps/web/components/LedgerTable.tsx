'use client';

import type { LedgerEntry } from '../lib/api';
import { MoneyDisplay } from './MoneyDisplay';

interface LedgerTableProps {
  entries: LedgerEntry[];
  loading?: boolean;
}

export function LedgerTable({ entries, loading = false }: LedgerTableProps) {
  if (loading) {
    return <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>Loading entries...</div>;
  }
  if (entries.length === 0) {
    return <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>No ledger entries yet.</div>;
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--bg-border)' }}>
            {['Date', 'Description', 'Type', 'Amount', 'Invoice'].map(h => (
              <th key={h} style={{
                padding: '10px 16px',
                textAlign: h === 'Amount' ? 'right' : 'left',
                color: 'var(--text-muted)',
                fontWeight: 600,
                fontSize: '0.75rem',
                textTransform: 'uppercase',
                letterSpacing: '0.05em',
                whiteSpace: 'nowrap',
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {entries.map((entry, i) => (
            <tr
              key={entry.id}
              style={{
                borderBottom: '1px solid var(--bg-border)',
                background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)',
              }}
            >
              <td style={{ padding: '10px 16px', color: 'var(--text-muted)', whiteSpace: 'nowrap', fontSize: '0.8rem' }}>
                {new Date(entry.postedAt).toLocaleString()}
              </td>
              <td style={{ padding: '10px 16px', color: 'var(--text-primary)', maxWidth: '280px' }}>
                {entry.description}
              </td>
              <td style={{ padding: '10px 16px', whiteSpace: 'nowrap' }}>
                <span style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  padding: '2px 10px',
                  borderRadius: '9999px',
                  fontSize: '0.72rem',
                  fontWeight: 700,
                  textTransform: 'uppercase',
                  letterSpacing: '0.06em',
                  background: entry.entryType === 'debit' ? '#064E3B22' : '#1E1B4B22',
                  color: entry.entryType === 'debit' ? 'var(--accent)' : 'var(--status-disbursed)',
                  border: entry.entryType === 'debit' ? '1px solid #064E3B44' : '1px solid #1E1B4B44',
                }}>
                  {entry.entryType}
                </span>
              </td>
              <td style={{ padding: '10px 16px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                <MoneyDisplay minorUnits={entry.amountMinorUnits} currency={entry.currency} size="sm" />
              </td>
              <td style={{ padding: '10px 16px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: '0.8rem' }}>
                {entry.invoiceId ? `#${entry.invoiceId}` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
