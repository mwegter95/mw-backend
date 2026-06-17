'use client';

type InvoiceStatus = 'pending' | 'approved' | 'rejected' | 'disbursed' | 'collected';

interface StatusBadgeProps {
  status: InvoiceStatus;
}

const labels: Record<InvoiceStatus, string> = {
  pending: 'Pending',
  approved: 'Approved',
  rejected: 'Rejected',
  disbursed: 'Disbursed',
  collected: 'Collected',
};

export function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span className={`badge badge-${status}`}>
      {labels[status]}
    </span>
  );
}
