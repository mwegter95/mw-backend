'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Sidebar } from '../../components/Sidebar';
import { InvoiceTable } from '../../components/InvoiceTable';
import { invoicesApi } from '../../lib/api';
import { getToken, getUser } from '../../lib/auth';
import type { Invoice } from '../../lib/api';
import type { AuthUser } from '../../lib/auth';

const BASE = '';

export default function InvoicesPage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  useEffect(() => {
    if (!getToken()) { router.replace(`${BASE}/login/`); return; }
    setUser(getUser());
  }, [router]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await invoicesApi.list();
      setInvoices(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleAction = async (id: number, action: 'approve' | 'reject' | 'disburse' | 'collect') => {
    setError(''); setSuccess('');
    try {
      if (action === 'approve') await invoicesApi.approve(id);
      else if (action === 'reject') await invoicesApi.reject(id);
      else if (action === 'disburse') await invoicesApi.disburse(id);
      else await invoicesApi.collect(id);
      setSuccess(`Invoice #${id} ${action}d successfully`);
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : `Failed to ${action}`);
    }
  };

  const canAct = user && ['underwriter', 'admin'].includes(user.role);

  return (
    <div className="page-layout">
      <Sidebar />
      <main className="main-content">
        <div className="page-inner">
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '24px' }}>
            <div>
              <h1 className="page-title">{user?.role === 'carrier' ? 'My Invoices' : 'All Invoices'}</h1>
              <p className="page-subtitle">{invoices.length} invoice{invoices.length !== 1 ? 's' : ''} total</p>
            </div>
            {user?.role === 'carrier' && (
              <a
                href={`${BASE}/invoices/new/`}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: '6px',
                  padding: '10px 18px', background: 'var(--accent)', color: '#000',
                  borderRadius: '8px', textDecoration: 'none', fontWeight: 700, fontSize: '0.875rem',
                }}
              >
                + Submit Invoice
              </a>
            )}
          </div>

          {error && <div className="error-box">{error}</div>}
          {success && <div className="success-box">{success}</div>}

          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <InvoiceTable
              invoices={invoices}
              loading={loading}
              showActions={!!canAct}
              onAction={handleAction}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
