'use client';

import { getToken } from './auth';
import { isMockMode, enableMockMode, mockRequest } from './mockData';

export const API_BASE = 'https://api.michaelwegter.com/api/factoring';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  // Fast path: already in mock mode (real API unavailable this session)
  if (isMockMode()) return mockRequest<T>(path, options);

  const token = getToken();
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  };
  if (token) (headers as Record<string, string>)['Authorization'] = `Bearer ${token}`;

  try {
    const res = await fetch(`${API_BASE}${path}`, { ...options, headers });

    if (res.status === 401) {
      // Real auth rejection — do NOT fall back to mock
      if (typeof window !== 'undefined') {
        localStorage.removeItem('factoring_token');
        localStorage.removeItem('factoring_user');
        window.location.href = '/demos/trucking-freight-factoring-and-banking/login/';
      }
      throw new Error('Unauthorized');
    }

    if (res.status === 502 || res.status === 503 || res.status === 504) {
      // Backend unreachable — activate mock mode for this session
      enableMockMode();
      return mockRequest<T>(path, options);
    }

    if (!res.ok) {
      const body = await res.text();
      throw new Error(body || `HTTP ${res.status}`);
    }

    const ct = res.headers.get('content-type') ?? '';
    if (ct.includes('application/json')) return res.json() as Promise<T>;
    return res.text() as unknown as T;
  } catch (err) {
    // Rethrow real auth errors and mock errors
    if (err instanceof Error && (err.message === 'Unauthorized' || err.message.startsWith('Mock:'))) throw err;
    // Network error (CORS preflight blocked, connection refused, etc.) — activate mock
    if (!isMockMode()) enableMockMode();
    return mockRequest<T>(path, options);
  }
}

// Auth
export const authApi = {
  login: (email: string, password: string) =>
    request<{ access_token: string; user: { id: number; email: string; role: string; name: string } }>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({ email, password }) },
    ),
  me: () => request<{ sub: number; email: string; role: string; name: string }>('/auth/me'),
};

// Invoices
export interface Invoice {
  id: number;
  invoiceNumber: string;
  carrierId: number;
  underwriterId: number | null;
  payerName: string;
  payerDaysToPay: number;
  faceValueMinorUnits: number;
  advanceRateBps: number;
  feeRateBps: number;
  advanceMinorUnits: number | null;
  feeMinorUnits: number | null;
  reserveMinorUnits: number | null;
  currency: 'USD' | 'BRL';
  status: 'pending' | 'approved' | 'rejected' | 'disbursed' | 'collected';
  notes: string | null;
  submittedAt: string;
  approvedAt: string | null;
  disbursedAt: string | null;
  collectedAt: string | null;
}

export const invoicesApi = {
  list: () => request<Invoice[]>('/invoices'),
  get: (id: number) => request<Invoice>(`/invoices/${id}`),
  create: (data: object) =>
    request<Invoice>('/invoices', { method: 'POST', body: JSON.stringify(data) }),
  approve: (id: number, notes?: string) =>
    request<Invoice>(`/invoices/${id}/approve`, { method: 'PATCH', body: JSON.stringify({ notes }) }),
  reject: (id: number, notes?: string) =>
    request<Invoice>(`/invoices/${id}/reject`, { method: 'PATCH', body: JSON.stringify({ notes }) }),
  disburse: (id: number) =>
    request<Invoice>(`/invoices/${id}/disburse`, { method: 'PATCH', body: '{}' }),
  collect: (id: number) =>
    request<Invoice>(`/invoices/${id}/collect`, { method: 'PATCH', body: '{}' }),
};

// Accounts & Ledger
export interface Account {
  id: number;
  name: string;
  code: string;
  type: 'asset' | 'liability' | 'revenue' | 'expense';
  currency: 'USD' | 'BRL';
  balanceMinorUnits: number;
  updatedAt: string;
}

export interface LedgerEntry {
  id: number;
  invoiceId: number | null;
  accountId: number;
  entryType: 'debit' | 'credit';
  amountMinorUnits: number;
  currency: 'USD' | 'BRL';
  description: string;
  postedAt: string;
}

export const ledgerApi = {
  accounts: () => request<Account[]>('/accounts'),
  accountEntries: (id: number) =>
    request<{ account: Account; entries: LedgerEntry[] }>(`/accounts/${id}/entries`),
  recent: () => request<LedgerEntry[]>('/ledger'),
};
