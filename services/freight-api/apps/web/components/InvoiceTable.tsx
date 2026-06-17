'use client';

import Link from 'next/link';
import type { Invoice } from '../lib/api';
import { MoneyDisplay } from './MoneyDisplay';
import { StatusBadge } from './StatusBadge';

const BASE = '';

interface InvoiceTableProps {
  invoices: Invoice[];
  onAction?: (id: number, action: 'approve' | 'reject' | 'disburse' | 'collect') => void;
  showActions?: boolean;
  loading?: boolean;
}

export function InvoiceTable({ invoices, onAction, showActions = false, loading = false }: InvoiceTableProps) {
  if (loading) {
    return (
      <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>
        Loading invoices...
      </div>
    );
  }

  if (invoices.length === 0) {
    return (
      <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>
        No invoices found.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--bg-border)' }}>
            {['Invoice #', 'Payer', 'Face Value', 'Advance', 'Fee', 'Status', 'Submitted', showActions ? 'Actions' : ''].filter(Boolean).map(h => (
              <th key={h} style={{
                padding: '10px 16px',
                textAlign: h === 'Face Value' || h === 'Advance' || h === 'Fee' ? 'right' : 'left',
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
          {invoices.map((inv, i) => (
            <tr
              key={inv.id}
              style={{
                borderBottom: '1px solid var(--bg-border)',
                background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)',
              }}
            >
              <td style={{ padding: '12px 16px', whiteSpace: 'nowrap' }}>
                <Link
                  href={`${BASE}/invoices/${inv.id}/`}
                  style={{ color: 'var(--accent)', textDecoration: 'none', fontWeight: 600, fontFamily: 'var(--font-mono)' }}
                >
                  {inv.invoiceNumber}
                </Link>
              </td>
              <td style={{ padding: '12px 16px', color: 'var(--text-primary)', maxWidth: '180px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {inv.payerName}
              </td>
              <td style={{ padding: '12px 16px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                <MoneyDisplay minorUnits={inv.faceValueMinorUnits} currency={inv.currency} size="sm" />
              </td>
              <td style={{ padding: '12px 16px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                {inv.advanceMinorUnits != null
                  ? <MoneyDisplay minorUnits={inv.advanceMinorUnits} currency={inv.currency} size="sm" />
                  : <span style={{ color: 'var(--text-muted)' }}>—</span>
                }
              </td>
              <td style={{ padding: '12px 16px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                {inv.feeMinorUnits != null
                  ? <MoneyDisplay minorUnits={inv.feeMinorUnits} currency={inv.currency} size="sm" />
                  : <span style={{ color: 'var(--text-muted)' }}>—</span>
                }
              </td>
              <td style={{ padding: '12px 16px', whiteSpace: 'nowrap' }}>
                <StatusBadge status={inv.status} />
              </td>
              <td style={{ padding: '12px 16px', color: 'var(--text-muted)', whiteSpace: 'nowrap', fontSize: '0.8rem' }}>
                {new Date(inv.submittedAt).toLocaleDateString()}
              </td>
              {showActions && (
                <td style={{ padding: '12px 16px', whiteSpace: 'nowrap' }}>
                  <ActionButtons inv={inv} onAction={onAction} />
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ActionButtons({
  inv,
  onAction,
}: {
  inv: Invoice;
  onAction?: (id: number, action: 'approve' | 'reject' | 'disburse' | 'collect') => void;
}) {
  if (!onAction) return null;

  const btn = (label: string, action: 'approve' | 'reject' | 'disburse' | 'collect', color: string) => (
    <button
      key={action}
      onClick={() => onAction(inv.id, action)}
      style={{
        padding: '4px 10px',
        borderRadius: '6px',
        border: `1px solid ${color}55`,
        background: `${color}22`,
        color,
        fontSize: '0.75rem',
        fontWeight: 600,
        cursor: 'pointer',
        marginRight: '6px',
      }}
    >
      {label}
    </button>
  );

  if (inv.status === 'pending') return <>{btn('Approve', 'approve', '#10B981')}{btn('Reject', 'reject', '#EF4444')}</>;
  if (inv.status === 'approved') return btn('Disburse', 'disburse', '#6366F1');
  if (inv.status === 'disbursed') return btn('Collect', 'collect', '#3B82F6');
  return <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>No actions</span>;
}
